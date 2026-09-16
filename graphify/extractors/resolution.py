from __future__ import annotations
from pathlib import Path
from graphify.extractors.base import _make_id, _read_text
import hashlib


def _source_key(source_file: str, root: Path) -> str:
    if not source_file:
        return ""
    source_path = Path(source_file)
    try:
        return str(source_path.resolve().relative_to(root))
    except Exception:
        return str(source_path)


def _node_disambiguation_source_key(node: dict, root: Path) -> str:
    source_file = str(node.get("source_file", ""))
    if source_file:
        return _source_key(source_file, root)
    return _source_key(str(node.get("origin_file", "")), root)


def _disambiguate_colliding_node_ids(
    nodes: list[dict], edges: list[dict], raw_calls: list[dict], root: Path
) -> None:
    """Rewrite only colliding node IDs, using source path as the disambiguator.

    Module anchor nodes (#1327) are exempt: ``import CoreKit`` from three files
    yields three ``type=module`` nodes with the same id but different
    source_files. Those are the *same* module, not distinct same-named symbols,
    so they must collapse to one shared node — disambiguating them by path would
    scatter a single module across N file-qualified duplicates.
    """
    by_id: dict[str, list[dict]] = {}
    for node in nodes:
        if node.get("type") in ("module", "namespace"):
            continue
        nid = node.get("id")
        if isinstance(nid, str) and nid:
            by_id.setdefault(nid, []).append(node)
    remap: dict[tuple[str, str], str] = {}
    ambiguous_ids: set[str] = set()
    for old_id, group in by_id.items():
        source_keys = {_node_disambiguation_source_key(node, root) for node in group}
        if len(group) < 2 or len(source_keys) < 2:
            continue
        ambiguous_ids.add(old_id)
        naive: dict[str, str] = {}
        for source_key in source_keys:
            if source_key:
                naive[source_key] = _make_id(source_key, old_id)
        seen: dict[str, int] = {}
        for nid in naive.values():
            seen[nid] = seen.get(nid, 0) + 1
        needs_hash = {sk for sk, nid in naive.items() if seen.get(nid, 0) > 1}
        for node in group:
            source_key = _node_disambiguation_source_key(node, root)
            if not source_key:
                continue
            if source_key in needs_hash:
                salt = hashlib.sha1(source_key.encode("utf-8")).hexdigest()[:6]
                new_id = _make_id(source_key, old_id, salt)
            else:
                new_id = naive.get(source_key) or _make_id(source_key, old_id)
            remap[old_id, source_key] = new_id
            if new_id != old_id:
                node["id"] = new_id
    if not remap:
        for edge in edges:
            edge.pop("target_file", None)
        return
    unambiguous_remaps: dict[str, str] = {}
    for old_id, group in by_id.items():
        if old_id in ambiguous_ids:
            continue
        candidates = {
            node["id"] for node in group if isinstance(node.get("id"), str) and node["id"] != old_id
        }
        if len(candidates) == 1:
            unambiguous_remaps[old_id] = next(iter(candidates))
    _HEADER_SUFFIXES = (".h", ".hpp", ".hh", ".hxx")
    header_remaps: dict[str, str] = {}
    for old_id in ambiguous_ids:
        for node in by_id.get(old_id, []):
            sk = _node_disambiguation_source_key(node, root)
            if sk and Path(sk).suffix.lower() in _HEADER_SUFFIXES:
                new_id = remap.get((old_id, sk))
                if new_id:
                    header_remaps[old_id] = new_id
                    break
    for edge in edges:
        edge_source_key = _source_key(str(edge.get("source_file", "")), root)
        source_key = (edge.get("source", ""), edge_source_key)
        target_file = edge.pop("target_file", None)
        if target_file and edge.get("relation") in ("imports", "imports_from", "re_exports"):
            target_edge_key = _source_key(str(target_file), root)
        else:
            target_edge_key = edge_source_key
        target_key = (edge.get("target", ""), target_edge_key)
        if source_key in remap:
            edge["source"] = remap[source_key]
        elif edge.get("source") in unambiguous_remaps:
            edge["source"] = unambiguous_remaps[str(edge["source"])]
        if (
            edge.get("relation") in ("imports", "imports_from")
            and edge.get("target") in header_remaps
        ):
            edge["target"] = header_remaps[str(edge["target"])]
        elif target_key in remap:
            edge["target"] = remap[target_key]
        elif edge.get("target") in unambiguous_remaps:
            edge["target"] = unambiguous_remaps[str(edge["target"])]
    for raw_call in raw_calls:
        call_source_key = _source_key(str(raw_call.get("source_file", "")), root)
        caller_key = (raw_call.get("caller_nid", ""), call_source_key)
        if caller_key in remap:
            raw_call["caller_nid"] = remap[caller_key]
        elif raw_call.get("caller_nid") in unambiguous_remaps:
            raw_call["caller_nid"] = unambiguous_remaps[str(raw_call["caller_nid"])]


