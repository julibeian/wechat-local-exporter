from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from wechat_exporter import key_capture


def test_spawned_wechat_is_resumed_when_attach_fails(tmp_path, monkeypatch) -> None:
    class FakeDevice:
        def __init__(self) -> None:
            self.resumed: list[int] = []

        def spawn(self, _argv) -> int:
            return 1234

        def attach(self, _pid: int):
            raise RuntimeError("synthetic attach failure")

        def resume(self, pid: int) -> None:
            self.resumed.append(pid)

    device = FakeDevice()
    dll = tmp_path / "Weixin.dll"
    dll.write_bytes(b"test")
    monkeypatch.setitem(
        sys.modules,
        "frida",
        SimpleNamespace(get_local_device=lambda: device),
    )
    monkeypatch.setattr(key_capture, "locate_weixin_dll", lambda _path: dll)
    monkeypatch.setattr(key_capture, "find_codec_config_rva", lambda _path: 0x1234)

    with pytest.raises(RuntimeError, match="synthetic attach failure"):
        key_capture.capture_keys_during_wechat_start(Path("Weixin.exe"), [])

    assert device.resumed == [1234]
