from __future__ import annotations

import hashlib
import heapq
import re
import sqlite3
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path

from .content import decode_database_content, parse_message_text, split_group_sender
from .crypto import DecryptedWorkspace
from .attachments import extract_attachment_reference
from .media import extract_media_reference
from .models import AccountLocation, Conversation, ExportWorkload, Message, Moment
from .moments import load_contact_moments


_SYSTEM_CONVERSATION_IDS = frozenset(
    {
        "brandsessionholder",
        "filehelper",
        "fmessage",
        "floatbottle",
        "helper_entry",
        "masssendapp",
        "medianote",
        "newsapp",
        "notification_messages",
        "officialaccounts",
        "qmessage",
        "qqmail",
        "qqsync",
        "shakeapp",
        "tmessage",
        "weibo",
        "weixin",
        "weixin_pay",
        "weixin_team",
    }
)


@dataclass(frozen=True, slots=True)
class SenderCalibration:
    source_db: str
    sender_id: int
    role: str  # "self" or "other"


@dataclass(frozen=True, slots=True)
class CalibrationSample:
    source_db: str
    sender_id: int
    timestamp: int
    text: str


class WeChatArchive:
    def __init__(
        self,
        account: AccountLocation,
        workspace: DecryptedWorkspace,
        calibrations: list[SenderCalibration] | None = None,
    ):
        self.account = account
        self.workspace = workspace
        self.self_wxid = _clean_account_wxid(account.wxid)
        self.contacts: dict[str, str] = {}
        self.personal_contacts: set[str] = set()
        self._calibrations = {
            (item.source_db, item.sender_id): item.role for item in calibrations or []
        }

    def load_metadata(self) -> None:
        self.personal_contacts.clear()
        self.contacts = self._load_contacts()
        if self.self_wxid not in self.contacts:
            prefix_matches = [value for value in self.contacts if self.account.wxid.startswith(value)]
            if prefix_matches:
                self.self_wxid = max(prefix_matches, key=len)

    def conversations(self) -> list[Conversation]:
        path = self.workspace.decrypted_path("session\\session.db")
        connection = _connect_readonly(path)
        try:
            table = _find_table(connection, ("SessionTable", "session"))
            if not table:
                raise ValueError("session.db 中没有找到会话表")
            rows = connection.execute(f'SELECT * FROM "{table}"').fetchall()
        finally:
            connection.close()
        result: list[Conversation] = []
        for row in rows:
            keys = set(row.keys())
            username = str(_first(row, keys, ("username", "user_name", "talker"), "")).strip()
            if not username:
                continue
            is_group = username.lower().endswith("@chatroom")
            if not is_group and username not in self.personal_contacts:
                continue
            timestamp = _as_timestamp(
                _first(row, keys, ("last_timestamp", "sort_timestamp", "last_msg_time"), 0)
            )
            raw_summary = _first(row, keys, ("summary", "digest", "last_msg"), "")
            summary = decode_database_content(raw_summary)
            display = self.contacts.get(username, username)
            result.append(
                Conversation(
                    username=username,
                    display_name=display,
                    last_timestamp=timestamp,
                    summary=summary[:160],
                    is_group=is_group,
                )
            )
        result.sort(key=lambda item: item.last_timestamp, reverse=True)
        return result

    def self_conversation(self) -> Conversation:
        """Return a UI-only entry used to export the logged-in user's Moments."""
        display = self.contacts.get(self.self_wxid, "").strip()
        display_name = (
            f"我自己（{display}）"
            if display and display != "我自己"
            else "我自己"
        )
        return Conversation(
            username=self.self_wxid,
            display_name=display_name,
            summary="用于导出我自己的全部朋友圈（含私密和分组可见）",
            is_self=True,
        )

    def contact_moments(self, conversation: Conversation) -> list[Moment]:
        """Return all locally synced visible posts for one contact or self."""
        return load_contact_moments(self.workspace, conversation)

    def calibration_samples(
        self, conversation: Conversation, limit_per_sender: int = 2
    ) -> list[CalibrationSample]:
        samples: list[CalibrationSample] = []
        for source_db, path in self._message_databases():
            connection = _connect_readonly(path)
            try:
                table = _message_table(connection, conversation.username)
                if not table:
                    continue
                columns = _column_names(connection, table)
                if "real_sender_id" not in columns or "message_content" not in columns:
                    continue
                if "computed_is_send" in columns or "is_send" in columns:
                    continue
                rows = connection.execute(
                    f'SELECT real_sender_id, create_time, message_content FROM "{table}" '
                    "WHERE local_type=1 AND message_content IS NOT NULL ORDER BY create_time DESC LIMIT 100"
                ).fetchall()
                counts: dict[int, int] = {}
                name_map = _load_name2id(connection)
                for row in rows:
                    sender_id = _as_int(row[0])
                    mapped_sender = name_map.get(sender_id, "")
                    if sender_id <= 0 or mapped_sender in {self.self_wxid, conversation.username}:
                        continue
                    if (source_db, sender_id) in self._calibrations:
                        continue
                    count = counts.get(sender_id, 0)
                    if count >= limit_per_sender:
                        continue
                    text = decode_database_content(row[2])
                    if not text:
                        continue
                    samples.append(
                        CalibrationSample(source_db, sender_id, _as_timestamp(row[1]), text[:120])
                    )
                    counts[sender_id] = count + 1
            finally:
                connection.close()
        return samples

    def set_calibration(self, source_db: str, sender_id: int, role: str) -> None:
        if role not in {"self", "other"}:
            raise ValueError("role 必须是 self 或 other")
        self._calibrations[(source_db, sender_id)] = role

    def iter_messages(
        self,
        conversation: Conversation,
        *,
        start_timestamp: int = 0,
        end_timestamp: int = 0,
    ) -> Iterator[Message]:
        iterators: list[Iterator[Message]] = []
        for source_db, path in self._message_databases():
            iterator = self._iter_database_messages(
                source_db,
                path,
                conversation,
                start_timestamp=start_timestamp,
                end_timestamp=end_timestamp,
            )
            iterators.append(iterator)

        heap: list[tuple[int, int, int, Message, Iterator[Message]]] = []
        sequence = 0
        for iterator in iterators:
            try:
                message = next(iterator)
            except StopIteration:
                continue
            heapq.heappush(
                heap, (message.timestamp, message.sort_seq, sequence, message, iterator)
            )
            sequence += 1

        seen: set[tuple[int, int, int, str, str, str]] = set()
        while heap:
            _, _, _, message, iterator = heapq.heappop(heap)
            fingerprint = (
                message.timestamp,
                message.sort_seq,
                message.message_type,
                message.sender_id,
                message.content,
                message.media.md5 if message.media else "",
            )
            if fingerprint not in seen:
                seen.add(fingerprint)
                yield message
            try:
                next_message = next(iterator)
            except StopIteration:
                continue
            heapq.heappush(
                heap,
                (
                    next_message.timestamp,
                    next_message.sort_seq,
                    sequence,
                    next_message,
                    iterator,
                ),
            )
            sequence += 1

    def export_workload(
        self,
        conversations: Iterable[Conversation],
        *,
        start_timestamp: int = 0,
        end_timestamp: int = 0,
    ) -> ExportWorkload:
        """Count rows and media types without decoding message bodies."""
        selected_conversations = tuple(conversations)
        message_count = 0
        image_count = 0
        emoticon_count = 0
        for _source_db, path in self._message_databases():
            connection = _connect_readonly(path)
            try:
                for conversation in selected_conversations:
                    table = _message_table(connection, conversation.username)
                    if not table:
                        continue
                    columns = _column_names(connection, table)
                    where: list[str] = []
                    parameters: list[int] = []
                    if start_timestamp > 0 and "create_time" in columns:
                        where.append("create_time >= ?")
                        parameters.append(start_timestamp)
                    if end_timestamp > 0 and "create_time" in columns:
                        where.append("create_time <= ?")
                        parameters.append(end_timestamp)
                    if "local_type" in columns:
                        selections = (
                            "COUNT(*)",
                            "COALESCE(SUM(CASE WHEN local_type = 3 THEN 1 ELSE 0 END), 0)",
                            "COALESCE(SUM(CASE WHEN local_type = 47 THEN 1 ELSE 0 END), 0)",
                        )
                    else:
                        selections = ("COUNT(*)", "0", "0")
                    sql = f'SELECT {", ".join(selections)} FROM "{table}"'
                    if where:
                        sql += " WHERE " + " AND ".join(where)
                    row = connection.execute(sql, parameters).fetchone()
                    if row is None:
                        continue
                    message_count += int(row[0] or 0)
                    image_count += int(row[1] or 0)
                    emoticon_count += int(row[2] or 0)
            finally:
                connection.close()
        return ExportWorkload(
            message_count=message_count,
            image_count=image_count,
            emoticon_count=emoticon_count,
        )

    def _load_contacts(self) -> dict[str, str]:
        path = self.workspace.decrypted_path("contact\\contact.db")
        connection = _connect_readonly(path)
        contacts: dict[str, str] = {}
        try:
            table = _find_table(connection, ("contact", "Contact"))
            if not table:
                return contacts
            for row in connection.execute(f'SELECT * FROM "{table}"'):
                keys = set(row.keys())
                username = str(_first(row, keys, ("username", "user_name"), "")).strip()
                if not username:
                    continue
                display = username
                for field in ("remark", "nick_name", "nickname", "alias"):
                    if field in keys and str(row[field] or "").strip():
                        display = str(row[field]).strip()
                        break
                contacts[username] = display
                if _is_personal_contact(row, keys, username):
                    self.personal_contacts.add(username)
        finally:
            connection.close()
        return contacts

    def _message_databases(self) -> list[tuple[str, Path]]:
        values = []
        for relative in self.workspace.keys.paths():
            if re.fullmatch(r"message\\message_\d+\.db", relative, re.I):
                path = self.workspace.decrypted_path(relative)
                if path.is_file():
                    values.append((relative, path))
        return sorted(values)

    def _iter_database_messages(
        self,
        source_db: str,
        path: Path,
        conversation: Conversation,
        *,
        start_timestamp: int,
        end_timestamp: int,
    ) -> Iterator[Message]:
        connection = _connect_readonly(path)
        try:
            table = _message_table(connection, conversation.username)
            if not table:
                return
            columns = _column_names(connection, table)
            desired = [
                "local_id",
                "local_type",
                "create_time",
                "sort_seq",
                "real_sender_id",
                "message_content",
                "compress_content",
                "packed_info_data",
                "is_send",
                "computed_is_send",
                "server_id",
            ]
            selected = [name for name in desired if name in columns]
            where = []
            parameters: list[int] = []
            if start_timestamp > 0 and "create_time" in columns:
                where.append("create_time >= ?")
                parameters.append(start_timestamp)
            if end_timestamp > 0 and "create_time" in columns:
                where.append("create_time <= ?")
                parameters.append(end_timestamp)
            sql = f'SELECT {", ".join(selected)} FROM "{table}"'
            if where:
                sql += " WHERE " + " AND ".join(where)
            order = [name for name in ("create_time", "sort_seq", "local_id") if name in columns]
            if order:
                sql += " ORDER BY " + ", ".join(order)
            name_map = _load_name2id(connection)
            for row in connection.execute(sql, parameters):
                yield self._row_to_message(source_db, row, conversation, name_map)
        finally:
            connection.close()

    def _row_to_message(
        self,
        source_db: str,
        row: sqlite3.Row,
        conversation: Conversation,
        name_map: dict[int, str],
    ) -> Message:
        keys = set(row.keys())
        local_id = _as_int(_first(row, keys, ("local_id",), 0))
        server_id = _as_int(_first(row, keys, ("server_id", "svr_id"), 0))
        message_type = _as_int(_first(row, keys, ("local_type",), 0))
        timestamp = _as_timestamp(_first(row, keys, ("create_time",), 0))
        sort_seq = _as_int(_first(row, keys, ("sort_seq",), timestamp * 1000))
        raw = decode_database_content(
            _first(row, keys, ("message_content",), None),
            _first(row, keys, ("compress_content",), None),
        )
        prefix_sender, stripped_raw = split_group_sender(raw) if conversation.is_group else ("", raw)
        sender_numeric = _as_int(_first(row, keys, ("real_sender_id",), 0))
        mapped_sender = name_map.get(sender_numeric, "")
        if conversation.is_group:
            sender_id = prefix_sender or mapped_sender
        else:
            sender_id = (
                mapped_sender
                if mapped_sender in {self.self_wxid, conversation.username}
                else ""
            )
        raw_is_send = _first(row, keys, ("computed_is_send", "is_send"), None)
        is_outgoing = _as_optional_bool(raw_is_send)

        if not sender_id and is_outgoing is not None:
            sender_id = self.self_wxid if is_outgoing else conversation.username
        elif not sender_id and sender_numeric > 0:
            role = self._calibrations.get((source_db, sender_numeric))
            if role == "self":
                sender_id = self.self_wxid
                is_outgoing = True
            elif role == "other":
                sender_id = conversation.username
                is_outgoing = False
            else:
                sender_id = f"未校准发送者#{sender_numeric}"
        if sender_id == self.self_wxid:
            is_outgoing = True
        elif sender_id:
            is_outgoing = False if is_outgoing is None else is_outgoing
        if not sender_id:
            if is_outgoing is True:
                sender_id = self.self_wxid
            elif is_outgoing is False:
                sender_id = conversation.username
            else:
                sender_id = "系统"

        if sender_id == self.self_wxid:
            sender_name = "我"
        else:
            sender_name = self.contacts.get(sender_id, sender_id)
        content = parse_message_text(message_type, stripped_raw)
        media = extract_media_reference(
            message_type,
            stripped_raw,
            _first(row, keys, ("packed_info_data",), None),
        )
        attachment = extract_attachment_reference(
            message_type,
            stripped_raw,
            _first(row, keys, ("packed_info_data",), None),
        )
        return Message(
            local_id=local_id,
            timestamp=timestamp,
            message_type=message_type,
            sender_id=sender_id,
            sender_name=sender_name,
            is_outgoing=is_outgoing,
            content=content,
            sort_seq=sort_seq,
            source_db=source_db,
            server_id=server_id,
            conversation_id=conversation.username,
            media=media,
            attachment=attachment,
            raw_content=stripped_raw,
        )


