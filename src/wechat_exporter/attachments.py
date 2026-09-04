from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import urllib.parse
import zipfile
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from xml.etree import ElementTree

from .content import decode_database_content
from .exporters import safe_filename
from .models import (
    CHAT_FILE_CATEGORIES,
    WECHAT_FILE_MESSAGE_TYPES,
    AccountLocation,
    AttachmentReference,
    Conversation,
    Message,
)


FILE_INDEX_SCHEMA_VERSION = 1
EXPLICIT_FILE_MESSAGE_TYPES = WECHAT_FILE_MESSAGE_TYPES
FILE_CATEGORIES = CHAT_FILE_CATEGORIES

_CATEGORY_EXTENSIONS = {
    "pdf": frozenset({"pdf"}),
    "word": frozenset({"doc", "docx", "docm", "dot", "dotx", "rtf"}),
    "excel": frozenset({"xls", "xlsx", "xlsm", "xlsb", "csv", "et"}),
    "powerpoint": frozenset({"ppt", "pptx", "pptm", "pps", "ppsx", "dps"}),
    "archive": frozenset({"zip", "rar", "7z", "tar", "gz", "bz2", "xz"}),
}
_CATEGORY_LABELS = {
    "pdf": "PDF",
    "word": "Word",
    "excel": "Excel",
    "powerpoint": "PowerPoint",
    "archive": "压缩包",
    "other": "其他文件",
}
_HEX_MD5 = re.compile(r"(?i)^[0-9a-f]{32}$")
_URL = re.compile(r"https?://[^\s<>\"']+", re.I)


def extract_attachment_reference(
    message_type: int,
    raw_content: str,
    packed_info_data: object = None,
) -> AttachmentReference | None:
    """Extract file metadata without treating every type-49 appmsg as a file."""
    root = _parse_xml(raw_content)
    appmsg = _first_node(root, "appmsg")
    subtype = _as_int(_node_text(appmsg, ("type",)))
    if message_type == 49:
        if subtype != 6:
            return None
    elif message_type not in EXPLICIT_FILE_MESSAGE_TYPES:
        return None

    app_container = appmsg if appmsg is not None else root
    attachment_node = _first_node(app_container, "appattach")
    filename = _node_text(
        app_container,
        ("title", "filename", "file_name", "displayname"),
    )
    extension = _normalise_extension(
        _first_node_text(
            (attachment_node, appmsg, root),
            ("fileext", "file_ext", "ext"),
        )
    )
    if not extension and filename:
        extension = _normalise_extension(Path(filename).suffix)

    size = _positive_int(
        _first_node_text(
            (attachment_node, appmsg, root),
            ("totallen", "filesize", "file_size", "size"),
        )
    )
    md5 = _normalise_md5(
        _first_node_text(
            (attachment_node, appmsg, root),
            ("md5", "filemd5", "file_md5"),
        )
    )
    attachment_id = _first_node_text(
        (attachment_node, appmsg, root),
        ("attachid", "attachmentid", "fileid", "mediaid"),
    )
    cdn_url = _first_node_text(
        (attachment_node, appmsg, root),
        ("cdnattachurl", "cdnurl", "fileurl", "url"),
    )
    aes_key = _first_node_text((attachment_node, appmsg, root), ("aeskey", "aes_key"))
    file_key = _first_node_text((attachment_node, appmsg, root), ("filekey", "file_key"))
    upload_token = _first_node_text(
        (attachment_node, appmsg, root),
        ("fileuploadtoken", "file_upload_token", "uploadtoken"),
    )

    packed_text = decode_database_content(packed_info_data)
    if not md5:
        md5_match = re.search(r"(?i)(?<![0-9a-f])([0-9a-f]{32})(?![0-9a-f])", packed_text)
        if md5_match:
            md5 = md5_match.group(1).lower()
    if not cdn_url:
        url_match = _URL.search(packed_text)
        if url_match:
            cdn_url = url_match.group(0)

    if not filename:
        suffix = f".{extension}" if extension else ""
        identity = safe_filename(attachment_id, fallback="未命名附件")[:48]
        filename = f"{identity}{suffix}"
    return AttachmentReference(
        filename=filename.strip(),
        extension=extension,
        size=size,
        md5=md5,
        attachment_id=attachment_id,
        cdn_url=cdn_url,
        aes_key=aes_key,
        file_key=file_key,
        file_upload_token=upload_token,
    )


