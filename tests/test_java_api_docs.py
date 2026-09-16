"""Evidence-preservation regressions and public API-document generation."""

import json
from pathlib import Path

import networkx as nx
from networkx.readwrite import json_graph
import pytest

from graphify.api_docs import generate, evidence
from graphify.extract import extract, collect_files
from graphify.serve import _query_graph_text


def corpus(root, files):
    for name, content in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    return extract(collect_files(root), root=root)


def graph_from(result):
    graph = nx.MultiDiGraph()
    for node in result["nodes"]:
        graph.add_node(node["id"], **{k: v for k, v in node.items() if k != "id"}, repo="catalog")
    for edge in result["edges"]:
        graph.add_edge(
            edge["source"],
            edge["target"],
            **{k: v for k, v in edge.items() if k not in {"source", "target"}},
        )
    return graph


def test_all_calls_and_large_conditions_survive(tmp_path):
    branches = "\n".join(f"if (v == {i}) client.load(v);" for i in range(65))
    result = corpus(
        tmp_path,
        {
            "Flow.java": """
        @RestController class Flow {
            Client client;
            @GetMapping("/addons") Result run(@RequestParam int v) {
                client.load(v); client.load(v);
    """
            + branches
            + """
                return client.load(v);
            }
        }
        class Client { Result load(int v) { return null; } }
    """
        },
    )
    run = next(n for n in result["nodes"] if n["label"] == ".run()")
    assert len(run["metadata"]["java_decisions"]) == 65
    calls = [e for e in result["edges"] if e["relation"] == "calls" and e["source"] == run["id"]]
    assert len(calls) == 1
    assert calls[0]["occurrence_count"] == 68
    assert len(calls[0]["call_sites"]) == 68
    assert calls[0]["call_sites"][0]["source_column"] != calls[0]["call_sites"][1]["source_column"]


def test_query_wording_never_deletes_calls_from_complete_inventory(tmp_path):
    result = corpus(
        tmp_path,
        {
            "Flow.java": """
        @RestController class Flow {
            @GetMapping("/addons") void run() {
                getAddonEntries(); getDeviceEntries(); getCartEntries();
            }
            void getAddonEntries() {}
            void getDeviceEntries() {}
            void getCartEntries() {}
        }
    """
        },
    )
    graph = graph_from(result)
    for suffix in ("for a BSA", "for a developer", ""):
        output = _query_graph_text(graph, "Explain GET /addons " + suffix, token_budget=60000)
        assert output.startswith("JAVA API FLOW")
        inventory = output.split("Complete reachable production call inventory:", 1)[1]
        assert all(
            name in inventory for name in ("getAddonEntries", "getDeviceEntries", "getCartEntries")
        )


def test_ternary_condition_call_is_not_in_false_branch(tmp_path):
    result = corpus(
        tmp_path,
        {
            "Flow.java": """
        class Flow {
            int run() { return allowed() ? accept() : reject(); }
            boolean allowed() { return true; }
            int accept() { return 1; }
            int reject() { return 0; }
        }
    """
        },
    )
    by_id = {n["id"]: n["label"] for n in result["nodes"]}
    calls = {by_id[e["target"]]: e for e in result["edges"] if e["relation"] == "calls"}
    assert not calls[".allowed()"].get("conditions")
    assert calls[".accept()"]["conditions"][0]["branch"] == "then"
    assert calls[".reject()"]["conditions"][0]["branch"] == "else"


def test_import_qualified_duplicate_types_and_wrong_arity(tmp_path):
    result = corpus(
        tmp_path,
        {
            "a/Client.java": "package a; public class Client { public void load(int id) {} }",
            "b/Client.java": "package b; public class Client { public void load(int id) {} }",
            "app/Flow.java": """package app; import a.Client;
            class Flow { Client client; void run() { client.load(1); client.load(); } }""",
        },
    )
    nodes = {n["id"]: n for n in result["nodes"]}
    run = next(n for n in result["nodes"] if n["label"] == ".run()")
    calls = [e for e in result["edges"] if e["relation"] == "calls" and e["source"] == run["id"]]
    assert len(calls) == 1
    assert nodes[calls[0]["target"]]["source_file"] == "a/Client.java"
    assert run["metadata"]["java_unresolved_calls"][0]["argument_count"] == 0


def test_recursion_remains_in_graph_and_document_traversal_terminates(tmp_path):
    result = corpus(
        tmp_path,
        {
            "Flow.java": """
        class Flow { void run(int n) { if (n > 0) run(n - 1); } }
    """
        },
    )
    run = next(n for n in result["nodes"] if n["label"] == ".run()")
    graph = graph_from(result)
    nodes, edges = evidence(graph, run["id"])
    assert nodes == [run["id"]]
    assert len(edges) == 1 and edges[0][0] == edges[0][1]