def _connect_readonly(path: Path) -> sqlite3.Connection:
    uri = f"file:{path.as_posix()}?mode=ro&immutable=1"
    connection = sqlite3.connect(uri, uri=True)
    connection.row_factory = sqlite3.Row
    return connection


def _find_table(connection: sqlite3.Connection, names: tuple[str, ...]) -> str | None:
    available = {
        str(row[0]).lower(): str(row[0])
        for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    for name in names:
        if name.lower() in available:
            return available[name.lower()]
    return None


def _message_table(connection: sqlite3.Connection, username: str) -> str | None:
    expected = "Msg_" + hashlib.md5(username.encode("utf-8")).hexdigest()
    row = connection.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND lower(name)=lower(?)", (expected,)
    ).fetchone()
    return str(row[0]) if row else None


def _column_names(connection: sqlite3.Connection, table: str) -> set[str]:
    escaped = table.replace('"', '""')
    return {str(row[1]).lower() for row in connection.execute(f'PRAGMA table_info("{escaped}")')}


def _load_name2id(connection: sqlite3.Connection) -> dict[int, str]:
    row = connection.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'Name2Id%' "
        "ORDER BY name DESC LIMIT 1"
    ).fetchone()
    if not row:
        return {}
    table = str(row[0]).replace('"', '""')
    result: dict[int, str] = {}
    try:
        for item in connection.execute(f'SELECT rowid, user_name FROM "{table}"'):
            result[_as_int(item[0])] = str(item[1] or "")
    except sqlite3.DatabaseError:
        return {}
    return result


