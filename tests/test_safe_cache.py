import json
import sys
import traceback
from types import SimpleNamespace

import pytest

from wechat_exporter import key_capture
from wechat_exporter.config import LocalConfig
from wechat_exporter.crypto import DatabaseKeys, DecryptedWorkspace
from wechat_exporter.errors import user_message
from wechat_exporter.models import AccountLocation
from wechat_exporter.service import ExporterService


def test_codec_cache_reused_and_invalidated_on_upgrade(tmp_path, monkeypatch):
    exe = tmp_path / "Weixin.exe"
    exe.write_bytes(b"exe")
    dll = tmp_path / "Weixin.dll"
    dll.write_bytes(b"dll-build-1")
    selected = [dll]
    monkeypatch.setitem(sys.modules, "frida", SimpleNamespace(get_local_device=lambda: object()))
    monkeypatch.setattr(key_capture, "locate_weixin_dll", lambda path: selected[0])
    scanned = []
    monkeypatch.setattr(key_capture, "find_codec_config_rva", lambda path: scanned.append(path) or 0x1234)
    config = LocalConfig(tmp_path / "s.json")
    first = key_capture.prepare_key_capture(exe, config=config)
    assert key_capture.prepare_key_capture(exe, config=LocalConfig(config.path)) == first
    assert len(scanned) == 1
    dll.write_bytes(b"larger-dll-build-2")
    key_capture.prepare_key_capture(exe, config=config)
    assert len(scanned) == 2
    import os
    timestamp = dll.stat().st_mtime_ns + 1_000_000
    os.utime(dll, ns=(timestamp, timestamp))
    key_capture.prepare_key_capture(exe, config=config)
    assert len(scanned) == 3
    second = tmp_path / "new" / "Weixin.dll"
    second.parent.mkdir()
    second.write_bytes(dll.read_bytes())
    selected[0] = second
    key_capture.prepare_key_capture(exe, config=config)
    assert len(scanned) == 4


def test_corrupt_and_unwritable_config_are_safe(tmp_path):
    path = tmp_path / "s.json"
    path.write_text("not json")
    assert LocalConfig(path).get("codec_rva") is None
    parent_file = tmp_path / "not-a-directory"
    parent_file.write_text("file")
    config = LocalConfig(parent_file / "s.json")
    assert not config.set(codec_rva=1)
    assert not config.reserve_auto_check(100_000, 86400)


def test_allowlist_rejects_secrets_and_bad_types(tmp_path, caplog):
    path = tmp_path / "s.json"
    secret = "UNIQUE_SECRET_MUST_NOT_LEAK"
    config = LocalConfig(path)
    for name in ("key", "master_key", "token", "cookie", "decrypted_database"):
        with pytest.raises(ValueError) as error:
            config.set(**{name: secret})
        assert secret not in str(error.value)
    with pytest.raises(ValueError):
        config.set(codec_rva=secret)
    config.set(last_account_wxid="wxid_test", dismissed_version="1.4.0")
    assert secret not in path.read_text()
    assert secret not in user_message(RuntimeError(secret))
    assert secret not in caplog.text
    path.write_text(json.dumps({"token": secret, "codec_rva": "invalid", "dll_size": -1}))
    loaded = LocalConfig(path)
    loaded.set(dismissed_version="1.5.0")
    assert secret not in path.read_text()


def test_workspace_cleanup_removes_decrypted_database_and_keys(tmp_path):
    keys = DatabaseKeys({"contact/contact.db": b"secret" * 5 + b"xx"})
    workspace = DecryptedWorkspace(tmp_path, keys)
    root = workspace.root
    decrypted = workspace.decrypted_path("contact/contact.db")
    decrypted.parent.mkdir(parents=True)
    decrypted.write_bytes(b"private chat database")
    workspace.close()
    assert not root.exists()
    assert len(workspace.keys) == 0
    assert "secret" not in repr(keys)


def test_prepare_failure_also_cleans_workspace(tmp_path, monkeypatch):
    roots = []
    def fail(self, **kwargs):
        roots.append(self.root)
        self.decrypted_dir.mkdir()
        (self.decrypted_dir / "private.db").write_bytes(b"private")
        raise RuntimeError("synthetic failure")
    monkeypatch.setattr(DecryptedWorkspace, "prepare", fail)
    service = ExporterService(AccountLocation(tmp_path, "a", "test"))
    with pytest.raises(RuntimeError):
        service._prepare(DatabaseKeys({}), progress=None, calibrations=None)
    assert service.workspace is None and service.archive is None
    assert not roots[0].exists()