def _is_type_like_definition(node: dict) -> bool:
    if node.get("type") == "namespace":
        return False
    label = str(node.get("label", "")).strip()
    if not label:
        return False
    if label.endswith(")") or label.startswith("."):
        return False
    if "." in label:
        return False
    return node.get("file_type") == "code"


def _resolve_cross_file_java_imports(per_file: list[dict], paths: list[Path]) -> list[dict]:
    """Two-pass Java import resolution.

    Pass 1: build a global index {ClassName: [node_id, ...]} across all Java nodes.
    Pass 2: re-parse each Java file; for every `import a.b.C;`, resolve C against
    the index. Wildcard and stdlib imports produce no edge.
    """
    try:
        import tree_sitter_java as tsjava
        from tree_sitter import Language, Parser
    except ImportError:
        return []
    language = Language(tsjava.language())
    parser = Parser(language)
    name_to_ids: dict[str, list[str]] = {}
    for file_result in per_file:
        for node in file_result.get("nodes", []):
            label = node.get("label", "")
            nid = node.get("id", "")
            src = node.get("source_file", "")
            if not label or not nid or (not src):
                continue
            if label.endswith(")") or label.endswith(".java"):
                continue
            if not label[0].isalpha() or not label[0].isupper():
                continue
            name_to_ids.setdefault(label, []).append(nid)
    new_edges: list[dict] = []
    seen_pairs: set[tuple[str, str]] = set()
    for path in paths:
        file_nid = _make_id(str(path))
        try:
            source = path.read_bytes()
            tree = parser.parse(source)
        except Exception:
            continue

        def walk(n) -> None:
            if n.type == "import_declaration":
                raw = _read_text(n, source).strip()
                body = raw[len("import") :].strip().rstrip(";").strip()
                if body.startswith("static "):
                    body = body[len("static ") :].strip()
                if body.endswith(".*"):
                    return
                parts = body.split(".")
                if not parts:
                    return
                last = parts[-1]
                if last and last[0].islower() and (len(parts) >= 2):
                    last = parts[-2]
                at_line = n.start_point[0] + 1
                for tgt_nid in name_to_ids.get(last, []):
                    target_node = next(
                        (
                            node
                            for result in per_file
                            for node in result.get("nodes", [])
                            if node.get("id") == tgt_nid
                        ),
                        {},
                    )
                    namespace = (target_node.get("metadata") or {}).get("namespace", "")
                    qualified = f"{namespace}.{last}" if namespace else last
                    imported_type = ".".join(parts[:-1]) if parts[-1][:1].islower() else body
                    if namespace and qualified != imported_type:
                        continue
                    if tgt_nid == file_nid:
                        continue
                    key = (file_nid, tgt_nid)
                    if key in seen_pairs:
                        continue
                    seen_pairs.add(key)
                    new_edges.append(
                        {
                            "source": file_nid,
                            "target": tgt_nid,
                            "relation": "imports",
                            "confidence": "EXTRACTED",
                            "confidence_score": 1.0,
                            "source_file": str(path),
                            "source_location": f"L{at_line}",
                            "weight": 1.0,
                        }
                    )
            for child in n.children:
                walk(child)

        walk(tree.root_node)
    return new_edges


