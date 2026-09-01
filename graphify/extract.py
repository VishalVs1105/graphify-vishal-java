"""Java-only structural extraction for backend service repositories."""

from __future__ import annotations

import json
import html
import os
import re
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
from graphify.security import sanitize_metadata


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
    call_types=frozenset({
        "method_invocation", "object_creation_expression", "method_reference",
    }),
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

    parents: dict[str, list[str]] = {}
    for edge in all_edges:
        if edge.get("relation") not in {"inherits", "implements"}:
            continue
        source = str(edge.get("source") or "")
        target = str(edge.get("target") or "")
        if source and target and target in node_by_id:
            parents.setdefault(source, []).append(target)

    def select_method(type_id: str, callee: str, argument_count: object) -> str | None:
        """Select one method, preferring the closest declaration in the hierarchy."""
        frontier = [type_id]
        visited: set[str] = set()
        while frontier:
            next_frontier: list[str] = []
            level_methods: set[str] = set()
            for owner in frontier:
                if owner in visited:
                    continue
                visited.add(owner)
                level_methods.update(method_index.get((owner, key(callee)), set()))
                next_frontier.extend(parents.get(owner, []))
            if argument_count is not None and len(level_methods) > 1:
                arity_matches = {
                    method_id
                    for method_id in level_methods
                    if isinstance(node_by_id.get(method_id, {}).get("metadata"), dict)
                    and node_by_id[method_id]["metadata"].get("java_parameter_count")
                    == argument_count
                }
                if arity_matches:
                    level_methods = arity_matches
            if len(level_methods) == 1:
                return next(iter(level_methods))
            if len(level_methods) > 1:
                return None
            frontier = next_frontier
        return None

    def declared_return_type(method_id: str) -> str | None:
        metadata = node_by_id.get(method_id, {}).get("metadata")
        if not isinstance(metadata, dict):
            return None
        raw = html.unescape(str(metadata.get("java_return_type") or "")).strip()
        raw = re.sub(r"^@[A-Za-z_$][\w$]*(?:\([^)]*\))?\s*", "", raw)
        raw = raw.removesuffix("...").removesuffix("[]").strip()
        base = raw.split("<", 1)[0].strip().rsplit(".", 1)[-1]
        if not base or base in {"void", "boolean", "byte", "short", "int", "long", "float", "double", "char"}:
            return None
        return base

    def inherited_field_type(type_id: str, receiver: str) -> str | None:
        field = receiver.removeprefix("this.")
        frontier = [type_id]
        visited: set[str] = set()
        while frontier:
            owner = frontier.pop(0)
            if owner in visited:
                continue
            visited.add(owner)
            metadata = node_by_id.get(owner, {}).get("metadata")
            fields = metadata.get("java_fields") if isinstance(metadata, dict) else None
            if isinstance(fields, dict) and fields.get(field):
                return str(fields[field])
            frontier.extend(parents.get(owner, []))
        return None

    def unique_type(type_name: object) -> str | None:
        candidates = type_defs.get(key(type_name), []) if type_name else []
        return candidates[0] if len(candidates) == 1 else None

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
                if not type_name and receiver.startswith("this."):
                    caller_type = enclosing_type.get(caller)
                    type_name = (
                        inherited_field_type(caller_type, receiver)
                        if caller_type else None
                    )
                if not type_name and receiver[:1].isupper():
                    type_name = receiver
                type_id = unique_type(type_name)
                if type_id is None:
                    continue
                exact = True

            chain = raw_call.get("receiver_chain")
            if isinstance(chain, list):
                chain_failed = False
                for hop in chain:
                    if not isinstance(hop, dict):
                        chain_failed = True
                        break
                    intermediate = select_method(
                        type_id,
                        str(hop.get("callee") or ""),
                        hop.get("argument_count"),
                    )
                    if intermediate is None:
                        chain_failed = True
                        break
                    return_type = declared_return_type(intermediate)
                    next_type = unique_type(return_type)
                    if next_type is None:
                        chain_failed = True
                        break
                    type_id = next_type
                if chain_failed:
                    continue

            argument_count = raw_call.get("argument_count")
            target = select_method(type_id, callee, argument_count)
            if target is None:
                continue
            if target == caller:
                continue
            method_reference = raw_call.get("call_kind") == "method_reference"
            edge = {
                "source": caller,
                "target": target,
                "relation": "calls",
                "context": "method_reference" if method_reference else "call",
                "confidence": "EXTRACTED" if exact and not method_reference else "INFERRED",
                "confidence_score": 1.0 if exact and not method_reference else 0.8,
                "source_file": raw_call.get("source_file", ""),
                "source_location": raw_call.get("source_location"),
                "weight": 1.0,
            }
            if raw_call.get("conditions"):
                edge["conditions"] = raw_call["conditions"]
            if raw_call.get("argument_count") is not None:
                edge["argument_count"] = raw_call["argument_count"]
            if raw_call.get("arguments"):
                edge["arguments"] = raw_call["arguments"]
            all_edges.append(edge)
            raw_call["_resolved"] = True


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
            if target == caller:
                continue
            edge = {
                "source": caller,
                "target": target,
                "relation": "calls",
                "context": "constructor",
                "confidence": "EXTRACTED",
                "confidence_score": 1.0,
                "source_file": raw_call.get("source_file", ""),
                "source_location": raw_call.get("source_location"),
                "weight": 1.0,
            }
            if raw_call.get("conditions"):
                edge["conditions"] = raw_call["conditions"]
            if raw_call.get("argument_count") is not None:
                edge["argument_count"] = raw_call["argument_count"]
            if raw_call.get("arguments"):
                edge["arguments"] = raw_call["arguments"]
            all_edges.append(edge)
            raw_call["_resolved"] = True


