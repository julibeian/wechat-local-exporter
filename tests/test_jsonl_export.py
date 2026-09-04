from __future__ import annotations

import json
from datetime import datetime

import pytest

from wechat_exporter.jsonl_exporter import (
    JsonlTranscriptWriter,
    message_to_json_record,
)
from wechat_exporter.models import (
    AccountLocation,
    AttachmentReference,
    Conversation,
    ExportRequest,
    ExportWorkload,
    Message,
)
from wechat_exporter.service import ExporterService, estimate_export_seconds


def _message(
    local_id: int,
    content: str,
    *,
    message_type: int = 1,
    server_id: int = 0,
    sender_id: str = "wxid_friend",
    sender_name: str = "好友😀",
    attachment: AttachmentReference | None = None,
) -> Message:
    return Message(
        local_id=local_id,
        timestamp=int(datetime(2026, 9, 1, 20, 31, local_id).timestamp()),
        message_type=message_type,
        sender_id=sender_id,
        sender_name=sender_name,
        is_outgoing=False,
        content=content,
        sort_seq=1000 + local_id,
        source_db="message/message_0.db",
        server_id=server_id,
        conversation_id="room@chatroom",
        attachment=attachment,
    )


def test_jsonl_is_one_valid_utf8_json_object_per_physical_line(tmp_path) -> None:
    conversation = Conversation("room@chatroom", "课程群😀", is_group=True)
    messages = (
        _message(1, "中文😀\n第二行", server_id=9001, sender_name="班长"),
        _message(2, "系统提示", message_type=10000, sender_id="系统", sender_name="系统"),
        _message(3, "[引用] 原文没有丢", message_type=244813135921),
    )
    path = tmp_path / "chat.jsonl"
    with JsonlTranscriptWriter(path, conversation) as writer:
        for message in messages:
            writer.write(message)

    raw = path.read_bytes()
    assert not raw.startswith(b"\xef\xbb\xbf")
    physical_lines = raw.decode("utf-8").splitlines()
    assert len(physical_lines) == len(messages)
    records = [json.loads(line) for line in physical_lines]
    assert records[0]["schema_version"] == 1
    assert records[0]["message_id"] == "server:9001"
    assert records[0]["conversation_type"] == "group"
    assert records[0]["sender_name"] == "班长"
    assert records[0]["text"] == "中文😀\n第二行"
    assert records[1]["type"] == "system"
    assert records[2]["type"] == "quote"
    assert records[2]["text"] == "[引用] 原文没有丢"


def test_jsonl_voice_transcript_and_missing_transcript_are_structured() -> None:
    conversation = Conversation("wxid_friend", "好友")
    official = message_to_json_record(
        _message(
            1,
            "[微信语音转文字] 老师说明天下午三点集合",
            message_type=34,
        ),
        conversation,
    )
    missing = message_to_json_record(
        _message(2, "[语音]（微信尚未生成转文字）", message_type=34),
        conversation,
    )
    assert official["type"] == "voice"
    assert official["text"] is None
    assert official["transcript"] == {
        "text": "老师说明天下午三点集合",
        "source": "wechat",
    }
    assert missing["text"] is None
    assert missing["transcript"] is None


def test_jsonl_attachment_metadata_and_stable_local_fallback() -> None:
    conversation = Conversation("wxid_friend", "好友")
    attachment = AttachmentReference(
        filename="实验要求.pdf",
        extension="pdf",
        size=1837422,
        md5="a" * 32,
        attachment_id="attach-123",
        cdn_url="https://example.invalid/file",
        aes_key="key",
    )
    message = _message(7, "[文件] 实验要求.pdf", message_type=49, attachment=attachment)
    first = message_to_json_record(message, conversation)
    second = message_to_json_record(message, conversation)
    assert first["message_id"] == "local:message\\message_0.db:7"
    assert first["message_id"] == second["message_id"]
    assert first["type"] == "file"
    assert first["attachment"] == {
        "filename": "实验要求.pdf",
        "extension": "pdf",
        "size": 1837422,
        "md5": "a" * 32,
        "attachment_id": "attach-123",
        "cdn_url": "https://example.invalid/file",
        "aes_key": "key",
    }


def test_standalone_jsonl_is_rejected_in_favor_of_advanced_package(tmp_path) -> None:
    conversations = (
        Conversation("wxid_friend", "好友"),
        Conversation("room@chatroom", "课程群", is_group=True),
    )
    start = int(datetime(2026, 8, 20).timestamp())
    end = int(datetime(2026, 9, 2, 23, 59, 59).timestamp())
    calls: list[tuple[str, int, int]] = []

    class Archive:
        self_wxid = "wxid_self"

        def export_workload(self, selected, **_kwargs):
            return ExportWorkload(message_count=len(tuple(selected)))

        def iter_messages(self, conversation, **kwargs):
            calls.append(
                (
                    conversation.username,
                    kwargs["start_timestamp"],
                    kwargs["end_timestamp"],
                )
            )
            yield Message(
                1,
                int(datetime(2026, 9, 1, 10, 0).timestamp()),
                1,
                conversation.username,
                conversation.display_name,
                False,
                f"来自 {conversation.display_name}",
                source_db="message\\message_0.db",
                conversation_id=conversation.username,
            )

    service = ExporterService(AccountLocation(tmp_path / "account", "wxid_self", "test"))
    service.archive = Archive()  # type: ignore[assignment]
    with pytest.raises(ValueError, match="AI 完整资料包"):
        service.export(
            ExportRequest(
                conversations=conversations,
                output_dir=tmp_path / "output",
                include_jsonl=True,
                include_txt=False,
                include_pdf=False,
                start_timestamp=start,
                end_timestamp=end,
            )
        )
    assert calls == []


def test_jsonl_estimate_is_supported_and_close_to_txt() -> None:
    workload = ExportWorkload(message_count=100_000)
    jsonl = estimate_export_seconds(
        workload,
        conversation_count=2,
        include_jsonl=True,
        include_txt=False,
        include_pdf=False,
        include_pdf_images=False,
    )
    txt = estimate_export_seconds(
        workload,
        conversation_count=2,
        include_jsonl=False,
        include_txt=True,
        include_pdf=False,
        include_pdf_images=False,
    )
    assert jsonl[0] > 0
    assert 0.8 <= jsonl[0] / txt[0] <= 1.4
