from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path

from .models import ExportResult


_HISTORY_VERSION = 1
_MAX_HISTORY_ENTRIES = 2_000


@dataclass(frozen=True, slots=True)
class ExportHistoryEntry:
    exported_at: str
    account_wxid: str
    conversation_name: str
    conversation_username: str
    conversation_type: str
    file_format: str
    file_path: str
    message_count: int
    duration_seconds: float


def default_history_path() -> Path:
    local_app_data = os.environ.get("LOCALAPPDATA")
    base = Path(local_app_data) if local_app_data else Path.home() / ".local" / "share"
    return base / "WeChatExporter" / "export_history.json"


def load_export_history(path: Path | None = None) -> list[ExportHistoryEntry]:
    history_path = path or default_history_path()
    if not history_path.is_file():
        return []
    try:
        payload = json.loads(history_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return []
    if not isinstance(payload, dict) or payload.get("version") != _HISTORY_VERSION:
        return []
    entries = payload.get("entries")
    if not isinstance(entries, list):
        return []
    result: list[ExportHistoryEntry] = []
    for item in entries:
        if not isinstance(item, dict):
            continue
        try:
            result.append(
                ExportHistoryEntry(
                    exported_at=str(item["exported_at"]),
                    account_wxid=str(item.get("account_wxid", "")),
                    conversation_name=str(item["conversation_name"]),
                    conversation_username=str(item.get("conversation_username", "")),
                    conversation_type=str(item["conversation_type"]),
                    file_format=str(item["file_format"]),
                    file_path=str(item["file_path"]),
                    message_count=int(item.get("message_count", 0)),
                    duration_seconds=float(item.get("duration_seconds", 0.0)),
                )
            )
        except (KeyError, TypeError, ValueError):
            continue
    return result


def append_export_history(
    result: ExportResult,
    *,
    account_wxid: str,
    path: Path | None = None,
    exported_at: datetime | None = None,
) -> list[ExportHistoryEntry]:
    """Persist one traceable history row per generated file using an atomic replace."""
    history_path = path or default_history_path()
    timestamp = (exported_at or datetime.now().astimezone()).isoformat(timespec="seconds")
    new_entries: list[ExportHistoryEntry] = []
    for file_path in result.files:
        conversation = result.file_conversations.get(file_path)
        if conversation is None:
            continue
        absolute_path = file_path.expanduser().resolve()
        new_entries.append(
            ExportHistoryEntry(
                exported_at=timestamp,
                account_wxid=account_wxid,
                conversation_name=conversation.display_name,
                conversation_username=conversation.username,
                conversation_type="群聊" if conversation.is_group else "联系人",
                file_format=absolute_path.suffix.lstrip(".").upper(),
                file_path=str(absolute_path),
                message_count=result.message_counts.get(conversation.username, 0),
                duration_seconds=result.duration_seconds,
            )
        )
    if not new_entries:
        return []

    combined = (new_entries + load_export_history(history_path))[:_MAX_HISTORY_ENTRIES]
    history_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = history_path.with_suffix(history_path.suffix + ".tmp")
    temporary_path.write_text(
        json.dumps(
            {
                "version": _HISTORY_VERSION,
                "entries": [asdict(entry) for entry in combined],
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    os.replace(temporary_path, history_path)
    return new_entries
