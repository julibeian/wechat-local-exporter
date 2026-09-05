from __future__ import annotations

import re
import runpy
import subprocess
import sys
from pathlib import Path

import pytest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "render_release_notes.py"
render_release_notes = runpy.run_path(str(SCRIPT))["render_release_notes"]
INSTALLED_HASH = "a" * 64


def test_cli_selects_exact_tag_and_builds_one_installer_link(tmp_path):
    notes = tmp_path / "notes.md"
    notes.write_bytes((
        "# v12.34.50\r\n\r\nNeighbor release.\r\n\r\n"
        "# v12.34.5\r\n\r\n当前更新。\r\n\r\n## 已知限制\r\n\r\n保留说明。\r\n\r\n"
        "# v1.0.0\r\n\r\nOlder release.\r\n"
    ).encode("utf-8"))
    output = tmp_path / "body.md"
    subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--tag",
            "v12.34.5",
            "--notes",
            str(notes),
            "--output",
            str(output),
            "--installed-exe-sha256",
            INSTALLED_HASH,
        ],
        check=True,
    )
    body = output.read_text(encoding="utf-8")
    assert "当前更新。\n\n## 已知限制\n\n保留说明。" in body
    assert "个人项目" in body
    assert "不上传" in body
    assert "未知发布者" in body
    assert "请只从本项目下载" in body
    assert (
        "[下载最新版本](https://github.com/julibeian/wechat-local-exporter/releases/latest)"
        in body
    )
    assert (
        "[查看全部历史版本](https://github.com/julibeian/wechat-local-exporter/releases)"
        in body
    )
    assert (
        "觉得好用，欢迎点个 "
        "[Star](https://github.com/julibeian/wechat-local-exporter)。"
        in body
    )
    assert "Neighbor release" not in body and "Older release" not in body
    links = re.findall(r"\]\((https://[^)]+/download/[^)]+)\)", body)
    assert [link.rsplit("/", 1)[1] for link in links] == [
        "WeChat-TXT-PDF-Exporter-Installer-v12.34.5.exe"
    ]
    assert "/releases/download/v12.34.5/" in links[0]
    assert "便携版" not in body and "SHA256SUMS" not in body
    assert body.endswith(
        f"<!-- wechat-exporter-target-sha256:{INSTALLED_HASH} -->\n"
    )
    assert not output.read_bytes().startswith(b"\xef\xbb\xbf")


def test_existing_release_links_only_to_the_installer():
    body = render_release_notes(
        "# v1.0.0\n\nOriginal notes.\n",
        "v1.0.0",
        ["WeChat-TXT-PDF-Exporter-Installer-v1.0.0.exe"],
        installed_exe_sha256=INSTALLED_HASH.upper(),
    )
    assert body.count("/releases/download/") == 1
    assert "Original notes." in body
    assert f"target-sha256:{INSTALLED_HASH}" in body


def test_release_workflow_publishes_and_verifies_only_the_installer():
    workflow = (SCRIPT.parents[1] / ".github" / "workflows" / "release.yml").read_text(
        encoding="utf-8"
    )
    assert "gh release upload $tag $installer --clobber" in workflow
    assert "gh release create $tag $installer" in workflow
    assert "Published release must contain exactly the one-click installer" in workflow
    assert "SHA256SUMS" not in workflow


@pytest.mark.parametrize(
    "tag, notes, assets, installed_hash",
    [
        ("v1.3.0/../../bad", "# v1.3.0\nNotes", None, INSTALLED_HASH),
        ("v1.3.0", "# v1.3.1\nOther notes", None, INSTALLED_HASH),
        ("v1.3.0", "# v1.3.0\n\n# v1.2.0\nOlder notes", None, INSTALLED_HASH),
        ("v1.3.0", "# v1.3.0\nFirst\n# v1.3.0\nDuplicate", None, INSTALLED_HASH),
        ("v1.3.0", "# v1.3.0\nNotes", [], INSTALLED_HASH),
        ("v1.3.0", "# v1.3.0\nNotes", None, "not-a-hash"),
    ],
)
def test_invalid_or_ambiguous_input_stops_publication(
    tag, notes, assets, installed_hash
):
    with pytest.raises(ValueError):
        render_release_notes(
            notes,
            tag,
            assets,
            installed_exe_sha256=installed_hash,
        )
