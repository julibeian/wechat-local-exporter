from __future__ import annotations

import json
import os
import zipfile
from pathlib import Path

from pypdf import PdfReader

from wechat_exporter.selftest import run_packaged_self_test


def test_packaged_self_test_payload(tmp_path: Path) -> None:
    receipt_path = run_packaged_self_test(tmp_path)
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    assert receipt["status"] == "ok"
    assert (
        receipt["json_count"]
        == receipt["jsonl_count"]
        == receipt["txt_count"]
        == receipt["pdf_count"]
        == receipt["html_count"]
        == 4
    )
    assert receipt["zstd"] == "ok"
    assert receipt["wechat_voice_text"] == "ok"
    assert receipt["pdf_image"] == "ok"
    assert receipt["image_key_cache"] == ("ok" if os.name == "nt" else "skipped_non_windows")
    assert receipt["emoticon_key_cache"] == ("ok" if os.name == "nt" else "skipped_non_windows")
    assert receipt["runtime_resource_query"] == ("ok" if os.name == "nt" else "skipped_non_windows")
    assert receipt["local_video_lookup"] == "ok"
    assert receipt["export_size_estimates"] == "ok"
    assert receipt["conversation_preview_faces"] == "ok"
    assert receipt["conversation_preview_colors"] == ("ok" if os.name == "nt" else "skipped_non_windows")
    assert receipt["html_archive"] == "ok"
    assert receipt["moments_archive"] == "ok"
    assert receipt["moments_count"] == 1
    assert receipt["jsonl_package"] == "ok"
    assert receipt["jsonl_package_count"] == 4
    assert receipt["auto_discovery"] == "skipped"

    json_payload = json.loads((tmp_path / receipt["json"]).read_text(encoding="utf-8"))
    assert len(json_payload["messages"]) == 4
    assert "不会访问真实微信数据" in json_payload["messages"][1]["text"]
    assert "❤️" in json_payload["messages"][1]["text"]
    assert "[爱心]" not in json_payload["messages"][1]["text"]
    assert "[爱心]" in json_payload["messages"][1]["raw_text"]
    jsonl_records = [
        json.loads(line)
        for line in (tmp_path / receipt["jsonl"]).read_text(encoding="utf-8").splitlines()
    ]
    assert len(jsonl_records) == 4
    assert "❤️" in jsonl_records[1]["text"]
    assert "[爱心]" in jsonl_records[1]["raw_text"]
    assert jsonl_records[2]["transcript"]["source"] == "wechat"
    with zipfile.ZipFile(tmp_path / receipt["jsonl_package_path"]) as package:
        assert "messages.jsonl" in package.namelist()
        assert any(name.startswith("media/images/") and name.endswith(".png") for name in package.namelist())

    txt_path = tmp_path / receipt["txt"]
    assert txt_path.read_bytes().startswith(b"\xef\xbb\xbf")
    txt_text = txt_path.read_text(encoding="utf-8-sig")
    assert "不会访问真实微信数据" in txt_text
    assert "❤️" in txt_text
    assert "[爱心]" not in txt_text
    assert "微信官方语音转写自检" in txt_text
    assert "julibeian" not in txt_text

    pdf_path = tmp_path / receipt["pdf"]
    reader = PdfReader(pdf_path)
    extracted = "\n".join(page.extract_text() or "" for page in reader.pages)
    assert "成品自检会话 [表情]" in extracted
    assert "不会访问真实微信数据" in extracted
    assert "微信官方语音转写自检" in extracted
    assert "julibeian" not in extracted
    assert "julibeian" not in str(reader.metadata)

    html_path = tmp_path / receipt["html"]
    html_manifest_path = tmp_path / receipt["html_manifest"]
    html_checksum_path = tmp_path / receipt["html_checksum"]
    assert html_path.is_file()
    assert html_manifest_path.is_file()
    assert html_checksum_path.is_file()
    html_manifest = json.loads(html_manifest_path.read_text(encoding="utf-8"))
    assert html_manifest["schema"] == "wechat-chat-offline-archive"
    assert html_manifest["summary"]["messages"] == 4
    assert html_manifest["summary"]["media_exported"] == 1
    html_chunks = "\n".join(
        path.read_text(encoding="utf-8")
        for path in sorted((html_path.parent / "data").glob("messages-*.js"))
    )
    assert "❤️" not in html_chunks
    assert "[爱心]" in html_chunks
    assert "[爱心]" in html_manifest["native_faces"]
    assert (html_path.parent / html_manifest["native_faces"]["[爱心]"]).is_file()
    assert "搜索全部记录" in html_path.read_text(encoding="utf-8")
    assert (html_path.parent / "assets" / "viewer.css").is_file()
    assert (html_path.parent / "assets" / "viewer.js").is_file()
    assert any((html_path.parent / "resources" / "images").iterdir())

    moments_html = (tmp_path / receipt["moments"]).read_text(encoding="utf-8")
    moments_payload = json.loads(
        (tmp_path / receipt["moments_json"]).read_text(encoding="utf-8")
    )
    assert "朋友圈离线归档打包自检" in moments_html
    assert "置顶" in moments_html
    assert 'loading="lazy"' in moments_html
    assert moments_payload["summary"]["images"] == 1
    assert (tmp_path / receipt["moments_manifest"]).is_file()


def test_optional_environment_probe_checks_availability_without_guessing_login(tmp_path, monkeypatch):
    from wechat_exporter import selftest
    executable = tmp_path / "Weixin.exe"
    executable.write_bytes(b"synthetic")
    monkeypatch.setattr(selftest, "find_weixin_executable", lambda: executable)
    monkeypatch.setattr(selftest, "discover_accounts", lambda **kwargs: [object()])
    receipt = run_packaged_self_test(tmp_path / "output", check_environment=True)
    assert json.loads(receipt.read_text(encoding="utf-8"))["auto_discovery"] == "ok"


def test_packaged_self_test_can_repeat_without_removing_siblings(tmp_path: Path) -> None:
    output = tmp_path / "output"
    first_receipt = json.loads(run_packaged_self_test(output).read_text(encoding="utf-8"))
    html_root = (output / first_receipt["html"]).parent
    stale_owned_file = html_root / "stale-from-previous-self-test.txt"
    stale_owned_file.write_text("replace me", encoding="utf-8")
    sibling = output / "用户文件.txt"
    sibling.write_text("keep", encoding="utf-8")

    second_receipt = json.loads(run_packaged_self_test(output).read_text(encoding="utf-8"))

    assert second_receipt["status"] == "ok"
    assert (output / second_receipt["html"]).parent == html_root
    assert not stale_owned_file.exists()
    assert sibling.read_text(encoding="utf-8") == "keep"


def test_self_test_refuses_to_replace_unowned_html_directory(tmp_path: Path) -> None:
    from wechat_exporter.selftest import _prepare_self_test_html_root

    output = tmp_path / "output"
    foreign_root = output / "packaged-self-test-chat-html"
    foreign_root.mkdir(parents=True)
    user_file = foreign_root / "用户文件.txt"
    user_file.write_text("keep", encoding="utf-8")

    try:
        _prepare_self_test_html_root(output)
    except RuntimeError as error:
        assert "未执行清理" in str(error)
    else:
        raise AssertionError("unowned directory should not be removed")
    assert user_file.read_text(encoding="utf-8") == "keep"
