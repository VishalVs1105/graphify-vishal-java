from pathlib import Path

from graphify.detect import FileType, classify_file, detect
from graphify.extract import collect_files


def test_detect_classifies_only_java_as_code(tmp_path: Path):
    java = tmp_path / "CheckoutController.java"
    java.write_text("class CheckoutController {}\n", encoding="utf-8")
    (tmp_path / "ignored.py").write_text("def ignored(): pass\n", encoding="utf-8")
    (tmp_path / "ignored.ts").write_text("function ignored() {}\n", encoding="utf-8")

    assert classify_file(java) is FileType.CODE
    assert classify_file(tmp_path / "ignored.py") is None
    result = detect(tmp_path)
    assert result["files"]["code"] == [str(java)]


def test_collect_files_returns_only_java(tmp_path: Path):
    java = tmp_path / "PaymentClient.java"
    java.write_text("class PaymentClient {}\n", encoding="utf-8")
    (tmp_path / "ignored.js").write_text("function ignored() {}\n", encoding="utf-8")

    assert collect_files(tmp_path) == [java]
