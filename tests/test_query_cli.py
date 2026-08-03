"""Tests for graphify query CLI context filtering."""
from __future__ import annotations

import json

import networkx as nx
from networkx.readwrite import json_graph

import graphify.__main__ as mainmod


def _write_graph(tmp_path):
    G = nx.Graph()
    G.add_node("n1", label="extract", source_file="extract.py", source_location="L10", community=0)
    G.add_node("n2", label="cluster", source_file="cluster.py", source_location="L5", community=0)
    G.add_node("n3", label="build", source_file="build.py", source_location="L1", community=1)
    G.add_edge("n1", "n2", relation="calls", confidence="EXTRACTED", context="call")
    G.add_edge("n2", "n3", relation="imports", confidence="EXTRACTED", context="import")
    graph_path = tmp_path / "graph.json"
    graph_path.write_text(json.dumps(json_graph.node_link_data(G, edges="links")))
    return graph_path


def test_query_cli_explicit_context_filter(monkeypatch, tmp_path, capsys):
    graph_path = _write_graph(tmp_path)
    monkeypatch.setattr(mainmod, "_check_skill_version", lambda _: None)
    monkeypatch.setattr(
        mainmod.sys,
        "argv",
        ["graphify", "query", "extract", "--context", "call", "--graph", str(graph_path)],
    )
    mainmod.main()
    out = capsys.readouterr().out
    assert "Context: call (explicit)" in out
    assert "cluster" in out
    assert "build" not in out


def test_query_cli_heuristic_context_filter(monkeypatch, tmp_path, capsys):
    graph_path = _write_graph(tmp_path)
    monkeypatch.setattr(mainmod, "_check_skill_version", lambda _: None)
    monkeypatch.setattr(
        mainmod.sys,
        "argv",
        ["graphify", "query", "who calls extract", "--graph", str(graph_path)],
    )
    mainmod.main()
    out = capsys.readouterr().out
    assert "Context: call (heuristic)" in out
    assert "cluster" in out
    assert "build" not in out


def _write_java_api_flow_graph(tmp_path, *, duplicate_route: bool = False):
    G = nx.Graph()
    nodes = [
        ("checkout_class", "CheckoutController", "checkout-service", "checkout/CheckoutController.java", "L10"),
        ("checkout", ".checkout()", "checkout-service", "checkout/CheckoutController.java", "L20"),
        ("order_class", "OrderService", "checkout-service", "checkout/OrderService.java", "L7"),
        ("place_order", ".placeOrder()", "checkout-service", "checkout/OrderService.java", "L14"),
        ("client_class", "PaymentClient", "checkout-service", "checkout/PaymentClient.java", "L9"),
        ("client_charge", ".charge()", "checkout-service", "checkout/PaymentClient.java", "L11"),
        ("controller_class", "PaymentController", "payment-service", "payment/PaymentController.java", "L11"),
        ("controller_charge", ".charge()", "payment-service", "payment/PaymentController.java", "L20"),
        ("processor_class", "PaymentProcessor", "payment-service", "payment/PaymentProcessor.java", "L7"),
        ("process", ".process()", "payment-service", "payment/PaymentProcessor.java", "L14"),
        ("stripe_class", "StripeGateway", "payment-service", "payment/StripeGateway.java", "L6"),
        ("stripe_charge", ".charge()", "payment-service", "payment/StripeGateway.java", "L7"),
    ]
    for node_id, label, repo, source, location in nodes:
        G.add_node(
            node_id,
            label=label,
            repo=repo,
            source_file=source,
            source_location=location,
        )
    G.nodes["client_charge"]["metadata"] = {
        "java_http_role": "outbound",
        "java_http_routes": [{"method": "POST", "path": "/payments/charge"}],
    }
    G.nodes["controller_charge"]["metadata"] = {
        "java_http_role": "inbound",
        "java_http_routes": [{"method": "POST", "path": "/payments/charge"}],
    }
    for owner, method in (
        ("checkout_class", "checkout"),
        ("order_class", "place_order"),
        ("client_class", "client_charge"),
        ("controller_class", "controller_charge"),
        ("processor_class", "process"),
        ("stripe_class", "stripe_charge"),
    ):
        G.add_edge(owner, method, relation="method", confidence="EXTRACTED")
    G.add_edge(
        "checkout", "place_order",
        relation="calls", confidence="INFERRED", confidence_score=0.8,
    )
    G.add_edge(
        "place_order", "client_charge",
        relation="calls", confidence="INFERRED", confidence_score=0.8,
    )
    G.add_edge(
        "client_charge",
        "controller_charge",
        relation="calls",
        confidence="INFERRED",
        confidence_score=0.95,
        bridge_strategy="java_http_route",
        cross_service=True,
    )
    G.add_edge(
        "controller_charge", "process",
        relation="calls", confidence="INFERRED", confidence_score=0.8,
    )
    G.add_edge(
        "process", "stripe_charge",
        relation="calls", confidence="INFERRED", confidence_score=0.8,
    )
    if duplicate_route:
        G.add_node(
            "legacy_class", label="LegacyPaymentController", repo="legacy-service",
            source_file="legacy/LegacyPaymentController.java", source_location="L10",
        )
        G.add_node(
            "legacy_charge", label=".charge()", repo="legacy-service",
            source_file="legacy/LegacyPaymentController.java", source_location="L20",
            metadata={
                "java_http_role": "inbound",
                "java_http_routes": [{"method": "POST", "path": "/payments/charge"}],
            },
        )
        G.add_edge("legacy_class", "legacy_charge", relation="method", confidence="EXTRACTED")
    graph_path = tmp_path / "java-flow.json"
    graph_path.write_text(json.dumps(json_graph.node_link_data(G, edges="links")))
    return graph_path


