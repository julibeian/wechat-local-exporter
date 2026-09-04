from __future__ import annotations

import json
from pathlib import Path

from .content import (
    WECHAT_VOICE_TEXT_PREFIX,
    app_message_semantic_type,
    extract_message_details,
)
from .models import AttachmentReference, Conversation, Message


SCHEMA_VERSION = 1

_MESSAGE_TYPES = {
    1: "text",
    3: "image",
    34: "voice",
    42: "contact_card",
    43: "video",
    47: "sticker",
    48: "location",
    49: "link",
    50: "call",
    10000: "system",
    10002: "system",
    244813135921: "quote",
    266287972401: "system",
    81604378673: "chat_history",
    154618822705: "mini_program",
    8594229559345: "red_packet",
    8589934592049: "transfer",
}


def understandable_message_type(message: Message) -> str:
    if message.attachment is not None:
        return "file"
    refined = app_message_semantic_type(message.message_type, message.raw_content)
    if refined:
        return refined
    return _MESSAGE_TYPES.get(message.message_type, "unknown")


def message_to_json_record(
    message: Message,
    conversation: Conversation,
) -> dict[str, object]:
    transcript: dict[str, str] | None = None
    text: str | None = message.content or None
    if message.message_type == 34:
        text = None
        if message.content.startswith(WECHAT_VOICE_TEXT_PREFIX):
            transcript_text = message.content[len(WECHAT_VOICE_TEXT_PREFIX) :].strip()
            if transcript_text:
                transcript = {"text": transcript_text, "source": "wechat"}

    return {
        "schema_version": SCHEMA_VERSION,
        "message_id": message.stable_id,
        "server_id": message.server_id if message.server_id > 0 else None,
        "local_id": message.local_id,
        "source_db": message.source_db,
        "conversation_id": message.conversation_id or conversation.username,
        "conversation_name": conversation.display_name,
        "conversation_type": "group" if conversation.is_group else "contact",
        "timestamp": message.timestamp,
        "time": message.datetime.isoformat(timespec="seconds"),
        "sort_seq": message.sort_seq,
        "sender_id": message.sender_id,
        "sender_name": message.sender_name,
        "is_outgoing": message.is_outgoing,
        "message_type": message.message_type,
        "type": understandable_message_type(message),
        "text": text,
        "transcript": transcript,
        "details": extract_message_details(message.message_type, message.raw_content),
        "attachment": attachment_to_json(message.attachment),
    }


def attachment_to_json(
    attachment: AttachmentReference | None,
) -> dict[str, object] | None:
    if attachment is None:
        return None
    value: dict[str, object] = {
        "filename": attachment.filename,
        "extension": attachment.extension,
        "size": attachment.size,
    }
    for key in (
        "md5",
        "attachment_id",
        "cdn_url",
        "aes_key",
        "file_key",
        "file_upload_token",
    ):
        field_value = getattr(attachment, key)
        if field_value:
            value[key] = field_value
    return value


class JsonlTranscriptWriter:
    """Write one independent UTF-8 JSON object per physical line."""

    def __init__(self, path: Path, conversation: Conversation):
        self.path = path
        self.conversation = conversation
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._stream = path.open("w", encoding="utf-8", newline="\n")
        self.count = 0

    def write(self, message: Message) -> None:
        self._stream.write(
            json.dumps(
                message_to_json_record(message, self.conversation),
                ensure_ascii=False,
                separators=(",", ":"),
                allow_nan=False,
            )
        )
        self._stream.write("\n")
        self.count += 1

    def close(self) -> None:
        if not self._stream.closed:
            self._stream.close()

    def __enter__(self) -> JsonlTranscriptWriter:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()
