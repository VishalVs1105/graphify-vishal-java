"""Java-only structural extraction for backend service repositories."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

from graphify.extractors.base import _file_stem, _make_id, _read_text
from graphify.extractors.engine import _extract_generic
from graphify.extractors.models import LanguageConfig
from graphify.extractors.resolution import (
    _disambiguate_colliding_node_ids,
    _is_type_like_definition,
    _resolve_cross_file_java_imports,
    _resolve_java_type_references,
)


def _import_java(
    node,
    source: bytes,
    file_nid: str,
    stem: str,
    edges: list,
    str_path: str,
    scope_stack: list[str] | None = None,
) -> None:
    """Emit a file-level import edge for a Java import declaration."""

    def walk_scoped(current) -> str:
        parts: list[str] = []
        while current:
            if current.type == "scoped_identifier":
                name_node = current.child_by_field_name("name")
                if name_node:
                    parts.append(_read_text(name_node, source))
                current = current.child_by_field_name("scope")
            elif current.type == "identifier":
                parts.append(_read_text(current, source))
                break
            else:
                break
        parts.reverse()
        return ".".join(parts)

    for child in node.children:
        if child.type not in ("scoped_identifier", "identifier"):
            continue
        path_str = walk_scoped(child)
        pieces = path_str.split(".")
        module_name = pieces[-1].strip("*").strip(".") or (
            pieces[-2] if len(pieces) > 1 else path_str
        )
        if module_name:
            edges.append({
                "source": file_nid,
                "target": _make_id(module_name),
                "relation": "imports",
                "context": "import",
                "confidence": "EXTRACTED",
                "source_file": str_path,
                "source_location": f"L{node.start_point[0] + 1}",
                "weight": 1.0,
            })
        break


_JAVA_CONFIG = LanguageConfig(
    ts_module="tree_sitter_java",
    class_types=frozenset({
        "class_declaration",
        "interface_declaration",
        "record_declaration",
        "enum_declaration",
        "annotation_type_declaration",
    }),
    function_types=frozenset({"method_declaration", "constructor_declaration"}),
    import_types=frozenset({"import_declaration"}),
    call_types=frozenset({"method_invocation", "object_creation_expression"}),
    call_function_field="name",
    call_accessor_node_types=frozenset(),
    function_boundary_types=frozenset({"method_declaration", "constructor_declaration"}),
    import_handler=_import_java,
)


def extract_java(path: Path) -> dict:
    """Extract Java types, members, imports, references, annotations, and calls."""
    return _extract_generic(Path(path), _JAVA_CONFIG)


_DISPATCH: dict[str, Any] = {".java": extract_java}


def _get_extractor(path: Path) -> Any | None:
    """Return the Java extractor, or ``None`` for every other input."""
    return extract_java if Path(path).suffix.lower() == ".java" else None


def _relative_source(value: object, root: Path) -> str:
    if not value:
        return ""
    source = Path(str(value))
    try:
        return source.resolve().relative_to(root).as_posix()
    except (OSError, ValueError):
        return source.as_posix()


def _canonicalize_java_ids(
    paths: list[Path],
    nodes: list[dict],
    edges: list[dict],
    raw_calls: list[dict],
    root: Path,
) -> None:
    """Replace machine-specific absolute path prefixes with repo-relative IDs."""
    remap: dict[str, str] = {}
    prefix_remap: list[tuple[str, str, str]] = []
    for path in paths:
        resolved = path.resolve()
        try:
            relative = resolved.relative_to(root)
        except ValueError:
            relative = Path(path.name)
        old_prefixes = {
            _make_id(_file_stem(path)),
            _make_id(_file_stem(resolved)),
            _make_id(str(path)),
            _make_id(str(resolved)),
        }
        new_prefix = _make_id(_file_stem(relative))
        source_keys = {str(path), str(resolved), path.as_posix(), resolved.as_posix()}
        for old_prefix in old_prefixes:
            if old_prefix:
                remap[old_prefix] = new_prefix
                for source_key in source_keys:
                    prefix_remap.append((source_key, old_prefix, new_prefix))

    for node in nodes:
        node_id = str(node.get("id", ""))
        source_file = str(node.get("source_file", ""))
        for source_key, old_prefix, new_prefix in prefix_remap:
            if source_file not in (source_key, Path(source_key).as_posix()):
                continue
            if node_id == old_prefix:
                remap[node_id] = new_prefix
            elif node_id.startswith(old_prefix + "_"):
                remap[node_id] = new_prefix + node_id[len(old_prefix):]
            break

    for node in nodes:
        node_id = node.get("id")
        if node_id in remap:
            node["id"] = remap[node_id]
        if node.get("source_file"):
            node["source_file"] = _relative_source(node["source_file"], root)
    for edge in edges:
        for endpoint in ("source", "target"):
            node_id = edge.get(endpoint)
            if node_id in remap:
                edge[endpoint] = remap[node_id]
        if edge.get("source_file"):
            edge["source_file"] = _relative_source(edge["source_file"], root)
    for raw_call in raw_calls:
        caller = raw_call.get("caller_nid")
        if caller in remap:
            raw_call["caller_nid"] = remap[caller]
        if raw_call.get("source_file"):
            raw_call["source_file"] = _relative_source(raw_call["source_file"], root)


def _rewire_unique_java_stubs(nodes: list[dict], edges: list[dict]) -> None:
    """Repoint a sourceless Java type stub when exactly one real type matches."""
    real_by_label: dict[str, list[str]] = {}
    for node in nodes:
        label = str(node.get("label", "")).strip()
        if node.get("source_file") and label and _is_type_like_definition(node):
            real_by_label.setdefault(label, []).append(str(node["id"]))

    remap: dict[str, str] = {}
    for node in nodes:
        if node.get("source_file"):
            continue
        label = str(node.get("label", "")).strip()
        candidates = real_by_label.get(label, [])
        if len(candidates) == 1 and node.get("id") != candidates[0]:
            remap[str(node["id"])] = candidates[0]
    if not remap:
        return
    for edge in edges:
        if edge.get("source") in remap:
            edge["source"] = remap[str(edge["source"])]
        if edge.get("target") in remap:
            edge["target"] = remap[str(edge["target"])]
    referenced = {endpoint for edge in edges for endpoint in (edge.get("source"), edge.get("target"))}
    nodes[:] = [
        node for node in nodes
        if node.get("id") not in remap or node.get("id") in referenced
    ]


def _resolve_java_member_calls(
    per_file: list[dict],
    all_nodes: list[dict],
    all_edges: list[dict],
) -> None:
    """Resolve receiver-typed Java calls without guessing ambiguous targets."""

    def key(label: object) -> str:
        return str(label or "").strip().removeprefix(".").removesuffix("()")

    contained = {
        edge.get("target") for edge in all_edges if edge.get("relation") == "contains"
    }
    node_by_id = {node.get("id"): node for node in all_nodes}
    type_defs: dict[str, list[str]] = {}
    for node in all_nodes:
        if (
            node.get("source_file")
            and node.get("id") in contained
            and _is_type_like_definition(node)
        ):
            type_defs.setdefault(key(node.get("label")), []).append(str(node["id"]))

    method_index: dict[tuple[str, str], set[str]] = {}
    enclosing_type: dict[str, str] = {}
    for edge in all_edges:
        if edge.get("relation") != "method":
            continue
        owner, method = str(edge.get("source")), str(edge.get("target"))
        method_node = node_by_id.get(method)
        if method_node is None:
            continue
        enclosing_type.setdefault(method, owner)
        method_index.setdefault((owner, key(method_node.get("label"))), set()).add(method)

    existing = {(edge.get("source"), edge.get("target")) for edge in all_edges}
    for result in per_file:
        for raw_call in result.get("raw_calls", []):
            if raw_call.get("lang") != "java" or not raw_call.get("is_member_call"):
                continue
            receiver = str(raw_call.get("receiver", ""))
            callee = str(raw_call.get("callee", ""))
            caller = str(raw_call.get("caller_nid", ""))
            if not receiver or not callee or not caller:
                continue
            exact = False
            if receiver == "this":
                type_id = enclosing_type.get(caller)
                exact = True
                if not type_id:
                    continue
            else:
                type_name = raw_call.get("receiver_type")
                if not type_name and receiver[:1].isupper():
                    type_name = receiver
                    exact = True
                candidates = type_defs.get(key(type_name), []) if type_name else []
                if len(candidates) != 1:
                    continue
                type_id = candidates[0]
            methods = method_index.get((type_id, key(callee)), set())
            if len(methods) != 1:
                continue
            target = next(iter(methods))
            if target == caller or (caller, target) in existing:
                continue
            existing.add((caller, target))
            all_edges.append({
                "source": caller,
                "target": target,
                "relation": "calls",
                "context": "call",
                "confidence": "EXTRACTED" if exact else "INFERRED",
                "confidence_score": 1.0 if exact else 0.8,
                "source_file": raw_call.get("source_file", ""),
                "source_location": raw_call.get("source_location"),
                "weight": 1.0,
            })


def _resolve_java_direct_calls(
    per_file: list[dict],
    all_nodes: list[dict],
    all_edges: list[dict],
) -> None:
    """Resolve unqualified constructor calls to a unique in-corpus Java type."""
    type_ids: dict[str, list[str]] = {}
    for node in all_nodes:
        label = str(node.get("label", "")).strip()
        if node.get("source_file") and label and _is_type_like_definition(node):
            type_ids.setdefault(label, []).append(str(node["id"]))
    existing = {(edge.get("source"), edge.get("target")) for edge in all_edges}
    for result in per_file:
        for raw_call in result.get("raw_calls", []):
            if raw_call.get("lang") != "java" or raw_call.get("is_member_call"):
                continue
            caller = str(raw_call.get("caller_nid", ""))
            callee = str(raw_call.get("callee", ""))
            candidates = type_ids.get(callee, [])
            if not caller or len(candidates) != 1:
                continue
            target = candidates[0]
            if target == caller or (caller, target) in existing:
                continue
            existing.add((caller, target))
            all_edges.append({
                "source": caller,
                "target": target,
                "relation": "calls",
                "context": "constructor",
                "confidence": "EXTRACTED",
                "confidence_score": 1.0,
                "source_file": raw_call.get("source_file", ""),
                "source_location": raw_call.get("source_location"),
                "weight": 1.0,
            })


def _deduplicate(nodes: list[dict], edges: list[dict]) -> tuple[list[dict], list[dict]]:
    node_by_id: dict[str, dict] = {}
    for node in nodes:
        node_id = str(node.get("id", ""))
        if node_id:
            node_by_id.setdefault(node_id, node)
    seen: set[str] = set()
    unique_edges: list[dict] = []
    for edge in edges:
        if not edge.get("source") or not edge.get("target") or edge["source"] == edge["target"]:
            continue
        identity = json.dumps(edge, sort_keys=True, default=str)
        if identity not in seen:
            seen.add(identity)
            unique_edges.append(edge)
    return list(node_by_id.values()), unique_edges


def extract(
    paths: list[Path],
    cache_root: Path | None = None,
    *,
    root: Path | None = None,
    parallel: bool = True,
    max_workers: int | None = None,
) -> dict:
    """Extract and resolve a Java corpus; non-Java paths are ignored."""
    java_paths = [Path(path) for path in paths if Path(path).suffix.lower() == ".java"]
    if root is not None:
        corpus_root = Path(root).resolve()
    elif cache_root is not None:
        corpus_root = Path(cache_root).resolve()
    elif java_paths:
        common = os.path.commonpath([str(path.resolve()) for path in java_paths])
        corpus_root = Path(common if Path(common).is_dir() else Path(common).parent).resolve()
    else:
        corpus_root = Path(".").resolve()

    per_file = [extract_java(path) for path in java_paths]
    all_nodes = [node for result in per_file for node in result.get("nodes", [])]
    all_edges = [edge for result in per_file for edge in result.get("edges", [])]
    raw_calls = [call for result in per_file for call in result.get("raw_calls", [])]

    all_edges.extend(_resolve_cross_file_java_imports(per_file, java_paths))
    _canonicalize_java_ids(java_paths, all_nodes, all_edges, raw_calls, corpus_root)
    _disambiguate_colliding_node_ids(all_nodes, all_edges, raw_calls, corpus_root)
    _rewire_unique_java_stubs(all_nodes, all_edges)
    _resolve_java_type_references(per_file, java_paths, all_nodes, all_edges)
    _resolve_java_member_calls(per_file, all_nodes, all_edges)
    _resolve_java_direct_calls(per_file, all_nodes, all_edges)
    all_nodes, all_edges = _deduplicate(all_nodes, all_edges)

    for node in all_nodes:
        node["_origin"] = "ast"
    for edge in all_edges:
        edge["_origin"] = "ast"
    return {
        "nodes": all_nodes,
        "edges": all_edges,
        "input_tokens": 0,
        "output_tokens": 0,
    }


def collect_files(
    target: Path,
    *,
    follow_symlinks: bool = False,
    root: Path | None = None,
) -> list[Path]:
    """Collect Java source files under ``target``."""
    target = Path(target)
    containment_root = Path(root) if root is not None else target
    from graphify.detect import _is_noise_dir, _resolves_under_root

    if target.is_file():
        return [target] if target.suffix.lower() == ".java" else []
    results: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(target, followlinks=follow_symlinks):
        directory = Path(dirpath)
        dirnames[:] = [name for name in dirnames if not _is_noise_dir(name, directory)]
        for filename in filenames:
            path = directory / filename
            if path.suffix.lower() != ".java":
                continue
            if _resolves_under_root(path, containment_root):
                results.append(path)
    return sorted(results)


if __name__ == "__main__":
    paths: list[Path] = []
    for argument in sys.argv[1:]:
        paths.extend(collect_files(Path(argument)))
    print(json.dumps(extract(paths), indent=2))