def test_query_cli_renders_deterministic_java_api_flow(monkeypatch, tmp_path, capsys):
    graph_path = _write_java_api_flow_graph(tmp_path)
    monkeypatch.setattr(mainmod, "_check_skill_version", lambda _: None)
    monkeypatch.setattr(mainmod.sys, "argv", [
        "graphify", "query",
        "Explain the complete flow of POST /payments/charge in payment-service",
        "--graph", str(graph_path),
    ])

    mainmod.main()
    out = capsys.readouterr().out
    assert "JAVA API FLOW" in out
    assert "Route: POST /payments/charge" in out
    assert "[payment-service] PaymentController.charge()" in out
    assert "Upstream calls:" in out
    assert "[checkout-service] CheckoutController.checkout() --calls" in out
    assert "[checkout-service] OrderService.placeOrder() --calls" in out
    assert "[checkout-service] PaymentClient.charge() --calls" in out
    assert "[payment-service] PaymentController.charge() --calls" in out
    assert "[payment-service] PaymentProcessor.process()" in out
    assert "[payment-service] StripeGateway.charge()" in out
    assert "Traversal:" not in out


def test_query_cli_renders_deterministic_java_method_flow(monkeypatch, tmp_path, capsys):
    graph_path = _write_java_api_flow_graph(tmp_path)
    monkeypatch.setattr(mainmod, "_check_skill_version", lambda _: None)
    monkeypatch.setattr(mainmod.sys, "argv", [
        "graphify", "query",
        "Explain the flow of PaymentProcessor.process in payment-service",
        "--graph", str(graph_path),
    ])

    mainmod.main()
    out = capsys.readouterr().out
    assert "JAVA METHOD FLOW" in out
    assert "Method mapping:" in out
    assert "PaymentController.charge() --calls" in out
    assert "PaymentProcessor.process() --calls" in out
    assert "StripeGateway.charge()" in out


def test_query_cli_requires_repo_when_http_route_is_ambiguous(monkeypatch, tmp_path, capsys):
    graph_path = _write_java_api_flow_graph(tmp_path, duplicate_route=True)
    monkeypatch.setattr(mainmod, "_check_skill_version", lambda _: None)
    monkeypatch.setattr(mainmod.sys, "argv", [
        "graphify", "query", "Explain POST /payments/charge API flow",
        "--graph", str(graph_path),
    ])

    mainmod.main()
    out = capsys.readouterr().out
    assert "AMBIGUOUS JAVA FLOW" in out
    assert "[payment-service] PaymentController.charge()" in out
    assert "[legacy-service] LegacyPaymentController.charge()" in out
    assert "Retry with the service/repository name" in out


