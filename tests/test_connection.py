from pathlib import Path
from types import SimpleNamespace

import pytest

from wechat_exporter import connection, windows
from wechat_exporter.config import LocalConfig
from wechat_exporter.crypto import DatabaseKeys
from wechat_exporter.errors import UserFacingError
from wechat_exporter.models import AccountLocation
from wechat_exporter.service import RestartRequired


def account(tmp_path, name):
    location = AccountLocation(tmp_path / name, name, "微信进程")
    for folder in ("contact", "session"):
        path = location.db_dir / folder / f"{folder}.db"
        path.parent.mkdir(parents=True)
        path.write_bytes(b"0" * 4096)
    return location


def test_process_account_beats_newer_local_directory(tmp_path, monkeypatch):
    a, b = account(tmp_path, "wxid_old"), account(tmp_path, "wxid_newer")
    processes = [SimpleNamespace(pid=10)]
    monkeypatch.setattr(connection, "list_wechat_processes", lambda: processes)
    monkeypatch.setattr(connection, "discover_db_paths_from_process", lambda pid: [a.db_dir])
    assert connection.resolve_running_account().account == a
    monkeypatch.setattr(connection, "discover_db_paths_from_process", lambda pid: [b.db_dir])
    assert connection.resolve_running_account().account == b


def test_ambiguous_process_paths_do_not_guess(tmp_path, monkeypatch):
    a, b = account(tmp_path, "a"), account(tmp_path, "b")
    monkeypatch.setattr(connection, "list_wechat_processes", lambda: [SimpleNamespace(pid=1)])
    monkeypatch.setattr(connection, "discover_db_paths_from_process", lambda pid: [a.db_dir, b.db_dir])
    assert connection.resolve_running_account() is None


def test_discovery_scans_process_even_when_normal_directory_exists(tmp_path, monkeypatch):
    local = account(tmp_path / "Documents" / "xwechat_files", "local")
    active = account(tmp_path / "elsewhere", "active")
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(windows, "find_weixin_executable", lambda: tmp_path / "Weixin.exe")
    monkeypatch.setattr(windows, "list_wechat_processes", lambda: [SimpleNamespace(pid=3)])
    monkeypatch.setattr(windows, "discover_db_paths_from_process", lambda pid: [active.db_dir])
    values = windows.discover_accounts()
    assert {v.wxid for v in values} == {local.wxid, active.wxid}
    assert windows.select_current_account(values) == active


class FakeService:
    instances = []
    fail = False

    def __init__(self, account, *, process_id=None):
        self.account, self.process_id = account, process_id
        self.closed = False
        self.direct = False
        self.prepared = False
        self.instances.append(self)

    def connect_without_restart(self, **kwargs):
        self.direct = True
        if self.fail:
            raise RestartRequired()

    def _prepare(self, keys, **kwargs):
        self.prepared = True

    def close(self):
        self.closed = True


@pytest.fixture
def manager(tmp_path, monkeypatch):
    FakeService.instances = []
    FakeService.fail = False
    monkeypatch.setattr(connection, "ExporterService", FakeService)
    monkeypatch.setattr(connection, "list_wechat_processes", lambda: [SimpleNamespace(pid=7)])
    return connection.ConnectionManager(LocalConfig(tmp_path / "settings.json"))


def test_direct_connection_never_restarts(manager, tmp_path, monkeypatch):
    current = connection.RunningAccount(7, account(tmp_path, "running"))
    monkeypatch.setattr(connection, "resolve_running_account", lambda pid=None: current)
    monkeypatch.setattr(connection, "request_wechat_exit", lambda **kw: pytest.fail("must not restart"))
    service = manager.connect()
    assert service.direct and not service.closed
    assert service.process_id == 7
    assert manager.config.get("last_account_wxid") == "running"


def test_restart_requires_consent_and_rebinds_a_to_b(manager, tmp_path, monkeypatch):
    a, b = account(tmp_path, "A"), account(tmp_path, "B")
    current = [connection.RunningAccount(7, a)]
    monkeypatch.setattr(connection, "resolve_running_account", lambda pid=None: current[0])
    FakeService.fail = True
    exited = []
    monkeypatch.setattr(connection, "request_wechat_exit", lambda **kw: exited.append(True))
    with pytest.raises(RestartRequired):
        manager.connect()
    assert not exited
    assert FakeService.instances[0].closed
    monkeypatch.setattr(manager, "executable", lambda: tmp_path / "Weixin.exe")
    monkeypatch.setattr(connection, "prepare_key_capture", lambda *a, **kw: object())

    def capture(executable, targets, **kwargs):
        assert not targets  # No pre-login A databases are passed to capture.
        current[0] = connection.RunningAccount(8, b)
        resolved = kwargs["target_resolver"](8)
        assert all(t.path.is_relative_to(b.db_dir) for t in resolved)
        assert len(FakeService.instances) == 1  # Service for B not created early.
        return DatabaseKeys({})

    monkeypatch.setattr(connection, "capture_keys_during_wechat_start", capture)
    service = manager.connect(allow_restart=True)
    assert exited == [True]
    assert service.account == b and service.process_id == 8 and service.prepared
    assert manager.config.get("last_account_wxid") == "B"


def test_identity_change_during_read_discards_workspace(manager, tmp_path, monkeypatch):
    a, b = account(tmp_path, "A"), account(tmp_path, "B")
    states = iter([connection.RunningAccount(7, a), connection.RunningAccount(7, b)])
    monkeypatch.setattr(connection, "resolve_running_account", lambda pid=None: next(states))
    with pytest.raises(UserFacingError, match="变化"):
        manager.connect()
    assert FakeService.instances[0].closed
    assert manager.config.get("last_account_wxid") is None


def test_unconfirmed_account_never_uses_cached_account(manager, tmp_path, monkeypatch):
    manager.config.set(last_account_wxid="A", last_db_path=str(tmp_path))
    monkeypatch.setattr(connection, "resolve_running_account", lambda pid=None: None)
    with pytest.raises(UserFacingError, match="尚未确认"):
        manager.connect()
    assert not FakeService.instances


def test_no_running_wechat_requests_start_without_launching(manager, monkeypatch):
    monkeypatch.setattr(connection, "list_wechat_processes", lambda: [])
    with pytest.raises(RestartRequired):
        manager.connect()
    assert not FakeService.instances