def _first(row: sqlite3.Row, keys: set[str], names: tuple[str, ...], default: object) -> object:
    for name in names:
        if name in keys:
            value = row[name]
            if value is not None:
                return value
    return default


def _as_int(value: object) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError, OverflowError):
        return 0


def _as_timestamp(value: object) -> int:
    result = _as_int(value)
    while result > 10_000_000_000:
        result //= 1000
    return result


def _as_optional_bool(value: object) -> bool | None:
    if value is None or value == "":
        return None
    if value in (1, "1", True, "true", "True"):
        return True
    if value in (0, "0", False, "false", "False"):
        return False
    return None


def _clean_account_wxid(value: str) -> str:
    match = re.fullmatch(r"(wxid_.+?)_[0-9a-fA-F]{4,8}", value)
    return match.group(1) if match else value


def _is_personal_contact(
    row: sqlite3.Row, keys: set[str], username: str
) -> bool:
    normalized = username.strip().lower()
    if (
        not normalized
        or normalized.startswith("gh_")
        or normalized.endswith("@chatroom")
        or normalized.endswith("@app")
        or normalized in _SYSTEM_CONVERSATION_IDS
    ):
        return False
    if _as_int(_first(row, keys, ("verify_flag", "verifyflag"), 0)) != 0:
        return False
    if _as_int(_first(row, keys, ("delete_flag", "deleteflag"), 0)) != 0:
        return False
    type_columns = ("local_type", "contact_type", "type")
    if any(name in keys for name in type_columns):
        return _as_int(_first(row, keys, type_columns, 0)) == 1
    return True