def _write_java_interface_flow_graph(tmp_path):
    G = nx.Graph()
    nodes = [
        ("controller_class", "AddonController", "rcom-catalog-ds", "api/src/main/java/app/AddonController.java", "L40"),
        ("controller_method", ".getAddonDetailsByExternalIds()", "rcom-catalog-ds", "api/src/main/java/app/AddonController.java", "L57"),
        ("service_interface", "AddonService", "rcom-catalog-ds", "api/src/main/java/app/AddonService.java", "L10"),
        ("service_method", ".getAddonDetailsByExternalIds()", "rcom-catalog-ds", "api/src/main/java/app/AddonService.java", "L15"),
        ("service_impl", "AddonServiceImpl", "rcom-catalog-ds", "api/src/main/java/app/AddonServiceImpl.java", "L20"),
        ("impl_method", ".getAddonDetailsByExternalIds()", "rcom-catalog-ds", "api/src/main/java/app/AddonServiceImpl.java", "L39"),
        ("repository_class", "BizCatalogRepository", "rcom-catalog-ds", "api/src/main/java/app/BizCatalogRepository.java", "L22"),
        ("repository_method", ".getProductOfferingByExternalIds()", "rcom-catalog-ds", "api/src/main/java/app/BizCatalogRepository.java", "L25"),
        ("mapper_class", "AddonsMapper", "rcom-catalog-ds", "api/src/main/java/app/AddonsMapper.java", "L20"),
        ("mapper_method", ".mapAddons()", "rcom-catalog-ds", "api/src/main/java/app/AddonsMapper.java", "L35"),
        ("catalog_controller", "SocController", "biz-catalog-service", "src/main/java/catalog/SocController.java", "L30"),
        ("catalog_controller_method", ".getProductOfferingByExternalIds()", "biz-catalog-service", "src/main/java/catalog/SocController.java", "L42"),
        ("catalog_service", "CatalogService", "biz-catalog-service", "src/main/java/catalog/CatalogService.java", "L10"),
        ("catalog_service_method", ".getProductOfferingByExternalIds()", "biz-catalog-service", "src/main/java/catalog/CatalogService.java", "L14"),
        ("catalog_service_impl", "CatalogServiceImpl", "biz-catalog-service", "src/main/java/catalog/CatalogServiceImpl.java", "L20"),
        ("catalog_impl_method", ".getProductOfferingByExternalIds()", "biz-catalog-service", "src/main/java/catalog/CatalogServiceImpl.java", "L35"),
        ("soc_repository", "CatalogSocRepository", "biz-catalog-service", "src/main/java/catalog/CatalogSocRepository.java", "L12"),
        ("soc_repository_method", ".findByExternalIds()", "biz-catalog-service", "src/main/java/catalog/CatalogSocRepository.java", "L18"),
        ("test_class", "AddonControllerTest", "rcom-catalog-ds", "api/src/test/java/app/AddonControllerTest.java", "L40"),
        ("test_method", ".getAddonDetailsByExternalIds()", "rcom-catalog-ds", "api/src/test/java/app/AddonControllerTest.java", "L54"),
    ]
    for node_id, label, repo, source, location in nodes:
        G.add_node(
            node_id,
            label=label,
            repo=repo,
            source_file=source,
            source_location=location,
        )
    G.add_node("response_entity", label="ResponseEntity", repo="rcom-catalog-ds")
    G.nodes["repository_class"]["metadata"] = {"java_http_role": "outbound"}
    G.nodes["catalog_controller"]["metadata"] = {"java_http_role": "inbound"}
    G.nodes["controller_method"]["metadata"] = {
        "java_http_role": "inbound",
        "java_http_routes": [{"method": "GET", "path": "/v1/remote/addons/details"}],
    }
    for owner, method in (
        ("controller_class", "controller_method"),
        ("service_interface", "service_method"),
        ("service_impl", "impl_method"),
        ("repository_class", "repository_method"),
        ("mapper_class", "mapper_method"),
        ("catalog_controller", "catalog_controller_method"),
        ("catalog_service", "catalog_service_method"),
        ("catalog_service_impl", "catalog_impl_method"),
        ("soc_repository", "soc_repository_method"),
        ("test_class", "test_method"),
    ):
        G.add_edge(owner, method, relation="method", confidence="EXTRACTED")
    G.add_edge(
        "service_impl", "service_interface",
        relation="implements", confidence="EXTRACTED",
        _src="service_impl", _tgt="service_interface",
    )
    G.add_edge(
        "catalog_service_impl", "catalog_service",
        relation="implements", confidence="EXTRACTED",
        _src="catalog_service_impl", _tgt="catalog_service",
    )
    G.add_edge("test_method", "controller_method", relation="calls", confidence="INFERRED")
    G.add_edge("controller_method", "service_method", relation="calls", confidence="INFERRED")
    G.add_edge("controller_method", "response_entity", relation="calls", confidence="EXTRACTED")
    G.add_edge("impl_method", "repository_method", relation="calls", confidence="INFERRED")
    G.add_edge("impl_method", "mapper_method", relation="calls", confidence="INFERRED")
    G.add_edge(
        "catalog_controller_method", "catalog_service_method",
        relation="calls", confidence="INFERRED",
    )
    G.add_edge(
        "catalog_impl_method", "soc_repository_method",
        relation="calls", confidence="INFERRED",
    )
    graph_path = tmp_path / "java-interface-flow.json"
    graph_path.write_text(json.dumps(json_graph.node_link_data(G, edges="links")))
    return graph_path


