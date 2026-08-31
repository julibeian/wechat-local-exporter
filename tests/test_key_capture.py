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

    with pytest.raises(RuntimeError, match="微信读取组件未能完成连接"):
        key_capture.capture_keys_during_wechat_start(Path("Weixin.exe"), [])

    assert device.resumed == [1234]


def test_candidates_wait_for_actual_login_before_validation(tmp_path, monkeypatch):
    from wechat_exporter.crypto import DatabaseKeys, DatabaseTarget

    secret = b"s" * 32
    selected = []
    resolutions = []
    targets = [DatabaseTarget(name, tmp_path / "B" / name, 4096, b"s" * 16, b"0" * 4096)
               for name in ("contact\\contact.db", "session\\session.db")]

    class Script:
        def on(self, kind, callback):
            self.callback = callback
        def load(self):
            self.callback({"payload": {"type": "candidate"}}, secret)
        def unload(self):
            pass

    class Session:
        def create_script(self, text):
            return Script()
        def detach(self):
            pass

    device = SimpleNamespace(spawn=lambda args: 44, attach=lambda pid: Session(), resume=lambda pid: None)
    monkeypatch.setitem(sys.modules, "frida", SimpleNamespace(get_local_device=lambda: device))
    monkeypatch.setattr(key_capture, "bring_wechat_to_front", lambda *a, **kw: None)
    dll = tmp_path / "Weixin.dll"
    dll.write_bytes(b"dll")
    monkeypatch.setattr(key_capture, "locate_weixin_dll", lambda path: dll)
    preparation = key_capture.KeyCapturePreparation(dll, dll.stat().st_size, dll.stat().st_mtime_ns, 0x1000)

    def resolve(pid):
        assert pid == 44
        resolutions.append(pid)
        return [] if len(resolutions) == 1 else targets

    def validate(candidate, current_targets):
        assert candidate == secret
        selected.extend(current_targets)
        return DatabaseKeys({target.relative_path: b"k" * 32 for target in current_targets})

    monkeypatch.setattr(key_capture, "keys_from_master_password", validate)
    result = key_capture.capture_keys_during_wechat_start(Path("Weixin.exe"), [],
        preparation=preparation, target_resolver=resolve, timeout_seconds=5)
    assert len(resolutions) >= 2
    assert selected == targets
    assert len(result) == 2


def test_hook_errors_never_expose_stack_or_candidate(tmp_path, monkeypatch, caplog):
    import traceback
    secret = "UNIQUE_DATABASE_KEY_MUST_NOT_APPEAR"

    class Script:
        def on(self, kind, callback):
            self.callback = callback
        def load(self):
            self.callback({"type": "error", "stack": secret}, None)
        def unload(self):
            pass

    session = SimpleNamespace(create_script=lambda text: Script(), detach=lambda: None)
    device = SimpleNamespace(spawn=lambda args: 4, attach=lambda pid: session, resume=lambda pid: None)
    monkeypatch.setitem(sys.modules, "frida", SimpleNamespace(get_local_device=lambda: device))
    monkeypatch.setattr(key_capture, "bring_wechat_to_front", lambda *a, **kw: None)
    monkeypatch.setattr(key_capture, "locate_weixin_dll", lambda path: tmp_path / "Weixin.dll")
    dll = tmp_path / "Weixin.dll"
    dll.write_bytes(b"dll")
    preparation = key_capture.KeyCapturePreparation(dll, 3, dll.stat().st_mtime_ns, 0x1000)
    with pytest.raises(RuntimeError) as error:
        key_capture.capture_keys_during_wechat_start(Path("Weixin.exe"), [], preparation=preparation)
    assert secret not in "".join(traceback.format_exception(error.value))
    assert secret not in caplog.text