def attachment_category(extension: str) -> str:
    normalised = _normalise_extension(extension)
    for category, extensions in _CATEGORY_EXTENSIONS.items():
        if normalised in extensions:
            return category
    return "other"


@dataclass(frozen=True, slots=True)
class _IndexedFile:
    path: Path
    name_key: str
    stem_key: str
    size: int


class AttachmentResolver:
    """Read-only, one-pass index over plausible file roots for one account."""

    def __init__(
        self,
        account: AccountLocation,
        conversations: Iterable[Conversation] = (),
    ):
        self.account = account
        self.index_build_count = 0
        self.indexed_file_count = 0
        self._entries: list[_IndexedFile] = []
        self._by_name: dict[str, list[_IndexedFile]] = {}
        self._by_size: dict[int, list[_IndexedFile]] = {}
        self._hash_cache: dict[Path, str] = {}
        self._build_index(tuple(conversations))

    def _build_index(self, conversations: tuple[Conversation, ...]) -> None:
        self.index_build_count += 1
        roots: list[Path] = []
        account_dir = self.account.account_dir
        for candidate in (
            account_dir / "msg" / "file",
            account_dir / "msg" / "File",
            account_dir / "msg" / "migrate" / "File",
            account_dir / "msg" / "migrate" / "file",
        ):
            if candidate.is_dir():
                roots.append(candidate)

        for conversation in conversations:
            chat_hash = hashlib.md5(conversation.username.encode("utf-8")).hexdigest()
            chat_root = account_dir / "msg" / "attach" / chat_hash
            for candidate in (chat_root / "File", chat_root / "file"):
                if candidate.is_dir():
                    roots.append(candidate)
            if chat_root.is_dir():
                for month_dir in chat_root.iterdir():
                    if not month_dir.is_dir():
                        continue
                    for name in ("File", "file"):
                        candidate = month_dir / name
                        if candidate.is_dir():
                            roots.append(candidate)

        seen_roots: set[Path] = set()
        seen_files: set[Path] = set()
        account_root = self.account.account_dir.resolve()
        for root in roots:
            resolved_root = root.resolve()
            if not resolved_root.is_relative_to(account_root):
                continue
            if resolved_root in seen_roots:
                continue
            seen_roots.add(resolved_root)
            try:
                candidates = root.rglob("*")
                for candidate in candidates:
                    try:
                        if not candidate.is_file():
                            continue
                        resolved = candidate.resolve()
                        if not resolved.is_relative_to(account_root):
                            continue
                        if resolved in seen_files:
                            continue
                        size = candidate.stat().st_size
                    except OSError:
                        continue
                    seen_files.add(resolved)
                    decoded_name = urllib.parse.unquote(candidate.name)
                    entry = _IndexedFile(
                        path=candidate,
                        name_key=decoded_name.casefold(),
                        stem_key=Path(decoded_name).stem.casefold(),
                        size=size,
                    )
                    self._entries.append(entry)
                    self._by_name.setdefault(entry.name_key, []).append(entry)
                    self._by_size.setdefault(size, []).append(entry)
            except OSError:
                continue
        self._entries.sort(key=lambda item: str(item.path).casefold())
        self.indexed_file_count = len(self._entries)

    def resolve(self, message: Message) -> Path | None:
        reference = message.attachment
        if reference is None:
            return None
        direct = self._direct_candidate(message, reference)
        if direct is not None:
            return direct
        filename_key = urllib.parse.unquote(Path(reference.filename).name).casefold()
        stem_key = Path(filename_key).stem
        candidates: set[_IndexedFile] = set(self._by_name.get(filename_key, ()))
        if reference.size is not None:
            candidates.update(self._by_size.get(reference.size, ()))
        identifiers = tuple(
            value.casefold()
            for value in (reference.md5, reference.attachment_id, reference.file_key)
            if len(value.strip()) >= 8
        )
        if identifiers:
            for entry in self._entries:
                path_key = str(entry.path).casefold()
                if any(value in path_key for value in identifiers):
                    candidates.add(entry)
        if not candidates:
            return None

        expected_md5 = _normalise_md5(reference.md5)
        if expected_md5:
            md5_matches = [
                entry
                for entry in candidates
                if self._file_md5(entry.path) == expected_md5
            ]
            if not md5_matches:
                return None
            candidates = set(md5_matches)

        month = message.datetime.strftime("%Y-%m") if message.timestamp > 0 else ""

        def score(entry: _IndexedFile) -> tuple[int, str]:
            value = 0
            if entry.name_key == filename_key:
                value += 100
            if stem_key and entry.stem_key == stem_key:
                value += 35
            if reference.size is not None and entry.size == reference.size:
                value += 30
            if reference.extension and entry.path.suffix.casefold().lstrip(".") == reference.extension.casefold():
                value += 8
            if month and month in str(entry.path):
                value += 12
            path_key = str(entry.path).casefold()
            if identifiers and any(identifier in path_key for identifier in identifiers):
                value += 80
            if expected_md5 and self._hash_cache.get(entry.path) == expected_md5:
                value += 250
            return value, str(entry.path).casefold()

        ranked = sorted(candidates, key=lambda item: (-score(item)[0], score(item)[1]))
        if len(ranked) > 1 and score(ranked[0])[0] == score(ranked[1])[0]:
            return None
        return ranked[0].path

    def _direct_candidate(
        self,
        message: Message,
        reference: AttachmentReference,
    ) -> Path | None:
        filename = Path(urllib.parse.unquote(reference.filename)).name
        if not filename:
            return None
        month = message.datetime.strftime("%Y-%m") if message.timestamp > 0 else ""
        compact_month = message.datetime.strftime("%Y%m") if message.timestamp > 0 else ""
        account_dir = self.account.account_dir
        candidates = [
            account_dir / "msg" / "migrate" / "File" / filename,
            account_dir / "msg" / "migrate" / "file" / filename,
        ]
        for base in (account_dir / "msg" / "file", account_dir / "msg" / "File"):
            candidates.append(base / filename)
            if month:
                candidates.extend(
                    (
                        base / month / filename,
                        base / month / "File" / filename,
                        base / compact_month / filename,
                    )
                )
        if message.conversation_id:
            chat_hash = hashlib.md5(message.conversation_id.encode("utf-8")).hexdigest()
            chat_root = account_dir / "msg" / "attach" / chat_hash
            candidates.extend((chat_root / "File" / filename, chat_root / "file" / filename))
            if month:
                candidates.extend(
                    (
                        chat_root / month / "File" / filename,
                        chat_root / month / "file" / filename,
                        chat_root / compact_month / "File" / filename,
                    )
                )
        expected_md5 = _normalise_md5(reference.md5)
        account_root = self.account.account_dir.resolve()
        for candidate in candidates:
            try:
                if not candidate.is_file():
                    continue
                if not candidate.resolve().is_relative_to(account_root):
                    continue
                if reference.size is not None and candidate.stat().st_size != reference.size:
                    continue
            except OSError:
                continue
            if expected_md5 and self._file_md5(candidate) != expected_md5:
                continue
            return candidate
        return None

    def _file_md5(self, path: Path) -> str:
        cached = self._hash_cache.get(path)
        if cached is not None:
            return cached
        digest = hashlib.md5()
        try:
            with path.open("rb") as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(chunk)
            value = digest.hexdigest()
        except OSError:
            value = ""
        self._hash_cache[path] = value
        return value


