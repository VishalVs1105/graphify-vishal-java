"""Deterministic Java API reference documents from persisted graph evidence.

No LLM, source execution, keyword pruning or automatic network-hop inference
is performed here. All production call alternatives remain reviewable.
"""

from __future__ import annotations

import argparse
from collections import deque
import hashlib
import html
import json
from pathlib import Path
import re

import networkx as nx
from networkx.readwrite import json_graph

from graphify.paths import default_graph_json
from graphify.security import check_graph_file_size_cap
from graphify.serve import (
    _true_edge_records,
    _java_method_owners,
    _java_interface_dispatch_records,
    _java_is_test_node,
    _java_flow_symbol,
    _java_flow_source,
    _java_metadata,
)


def text(value: object) -> str:
    return html.unescape(str(value or ""))


def cell(value: object) -> str:
    # Escape HTML *after* decoding AST storage; preserve Markdown tables.
    return (
        html.escape(text(value), quote=False)
        .replace("|", "&#124;")
        .replace("\n", "<br>")
        .replace("`", "&#96;")
    )


def code(value: object) -> str:
    return f"<code>{cell(value)}</code>"


def table(headers: list[str], rows: list[list[object]]) -> list[str]:
    if not rows:
        return ["No evidence recorded.", ""]
    return [
        "| " + " | ".join(headers) + " |",
        "| " + " | ".join("---" for _ in headers) + " |",
        *["| " + " | ".join(cell(v) for v in row) + " |" for row in rows],
        "",
    ]


def endpoint_catalog(graph: nx.Graph, service: str | None = None) -> list[tuple[str, str, str]]:
    result = set()
    for node, data in graph.nodes(data=True):
        if _java_is_test_node(graph, node) or (service and data.get("repo") != service):
            continue
        meta = _java_metadata(graph, node)
        if meta.get("java_http_role") != "inbound":
            continue
        for route in meta.get("java_http_routes") or []:
            if isinstance(route, dict) and route.get("path"):
                result.add((str(node), str(route.get("method") or "*"), text(route["path"])))
    return sorted(result, key=lambda item: (item[2], item[1], item[0]))


def evidence(graph: nx.Graph, endpoint: str) -> tuple[list[str], list[tuple[str, str, dict]]]:
    """Reach every production call, including recursion and conditional branches."""
    records = list(_true_edge_records(graph))
    owners = _java_method_owners(graph, records)
    records += _java_interface_dispatch_records(graph, records, owners)
    outgoing: dict[str, list] = {}
    for source, target, data in records:
        if data.get("relation") not in {"calls", "dispatches_to"}:
            continue
        if _java_is_test_node(graph, source) or _java_is_test_node(graph, target):
            continue
        outgoing.setdefault(source, []).append((source, target, data))
    queue = deque([endpoint])
    seen = {endpoint}
    nodes, edges = [endpoint], []

    def for_values(item):
        numbers = re.findall(r"\d+", text(item[2].get("source_location")))
        return (int(numbers[-1]) if numbers else 0, item[2].get("source_column", 0), item[1])

    while queue:
        source = queue.popleft()
        for edge in sorted(outgoing.get(source, []), key=for_values):
            edges.append(edge)
            target = edge[1]
            if target not in seen:
                seen.add(target)
                nodes.append(target)
                queue.append(target)
    return nodes, edges


def conditions(site: dict) -> str:
    meanings = {
        "then": "predicate true",
        "else": "predicate false / default",
        "after_guard": "earlier exit predicate false",
        "after_else_guard": "earlier predicate true",
        "do_at_least_once": "first iteration unconditional; repeat while true",
    }
    return (
        "; ".join(
            f"{c.get('kind', 'condition')} [{c.get('branch', '?')}]: {text(c.get('expression'))}"
            + (f" ({meanings[c['branch']]})" if c.get("branch") in meanings else "")
            for c in site.get("conditions") or []
            if isinstance(c, dict)
        )
        or "No enclosing predicate recorded"
    )