def _attach_java_unresolved_calls(nodes: list[dict], raw_calls: list[dict]) -> None:
    """Retain observed-but-unbound Java invocations on their caller method."""
    node_by_id = {str(node.get("id")): node for node in nodes if node.get("id")}
    unresolved_by_caller: dict[str, list[dict]] = {}
    for raw_call in raw_calls:
        if raw_call.get("lang") != "java" or raw_call.get("_resolved"):
            continue
        caller = str(raw_call.get("caller_nid") or "")
        callee = str(raw_call.get("callee") or "")
        if not caller or not callee or caller not in node_by_id:
            continue
        item: dict[str, object] = {
            "callee": callee,
            "receiver": raw_call.get("receiver") or "",
            "receiver_type": raw_call.get("receiver_type") or "",
            "source_file": raw_call.get("source_file") or "",
            "source_location": raw_call.get("source_location"),
            "call_kind": raw_call.get("call_kind") or "method",
        }
        if raw_call.get("argument_count") is not None:
            item["argument_count"] = raw_call["argument_count"]
        if raw_call.get("arguments"):
            item["arguments"] = raw_call["arguments"]
        if raw_call.get("receiver_chain"):
            item["receiver_chain"] = raw_call["receiver_chain"]
        if raw_call.get("conditions"):
            item["conditions"] = raw_call["conditions"]
        values = unresolved_by_caller.setdefault(caller, [])
        if item not in values:
            values.append(item)
    for caller, unresolved in unresolved_by_caller.items():
        node = node_by_id[caller]
        metadata = dict(node.get("metadata") or {})
        sanitized = sanitize_metadata({"java_unresolved_calls": unresolved})
        metadata["java_unresolved_calls"] = sanitized["java_unresolved_calls"]
        node["metadata"] = metadata


def _aggregate_java_call_edges(edges: list[dict]) -> list[dict]:
    """Preserve every Java call occurrence in a simple-graph edge.

    NetworkX ``Graph`` stores one edge per node pair. Repeated invocations of
    the same target (especially in different branches) would otherwise be
    overwritten. This folds them into a stable edge and retains every call site
    plus every distinct guarding condition.
    """
    grouped: dict[tuple[object, object, object], dict] = {}
    output: list[dict] = []

    def occurrence(edge: dict) -> dict:
        item: dict[str, object] = {
            "source_file": edge.get("source_file", ""),
            "source_location": edge.get("source_location"),
        }
        if edge.get("conditions"):
            item["conditions"] = [dict(value) for value in edge["conditions"]]
        if edge.get("argument_count") is not None:
            item["argument_count"] = edge["argument_count"]
        if edge.get("arguments"):
            item["arguments"] = list(edge["arguments"])
        return item

    for edge in edges:
        if edge.get("relation") != "calls":
            output.append(edge)
            continue
        key = (edge.get("source"), edge.get("target"), edge.get("relation"))
        current = grouped.get(key)
        if current is None:
            current = dict(edge)
            if edge.get("conditions"):
                current["conditions"] = [dict(value) for value in edge["conditions"]]
            current["call_sites"] = [occurrence(edge)]
            current["occurrence_count"] = 1
            grouped[key] = current
            output.append(current)
            continue
        site = occurrence(edge)
        if site not in current["call_sites"]:
            current["call_sites"].append(site)
            current["occurrence_count"] = int(current.get("occurrence_count") or 1) + 1
        conditions = current.setdefault("conditions", [])
        for condition in edge.get("conditions") or []:
            if condition not in conditions:
                conditions.append(condition)
        if current.get("confidence") != "EXTRACTED" and edge.get("confidence") == "EXTRACTED":
            current["confidence"] = "EXTRACTED"
            current["confidence_score"] = edge.get("confidence_score", 1.0)
    for edge in output:
        if edge.get("relation") != "calls":
            continue
        bounded = sanitize_metadata({
            "conditions": edge.get("conditions") or [],
            "call_sites": edge.get("call_sites") or [],
            "arguments": edge.get("arguments") or [],
        })
        if bounded["conditions"]:
            edge["conditions"] = bounded["conditions"]
        else:
            edge.pop("conditions", None)
        edge["call_sites"] = bounded["call_sites"]
        if bounded["arguments"]:
            edge["arguments"] = bounded["arguments"]
        else:
            edge.pop("arguments", None)
    return output


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
    _attach_java_unresolved_calls(all_nodes, raw_calls)
    all_edges = _aggregate_java_call_edges(all_edges)
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