@dataclass(frozen=True, slots=True)
class ChatFileArchiveResult:
    path: Path
    message_count: int
    status_counts: dict[str, int]


def export_conversation_attachments(
    *,
    output_dir: Path,
    conversation: Conversation,
    messages: Iterable[Message],
    resolver: AttachmentResolver,
    categories: frozenset[str],
    max_file_size_bytes: int,
    start_timestamp: int = 0,
    end_timestamp: int = 0,
    progress: Callable[[str, float], None] | None = None,
    check_cancelled: Callable[[], None] | None = None,
) -> ChatFileArchiveResult:
    if not categories.issubset(FILE_CATEGORIES):
        raise ValueError("聊天文件类型筛选包含未知类别。")
    if max_file_size_bytes < 0:
        raise ValueError("单个文件最大体积不能小于 0。")

    output_dir.mkdir(parents=True, exist_ok=True)
    file_messages = [message for message in messages if message.attachment is not None]
    display_name = safe_filename(conversation.display_name)[:60]
    base_name = safe_filename(
        f"{display_name}_聊天文件_{_date_range_tag(start_timestamp, end_timestamp)}"
    )
    final_path = _available_zip_path(output_dir, base_name)
    root_name = final_path.stem
    status_counts = {
        "exported": 0,
        "too_large": 0,
        "not_available_locally": 0,
        "unsupported": 0,
        "read_error": 0,
    }
    records: list[dict[str, object]] = []
    temporary_zip: Path | None = None
    published = False
    try:
        with tempfile.TemporaryDirectory(prefix=".聊天文件构建-", dir=output_dir) as temporary:
            temporary_dir = Path(temporary)
            root_dir = temporary_dir / root_name
            files_dir = root_dir / "files"
            files_dir.mkdir(parents=True)
            used_names: set[str] = set()
            total = len(file_messages)
            for index, message in enumerate(file_messages, start=1):
                if check_cancelled:
                    check_cancelled()
                reference = message.attachment
                assert reference is not None
                category = attachment_category(reference.extension)
                record = _base_index_record(message, reference)
                declared_size = reference.size
                if category not in categories:
                    record.update(
                        status="unsupported",
                        reason=f"本次未选择{_CATEGORY_LABELS[category]}",
                    )
                elif (
                    max_file_size_bytes > 0
                    and declared_size is not None
                    and declared_size > max_file_size_bytes
                ):
                    record.update(
                        status="too_large",
                        reason="微信记录中的文件大小超过本次单文件上限",
                    )
                else:
                    source = resolver.resolve(message)
                    if source is None:
                        record.update(
                            status="not_available_locally",
                            reason="本机未找到可读取的附件实体",
                        )
                    else:
                        try:
                            actual_size = source.stat().st_size
                            if max_file_size_bytes > 0 and actual_size > max_file_size_bytes:
                                record.update(
                                    status="too_large",
                                    reason="本机附件实际大小超过本次单文件上限",
                                )
                            else:
                                exported_name = _available_attachment_name(
                                    message,
                                    reference,
                                    used_names,
                                    max_length=max(
                                        48,
                                        min(140, 230 - len(str(files_dir))),
                                    ),
                                )
                                destination = files_dir / exported_name
                                _copy_file(source, destination, check_cancelled)
                                exported_size = destination.stat().st_size
                                record.update(
                                    exported_size=exported_size,
                                    path=f"files/{exported_name}",
                                    status="exported",
                                    reason=None,
                                )
                        except OSError as error:
                            record.update(
                                status="read_error",
                                reason=f"读取本机附件失败：{error}",
                            )
                status = str(record["status"])
                status_counts[status] += 1
                records.append(record)
                if progress:
                    progress(
                        f"已处理 {index}/{total} 个文件",
                        index / total if total else 1.0,
                    )

            (root_dir / "files.jsonl").write_text(
                "".join(
                    json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n"
                    for record in records
                ),
                encoding="utf-8",
                newline="\n",
            )
            (root_dir / "导出说明.txt").write_text(
                _readme_text(
                    conversation=conversation,
                    start_timestamp=start_timestamp,
                    end_timestamp=end_timestamp,
                    categories=categories,
                    max_file_size_bytes=max_file_size_bytes,
                    found=len(file_messages),
                    status_counts=status_counts,
                ),
                encoding="utf-8",
                newline="\n",
            )
            if check_cancelled:
                check_cancelled()
            with tempfile.NamedTemporaryFile(
                prefix=".聊天文件-",
                suffix=".zip.tmp",
                dir=output_dir,
                delete=False,
            ) as stream:
                temporary_zip = Path(stream.name)
            with zipfile.ZipFile(
                temporary_zip,
                "w",
                compression=zipfile.ZIP_DEFLATED,
                allowZip64=True,
            ) as archive:
                archive.writestr(f"{root_name}/files/", b"")
                for path in sorted(root_dir.rglob("*"), key=lambda item: str(item).casefold()):
                    if path.is_file():
                        archive.write(path, path.relative_to(temporary_dir).as_posix())
            if check_cancelled:
                check_cancelled()
            os.replace(temporary_zip, final_path)
            temporary_zip = None
            published = True
            if check_cancelled:
                check_cancelled()
        return ChatFileArchiveResult(final_path, len(file_messages), status_counts)
    except BaseException:
        if temporary_zip is not None:
            temporary_zip.unlink(missing_ok=True)
        if published:
            final_path.unlink(missing_ok=True)
        raise


