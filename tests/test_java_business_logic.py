from __future__ import annotations

from pathlib import Path
import json

import networkx as nx
from networkx.readwrite import json_graph

import graphify.__main__ as mainmod
from graphify.extract import collect_files, extract
from graphify.serve import _query_graph_text


def _write_business_service(root: Path) -> None:
    (root / "OrderController.java").write_text(
        """
        @RestController
        @RequestMapping("/orders")
        class OrderController {
            private final OrderService service = null;

            @PostMapping("/{id}")
            ResponseEntity<OrderResponse> create(
                @PathVariable(name = "id", required = true) String orderId,
                @Valid @RequestBody OrderRequest request
            ) {
                if (request.isPriority()) {
                    return ResponseEntity.ok(service.process(request));
                } else {
                    return ResponseEntity.ok(service.process(request));
                }
            }
        }
        """,
        encoding="utf-8",
    )
    (root / "OrderService.java").write_text(
        """
        class OrderService {
            private final OrderRepository repository = null;

            OrderResponse process(OrderRequest request) {
                if (request.hasInventory()) {
                    repository.reserve(request);
                } else {
                    repository.backorder(request);
                }
                return repository.loadResult(request);
            }
        }
        """,
        encoding="utf-8",
    )
    (root / "OrderRepository.java").write_text(
        """
        class OrderRepository {
            void reserve(OrderRequest request) {}
            void backorder(OrderRequest request) {}
            OrderResponse loadResult(OrderRequest request) { return null; }
        }
        """,
        encoding="utf-8",
    )
    (root / "OrderRequest.java").write_text(
        "class OrderRequest {}",
        encoding="utf-8",
    )
    (root / "OrderResponse.java").write_text(
        "class OrderResponse {}",
        encoding="utf-8",
    )


def _business_graph(tmp_path: Path) -> nx.Graph:
    _write_business_service(tmp_path)
    result = extract(collect_files(tmp_path), root=tmp_path)
    graph = nx.Graph()
    for node in result["nodes"]:
        graph.add_node(node["id"], **{key: value for key, value in node.items() if key != "id"})
    for edge in result["edges"]:
        source, target = edge["source"], edge["target"]
        graph.add_edge(
            source,
            target,
            **{
                **{key: value for key, value in edge.items() if key not in {"source", "target"}},
                "_src": source,
                "_tgt": target,
            },
        )
    for _node_id, data in graph.nodes(data=True):
        data["repo"] = "order-service"
    return graph


def test_java_extraction_records_contracts_conditions_and_repeated_call_sites(tmp_path: Path):
    graph = _business_graph(tmp_path)
    create = next(
        node_id for node_id, data in graph.nodes(data=True)
        if data.get("label") == ".create()"
    )
    process = next(
        node_id for node_id, data in graph.nodes(data=True)
        if data.get("label") == ".process()"
    )
    metadata = graph.nodes[create]["metadata"]
    assert metadata["java_return_type"] == "ResponseEntity&lt;OrderResponse&gt;"
    assert metadata["java_response_types"] == ["ResponseEntity", "OrderResponse"]
    assert metadata["java_parameters"][0] == {
        "name": "orderId",
        "type": "String",
        "binding": "path",
        "annotations": [{
            "name": "PathVariable",
            "values": {"name": ["id"], "required": ["true"]},
        }],
        "validated": False,
        "external_name": "id",
        "required": True,
    }
    assert metadata["java_parameters"][1]["type"] == "OrderRequest"
    assert metadata["java_parameters"][1]["binding"] == "body"
    assert metadata["java_parameters"][1]["validated"] is True
    edge = graph[create][process]
    assert len(edge["call_sites"]) == 2
    assert edge["call_sites"][0]["conditions"][0]["branch"] == "then"
    assert edge["call_sites"][1]["conditions"][0]["branch"] == "else"


