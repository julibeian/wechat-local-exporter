from __future__ import annotations

from datetime import datetime
import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
import uuid

import pytest

from wechat_exporter import agent_cli as cli
from wechat_exporter.instance_control import claim_headless_instance, claim_primary_instance
from wechat_exporter.models import AccountLocation, Conversation, ExportResult, ExportWorkload, Message
from wechat_exporter.service import ExportCancelled, ExporterService, RestartRequired


@pytest.fixture
def connection(tmp_path, monkeypatch):
    conversations = [Conversation("wxid_a", "项目 同名"), Conversation("wxid_b", "项目 同名"),
        Conversation("1@chatroom", "示例群聊", is_group=True)]
    connected, leases, calls = [], [], []
    failures = set()

    class Archive:
        self_wxid = "wxid_self"

        def conversations(self):
            return list(conversations)

        def self_conversation(self):
            return Conversation(self.self_wxid, "我自己", is_self=True)

        def export_workload(self, *args, **kwargs):
            return ExportWorkload(message_count=2)

        def iter_messages(self, conversation, *, start_timestamp=0, end_timestamp=0):
            for number in (1, 2):
                timestamp = int(datetime(2026, 10, 10, 12, number).timestamp())
                if (not start_timestamp or timestamp >= start_timestamp) and (not end_timestamp or timestamp <= end_timestamp):
                    yield Message(number, timestamp, 1, conversation.username, conversation.display_name,
                        False, f"合成中文消息 {number} ❤️", conversation_id=conversation.username)

    def connect(_manager, **options):
        service = ExporterService(AccountLocation(tmp_path / "synthetic-account", "wxid_self", "test"))
        service.archive = Archive()
        service.workspace = SimpleNamespace(close=lambda: None)
        service.using_saved_account_cache = True
        original = service.export

        def export(request, **kwargs):
            calls.append(("export", request))
            if request.conversations[0].username in failures:
                raise ValueError("合成导出失败")
            return original(request, **kwargs)

        def other(task):
            def run(request, **kwargs):
                calls.append((task, request))
                return ExportResult(message_counts={item.username: 2 for item in request.conversations})
            return run

        service.export = export
        service.export_jsonl_package = other("package")
        service.export_chat_files = other("files")
        service.export_moments_archive = lambda conversation, output, **kw: calls.append(("moments", conversation)) or ExportResult()
        connected.append((service, options))
        return service

    def claim():
        lease = SimpleNamespace(closed=False)
        lease.close = lambda: setattr(lease, "closed", True)
        leases.append(lease)
        return lease

    monkeypatch.setattr(cli.ConnectionManager, "connect", connect)
    monkeypatch.setattr(cli, "claim_headless_instance", claim)
    return SimpleNamespace(connected=connected, leases=leases, calls=calls, failures=failures)


def run(argv, capsys):
    code = cli.main(argv)
    stdout, stderr = capsys.readouterr()
    return code, json.loads(stdout), stderr


def test_batch_uses_one_connection_and_writes_real_json_for_exact_ids(connection, tmp_path, capsys):
    job = tmp_path / "任务 文件.json"
    job.write_text(json.dumps({"version": 1, "account": "wxid_self", "jobs": [
        {"task": "export", "conversations": ["wxid_a", "wxid_b"], "format": "json", "output": "相对 输出"},
        {"task": "package", "all": True, "type": "group", "include_videos": True, "output": "资料包"},
    ]}, ensure_ascii=False), encoding="utf-8-sig")
    code, result, _ = run(["batch", "--job", str(job), "--quiet"], capsys)
    assert code == 0 and result["success"]
    assert len(connection.connected) == 1
    assert connection.connected[0][1]["allow_restart"] is False
    assert connection.leases[0].closed
    assert connection.connected[0][0].archive is None
    assert result["summary"] == {"requested": 3, "completed": 3, "failed": 0, "skipped": 0}
    for item in result["results"][:2]:
        path = Path(item["files"][0])
        assert path.is_relative_to(tmp_path / "相对 输出")
        data = json.loads(path.read_text(encoding="utf-8"))
        assert len(data["messages"]) == 2
        assert "合成中文消息" in data["messages"][0]["text"]
        assert item["message_count"] == 2
        assert item["counts"] == {"messages": 2}
    assert connection.calls[-1][0] == "package"
    assert connection.calls[-1][1].include_videos