def test_query_cli_follows_java_interface_dispatch_and_omits_noise(
    monkeypatch, tmp_path, capsys,
):
    graph_path = _write_java_interface_flow_graph(tmp_path)
    monkeypatch.setattr(mainmod, "_check_skill_version", lambda _: None)
    monkeypatch.setattr(mainmod.sys, "argv", [
        "graphify", "query",
        "Explain the complete flow of GET /v1/remote/addons/details in rcom-catalog-ds",
        "--graph", str(graph_path),
    ])

    mainmod.main()
    out = capsys.readouterr().out
    assert "AddonController.getAddonDetailsByExternalIds() --calls" in out
    assert "AddonService.getAddonDetailsByExternalIds() --dispatches_to" in out
    assert "bridge=java_interface_dispatch" in out
    assert "AddonServiceImpl.getAddonDetailsByExternalIds() --calls" in out
    assert "BizCatalogRepository.getProductOfferingByExternalIds() --calls" in out
    assert "bridge=java_repository_controller_method_name" in out
    assert "[biz-catalog-service] SocController.getProductOfferingByExternalIds()" in out
    assert "CatalogService.getProductOfferingByExternalIds() --dispatches_to" in out
    assert "CatalogServiceImpl.getProductOfferingByExternalIds() --calls" in out
    assert "CatalogSocRepository.findByExternalIds()" in out
    assert out.index("CatalogSocRepository.findByExternalIds()") < out.index("AddonsMapper.mapAddons()")
    assert "Recorded terminal points:" in out
    assert "CatalogSocRepository.findByExternalIds() (repository/external boundary" in out
    assert "AddonControllerTest" not in out
    assert "ResponseEntity" not in out