def _resolve_java_type_references(
    per_file: list[dict], paths: list[Path], all_nodes: list[dict], all_edges: list[dict]
) -> None:
    """Re-point dangling Java ``implements``/``inherits`` edges to the real
    definition, using the referencing file's ``import`` statements (+ package)
    for exact disambiguation.

    Cross-file type references resolve by bare name and fall back to a no-source
    "shadow" stub. ``_rewire_unique_stub_nodes`` repairs that only when the name
    is globally unique; when two packages define a same-named type it bails, so
    the ``implements`` edge stays stuck on the shadow node and the real interface
    is wrongly isolated (#1318). An ``import com.a.handler.AIResponseHandler``
    names the exact package, so it disambiguates where bare-name matching cannot.

    Mutates ``all_nodes``/``all_edges`` in place. Runs after id-disambiguation so
    target ids are final, and after ``_rewire_unique_stub_nodes`` so it only has
    to handle the ambiguous remainder.
    """
    try:
        import tree_sitter_java as tsjava
        from tree_sitter import Language, Parser
    except ImportError:
        return
    language = Language(tsjava.language())
    parser = Parser(language)
    pkg_by_file: dict[str, str] = {}
    imports_by_file: dict[str, dict[str, str]] = {}
    for path, result in zip(paths, per_file):
        srcs = {n.get("source_file") for n in result.get("nodes", []) if n.get("source_file")}
        if not srcs:
            continue
        try:
            source = path.read_bytes()
            tree = parser.parse(source)
        except Exception:
            continue
        pkg = ""
        imps: dict[str, str] = {}

        def walk(n) -> None:
            nonlocal pkg
            if n.type == "package_declaration":
                pkg = _read_text(n, source).strip()[len("package") :].strip().rstrip(";").strip()
            elif n.type == "import_declaration":
                body = _read_text(n, source).strip()[len("import") :].strip().rstrip(";").strip()
                if body.startswith("static "):
                    body = body[len("static ") :].strip()
                if body.endswith(".*") or "." not in body:
                    return
                simple = body.split(".")[-1]
                if simple and simple[0].isupper():
                    imps[simple] = body
            for child in n.children:
                walk(child)

        walk(tree.root_node)
        for s in srcs:
            pkg_by_file[s] = pkg
            imports_by_file[s] = imps
    fqn_to_id: dict[str, str] = {}
    for node in all_nodes:
        label = node.get("label", "")
        src = node.get("source_file", "")
        nid = node.get("id", "")
        if not (label and src and nid) or src not in pkg_by_file:
            continue
        if not label[:1].isupper() or label.endswith(")") or label.endswith(".java"):
            continue
        pkg = pkg_by_file[src]
        fqn_to_id.setdefault(f"{pkg}.{label}" if pkg else label, nid)
    stub_label: dict[str, str] = {
        node["id"]: node.get("label", "")
        for node in all_nodes
        if node.get("id") and (not node.get("source_file")) and node.get("label", "")[:1].isupper()
    }
    if not stub_label:
        return
    REPOINT_RELATIONS = {"implements", "inherits", "extends", "imports", "references"}
    repointed_from: set[str] = set()
    for edge in all_edges:
        if edge.get("relation") not in REPOINT_RELATIONS:
            continue
        tgt = edge.get("target")
        label = stub_label.get(tgt)
        if not label:
            continue
        ref_file = edge.get("source_file", "")
        resolved = None
        fqn = imports_by_file.get(ref_file, {}).get(label)
        if fqn:
            resolved = fqn_to_id.get(fqn)
        if resolved is None:
            pkg = pkg_by_file.get(ref_file, "")
            resolved = fqn_to_id.get(f"{pkg}.{label}" if pkg else label)
        if resolved and resolved != tgt:
            edge["target"] = resolved
            repointed_from.add(tgt)
    if not repointed_from:
        return
    still_referenced: set[str] = set()
    for edge in all_edges:
        still_referenced.add(edge.get("source"))
        still_referenced.add(edge.get("target"))
    all_nodes[:] = [
        node
        for node in all_nodes
        if node.get("id") not in repointed_from or node.get("id") in still_referenced
    ]