@pytest.mark.parametrize("fail_fast,code,summary", [
    (False, 6, {"requested": 3, "completed": 2, "failed": 1, "skipped": 0}),
    (True, 1, {"requested": 3, "completed": 0, "failed": 1, "skipped": 2}),
])
def test_failed_conversation_continues_or_stops_with_truthful_receipt(connection, tmp_path, capsys, fail_fast, code, summary):
    connection.failures.add("wxid_a")
    argv = ["export", "--all", "--output", str(tmp_path / "exports"), "--quiet"]
    if fail_fast:
        argv.append("--fail-fast")
    actual, result, _ = run(argv, capsys)
    assert actual == code and not result["success"]
    assert result["summary"] == summary
    assert result["results"][0]["error"]["code"] == "EXPORT_REJECTED"
    assert all(Path(name).is_file() for item in result["results"][1:] for name in item["files"])
    assert connection.leases[0].closed


def test_batch_checks_all_selectors_before_any_output(connection, tmp_path, capsys):
    job = tmp_path / "job.json"
    job.write_text(json.dumps({"version": 1, "jobs": [
        {"task": "export", "all": True, "output": "out"},
        {"task": "export", "conversations": ["unknown"], "output": "out"},
    ]}), encoding="utf-8")
    code, result, _ = run(["batch", "--job", str(job), "--quiet"], capsys)
    assert code == 2 and result["error"]["code"] == "CONVERSATION_NOT_FOUND"
    assert not connection.calls and not (tmp_path / "out").exists()
    assert connection.leases[0].closed


@pytest.mark.parametrize("payload", [
    {"version": True, "jobs": []},
    {"version": 1, "jobs": [{"task": "export", "all": True, "output": "out", "allow_restart": True}]},
    {"version": 1, "jobs": [{"task": "files", "all": True, "output": "out", "max_file_mb": True}]},
    {"version": 1, "jobs": [{"task": "export", "all": "true", "output": "out"}]},
    {"version": 1, "jobs": [{"task": "export", "all": True, "output": "out", "since": "bad"}]},
    {"version": 1, "jobs": [{"task": "moments", "all": True, "output": "out", "format": "json"}]},
    {"version": 1, "jobs": [{"task": "files", "all": True, "output": "out", "include_videos": False}]},
])
def test_invalid_job_rejected_before_connect(connection, tmp_path, capsys, payload):
    job = tmp_path / "job.json"
    job.write_text(json.dumps(payload), encoding="utf-8")
    code, result, _ = run(["batch", "--job", str(job)], capsys)
    assert code == 2 and not result["success"]
    assert not connection.connected and not connection.calls


@pytest.mark.parametrize("argv", [
    ["export", "--all", "--format", "jsonl", "--output", "unused"],
    ["export", "--all", "--pdf-images", "--output", "unused"],
    ["export", "--query", " ", "--output", "unused"],
    ["export", "--all", "--include-videos", "--output", "unused"],
    ["export", "--all", "--since", "2026-10-11", "--until", "2026-10-10", "--output", "unused"],
    ["export", "--all", "--since", "2026-02-30", "--output", "unused"],
    ["files", "--all", "--max-file-mb", "0", "--output", "unused"],
])
def test_invalid_cli_options_do_not_connect(connection, capsys, argv):
    code, result, _ = run(argv, capsys)
    assert code == 2 and not result["success"]
    assert not connection.connected


def test_dry_run_resolves_group_selection_without_writing(connection, tmp_path, capsys):
    output = tmp_path / "dry"
    code, result, _ = run(["export", "--all", "--type", "group", "--output", str(output), "--dry-run", "--quiet"], capsys)
    assert code == 0 and result["status"] == "planned"
    assert [v["id"] for v in result["jobs"][0]["conversations"]] == ["1@chatroom"]
    assert not output.exists() and not connection.calls


def test_account_mismatch_never_exports(connection, tmp_path, capsys):
    code, result, _ = run(["export", "--all", "--account", "wxid_wrong", "--output", str(tmp_path / "out"), "--quiet"], capsys)
    assert code == 4 and result["error"]["code"] == "ACCOUNT_MISMATCH"
    assert not connection.calls and connection.leases[0].closed


def test_busy_does_not_connect_or_activate_gui(connection, monkeypatch, capsys):
    monkeypatch.setattr(cli, "claim_headless_instance", lambda: None)
    code, result, _ = run(["connect", "--quiet"], capsys)
    assert code == 5 and result["error"]["code"] == "BUSY"
    assert not connection.connected


def test_missing_cache_returns_action_and_releases_lease(connection, monkeypatch, capsys):
    def missing(*args, **kwargs):
        raise RestartRequired("本机尚无可验证的账号缓存")
    monkeypatch.setattr(cli.ConnectionManager, "connect", missing)
    code, result, _ = run(["connect", "--quiet"], capsys)
    assert code == 3 and result["error"]["action"] == "connect --allow-restart"
    assert connection.leases[0].closed


