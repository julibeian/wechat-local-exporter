from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from .models import Conversation, Message


SCHEMA_VERSION = 1


class JsonTextTranscriptWriter:
    """Stream a compact JSON document containing conversation text only."""

    def __init__(
        self,
        path: Path,
        conversation: Conversation,
        *,
        start_timestamp: int = 0,
        end_timestamp: int = 0,
    ):
        self.path = path
        self.conversation = conversation
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._stream = path.open("w", encoding="utf-8", newline="\n")
        self._first = True
        self.count = 0
        header = {
            "schema_version": SCHEMA_VERSION,
            "format": "wechat_conversation_text",
            "conversation": {
                "id": conversation.username,
                "name": conversation.display_name,
                "type": "group" if conversation.is_group else "contact",
            },
            "exported_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "time_range": {
                "start": _time_value(start_timestamp),
                "end": _time_value(end_timestamp),
            },
        }
        encoded = json.dumps(header, ensure_ascii=False, separators=(",", ":"))
        self._stream.write(encoded[:-1])
        self._stream.write(',"messages":[')

    def write(self, message: Message) -> None:
        if not self._first:
            self._stream.write(",")
        self._stream.write(
            json.dumps(
                {
                    "timestamp": message.timestamp,
                    "time": message.datetime.isoformat(timespec="seconds"),
                    "sender": message.sender_name,
                    "text": message.content or "",
                },
                ensure_ascii=False,
                separators=(",", ":"),
            )
        )
        self._first = False
        self.count += 1

    def close(self) -> None:
        if self._stream.closed:
            return
        self._stream.write("]}\n")
        self._stream.close()

    def __enter__(self) -> JsonTextTranscriptWriter:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


def _time_value(timestamp: int) -> str | None:
    if timestamp <= 0:
        return None
    return datetime.fromtimestamp(timestamp).isoformat(timespec="seconds")
