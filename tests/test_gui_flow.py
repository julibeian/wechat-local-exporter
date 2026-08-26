from __future__ import annotations

import os
from datetime import date, datetime
from pathlib import Path

from wechat_exporter.models import AccountLocation
from wechat_exporter.gui import _date_range_timestamps, _format_date_range
from wechat_exporter.windows import select_current_account


def _account(root: Path, name: str, source: str, timestamp: int) -> AccountLocation:
    account_dir = root / name
    session = account_dir / "db_storage" / "session" / "session.db"
    session.parent.mkdir(parents=True)
    session.write_bytes(b"test")
    os.utime(session, ns=(timestamp, timestamp))
    return AccountLocation(account_dir, name, source)


def test_auto_select_prefers_current_process_account(tmp_path: Path) -> None:
    newest = _account(tmp_path, "wxid_newest", "常用目录", 200)
    running = _account(tmp_path, "wxid_running", "微信进程", 100)
    assert select_current_account([newest, running]) == running


def test_auto_select_uses_newest_database_without_process_match(tmp_path: Path) -> None:
    older = _account(tmp_path, "wxid_older", "常用目录", 100)
    newest = _account(tmp_path, "wxid_newest", "常用目录", 200)
    assert select_current_account([older, newest]) == newest


def test_date_range_uses_full_local_days() -> None:
    start, end = _date_range_timestamps(date(2026, 8, 1), date(2026, 8, 23))
    assert datetime.fromtimestamp(start).strftime("%Y-%m-%d %H:%M:%S") == "2026-08-01 00:00:00"
    assert datetime.fromtimestamp(end).strftime("%Y-%m-%d %H:%M:%S") == "2026-08-23 23:59:59"
    assert _format_date_range(None, None) == "全部日期"
    assert _format_date_range(date(2026, 8, 1), None) == "2026-08-01  至  不限"
