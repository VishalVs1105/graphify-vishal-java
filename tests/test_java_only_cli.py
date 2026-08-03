from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


def _run(args: list[str], cwd: Path):
    return subprocess.run(
        [sys.executable, "-m", "graphify", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
    )


def _sources(graph_path: Path) -> set[str]:
    data = json.loads(graph_path.read_text(encoding="utf-8"))
    return {
        str(node.get("source_file", "")).replace("\\", "/")
        for node in data.get("nodes", [])
        if node.get("source_file")
    }


def test_java_backend_default_indexes_only_java_on_incremental_runs(tmp_path: Path):
    (tmp_path / "CheckoutController.java").write_text(
        "class CheckoutController { void checkout() {} }\n", encoding="utf-8"
    )
    (tmp_path / "ignored.py").write_text("def ignored(): pass\n", encoding="utf-8")
    (tmp_path / "README.md").write_text("# ignored documentation\n", encoding="utf-8")

    first = _run(["extract", ".", "--no-cluster"], tmp_path)
    assert first.returncode == 0, first.stderr
    graph_path = tmp_path / "graphify-out" / "graph.json"
    assert graph_path.exists()
    assert _sources(graph_path) == {"CheckoutController.java"}

    (tmp_path / "PaymentClient.java").write_text(
        "class PaymentClient { void charge() {} }\n", encoding="utf-8"
    )
    (tmp_path / "still_ignored.js").write_text("function nope() {}\n", encoding="utf-8")
    second = _run(["extract", ".", "--no-cluster"], tmp_path)

    assert second.returncode == 0, second.stderr
    sources = _sources(graph_path)
    assert sources == {"CheckoutController.java", "PaymentClient.java"}
    assert "Java backend scope" in second.stdout


def test_two_java_services_extract_merge_and_form_e2e_class_path(tmp_path: Path):
    checkout = tmp_path / "checkout-service"
    payment = tmp_path / "payment-service"
    checkout.mkdir()
    payment.mkdir()

    checkout_sources = {
        "CheckoutController.java": "class CheckoutController { OrderService orders; }\n",
        "OrderService.java": "class OrderService { PaymentClient payment; }\n",
        "PaymentClient.java": "class PaymentClient {}\n",
    }
    payment_sources = {
        "PaymentController.java": "class PaymentController { PaymentProcessor processor; }\n",
        "PaymentProcessor.java": "class PaymentProcessor { StripeGateway stripe; }\n",
        "StripeGateway.java": "class StripeGateway {}\n",
    }
    for name, source in checkout_sources.items():
        (checkout / name).write_text(source, encoding="utf-8")
    for name, source in payment_sources.items():
        (payment / name).write_text(source, encoding="utf-8")

    for service in (checkout, payment):
        result = _run(["extract", str(service), "--no-cluster"], tmp_path)
        assert result.returncode == 0, result.stderr

    merged = _run([
        "merge-graphs",
        str(checkout / "graphify-out" / "graph.json"),
        str(payment / "graphify-out" / "graph.json"),
    ], tmp_path)
    assert merged.returncode == 0, merged.stderr

    data = json.loads((tmp_path / "graphify-out" / "graph.json").read_text(encoding="utf-8"))
    ids = {
        (node.get("repo"), node.get("label")): node["id"]
        for node in data["nodes"]
    }
    labels = [
        ("checkout-service", "CheckoutController"),
        ("checkout-service", "OrderService"),
        ("checkout-service", "PaymentClient"),
        ("payment-service", "PaymentController"),
        ("payment-service", "PaymentProcessor"),
        ("payment-service", "StripeGateway"),
    ]
    chain_ids = [ids[label] for label in labels]
    connected = {
        frozenset((link["source"], link["target"]))
        for link in data["links"]
    }
    assert all(
        frozenset((source, target)) in connected
        for source, target in zip(chain_ids, chain_ids[1:])
    )
    bridge = next(link for link in data["links"] if link.get("cross_service"))
    assert bridge["source"] == chain_ids[2]
    assert bridge["target"] == chain_ids[3]


def test_http_annotations_auto_bridge_repository_method_to_controller_handler(tmp_path: Path):
    catalog_ds = tmp_path / "catalog-ds"
    catalog_service = tmp_path / "biz-catalog-service"
    catalog_ds.mkdir()
    catalog_service.mkdir()

    (catalog_ds / "BizCatalogRepository.java").write_text(
        '''
@FeignClient(name = "biz-catalog", path = "/api/catalog")
interface BizCatalogRepository {
    @GetMapping("/addons/{externalId}")
    Object checkAddonsCompatibility(String externalId);
}
''',
        encoding="utf-8",
    )
    (catalog_service / "AddonController.java").write_text(
        '''
@RestController
@RequestMapping("/api/catalog")
class AddonController {
    @GetMapping(path = "/addons/{id}")
    Object checkCompatibility(String id) { return null; }
}
''',
        encoding="utf-8",
    )

    for service in (catalog_ds, catalog_service):
        result = _run(["extract", str(service), "--no-cluster"], tmp_path)
        assert result.returncode == 0, result.stderr

    merged = _run([
        "merge-graphs",
        str(catalog_ds / "graphify-out" / "graph.json"),
        str(catalog_service / "graphify-out" / "graph.json"),
    ], tmp_path)
    assert merged.returncode == 0, merged.stderr
    assert "Java HTTP route bridge" in merged.stdout

    data = json.loads((tmp_path / "graphify-out" / "graph.json").read_text(encoding="utf-8"))
    node_by_id = {node["id"]: node for node in data["nodes"]}
    route_bridges = [
        link for link in data["links"]
        if link.get("bridge_strategy") == "java_http_route"
    ]
    assert len(route_bridges) == 1
    bridge = route_bridges[0]
    assert node_by_id[bridge["source"]]["label"] == ".checkAddonsCompatibility()"
    assert node_by_id[bridge["target"]]["label"] == ".checkCompatibility()"
    assert bridge["http_method"] == "GET"
    assert bridge["http_route"] == "/api/catalog/addons/{}"
    assert bridge["confidence"] == "INFERRED"
    assert bridge["confidence_score"] == 0.95


def test_http_route_bridge_skips_ambiguous_controller_handlers(tmp_path: Path):
    source = tmp_path / "catalog-ds"
    target_a = tmp_path / "catalog-service-a"
    target_b = tmp_path / "catalog-service-b"
    for service in (source, target_a, target_b):
        service.mkdir()

    (source / "BizCatalogRepository.java").write_text(
        '''
@FeignClient(name = "catalog")
interface BizCatalogRepository {
    @RequestMapping(path = "/devices/{id}", method = RequestMethod.GET)
    Object getDevice(String id);
}
''',
        encoding="utf-8",
    )
    for directory, controller in (
        (target_a, "DeviceController"),
        (target_b, "LegacyDeviceController"),
    ):
        (directory / f"{controller}.java").write_text(
            f'''
@RestController
class {controller} {{
    @GetMapping("/devices/{{deviceId}}")
    Object getDevice(String deviceId) {{ return null; }}
}}
''',
            encoding="utf-8",
        )

    for service in (source, target_a, target_b):
        result = _run(["extract", str(service), "--no-cluster"], tmp_path)
        assert result.returncode == 0, result.stderr

    merged = _run([
        "merge-graphs",
        str(source / "graphify-out" / "graph.json"),
        str(target_a / "graphify-out" / "graph.json"),
        str(target_b / "graphify-out" / "graph.json"),
    ], tmp_path)
    assert merged.returncode == 0, merged.stderr
    data = json.loads((tmp_path / "graphify-out" / "graph.json").read_text(encoding="utf-8"))
    assert not any(
        link.get("bridge_strategy") == "java_http_route"
        for link in data["links"]
    )


def test_method_name_auto_bridges_repository_to_controller_when_route_is_constant(
    tmp_path: Path,
):
    source = tmp_path / "catalog-ds"
    target = tmp_path / "biz-catalog-service"
    source.mkdir()
    target.mkdir()
    (source / "BizCatalogRepository.java").write_text(
        '''
interface BizCatalogRepository {
    @GetMapping(CatalogRoutes.GET_ALL_DEVICES)
    Object getAllDevices();
}
''',
        encoding="utf-8",
    )
    (target / "DeviceController.java").write_text(
        '''
@RestController
class DeviceController {
    @GetMapping(CatalogRoutes.GET_ALL_DEVICES)
    Object getAllDevices() { return null; }
}
''',
        encoding="utf-8",
    )
    for service in (source, target):
        result = _run(["extract", str(service), "--no-cluster"], tmp_path)
        assert result.returncode == 0, result.stderr

    merged = _run([
        "merge-graphs",
        str(source / "graphify-out" / "graph.json"),
        str(target / "graphify-out" / "graph.json"),
    ], tmp_path)
    assert merged.returncode == 0, merged.stderr
    assert "Repository-to-Controller method bridge" in merged.stdout
    data = json.loads((tmp_path / "graphify-out" / "graph.json").read_text(encoding="utf-8"))
    nodes = {node["id"]: node for node in data["nodes"]}
    bridges = [
        link for link in data["links"]
        if link.get("bridge_strategy") == "java_repository_controller_method_name"
    ]
    assert len(bridges) == 1
    bridge = bridges[0]
    assert nodes[bridge["source"]]["label"] == ".getAllDevices()"
    assert nodes[bridge["source"]]["repo"] == "catalog-ds"
    assert nodes[bridge["target"]]["label"] == ".getAllDevices()"
    assert nodes[bridge["target"]]["repo"] == "biz-catalog-service"
    assert bridge["confidence_score"] == 0.85


def test_method_name_bridge_does_not_guess_between_controllers(tmp_path: Path):
    source = tmp_path / "catalog-ds"
    targets = [tmp_path / "catalog-a", tmp_path / "catalog-b"]
    source.mkdir()
    for target in targets:
        target.mkdir()
    (source / "BizCatalogRepository.java").write_text(
        "interface BizCatalogRepository { Object getAllDevices(); }\n",
        encoding="utf-8",
    )
    for index, target in enumerate(targets):
        (target / f"Device{index}Controller.java").write_text(
            f"@RestController class Device{index}Controller "
            "{ Object getAllDevices() { return null; } }\n",
            encoding="utf-8",
        )
    for service in (source, *targets):
        result = _run(["extract", str(service), "--no-cluster"], tmp_path)
        assert result.returncode == 0, result.stderr

    merged = _run([
        "merge-graphs",
        str(source / "graphify-out" / "graph.json"),
        *(str(target / "graphify-out" / "graph.json") for target in targets),
    ], tmp_path)
    assert merged.returncode == 0, merged.stderr
    data = json.loads((tmp_path / "graphify-out" / "graph.json").read_text(encoding="utf-8"))
    assert not any(
        link.get("bridge_strategy") == "java_repository_controller_method_name"
        for link in data["links"]
    )