def test_conversation_ids_disambiguate_same_names_and_omit_message_preview(connection, capsys):
    code, result, _ = run(["conversations", "--query", "项目", "--quiet"], capsys)
    assert code == 0
    assert [item["id"] for item in result["conversations"]] == ["wxid_a", "wxid_b"]
    assert all("summary" not in item for item in result["conversations"])


def test_dates_include_last_second_of_end_date_and_accept_explicit_offset():
    assert cli._timestamp("2026-10-10", end=True) == int(datetime(2026, 10, 11).timestamp()) - 1
    assert cli._timestamp("2026-10-10T00:00:00+08:00") == 1791561600


def test_media_file_and_moments_options_reach_existing_service(connection, tmp_path, capsys):
    code, result, _ = run(["files", "--conversation", "wxid_a", "--category", "pdf", "--category", "word",
        "--max-file-mb", "25", "--since", "2026-10-01", "--output", str(tmp_path), "--quiet"], capsys)
    assert code == 0
    assert result["results"][0]["counts"] == {"attachments": 2}
    assert "message_count" not in result["results"][0]
    kind, request = connection.calls[-1]
    assert kind == "files" and request.categories == frozenset({"pdf", "word"})
    assert request.max_file_size_bytes == 25 * 1024**2 and request.start_timestamp > 0
    code, result, _ = run(["moments", "--self", "--output", str(tmp_path), "--quiet"], capsys)
    assert code == 0 and connection.calls[-1][1].is_self
    assert result["results"][0]["counts"] == {"moments": 0}
    code, _, _ = run(["package", "--all", "--type", "group", "--output", str(tmp_path), "--quiet"], capsys)
    assert code == 0 and not connection.calls[-1][1].include_videos
    assert not connection.calls[-1][1].allow_network_media


def test_cancellation_returns_130_and_preserves_successful_receipts(connection, monkeypatch, tmp_path, capsys):
    original = cli._export_one
    def export(service, args, conversation, progress, cancelled):
        if conversation.username == "wxid_b":
            raise ExportCancelled("合成取消")
        return original(service, args, conversation, progress, cancelled)
    monkeypatch.setattr(cli, "_export_one", export)
    code, result, _ = run(["export", "--all", "--output", str(tmp_path / "out"), "--quiet"], capsys)
    assert code == 130 and result["status"] == "cancelled"
    assert result["summary"] == {"requested": 3, "completed": 1, "failed": 1, "skipped": 1}
    assert Path(result["results"][0]["files"][0]).is_file()
    assert result["results"][1]["error"]["code"] == "CANCELLED"
    assert connection.leases[0].closed


def test_source_commands_import_no_gui_or_tkinter():
    code = "from wechat_exporter.agent_cli import main; import sys; main(['version']); assert 'wechat_exporter.gui' not in sys.modules; assert 'tkinter' not in sys.modules"
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, encoding="utf-8", check=True, timeout=30)
    assert json.loads(result.stdout)["headless"]


def test_module_forwards_exit_status_for_invalid_command():
    result = subprocess.run([sys.executable, "-m", "wechat_exporter", "unknown"],
        capture_output=True, encoding="utf-8", timeout=30)
    assert result.returncode == 2
    assert json.loads(result.stdout)["error"]["code"] == "INVALID_ARGUMENT"


@pytest.mark.skipif(os.name != "nt", reason="Windows shared mutex")
def test_gui_and_cli_share_mutex_without_cli_activating_gui():
    namespace = f"WeChatCLI.{uuid.uuid4().hex}"
    gui = claim_primary_instance(namespace)
    try:
        assert claim_headless_instance(namespace) is None
    finally:
        gui.close()
    lease = claim_headless_instance(namespace)
    try:
        assert claim_primary_instance(namespace) is None
        assert claim_headless_instance(namespace) is None
    finally:
        lease.close()
    replacement = claim_headless_instance(namespace)
    replacement.close()


def test_headless_self_test_runs_without_gui(tmp_path):
    code = "from wechat_exporter.agent_cli import main; import sys; result=main(['self-test','--output',sys.argv[1]]); assert 'wechat_exporter.gui' not in sys.modules; assert 'tkinter' not in sys.modules; raise SystemExit(result)"
    environment = os.environ.copy()
    environment["LOCALAPPDATA"] = str(tmp_path / "owned-settings")
    result = subprocess.run([sys.executable, "-c", code, str(tmp_path / "中文 合成数据")],
        capture_output=True, encoding="utf-8", timeout=120, env=environment)
    assert result.returncode == 0, result.stdout + result.stderr
    checks = json.loads(result.stdout)["checks"]
    assert checks["conversation_preview_faces"] == "skipped_headless"
    assert checks["json_count"] == checks["html_count"] == checks["jsonl_package_count"] == 4
