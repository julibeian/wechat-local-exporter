from __future__ import annotations

import os
from pathlib import Path

import pytest

import wechat_exporter.gui as gui
from wechat_exporter.instance_control import UPDATE_EXIT_EVENT_NAME


def test_local_build_installs_by_default_and_package_only_is_explicit() -> None:
    script = (Path(__file__).parents[1] / "scripts" / "build.ps1").read_text(
        encoding="utf-8"
    )

    assert "[switch]$PackageOnly" in script
    assert "[switch]$ForceStopInstalled" in script
    assert "$installAfterBuild = $Install -or (-not $PackageOnly -and -not $isCi)" in script
    assert "if (-not $installAfterBuild)" in script
    assert "SHA256SUMS" not in script
    assert "Remove-Item -LiteralPath $portable" in script
    assert "self-test-result.json" in script
    assert "CloseMainWindow" not in script
    assert UPDATE_EXIT_EVENT_NAME in script
    assert "/NOCLOSEAPPLICATIONS" in script
    assert "the desktop installation and shortcut were not updated" in script
    assert script.index("Installed executable does not match") < script.index(
        'update_desktop_shortcut.ps1'
    )

    installer = (Path(__file__).parents[1] / "packaging" / "installer.iss").read_text(
        encoding="utf-8"
    )
    assert "CloseApplications=no" in installer
    assert '#define AppName "微信聊天本地导出工具"' in installer
    assert '#define DesktopShortcutName "微信聊天本地导出工具"' in installer
    assert 'Name: "{autodesktop}\\{#DesktopShortcutName}"' in installer
    assert 'Name: "{autodesktop}\\微信聊天 TXT-PDF 导出.lnk"' in installer
    assert '#define InstalledExeSHA256 GetSHA256OfFile' in installer
    assert "GetSHA256OfFile(ExpandConstant('{app}\\{#AppExeName}'))" in installer
    assert "Installed executable failed SHA-256 verification" in installer


@pytest.mark.skipif(os.name != "nt", reason="Windows desktop shortcut behavior")
def test_portable_build_cannot_take_over_installed_shortcut(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    executable = tmp_path / "WeChat-TXT-PDF-Exporter-v1.4.0.exe"
    executable.write_bytes(b"portable")
    monkeypatch.setattr(gui.sys, "frozen", True, raising=False)
    monkeypatch.setattr(gui.sys, "executable", str(executable))
    monkeypatch.setattr(gui, "installation_kind", lambda _path: "portable")

    def unexpected_run(*_args, **_kwargs):
        raise AssertionError("portable build attempted to rewrite the desktop shortcut")

    monkeypatch.setattr(gui.subprocess, "run", unexpected_run)
    gui._sync_desktop_shortcut()


@pytest.mark.skipif(os.name != "nt", reason="Windows desktop shortcut behavior")
def test_installed_build_checks_shortcut_update_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    executable = tmp_path / "WeChat-TXT-PDF-Exporter.exe"
    executable.write_bytes(b"installed")
    captured: dict[str, object] = {}
    monkeypatch.setattr(gui.sys, "frozen", True, raising=False)
    monkeypatch.setattr(gui.sys, "executable", str(executable))
    monkeypatch.setattr(gui, "installation_kind", lambda _path: "installer")

    def capture_run(command, **kwargs):
        captured["command"] = command
        captured.update(kwargs)

    monkeypatch.setattr(gui.subprocess, "run", capture_run)
    gui._sync_desktop_shortcut()

    assert captured["check"] is True
    environment = captured["env"]
    assert isinstance(environment, dict)
    assert environment["WECHAT_EXPORTER_SHORTCUT_TARGET"] == str(
        executable.resolve()
    )