def _base_index_record(
    message: Message,
    reference: AttachmentReference,
) -> dict[str, object]:
    return {
        "schema_version": FILE_INDEX_SCHEMA_VERSION,
        "message_id": message.stable_id,
        "timestamp": message.timestamp,
        "time": message.datetime.isoformat(timespec="seconds"),
        "sender_id": message.sender_id,
        "sender_name": message.sender_name,
        "original_filename": reference.filename,
        "extension": reference.extension,
        "declared_size": reference.size,
        "exported_size": None,
        "path": None,
        "status": "not_available_locally",
        "reason": None,
    }


def _available_attachment_name(
    message: Message,
    reference: AttachmentReference,
    used_names: set[str],
    *,
    max_length: int = 140,
) -> str:
    original = Path(reference.filename).name
    extension = _normalise_extension(reference.extension or Path(original).suffix)
    original_stem = Path(original).stem if Path(original).suffix else original
    suffix = f".{extension}" if extension else ""
    timestamp = message.datetime.strftime("%Y-%m-%d_%H%M%S")
    sender_limit = max(6, min(36, max_length - len(timestamp) - len(suffix) - 20))
    sender = safe_filename(message.sender_name, fallback="未知发送者")[:sender_limit]
    stem = safe_filename(original_stem, fallback="未命名附件")
    visible_prefix = f"{timestamp}_{sender}_"
    max_stem = max(8, max_length - len(visible_prefix) - len(suffix))
    candidate = f"{visible_prefix}{stem[:max_stem]}{suffix}"
    key = candidate.casefold()
    if key not in used_names:
        used_names.add(key)
        return candidate

    identity = safe_filename(message.stable_id, fallback="消息")[-20:]
    candidate = f"{visible_prefix}{stem[:max(6, max_stem - len(identity) - 3)]} [{identity}]{suffix}"
    ordinal = 2
    while candidate.casefold() in used_names:
        marker = f" [{identity}-{ordinal}]"
        candidate = f"{visible_prefix}{stem[:max(6, max_stem - len(marker))]}{marker}{suffix}"
        ordinal += 1
    used_names.add(candidate.casefold())
    return candidate


