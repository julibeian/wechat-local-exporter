from __future__ import annotations

import json
from datetime import datetime

import pytest

from wechat_exporter.json_exporter import JsonTextTranscriptWriter
from wechat_exporter.models import (
    AccountLocation,
    AttachmentReference,
    Conversation,
    ExportRequest,
    ExportWorkload,
    Message,
)
from wechat_exporter.service import ExporterService, estimate_export_seconds


def _message(local_id: int, text: str) -> Message:
    return Message(
        local_id,
        int(datetime(2026, 9, 2, 10, 0, local_id).timestamp()),
        1,
        "wxid_friend",
        "好友😀",
        False,
        text,
        source_db="message\\message_0.db",
        conversation_id="wxid_friend",
        attachment=AttachmentReference("不应进入纯文字 JSON.pdf", "pdf", 10),
    )


def test_json_text_export_is_valid_utf8_and_contains_only_text_message_fields(tmp_path) -> None:
    conversation = Conversation("wxid_friend", "好友😀")
    path = tmp_path / "chat.json"
    with JsonTextTranscriptWriter(
        path,
        conversation,
        start_timestamp=int(datetime(2026, 9, 1).timestamp()),
        end_timestamp=int(datetime(2026, 9, 2, 23, 59, 59).timestamp()),
    ) as writer:
        writer.write(_message(1, "第一行\n第二行😀"))

    raw = path.read_bytes()
    assert not raw.startswith(b"\xef\xbb\xbf")
    payload = json.loads(raw.decode("utf-8"))
    assert payload["schema_version"] == 1
    assert payload["format"] == "wechat_conversation_text"
    assert payload["conversation"] == {
        "id": "wxid_friend",
        "name": "好友😀",
        "type": "contact",
    }
    assert payload["messages"] == [
        {
            "timestamp": int(datetime(2026, 9, 2, 10, 0, 1).timestamp()),
            "time": datetime(2026, 9, 2, 10, 0, 1).isoformat(timespec="seconds"),
            "sender": "好友😀",
            "text": "第一行\n第二行😀",
        }
    ]
    encoded = json.dumps(payload, ensure_ascii=False)
    assert "attachment" not in encoded
    assert "source_db" not in encoded
    assert "message_type" not in encoded


def test_json_only_service_export_does_not_create_other_chat_formats(
    tmp_path,
    monkeypatch,
) -> None:
    conversation = Conversation("wxid_friend", "好友")
    iterations: list[str] = []

    class Archive:
        self_wxid = "wxid_self"

        def export_workload(self, *_args, **_kwargs):
            return ExportWorkload(message_count=1)

        def iter_messages(self, *_args, **_kwargs):
            iterations.append("once")
            yield _message(1, "快速纯文字")

    class UnexpectedMediaResolver:
        def __init__(self, *_args, **_kwargs):
            raise AssertionError("快速纯文字 JSON 不应初始化媒体解析器")

    monkeypatch.setattr("wechat_exporter.service.MediaResolver", UnexpectedMediaResolver)
    service = ExporterService(AccountLocation(tmp_path / "account", "wxid_self", "test"))
    service.archive = Archive()  # type: ignore[assignment]
    result = service.export(
        ExportRequest(
            conversations=(conversation,),
            output_dir=tmp_path / "output",
            include_json=True,
            include_jsonl=False,
            include_txt=False,
            include_pdf=False,
        )
    )
    assert [path.suffix for path in result.files] == [".json"]
    assert json.loads(result.files[0].read_text(encoding="utf-8"))["messages"][0]["text"] == "快速纯文字"
    assert iterations == ["once"]


def test_export_request_defaults_to_exactly_one_fast_text_format(tmp_path) -> None:
    request = ExportRequest(
        conversations=(Conversation("wxid_friend", "好友"),),
        output_dir=tmp_path,
    )

    assert request.include_json is False
    assert request.include_txt is True
    assert request.include_pdf is False
    assert request.include_pdf_images is False
    assert request.include_jsonl is False


@pytest.mark.parametrize(
    ("include_json", "include_txt", "include_pdf"),
    [
        (False, False, False),
        (True, True, False),
        (True, False, True),
        (False, True, True),
    ],
)
def test_service_rejects_zero_or_multiple_ordinary_formats(
    tmp_path,
    include_json: bool,
    include_txt: bool,
    include_pdf: bool,
) -> None:
    class Archive:
        self_wxid = "wxid_self"

        def export_workload(self, *_args, **_kwargs):
            raise AssertionError("格式校验应发生在读取消息之前")

    service = ExporterService(AccountLocation(tmp_path / "account", "wxid_self", "test"))
    service.archive = Archive()  # type: ignore[assignment]

    with pytest.raises(ValueError, match="每次只能选择"):
        service.export(
            ExportRequest(
                conversations=(Conversation("wxid_friend", "好友"),),
                output_dir=tmp_path / "output",
                include_json=include_json,
                include_txt=include_txt,
                include_pdf=include_pdf,
            )
        )

    assert not (tmp_path / "output").exists()


def test_json_text_estimate_targets_seconds_for_typical_export() -> None:
    lower, upper = estimate_export_seconds(
        ExportWorkload(message_count=10_000),
        conversation_count=1,
        include_json=True,
        include_jsonl=False,
        include_txt=False,
        include_pdf=False,
        include_pdf_images=False,
    )
    assert 0 < lower <= upper < 5
