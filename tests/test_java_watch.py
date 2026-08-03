from pathlib import Path

from graphify.watch import _WATCHED_EXTENSIONS, _rebuild_code


def test_watch_scope_contains_only_java_source_extension():
    assert _WATCHED_EXTENSIONS == {".java"}


def test_java_update_rebuilds_graph(tmp_path: Path):
    source = tmp_path / "CheckoutController.java"
    source.write_text("class CheckoutController {}\n", encoding="utf-8")

    assert _rebuild_code(tmp_path, changed_paths=[source], force=True)
    assert (tmp_path / "graphify-out" / "graph.json").exists()
