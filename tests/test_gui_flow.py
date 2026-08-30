from __future__ import annotations

import os
from datetime import date, datetime
from pathlib import Path

from wechat_exporter.models import AccountLocation, Conversation
from wechat_exporter.gui import (
    ExporterApp,
    HISTORY_DIALOG_MIN_SIZE,
    HISTORY_DIALOG_SIZE,
    STAR_PROMPT_DELAY_SECONDS,
    StarPrompt,
    _conversation_matches_filters,
    _group_conversations,
    _name_initials,
    _date_range_timestamps,
    _format_date_range,
    _moments_export_eligibility,
)
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


def test_conversation_type_dropdown_filters_categories_and_search() -> None:
    contact = Conversation("wxid_friend", "朋友", summary="周末见")
    group = Conversation("study@chatroom", "学习群", summary="作业", is_group=True)

    assert _conversation_matches_filters(
        contact, query="朋友", type_filter="contact"
    )
    assert not _conversation_matches_filters(
        group, query="", type_filter="contact"
    )
    assert _conversation_matches_filters(
        group, query="作业", type_filter="group"
    )
    assert not _conversation_matches_filters(
        contact, query="", type_filter="group"
    )
    assert _conversation_matches_filters(contact, query="", type_filter="all")
    assert _conversation_matches_filters(group, query="", type_filter="all")


def test_conversations_are_grouped_by_latin_and_pinyin_initials() -> None:
    conversations = [
        Conversation("wxid_zhang", "张三"),
        Conversation("wxid_alice", "Alice"),
        Conversation("study@chatroom", "学习群", is_group=True),
        Conversation("wxid_an", "安安"),
        Conversation("wxid_self", "我自己", is_self=True),
    ]

    grouped = _group_conversations(conversations)

    assert [section for section, _items in grouped] == ["★ 本人", "A", "X", "Z"]
    assert [item.display_name for item in grouped[1][1]] == ["安安", "Alice"]
    assert _name_initials("吕新颜") == "LXY"


def test_star_prompt_is_embedded_and_history_dialog_is_larger() -> None:
    assert issubclass(StarPrompt, __import__("tkinter").Frame)
    assert HISTORY_DIALOG_SIZE == (1380, 760)
    assert HISTORY_DIALOG_MIN_SIZE == (1080, 580)


def test_moments_export_requires_one_personal_contact_only() -> None:
    contact = Conversation("wxid_friend", "朋友")
    self_contact = Conversation("wxid_self", "我自己", is_self=True)
    group = Conversation("study@chatroom", "学习群", is_group=True)

    assert _moments_export_eligibility((contact,)) == (True, "")
    assert _moments_export_eligibility((self_contact,)) == (True, "")
    assert not _moments_export_eligibility(())[0]
    assert not _moments_export_eligibility((contact, contact))[0]
    assert not _moments_export_eligibility((group,))[0]


class _AfterRoot:
    def __init__(self) -> None:
        self.calls: list[tuple[int, object]] = []
        self.cancelled: list[str] = []

    def after(self, delay_ms: int, callback) -> str:
        self.calls.append((delay_ms, callback))
        return f"after-{len(self.calls)}"

    def after_cancel(self, after_id: str) -> None:
        self.cancelled.append(after_id)


def test_star_timer_starts_as_full_delay_after_connection() -> None:
    app = object.__new__(ExporterApp)
    app.root = _AfterRoot()
    app._star_prompt_shown = False
    app._star_prompt_after_id = None

    app._schedule_star_prompt_after_connection()
    assert app.root.calls[0][0] == int(STAR_PROMPT_DELAY_SECONDS * 1000)

    app._schedule_star_prompt_after_connection()
    assert app.root.cancelled == ["after-1"]
    assert app.root.calls[1][0] == 60_000
