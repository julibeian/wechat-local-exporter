from __future__ import annotations

from datetime import datetime
from pathlib import Path

from wechat_exporter.history import append_export_history, load_export_history
from wechat_exporter.integrity import verify_signature
from wechat_exporter.models import Conversation, ExportResult
from wechat_exporter.service import _conversation_output_dir


def test_contact_and_group_use_separate_conversation_directories(tmp_path: Path) -> None:
    contact = Conversation("wxid_friend", "同名会话")
    group = Conversation("room@chatroom", "同名会话", is_group=True)

    contact_dir = _conversation_output_dir(tmp_path, contact)
    group_dir = _conversation_output_dir(tmp_path, group)

    assert contact_dir.parent == tmp_path / "联系人"
    assert group_dir.parent == tmp_path / "群聊"
    assert contact_dir.name != group_dir.name
    assert contact_dir.name.startswith("同名会话 [")


def test_export_history_round_trip_keeps_time_and_file_address(tmp_path: Path) -> None:
    history_path = tmp_path / "state" / "export_history.json"
    conversation = Conversation("wxid_friend", "好友备注")
    exported_file = tmp_path / "联系人" / "好友备注 [abc]" / "好友备注.txt"
    exported_file.parent.mkdir(parents=True)
    exported_file.write_text("test", encoding="utf-8")
    result = ExportResult(
        files=[exported_file],
        file_conversations={exported_file: conversation},
        message_counts={conversation.username: 23},
        duration_seconds=1.5,
    )

    created = append_export_history(
        result,
        account_wxid="wxid_self",
        path=history_path,
        exported_at=datetime.fromisoformat("2026-08-26T18:30:00+08:00"),
    )
    loaded = load_export_history(history_path)

    assert created == loaded
    assert loaded[0].exported_at == "2026-08-26T18:30:00+08:00"
    assert loaded[0].file_path == str(exported_file.resolve())
    assert loaded[0].conversation_type == "联系人"
    assert loaded[0].file_format == "TXT"
    assert loaded[0].message_count == 23


def test_personal_signature_detects_tampering() -> None:
    assert verify_signature()
    assert not verify_signature("someone-else")
