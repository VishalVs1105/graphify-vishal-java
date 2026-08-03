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
