from __future__ import annotations

import json
from pathlib import Path

from pypdf import PdfReader

from wechat_exporter.selftest import run_packaged_self_test


def test_packaged_self_test_payload(tmp_path: Path) -> None:
    receipt_path = run_packaged_self_test(tmp_path)
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    assert receipt["status"] == "ok"
    assert receipt["txt_count"] == receipt["pdf_count"] == 4
    assert receipt["zstd"] == "ok"
    assert receipt["wechat_voice_text"] == "ok"
    assert receipt["pdf_image"] == "ok"
    assert receipt["moments_archive"] == "ok"
    assert receipt["moments_count"] == 1
    assert receipt["auto_discovery"] == "skipped"

    txt_path = tmp_path / receipt["txt"]
    assert txt_path.read_bytes().startswith(b"\xef\xbb\xbf")
    txt_text = txt_path.read_text(encoding="utf-8-sig")
    assert "不会访问真实微信数据" in txt_text
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