def _copy_file(
    source: Path,
    destination: Path,
    check_cancelled: Callable[[], None] | None,
) -> None:
    with source.open("rb") as source_stream, destination.open("xb") as destination_stream:
        while True:
            if check_cancelled:
                check_cancelled()
            chunk = source_stream.read(1024 * 1024)
            if not chunk:
                break
            destination_stream.write(chunk)


def _readme_text(
    *,
    conversation: Conversation,
    start_timestamp: int,
    end_timestamp: int,
    categories: frozenset[str],
    max_file_size_bytes: int,
    found: int,
    status_counts: dict[str, int],
) -> str:
    selected = "、".join(
        _CATEGORY_LABELS[category]
        for category in ("pdf", "word", "excel", "powerpoint", "archive", "other")
        if category in categories
    ) or "无"
    size_text = (
        "不限制"
        if max_file_size_bytes == 0
        else f"{max_file_size_bytes / (1024 * 1024):g} MB"
    )
    return (
        "微信聊天文件导出说明\n"
        "====================\n"
        f"聊天名称：{conversation.display_name}\n"
        f"聊天类型：{'群聊' if conversation.is_group else '联系人'}\n"
        f"微信会话 ID：{conversation.username}\n"
        f"本次导出时间：{datetime.now().astimezone().isoformat(timespec='seconds')}\n"
        f"聊天时间范围：{_date_range_text(start_timestamp, end_timestamp)}\n"
        f"文件类型筛选：{selected}\n"
        f"单个文件最大体积：{size_text}\n"
        f"找到文件消息：{found}\n"
        f"成功导出：{status_counts['exported']}\n"
        f"因过大跳过：{status_counts['too_large']}\n"
        f"本机找不到：{status_counts['not_available_locally']}\n"
        f"筛选未包含/不支持：{status_counts['unsupported']}\n"
        f"读取错误：{status_counts['read_error']}\n\n"
        "files.jsonl：一行一条文件消息的索引；未导出的附件也会保留状态和原因。\n"
        "files/：本次实际成功复制出的聊天文件。\n"
        "说明：本工具只读取当前微信账号目录内已存在的附件，不修改微信文件；"
        "未在本机缓存、无法可靠读取的附件不会伪装成成功导出。\n"
    )