def test_api_docs_end_to_end_deterministic_and_no_overwrite(tmp_path):
    result = corpus(
        tmp_path,
        {
            "Orders.java": """
        @RestController @RequestMapping("/orders") class OrderController {
            OrderService service;
            @PostMapping("/{id}") OrderResponse create(
                @PathVariable String id, @Valid @RequestBody OrderRequest request) {
                if (request == null) throw new IllegalArgumentException("request");
                return service.create(request);
            }
        }
        class OrderService {
            OrderResponse create(OrderRequest request) { audit(request); return null; }
        }
        class OrderRequest { String sku; int quantity; }
        class OrderResponse { String orderId; }
    """
        },
    )
    graph_path = tmp_path / "graph.json"
    graph_path.write_text(json.dumps(json_graph.node_link_data(graph_from(result), edges="links")))
    output = tmp_path / "docs"
    paths = generate(graph_path, output, endpoint="POST /orders/{id}", strict=True)
    document = next(p for p in paths if p.name != "index.md").read_text(encoding="utf-8")
    assert "```mermaid" in document
    assert "OrderService.create()" in document
    assert "OrderRequest" in document and "quantity" in document
    assert "after_guard" in document and "request == null" in document
    assert ".audit" in document
    assert "Graph SHA-256" in document
    with pytest.raises(ValueError, match="already exists"):
        generate(graph_path, output)
    generate(graph_path, output, force=True)
    assert next(p for p in paths if p.name != "index.md").read_text(encoding="utf-8") == document


def test_malformed_java_reports_diagnostics_and_strict_docs_fail(tmp_path):
    result = corpus(
        tmp_path,
        {
            "Broken.java": """
        @RestController class Broken {
            @GetMapping("/broken") void run() { int x = ; }
        }
    """
        },
    )
    assert any(n.get("metadata", {}).get("java_parse_errors") for n in result["nodes"])
    path = tmp_path / "graph.json"
    path.write_text(json.dumps(json_graph.node_link_data(graph_from(result), edges="links")))
    with pytest.raises(ValueError, match="syntax errors"):
        generate(path, tmp_path / "docs", strict=True)


def test_unreadable_source_is_not_reported_as_complete(tmp_path):
    result = extract([tmp_path / "missing.java"], root=tmp_path)
    assert result["complete"] is False
    assert result["diagnostics"][0]["source_file"] == "missing.java"


def test_short_circuit_predicates_and_conditional_return_values(tmp_path):
    result = corpus(
        tmp_path,
        {
            "Flow.java": """
        class Flow {
            int run() { return allowed() && ready() ? accept() : reject(); }
            boolean allowed() { return true; }
            boolean ready() { return true; }
            int accept() { return 1; }
            int reject() { return 0; }
        }
    """
        },
    )
    by_id = {n["id"]: n["label"] for n in result["nodes"]}
    calls = {by_id[e["target"]]: e for e in result["edges"] if e["relation"] == "calls"}
    assert not calls[".allowed()"].get("conditions")
    assert calls[".ready()"]["conditions"][0]["kind"] == "short_circuit"
    run = next(n for n in result["nodes"] if n["label"] == ".run()")
    returns = run["metadata"]["java_outcomes"]
    assert {v["expression"] for v in returns} == {"accept()", "reject()"}
    assert {v["conditions"][0]["branch"] for v in returns} == {"then", "else"}


def test_unknown_request_values_never_hide_query_error_outcomes(tmp_path):
    result = corpus(tmp_path, {"Flow.java": '''
        @RestController class Flow {
            @GetMapping("/flow") int run(@RequestParam String id) {
                if (id == null) { audit(); return 400; }
                return 200;
            }
            void audit() { missingLibrary(); }
        }
    '''})
    output = _query_graph_text(graph_from(result), "Explain GET /flow", token_budget=60000)
    assert "Flow.audit()" in output
    assert "return 400" in output and "return 200" in output
    assert "missingLibrary" in output


def test_varargs_member_calls_remain_resolvable(tmp_path):
    result = corpus(
        tmp_path,
        {
            "Client.java": "class Client { void load(String... ids) {} }",
            "Flow.java": 'class Flow { Client client; void run() { client.load(); client.load("a", "b"); } }',
        },
    )
    run = next(n for n in result["nodes"] if n["label"] == ".run()")
    calls = [e for e in result["edges"] if e["relation"] == "calls" and e["source"] == run["id"]]
    assert len(calls) == 1 and calls[0]["occurrence_count"] == 2


def test_raw_graph_keeps_direction_and_parallel_relations(tmp_path):
    result = corpus(
        tmp_path,
        {
            "Flow.java": """
        class Worker { void work() {} }
        @RestController class Flow { Worker worker;
            @GetMapping("/work") void run() { worker.work(); }
        }
    """
        },
    )
    path = tmp_path / "raw.json"
    path.write_text(json.dumps(result), encoding="utf-8")
    paths = generate(path, tmp_path / "docs")
    document = paths[0].read_text(encoding="utf-8")
    assert "Worker.work()" in document
    assert "Flow.run()" in document


def test_public_cli_extract_then_api_docs(tmp_path):
    import subprocess
    import sys

    source = Path(__file__).resolve().parents[1] / "examples" / "order-service"
    graph_dir = tmp_path / "graph"
    command = [sys.executable, "-m", "graphify"]
    extraction = subprocess.run(
        command + ["extract", str(source), "--no-cluster", "--out", str(graph_dir)],
        capture_output=True,
        text=True,
    )
    assert extraction.returncode == 0, extraction.stdout + extraction.stderr
    output = tmp_path / "docs"
    docs = subprocess.run(
        command
        + [
            "api-docs",
            "--graph",
            str(graph_dir / "graphify-out" / "graph.json"),
            "--output",
            str(output),
            "--strict",
        ],
        capture_output=True,
        text=True,
    )
    assert docs.returncode == 0, docs.stdout + docs.stderr
    document = next(p for p in output.glob("*.md") if p.name != "index.md").read_text(
        encoding="utf-8"
    )
    assert "DefaultOrderService.create()" in document
    for target in (
        "PaymentClient.charge()",
        "InventoryRepository.available()",
        "OrderRepository.save()",
    ):
        assert target in document
    assert "response == null" in document
