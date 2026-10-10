# -*- mode: python ; coding: utf-8 -*-
from pathlib import Path
import tomllib

from PyInstaller.utils.hooks import collect_all


root = Path(SPEC).resolve().parent.parent
version = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]
frida_data, frida_binaries, frida_hiddenimports = collect_all("frida")
a = Analysis(
    [str(root / "scripts" / "cli_launcher.py")],
    pathex=[str(root / "src")],
    binaries=frida_binaries,
    datas=frida_data + [
        (str(root / "THIRD_PARTY_NOTICES.md"), "."),
        (str(root / "src" / "wechat_exporter" / "html_assets"), "wechat_exporter/html_assets"),
    ],
    hiddenimports=frida_hiddenimports,
    hookspath=[], hooksconfig={}, runtime_hooks=[],
    excludes=["tkinter", "wechat_exporter.gui", "wechat_exporter.tray", "wechat_exporter.update_ui"],
    noarchive=False, optimize=0,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz, a.scripts, a.binaries, a.datas, [],
    name=f"WeChat-Local-Exporter-CLI-v{version}",
    debug=False, bootloader_ignore_signals=False, strip=False,
    upx=True, upx_exclude=[], runtime_tmpdir=None,
    console=True, disable_windowed_traceback=False, argv_emulation=False,
    target_arch=None, codesign_identity=None, entitlements_file=None,
    manifest=str(root / "assets" / "windows.manifest"),
)