def render_endpoint(graph: nx.Graph, endpoint: str, verb: str, route: str, digest: str) -> str:
    records = list(_true_edge_records(graph))
    owners = _java_method_owners(graph, records)
    nodes, edges = evidence(graph, endpoint)
    symbol = lambda node: text(_java_flow_symbol(graph, node, owners))
    meta = _java_metadata(graph, endpoint)
    lines = [f"# {cell(verb)} {cell(route)}", "", "## Document control", ""]
    lines += table(
        ["Property", "Evidence"],
        [
            ["Service", graph.nodes[endpoint].get("repo") or "Single-service graph"],
            ["Handler", symbol(endpoint)],
            ["Source", _java_flow_source(graph, endpoint)],
            ["Graph SHA-256", digest],
            ["Document schema", "java-api-docs/1"],
            ["Status", "Generated static reference — review unresolved boundaries before approval"],
        ],
    )
    lines += [
        "## Scope and reading guide",
        "",
        "This document records what the Java graph contains. Diagrams show possible calls, "
        "not a runtime sequence. All conditional alternatives are retained. EXTRACTED means "
        "a syntactic/declared-type binding; INFERRED dispatch is a possible implementation. "
        "A missing edge does not establish that a network call or behavior is absent.",
        "",
        "Sections: [Request](#request-contract), [Response](#response-contract), "
        "[Flow](#api-flow-diagram), [Calls](#call-evidence), "
        "[Logic](#conditions-and-outcomes), [Gaps](#boundaries-and-quality).",
        "",
        "## Request contract",
        "",
    ]
    lines += table(
        ["Input", "Binding", "Java type", "Required", "Validation / default"],
        [
            [
                p.get("external_name") or p.get("name"),
                p.get("binding"),
                p.get("type"),
                p.get("required", "unspecified"),
                json.dumps(
                    {k: p[k] for k in ("constraints", "default", "validated") if k in p},
                    ensure_ascii=False,
                ),
            ]
            for p in meta.get("java_parameters") or []
            if isinstance(p, dict)
        ],
    )
    lines += [
        "## Response contract",
        "",
        f"Declared return: {code(meta.get('java_return_type') or 'not recorded')}.",
        "",
        "HTTP statuses and return expressions appear below only when recorded. "
        "No example payload or error-to-status mapping is invented.",
        "",
    ]
    contract_types = set(meta.get("java_response_types") or [])
    for p in meta.get("java_parameters") or []:
        contract_types.update(re.findall(r"[A-Za-z_$][\w$]*", text(p.get("type"))))
    dto_rows = []
    for node, data in graph.nodes(data=True):
        if data.get("label") not in contract_types:
            continue
        if data.get("repo") != graph.nodes[endpoint].get("repo"):
            continue
        metadata = _java_metadata(graph, node)
        for field, field_type in (
            metadata.get("java_declared_fields") or metadata.get("java_fields") or {}
        ).items():
            dto_rows.append([data.get("label"), field, field_type, _java_flow_source(graph, node)])
    lines += ["### Recorded request/response fields", ""]
    lines += table(["Type", "Field", "Declared type", "Type declaration"], dto_rows)
    lines += [
        "## API flow diagram",
        "",
        "Solid arrows are extracted bindings; dotted arrows are inferred. "
        "C-numbers link to the evidence table. Large flows are split without dropping edges.",
        "",
    ]
    ids = {node: f"n{i}" for i, node in enumerate(nodes)}
    # Fixed chunks preserve every edge and make large enterprise flows renderable.
    for start in range(0, max(1, len(edges)), 35):
        chunk = edges[start : start + 35]
        used = {n for source, target, _ in chunk for n in (source, target)} or {endpoint}
        lines += ["```mermaid", "flowchart TD"]
        for node in nodes:
            if node in used:
                label = (
                    html.escape(symbol(node), quote=True).replace("\n", " ").replace("`", "&#96;")
                )
                lines.append(f'    {ids[node]}["{label}"]')
        for number, (source, target, data) in enumerate(chunk, start + 1):
            arrow = "-->" if data.get("confidence") == "EXTRACTED" else "-.->"
            lines.append(f"    {ids[source]} {arrow}|C{number}| {ids[target]}")
        lines += ["```", ""]
    lines += ["## Call evidence", ""]
    rows = []
    for number, (source, target, data) in enumerate(edges, 1):
        for site in data.get("call_sites") or [data]:
            location = f"{site.get('source_file', '')}:{site.get('source_location', '')}"
            if site.get("source_column"):
                location += f":{site['source_column']}"
            rows.append(
                [
                    f"C{number}",
                    symbol(source),
                    symbol(target),
                    f"{data.get('confidence', 'UNKNOWN')} / {data.get('relation', 'calls')}",
                    conditions(site),
                    ", ".join(text(v) for v in site.get("arguments") or []),
                    location,
                ]
            )
    lines += table(["ID", "Caller", "Target", "Evidence", "Guard", "Arguments", "Call site"], rows)
    lines += ["## Method contracts", ""]
    lines += table(
        ["Method", "Parameters", "Return", "Declaration"],
        [
            [
                symbol(node),
                ", ".join(
                    f"{text(p.get('type'))} {p.get('name')}"
                    for p in _java_metadata(graph, node).get("java_parameters") or []
                ),
                _java_metadata(graph, node).get("java_return_type", "not recorded"),
                _java_flow_source(graph, node),
            ]
            for node in nodes
            if node in owners
        ],
    )
    lines += ["## Conditions and outcomes", ""]
    rows = []
    for node in nodes:
        metadata = _java_metadata(graph, node)
        for key in ("java_decisions", "java_outcomes"):
            for item in metadata.get(key) or []:
                rows.append(
                    [
                        symbol(node),
                        item.get("kind"),
                        item.get("expression"),
                        conditions(item),
                        item.get("line"),
                    ]
                )
    lines += table(["Method", "Kind", "Expression", "Enclosing guard", "Line"], rows)
    lines += ["## Boundaries and quality", ""]
    unresolved = []
    for node in nodes:
        for call in _java_metadata(graph, node).get("java_unresolved_calls") or []:
            unresolved.append(
                [
                    symbol(node),
                    f"{call.get('receiver', '')}.{call.get('callee', '')}",
                    conditions(call),
                    call.get("source_location"),
                ]
            )
    lines += table(["Caller", "Unresolved invocation", "Guard", "Line"], unresolved)
    parse_errors = [
        (data.get("source_file"), err)
        for _, data in graph.nodes(data=True)
        for err in (data.get("metadata") or {}).get("java_parse_errors") or []
    ]
    lines += [
        f"- Reachable symbols: {len(nodes)}; directed call edges: {len(edges)}; "
        f"unresolved call sites: {len(unresolved)}.",
        f"- Syntax diagnostics in this graph: {len(parse_errors)}.",
        "- Reflection, dependency binaries, generated methods, Spring bean selection, "
        "exception advice, authorization and runtime configuration may need source/runtime verification.",
        "- No runtime latency, data values or complete business semantics are asserted.",
        "",
    ]
    if parse_errors:
        lines += table(["File", "Parse diagnostic"], [[f, json.dumps(e)] for f, e in parse_errors])
    if graph.graph.get("java_extraction_diagnostics"):
        lines += ["### Extraction diagnostics", ""]
        lines += table(
            ["File", "Diagnostic"],
            [
                [d.get("source_file"), d.get("message")]
                for d in graph.graph["java_extraction_diagnostics"]
            ],
        )
    return "\n".join(lines)


