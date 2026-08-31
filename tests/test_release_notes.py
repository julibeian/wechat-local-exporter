from __future__ import annotations

import re
import runpy
import subprocess
import sys
from pathlib import Path

import pytest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "render_release_notes.py"
render_release_notes = runpy.run_path(str(SCRIPT))["render_release_notes"]


def test_cli_selects_exact_tag_and_builds_future_asset_links(tmp_path):
    notes = tmp_path / "notes.md"
    notes.write_bytes((
        "# v12.34.50\r\n\r\nNeighbor release.\r\n\r\n"
        "# v12.34.5\r\n\r\n当前更新。\r\n\r\n## 已知限制\r\n\r\n保留说明。\r\n\r\n"
        "# v1.0.0\r\n\r\nOlder release.\r\n"
    ).encode("utf-8"))
    output = tmp_path / "body.md"
    subprocess.run(
        [sys.executable, str(SCRIPT), "--tag", "v12.34.5",
         "--notes", str(notes), "--output", str(output)],
        check=True,
    )
    body = output.read_text(encoding="utf-8")
    assert body.endswith("当前更新。\n\n## 已知限制\n\n保留说明。\n")
    assert "Neighbor release" not in body and "Older release" not in body
    links = re.findall(r"\]\((https://[^)]+/download/[^)]+)\)", body)
    assert {link.rsplit("/", 1)[1] for link in links} == {
        "WeChat-TXT-PDF-Exporter-Installer-v12.34.5.exe",
        "WeChat-TXT-PDF-Exporter-v12.34.5.exe",
        "SHA256SUMS-v12.34.5.txt",
    }
    assert all("/releases/download/v12.34.5/" in link for link in links)
    assert not output.read_bytes().startswith(b"\xef\xbb\xbf")


def test_existing_release_does_not_link_to_unpublished_assets():
    body = render_release_notes(
        "# v1.0.0\n\nOriginal notes.\n",
        "v1.0.0",
        ["WeChat-TXT-PDF-Exporter-Installer-v1.0.0.exe"],
    )
    assert body.count("/releases/download/") == 1
    assert body.count("| 本版本未提供 |") == 2
    assert body.endswith("Original notes.\n")


@pytest.mark.parametrize("tag, notes, assets", [
    ("v1.3.0/../../bad", "# v1.3.0\nNotes", None),
    ("v1.3.0", "# v1.3.1\nOther notes", None),
    ("v1.3.0", "# v1.3.0\n\n# v1.2.0\nOlder notes", None),
    ("v1.3.0", "# v1.3.0\nFirst\n# v1.3.0\nDuplicate", None),
    ("v1.3.0", "# v1.3.0\nNotes", []),
])
def test_invalid_or_ambiguous_input_stops_publication(tag, notes, assets):
    with pytest.raises(ValueError):
        render_release_notes(notes, tag, assets)