def _available_zip_path(output_dir: Path, base_name: str) -> Path:
    candidate = output_dir / f"{base_name}.zip"
    ordinal = 2
    while candidate.exists():
        candidate = output_dir / f"{base_name} ({ordinal}).zip"
        ordinal += 1
    return candidate


def _date_range_tag(start_timestamp: int, end_timestamp: int) -> str:
    if not start_timestamp and not end_timestamp:
        return "全部日期"
    start = datetime.fromtimestamp(start_timestamp).strftime("%Y%m%d") if start_timestamp else "最早"
    end = datetime.fromtimestamp(end_timestamp).strftime("%Y%m%d") if end_timestamp else "最新"
    return f"{start}-{end}"


def _date_range_text(start_timestamp: int, end_timestamp: int) -> str:
    start = datetime.fromtimestamp(start_timestamp).strftime("%Y-%m-%d") if start_timestamp else "最早"
    end = datetime.fromtimestamp(end_timestamp).strftime("%Y-%m-%d") if end_timestamp else "最新"
    return f"{start} 至 {end}"


def _parse_xml(content: str) -> ElementTree.Element | None:
    value = content.strip()
    if not value:
        return None
    start = value.find("<")
    if start > 0:
        value = value[start:]
    try:
        return ElementTree.fromstring(value)
    except ElementTree.ParseError:
        return None


def _first_node(
    root: ElementTree.Element | None,
    name: str,
) -> ElementTree.Element | None:
    if root is None:
        return None
    wanted = name.casefold()
    for node in root.iter():
        if node.tag.rsplit("}", 1)[-1].casefold() == wanted:
            return node
    return None


def _node_text(
    root: ElementTree.Element | None,
    names: tuple[str, ...],
) -> str:
    if root is None:
        return ""
    wanted = {name.casefold() for name in names}
    for node in root.iter():
        if node.tag.rsplit("}", 1)[-1].casefold() in wanted:
            value = "".join(node.itertext()).strip()
            if value:
                return value
    return ""


def _first_node_text(
    roots: tuple[ElementTree.Element | None, ...],
    names: tuple[str, ...],
) -> str:
    for root in roots:
        value = _node_text(root, names)
        if value:
            return value
    return ""


def _normalise_extension(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", value.casefold().lstrip("."))[:16]


def _normalise_md5(value: str) -> str:
    normalised = value.strip().lower()
    return normalised if _HEX_MD5.fullmatch(normalised) else ""


def _as_int(value: object) -> int:
    try:
        return int(str(value).strip() or 0)
    except (TypeError, ValueError, OverflowError):
        return 0


def _positive_int(value: object) -> int | None:
    result = _as_int(value)
    return result if result > 0 else None