def generate(
    graph_path: Path,
    output: Path,
    *,
    endpoint: str | None = None,
    service: str | None = None,
    force: bool = False,
    strict: bool = False,
) -> list[Path]:
    check_graph_file_size_cap(graph_path)
    raw = graph_path.read_bytes()
    payload = json.loads(raw)
    if not isinstance(payload, dict) or not isinstance(payload.get("nodes"), list):
        raise ValueError("Expected a graph object with a nodes list.")
    # Raw --no-cluster graphs use edges; exported graphs use links. Preserve
    # serialized direction and distinct relationships regardless of legacy flags.
    payload["links"] = payload.get("links", payload.get("edges", []))
    payload.update(directed=True, multigraph=True)
    graph = json_graph.node_link_graph(payload, edges="links")
    catalog = endpoint_catalog(graph, service)
    if endpoint:
        catalog = [item for item in catalog if f"{item[1]} {item[2]}" == endpoint]
    if not catalog:
        raise ValueError(
            "No matching inbound Java HTTP endpoint. Extract Java sources first; check route constants and service name."
        )
    if strict:
        if graph.graph.get("java_extraction_diagnostics"):
            raise ValueError("Strict documentation refused: extraction diagnostics are present.")
        if any(
            (data.get("metadata") or {}).get("java_parse_errors")
            for _, data in graph.nodes(data=True)
        ):
            raise ValueError("Strict documentation refused: Java syntax errors are present.")
    digest = hashlib.sha256(raw).hexdigest()
    documents = {}
    rows = []
    for node, verb, route in catalog:
        suffix = hashlib.sha256(f"{node}|{verb}|{route}".encode()).hexdigest()[:10]
        slug = re.sub(r"[^a-z0-9]+", "-", f"{verb}-{route}".lower()).strip("-")[:90]
        name = f"{slug}-{suffix}.md"
        documents[name] = render_endpoint(graph, node, verb, route, digest)
        rows.append(
            f"- [{cell(verb)} {cell(route)}]({name}) — {cell(graph.nodes[node].get('repo') or 'service')}"
        )
    documents["index.md"] = (
        "# Java API reference\n\nGraph SHA-256: " + digest + "\n\n" + "\n".join(rows) + "\n"
    )
    paths = [output / name for name in documents]
    if not force and any(path.exists() for path in paths):
        raise ValueError(
            "Documentation already exists; choose another --output or use --force to regenerate."
        )
    output.mkdir(parents=True, exist_ok=True)
    for name, content in documents.items():
        (output / name).write_text(content, encoding="utf-8")
    return paths


def main(argv: list[str]) -> None:
    parser = argparse.ArgumentParser(prog="graphify api-docs", description=__doc__)
    parser.add_argument("--graph", type=Path, default=default_graph_json())
    parser.add_argument("--output", type=Path, default=Path("api-docs"))
    parser.add_argument("--endpoint", help='Exact mapping, for example "GET /orders/{id}"')
    parser.add_argument("--service", help="Repository tag when a graph contains multiple services")
    parser.add_argument(
        "--force", action="store_true", help="Replace generated documents with matching names"
    )
    parser.add_argument(
        "--strict", action="store_true", help="Fail on recorded parse/extraction errors"
    )
    args = parser.parse_args(argv)
    try:
        paths = generate(
            args.graph,
            args.output,
            endpoint=args.endpoint,
            service=args.service,
            force=args.force,
            strict=args.strict,
        )
    except (OSError, ValueError, KeyError, nx.NetworkXError) as exc:
        parser.exit(2, f"error: {exc}\n")
    print(f"Generated {len(paths) - 1} API document(s): {(args.output / 'index.md').resolve()}")