def test_query_cli_continues_deep_java_flow_to_recorded_leaf(monkeypatch, tmp_path, capsys):
    G = nx.Graph()
    methods = []
    for index in range(12):
        owner = f"stage_class_{index}"
        method = f"stage_method_{index}"
        source = f"src/main/java/app/Stage{index}.java"
        G.add_node(owner, label=f"Stage{index}", repo="deep-service", source_file=source)
        G.add_node(
            method,
            label=f".step{index}()",
            repo="deep-service",
            source_file=source,
            source_location=f"L{index + 10}",
        )
        G.add_edge(owner, method, relation="method", confidence="EXTRACTED")
        methods.append(method)
    G.nodes[methods[0]]["metadata"] = {
        "java_http_role": "inbound",
        "java_http_routes": [{"method": "GET", "path": "/deep"}],
    }
    for source, target in zip(methods, methods[1:]):
        G.add_edge(source, target, relation="calls", confidence="EXTRACTED")
    graph_path = tmp_path / "deep-java-flow.json"
    graph_path.write_text(json.dumps(json_graph.node_link_data(G, edges="links")))
    monkeypatch.setattr(mainmod, "_check_skill_version", lambda _: None)
    monkeypatch.setattr(mainmod.sys, "argv", [
        "graphify", "query", "Explain the complete flow of GET /deep in deep-service",
        "--graph", str(graph_path), "--budget", "10000",
    ])

    mainmod.main()
    out = capsys.readouterr().out
    assert "Stage11.step11()" in out


def _write_calls_graph(tmp_path):
    """A single directed `calls` edge on an (on-disk) undirected graph.json,

    the standard `graphify extract`/`update` output shape (`"directed":
    false`, direction implied only by each link's source/target).
    """
    G = nx.Graph()
    G.add_node("caller", label="caller_fn", source_file="a.py", source_location="L1", community=0)
    G.add_node("callee", label="callee_fn", source_file="b.py", source_location="L1", community=1)
    G.add_edge("caller", "callee", relation="calls", confidence="EXTRACTED", context="call")
    graph_path = tmp_path / "graph.json"
    graph_path.write_text(json.dumps(json_graph.node_link_data(G, edges="links")))
    return graph_path


def test_query_cli_preserves_calls_direction_when_seeded_on_callee(monkeypatch, tmp_path, capsys):
    """`graphify query` must render `calls` edges caller->callee regardless of
    which endpoint the query term matches first.

    The graph `query` loads is undirected (so BFS/DFS can explore both
    callers and callees of the seed), so `G.neighbors()` returns `caller_fn`
    as a neighbor of `callee_fn` with no direction of its own. Before the
    fix, the renderer assumed the BFS/DFS visit order (u, v) was the edge's
    (source, target), so seeding on the callee printed the edge backwards:
    "callee_fn --calls--> caller_fn". graph.json's `source`/`target` for this
    edge stay correct on disk either way; only the query rendering was wrong.
    """
    graph_path = _write_calls_graph(tmp_path)
    monkeypatch.setattr(mainmod, "_check_skill_version", lambda _: None)
    monkeypatch.setattr(
        mainmod.sys,
        "argv",
        ["graphify", "query", "callee_fn", "--graph", str(graph_path)],
    )
    mainmod.main()
    out = capsys.readouterr().out
    assert "caller_fn --calls" in out
    assert "callee_fn --calls" not in out


def test_query_cli_preserves_calls_direction_when_seeded_on_caller(monkeypatch, tmp_path, capsys):
    """Same edge, seeded from the caller side — must stay correct too."""
    graph_path = _write_calls_graph(tmp_path)
    monkeypatch.setattr(mainmod, "_check_skill_version", lambda _: None)
    monkeypatch.setattr(
        mainmod.sys,
        "argv",
        ["graphify", "query", "caller_fn", "--graph", str(graph_path)],
    )
    mainmod.main()
    out = capsys.readouterr().out
    assert "caller_fn --calls" in out
    assert "callee_fn --calls" not in out


def test_query_cli_rejects_oversized_graph(monkeypatch, tmp_path, capsys):
    """#F4: query CLI must refuse to parse a graph.json that exceeds the cap."""
    import pytest

    graph_path = _write_graph(tmp_path)
    monkeypatch.setattr(mainmod, "_check_skill_version", lambda _: None)
    monkeypatch.setattr("graphify.security._MAX_GRAPH_FILE_BYTES", 16)
    monkeypatch.setattr(
        mainmod.sys,
        "argv",
        ["graphify", "query", "extract", "--graph", str(graph_path)],
    )
    with pytest.raises(SystemExit):
        mainmod.main()
    err = capsys.readouterr().err
    assert "exceeds" in err
    assert "byte cap" in err