def test_developer_flow_includes_contracts_conditions_and_complete_call_inventory(tmp_path: Path):
    graph = _business_graph(tmp_path)
    output = _query_graph_text(
        graph,
        "Explain the complete developer flow of POST /orders/{id} in order-service",
        token_budget=60_000,
        audience="developer",
    )
    assert output.startswith("JAVA API FLOW")
    assert "Endpoint request/response contract:" in output
    assert "body request: OrderRequest" in output
    assert "returns=ResponseEntity<OrderResponse>" in output
    assert "Complete reachable production call inventory:" in output
    assert "occurrences=2" in output
    assert "otherwise (not request.isPriority())" in output
    assert "OrderRepository.reserve()" in output
    assert "OrderRepository.backorder()" in output
    assert "Method request/response contracts:" in output
    assert "Decision and outcome logic:" in output
    assert "Observed unresolved Java calls:" in output
    assert "request.isPriority()" in output


def test_bsa_flow_translates_rules_without_java_chain_or_dto_types(tmp_path: Path):
    graph = _business_graph(tmp_path)
    output = _query_graph_text(
        graph,
        "Explain the BSA business flow of POST /orders/{id} in order-service",
        token_budget=60_000,
        audience="bsa",
    )
    assert output.startswith("BUSINESS API FLOW")
    assert "Business request:" in output
    assert "Id is a required URL path value" in output
    assert "Request is a required request body information and validated" in output
    assert "Business rules and decision points:" in output
    assert "request is priority" in output.casefold()
    assert "request has inventory" in output.casefold()
    assert "Business response and outcomes:" in output
    assert "observed Java call(s)" in output
    assert "OrderRequest" not in output
    assert "OrderResponse" not in output
    assert "OrderController" not in output
    assert "OrderService.process" not in output


def test_java_overloads_are_preserved_and_resolved_by_argument_arity(tmp_path: Path):
    source = tmp_path / "Overloaded.java"
    source.write_text(
        """
        class Overloaded {
            void run() {
                helper();
                helper("value");
            }
            void helper() {}
            void helper(String value) {}
        }
        """,
        encoding="utf-8",
    )
    result = extract([source], root=tmp_path)
    methods = [node for node in result["nodes"] if node.get("label") == ".helper()"]
    assert len(methods) == 2
    assert {node["metadata"]["java_parameter_count"] for node in methods} == {0, 1}
    run = next(node for node in result["nodes"] if node.get("label") == ".run()")
    calls = [
        edge for edge in result["edges"]
        if edge.get("relation") == "calls" and edge.get("source") == run["id"]
    ]
    helper_ids = {node["id"] for node in methods}
    resolved = [edge for edge in calls if edge.get("target") in helper_ids]
    assert len(resolved) == 2
    assert {edge.get("argument_count") for edge in resolved} == {0, 1}


def test_java_switch_branches_are_attached_to_calls(tmp_path: Path):
    source = tmp_path / "SwitchFlow.java"
    source.write_text(
        """
        class SwitchFlow {
            void run(String type) {
                switch (type) {
                    case "ADDON" -> loadAddon();
                    default -> loadDefault();
                }
            }
            void loadAddon() {}
            void loadDefault() {}
        }
        """,
        encoding="utf-8",
    )
    result = extract([source], root=tmp_path)
    labels = {node["id"]: node.get("label") for node in result["nodes"]}
    calls = [edge for edge in result["edges"] if edge.get("relation") == "calls"]
    addon = next(edge for edge in calls if labels.get(edge["target"]) == ".loadAddon()")
    default = next(edge for edge in calls if labels.get(edge["target"]) == ".loadDefault()")
    assert addon["conditions"][0]["branch"] == "case"
    assert "type is" in addon["conditions"][0]["expression"]
    assert default["conditions"][0]["branch"] == "else"
    assert "no explicit type case matches" == default["conditions"][0]["expression"]


def test_query_cli_accepts_explicit_bsa_audience(monkeypatch, tmp_path: Path, capsys):
    graph = _business_graph(tmp_path)
    graph_path = tmp_path / "business-graph.json"
    graph_path.write_text(
        json.dumps(json_graph.node_link_data(graph, edges="links")),
        encoding="utf-8",
    )
    monkeypatch.setattr(mainmod, "_check_skill_version", lambda _cmd: None)
    monkeypatch.setattr(mainmod.sys, "argv", [
        "graphify", "query",
        "Explain POST /orders/{id} in order-service",
        "--audience", "bsa",
        "--budget", "60000",
        "--graph", str(graph_path),
    ])
    mainmod.main()
    output = capsys.readouterr().out
    assert output.startswith("BUSINESS API FLOW")
    assert "Business rules and decision points:" in output
    assert "OrderRequest" not in output
