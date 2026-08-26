# -*- mode: python ; coding: utf-8 -*-
from pathlib import Path
import tomllib

from PyInstaller.utils.hooks import collect_all


root = Path(SPEC).resolve().parent.parent
project = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
version = project["project"]["version"]

datas = [(str(root / "THIRD_PARTY_NOTICES.md"), ".")]
binaries = []
hiddenimports = []
frida_data, frida_binaries, frida_hiddenimports = collect_all("frida")
datas += frida_data
binaries += frida_binaries
hiddenimports += frida_hiddenimports

a = Analysis(
    [str(root / "scripts" / "launcher.py")],
    pathex=[str(root / "src")],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name=f"WeChat-TXT-PDF-Exporter-v{version}",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    manifest=str(root / "assets" / "windows.manifest"),
)
