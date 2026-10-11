from __future__ import annotations

import hashlib
from pathlib import Path
import runpy
import zipfile


ROOT = Path(__file__).resolve().parents[1]
package_cli = runpy.run_path(str(ROOT / "scripts/package_cli.py"))["package_cli"]
render_release_notes = runpy.run_path(str(ROOT / "scripts/render_release_notes.py"))["render_release_notes"]


def test_download_contains_stable_exe_job_docs_and_matching_checksums(tmp_path):
    executable = tmp_path / "console.exe"
    executable.write_bytes(b"synthetic console payload")
    package = package_cli(executable, "1.6.1", tmp_path / "assets")
    assert package.name == "命令行_wechatexporterCLI_v1.6.1.zip"
    with zipfile.ZipFile(package) as archive:
        assert {"wechatexporter.exe", "README.md", "example-job.json", "LICENSE", "THIRD_PARTY_NOTICES.md", "SHA256SUMS.txt"} == set(archive.namelist())
        for line in archive.read("SHA256SUMS.txt").decode().splitlines():
            digest, name = line.split("  ", 1)
            assert hashlib.sha256(archive.read(name)).hexdigest() == digest
    assert not package.with_suffix(".zip.sha256").exists()


def test_cli_download_link_is_only_rendered_for_a_real_asset():
    installer = "WeChat-TXT-PDF-Exporter-Installer-v1.6.1.exe"
    cli = "命令行_wechatexporterCLI_v1.6.1.zip"
    body = render_release_notes("# v1.6.1\n\nNew CLI download.\n", "v1.6.1",
        [installer, cli], installed_exe_sha256="a" * 64)
    assert body.count("/releases/download/") == 2
    assert f"/releases/download/v1.6.1/{cli}" in body
    assert f"/releases/download/v1.6.1/{installer}" in body
    without_cli = render_release_notes("# v1.6.1\n\nNew CLI download.\n", "v1.6.1",
        [installer], installed_exe_sha256="a" * 64)
    assert cli not in without_cli


def test_gui_updater_does_not_treat_cli_download_as_a_gui_update():
    from wechat_exporter.update import Asset, Release
    cli = Asset("命令行_wechatexporterCLI_v1.6.1.zip", "https://example.invalid/cli", 1)
    release = Release("1.6.1", "", "", (cli,))
    assert release.asset("installer") is None
    assert release.asset("portable") is None
    assert release.asset("update") is None
