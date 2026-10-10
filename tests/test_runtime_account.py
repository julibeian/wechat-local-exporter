from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from wechat_exporter import connection, runtime_account, windows
from wechat_exporter.config import LocalConfig
from wechat_exporter.models import AccountLocation


@pytest.fixture
def live_account(tmp_path, monkeypatch):
    account = AccountLocation(tmp_path / "wxid_sample_abcd", "wxid_sample_abcd", "微信进程")
    for folder in ("contact", "session"):
        path = account.db_dir / folder / f"{folder}.db"
        path.parent.mkdir(parents=True)
        path.write_bytes(b"synthetic")
    processes = [SimpleNamespace(pid=10), SimpleNamespace(pid=11)]
    monkeypatch.setattr(runtime_account, "file_resource_users", lambda paths: {(10, 12345)})
    monkeypatch.setattr(runtime_account, "process_start_time", lambda pid: 12345)
    monkeypatch.setattr(runtime_account, "_find_weixin_module", lambda pid, **kw: (1, 100) if pid == 10 else None)
    monkeypatch.setattr(runtime_account, "_read_cfg_candidates", lambda pid, **kw: [(123, "wxid_sample")])
    return account, processes


def test_saved_path_requires_both_live_file_ownership_and_matching_login(live_account):
    account, processes = live_account
    assert runtime_account.confirm_runtime_account(account, processes) == 10


@pytest.mark.parametrize("failure", ["not_open", "other_process", "reused_pid", "other_login", "unknown_login", "ambiguous_login", "multiple_clients", "unreadable_client", "missing_db", "no_process"])
def test_unverifiable_hint_falls_back_instead_of_guessing(live_account, monkeypatch, failure):
    account, processes = live_account
    if failure == "not_open":
        monkeypatch.setattr(runtime_account, "file_resource_users", lambda paths: set())
    elif failure == "other_process":
        monkeypatch.setattr(runtime_account, "file_resource_users", lambda paths: {(99, 12345)})
    elif failure == "reused_pid":
        monkeypatch.setattr(runtime_account, "process_start_time", lambda pid: 999)
    elif failure in {"other_login", "unknown_login", "ambiguous_login"}:
        identities = {"other_login": [(123, "wxid_other")], "unknown_login": [],
                      "ambiguous_login": [(123, "wxid_sample"), (456, "wxid_other")]}[failure]
        monkeypatch.setattr(runtime_account, "_read_cfg_candidates", lambda *_a, **_k: identities)
    elif failure == "multiple_clients":
        monkeypatch.setattr(runtime_account, "_find_weixin_module", lambda *_a, **_k: (1, 100))
    elif failure == "unreadable_client":
        def find(pid, **kwargs):
            if pid == 11:
                raise PermissionError("cannot establish whether this is another client")
            return (1, 100)
        monkeypatch.setattr(runtime_account, "_find_weixin_module", find)
    elif failure == "missing_db":
        (account.db_dir / "session/session.db").unlink()
    else:
        processes = []
    assert runtime_account.confirm_runtime_account(account, processes) is None


def test_live_cache_reopen_and_final_identity_check_do_not_scan_all_memory(live_account, tmp_path, monkeypatch):
    account, processes = live_account
    config = LocalConfig(tmp_path / "settings.json")
    config.set(last_account_wxid=account.wxid, last_db_path=str(account.db_dir))
    monkeypatch.setattr(connection, "list_wechat_processes", lambda: processes)
    monkeypatch.setattr(connection, "resolve_running_account", lambda *_a, **_k: pytest.fail("full memory scan on validated reopen"))
    calls = []
    class Service:
        def __init__(self, location, **kwargs):
            self.account = location
            self.closed = False
        def connect_without_restart(self, **kwargs):
            calls.append("database-cache-validation-and-refresh")
        def close(self):
            self.closed = True
    monkeypatch.setattr(connection, "ExporterService", Service)
    service = connection.ConnectionManager(config).connect()
    assert service.account.account_dir == account.account_dir
    assert calls == ["database-cache-validation-and-refresh"] and not service.closed


def test_account_switch_after_refresh_discards_the_workspace(live_account, tmp_path, monkeypatch):
    account, processes = live_account
    config = LocalConfig(tmp_path / "settings.json")
    config.set(last_account_wxid=account.wxid, last_db_path=str(account.db_dir))
    monkeypatch.setattr(connection, "list_wechat_processes", lambda: processes)
    changed = AccountLocation(tmp_path / "wxid_other", "wxid_other", "微信进程")
    monkeypatch.setattr(connection, "resolve_running_account", lambda pid=None: connection.RunningAccount(10, changed))
    instances = []
    class Service:
        def __init__(self, location, **kwargs):
            self.account, self.closed = location, False
            instances.append(self)
        def connect_without_restart(self, **kwargs):
            monkeypatch.setattr(runtime_account, "_read_cfg_candidates", lambda *_a, **_k: [(456, "wxid_other")])
        def close(self):
            self.closed = True
    monkeypatch.setattr(connection, "ExporterService", Service)
    with pytest.raises(connection.UserFacingError, match="账号发生变化"):
        connection.ConnectionManager(config).connect()
    assert instances[0].closed
    assert config.get("last_account_wxid") == account.wxid


@pytest.mark.skipif(os.name != "nt", reason="Windows resource ownership")
def test_native_query_identifies_the_actual_file_user_and_creation_time(tmp_path):
    path = tmp_path / "owned-synthetic-resource.db"
    path.write_bytes(b"synthetic")
    with path.open("rb"):
        assert (os.getpid(), windows.process_start_time(os.getpid())) in windows.file_resource_users((path,))


@pytest.mark.parametrize("encoding,prefix", [("utf-8", b""), ("utf-16le", b""), ("utf-16le", b"x")])
def test_memory_discovery_still_finds_every_account_in_bounded_windows(encoding, prefix):
    paths = [r"C:\Users\甲\xwechat_files\wxid_a\db_storage", r"D:\聊天\wxid_b\DB_STORAGE"]
    chunk = prefix + ("\0" + paths[0] + "\\session\0" + paths[1] + "\\message\0").encode(encoding)
    assert windows._paths_from_memory_chunk(chunk) == {Path(path) for path in paths}


def test_path_lookup_avoids_slow_regex_scanning_of_unrelated_memory():
    import time
    path = r"C:\xwechat_files\wxid_a\db_storage"
    chunk = (b"C:\\unrelated\\data\\" * 1_000_000) + path.encode() + b"\0"
    started = time.perf_counter()
    assert windows._paths_from_memory_chunk(chunk) == {Path(path)}
    assert time.perf_counter() - started < 1.0
