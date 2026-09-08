from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence, Sized
from dataclasses import asdict, dataclass
from datetime import datetime
import hashlib
import html
import json
import mimetypes
import os
from pathlib import Path
import re
import shutil
import tempfile
import time
from typing import Any

from .faces import normalize_wechat_faces
from .jsonl_exporter import understandable_message_type
from .models import Conversation, HtmlExportOptions, Message


CHAT_HTML_SCHEMA = "wechat-chat-offline-archive"
CHAT_HTML_SCHEMA_VERSION = 1
DEFAULT_CHUNK_SIZE = 2_000
PROGRESS_MESSAGE_INTERVAL = 256
PROGRESS_PROBE_INTERVAL = 32
PROGRESS_TIME_INTERVAL_SECONDS = 0.2
MAX_WARNING_DETAILS = 100

_ASSET_ROOT = Path(__file__).with_name("html_assets")
_SYSTEM_TYPES = frozenset({10_000, 10_002, 266_287_972_401})
_SAFE_EXTENSION = re.compile(r"^[a-z0-9]{1,10}$")
_PLACEHOLDER_TEXT = frozenset(
    {
        "[图片]",
        "[动画表情]",
        "[视频]",
        "[语音]",
    }
)
_IMAGE_EXTENSIONS = {
    "JPEG": "jpg",
    "JPG": "jpg",
    "PNG": "png",
    "GIF": "gif",
    "WEBP": "webp",
    "BMP": "bmp",
    "AVIF": "avif",
}
_IMAGE_MIME_TYPES = {
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "png": "image/png",
    "gif": "image/gif",
    "webp": "image/webp",
    "bmp": "image/bmp",
    "avif": "image/avif",
}
_VIDEO_EXTENSIONS = frozenset({"mp4", "webm", "mov", "m4v"})


@dataclass(frozen=True, slots=True)
class ChatArchiveMedia:
    """A local media payload ready to copy into an offline chat archive.

    Existing ``PdfImage`` and ``ResolvedAvatar`` instances do not need to be
    converted first: the writer deliberately reads their common attributes by
    duck typing.  ``path`` is accepted only as a local file; URLs are never
    copied to the generated manifest.
    """

    kind: str
    data: bytes = b""
    path: Path | None = None
    extension: str = ""
    mime_type: str = ""
    filename: str = ""
    is_animated: bool = False
    status: str = "available"
    reason: str = ""
    fallback_data: bytes = b""
    fallback_extension: str = ""


@dataclass(frozen=True, slots=True)
class ChatHtmlArchiveResult:
    root: Path
    entrypoint: Path
    manifest: Path
    checksum_manifest: Path
    status: str
    message_count: int
    participant_count: int
    chunk_count: int
    exported_media_count: int
    missing_media_count: int
    warnings: tuple[str, ...] = ()


class ChatHtmlArchiveWriter:
    """Stream one conversation into a portable, file://-friendly archive."""

    def __init__(
        self,
        root: Path,
        conversation: Conversation,
        *,
        options: HtmlExportOptions | None = None,
        profiles: object | None = None,
        avatar_resolver: object | None = None,
        media_resolver: object | None = None,
        attachment_resolver: object | None = None,
        display_text: Callable[[str], str | object] | None = None,
        check_cancelled: Callable[[], None] | None = None,
        progress: Callable[[int, int | None], None] | None = None,
        chunk_size: int = DEFAULT_CHUNK_SIZE,
        total_messages: int | None = None,
        generated_at: datetime | None = None,
    ) -> None:
        self.root = Path(root)
        self.conversation = conversation
        self.options = options or HtmlExportOptions()
        self.profiles = profiles
        self.avatar_resolver = avatar_resolver
        self.media_resolver = media_resolver
        self.attachment_resolver = attachment_resolver
        self.display_text = display_text or normalize_wechat_faces
        self.check_cancelled = check_cancelled or (lambda: None)
        self.progress = progress
        self.chunk_size = max(1, int(chunk_size))
        self.total_messages = total_messages
        self.generated_at = (generated_at or datetime.now().astimezone()).isoformat(
            timespec="seconds"
        )

        self.assets_root = self.root / "assets"
        self.data_root = self.root / "data"
        self.resources_root = self.root / "resources"
        self._chunk_records: list[dict[str, object]] = []
        self._chunks: list[dict[str, object]] = []
        self._participants: dict[str, dict[str, object]] = {}
        self._participant_profiles: dict[str, object | None] = {}
        self._message_types: dict[str, int] = {}
        self._processed_count = 0
        self._message_count = 0
        self._exported_media_count = 0
        self._missing_media_count = 0
        self._warnings: list[str] = []
        self._suppressed_warning_count = 0
        self._resource_paths: dict[tuple[str, str], str] = {}
        self._checksums: dict[str, str] = {}
        self._first_timestamp: int | None = None
        self._last_timestamp: int | None = None
        self._last_progress_count = 0
        self._last_progress_at = time.perf_counter()
        self._closed = False

        self._prepare_root()

    def write(self, message: Message) -> bool:
        """Write one message; return ``False`` when settings exclude it."""

        if self._closed:
            raise RuntimeError("HTML archive writer is already closed")
        self.check_cancelled()
        self._processed_count += 1
        if not self.options.include_system_messages and _is_system_message(message):
            self._report_progress()
            return False

        participant_id, participant_name = self._ensure_participant(message)
        record = self._message_record(message, participant_id, participant_name)
        self._chunk_records.append(record)
        self._message_count += 1
        self._message_types[str(record["type"])] = (
            self._message_types.get(str(record["type"]), 0) + 1
        )
        if message.timestamp > 0:
            self._first_timestamp = (
                message.timestamp
                if self._first_timestamp is None
                else min(self._first_timestamp, message.timestamp)
            )
            self._last_timestamp = (
                message.timestamp
                if self._last_timestamp is None
                else max(self._last_timestamp, message.timestamp)
            )
        if len(self._chunk_records) >= self.chunk_size:
            self._flush_chunk()
        self._report_progress()
        return True

    def finish(self) -> ChatHtmlArchiveResult:
        if self._closed:
            raise RuntimeError("HTML archive writer is already closed")
        self.check_cancelled()
        if self._chunk_records or not self._chunks:
            self._flush_chunk()
        self._report_progress(force=True)
        self.check_cancelled()
        self._write_participant_avatars()

        manifest = self._manifest_payload()
        manifest_path = self.data_root / "manifest.json"
        self._write_text(
            manifest_path,
            json.dumps(manifest, ensure_ascii=False, indent=2, allow_nan=False),
        )
        manifest_script = self.data_root / "manifest.js"
        self._write_text(
            manifest_script,
            "window.WeChatArchive.setManifest("
            + _javascript_json(manifest)
            + ");\n",
        )

        template = (_ASSET_ROOT / "index.template.html").read_text(encoding="utf-8")
        entrypoint = self.root / "index.html"
        self._write_text(
            entrypoint,
            template.replace(
                "{{TITLE}}",
                html.escape(f"聊天记录 · {self.conversation.display_name}"),
            )
            .replace(
                "{{CONVERSATION_NAME}}",
                html.escape(self.conversation.display_name),
            )
            .replace(
                "{{CONVERSATION_KIND}}",
                "群聊记录" if self.conversation.is_group else "私聊记录",
            ),
        )
        checksum_manifest = self.root / "manifest-sha256.txt"
        self._write_checksum_manifest(checksum_manifest)
        self._closed = True
        return ChatHtmlArchiveResult(
            root=self.root,
            entrypoint=entrypoint,
            manifest=manifest_path,
            checksum_manifest=checksum_manifest,
            status="complete",
            message_count=self._message_count,
            participant_count=len(self._participants),
            chunk_count=len(self._chunks),
            exported_media_count=self._exported_media_count,
            missing_media_count=self._missing_media_count,
            warnings=self._result_warnings(),
        )

    def abort(self) -> None:
        """Remove only the empty/staging root accepted by this writer."""

        self._closed = True
        if self.root.exists():
            shutil.rmtree(self.root)

    def _prepare_root(self) -> None:
        if self.root.exists() and any(self.root.iterdir()):
            raise FileExistsError(f"HTML archive directory is not empty: {self.root}")
        self.assets_root.mkdir(parents=True, exist_ok=True)
        self.data_root.mkdir(parents=True, exist_ok=True)
        for name in ("avatars", "images", "stickers", "videos", "files"):
            (self.resources_root / name).mkdir(parents=True, exist_ok=True)
        for filename in ("viewer.css", "viewer.js"):
            source = _ASSET_ROOT / filename
            if not source.is_file():
                raise FileNotFoundError(f"Missing packaged HTML asset: {source}")
            target = self.assets_root / filename
            shutil.copyfile(source, target)
            self._remember_checksum(target, _file_sha256(target))

    def _message_record(
        self,
        message: Message,
        participant_id: str,
        participant_name: str,
    ) -> dict[str, object]:
        display, raw_text = _normalize_display_text(self.display_text, message.content)
        message_kind = understandable_message_type(message)
        media_items = self._message_media(message, message_kind)
        attachment = self._attachment_record(message)
        if (media_items or attachment) and display in _PLACEHOLDER_TEXT:
            display = ""

        time_value, date_value, clock_value = _message_times(message.timestamp)
        message_id = self._message_id(message.stable_id)
        record: dict[str, object] = {
            "id": message_id,
            "timestamp": message.timestamp,
            "datetime": time_value,
            "date": date_value,
            "clock": clock_value,
            "sender": participant_id,
            "sender_name": participant_name,
            "outgoing": message.is_outgoing is True,
            "direction_known": message.is_outgoing is not None,
            "type": message_kind,
            "text": display,
            "media": media_items,
            "attachment": attachment,
        }
        if raw_text is not None:
            record["raw_text"] = raw_text
        return record

    def _message_media(
        self,
        message: Message,
        message_kind: str,
    ) -> list[dict[str, object]]:
        kind = _resource_kind(message_kind, message)
        if kind not in {"image", "sticker", "video"}:
            return []
        enabled = {
            "image": self.options.include_images,
            "sticker": self.options.include_stickers,
            "video": self.options.include_videos,
        }[kind]
        if not enabled:
            return [
                {
                    "kind": kind,
                    "status": "omitted_by_settings",
                    "reason": _omitted_reason(kind),
                }
            ]

        try:
            resolved = _call_resolver(self.media_resolver, message)
        except (OSError, RuntimeError, ValueError) as error:
            self._warn(f"消息 {message.stable_id} 媒体解析失败：{error}")
            resolved = None
        items = _as_media_sequence(resolved)
        if not items:
            self._missing_media_count += 1
            return [
                {
                    "kind": kind,
                    "status": "missing",
                    "reason": _missing_reason(kind),
                }
            ]

        records: list[dict[str, object]] = []
        for item in items:
            record = self._copy_media_item(item, default_kind=kind)
            records.append(record)
        return records

    def _copy_media_item(
        self,
        item: object,
        *,
        default_kind: str,
    ) -> dict[str, object]:
        status = str(_value(item, "status", "available") or "available")
        available = _value(item, "available", None)
        if available is False or status in {
            "missing",
            "rejected",
            "network_disabled",
            "network_unavailable",
            "not_available_locally",
            "too_large",
        }:
            self._missing_media_count += 1
            return {
                "kind": default_kind,
                "status": "missing",
                "reason": str(_value(item, "reason", "媒体文件不可用") or "媒体文件不可用"),
            }

        extension = _media_extension(item, default_kind)
        if not extension:
            self._missing_media_count += 1
            return {
                "kind": default_kind,
                "status": "unsupported",
                "reason": "媒体格式不受离线查看器支持",
            }
        category = {
            "image": "images",
            "sticker": "stickers",
            "video": "videos",
            "file": "files",
            "avatar": "avatars",
        }.get(default_kind, "files")
        stored = self._store_media_payload(category, item, extension)
        if stored is None:
            self._missing_media_count += 1
            return {
                "kind": default_kind,
                "status": "missing",
                "reason": str(
                    _value(item, "reason", "媒体文件没有可用数据")
                    or "媒体文件没有可用数据"
                ),
            }
        path, digest, byte_count = stored
        mime_type = _media_mime_type(item, extension, default_kind)
        record: dict[str, object] = {
            "kind": default_kind,
            "status": "exported",
            "path": path,
            "mime_type": mime_type,
            "bytes": byte_count,
            "sha256": digest,
            "animated": bool(_value(item, "is_animated", False)),
        }
        fallback_data = _bytes_value(_value(item, "fallback_data", b""))
        fallback_extension = _safe_extension(
            str(_value(item, "fallback_extension", "") or "")
        )
        if fallback_data and fallback_extension:
            fallback_path, _ = self._store_resource(
                category, fallback_data, fallback_extension
            )
            record["fallback_path"] = fallback_path
        self._exported_media_count += 1
        return record

    def _attachment_record(self, message: Message) -> dict[str, object] | None:
        attachment = message.attachment
        if attachment is None or not self.options.include_file_metadata:
            return None
        record: dict[str, object] = {
            "filename": attachment.filename or "未命名文件",
            "extension": attachment.extension,
            "size": attachment.size,
            "status": "metadata_only",
        }
        if not self.options.include_file_binaries:
            return record
        try:
            resolved = _call_resolver(self.attachment_resolver, message)
        except (OSError, RuntimeError, ValueError) as error:
            self._warn(f"消息 {message.stable_id} 文件解析失败：{error}")
            resolved = None
        if resolved is None:
            record.update(status="missing", reason="文件原件在本机不可用")
            self._missing_media_count += 1
            return record
        exported = self._copy_media_item(resolved, default_kind="file")
        record.update(exported)
        record["filename"] = attachment.filename or str(
            _value(resolved, "filename", "未命名文件")
        )
        return record

    def _ensure_participant(self, message: Message) -> tuple[str, str]:
        participant_id = (message.sender_id or "__unknown_sender__").strip()
        if participant_id in self._participants:
            return participant_id, str(self._participants[participant_id]["name"])

        profile = _lookup_profile(
            self.profiles,
            participant_id,
            self.conversation.username if self.conversation.is_group else "",
        )
        name = _profile_display_name(
            profile,
            fallback=message.sender_name or participant_id,
            prefer_group_name=(
                self.conversation.is_group and self.options.prefer_group_member_names
            ),
        )
        fallback = _avatar_fallback(profile, participant_id, name)
        self._participants[participant_id] = {
            "id": participant_id,
            "name": name,
            "avatar": {
                "status": "placeholder",
                "path": None,
                "fallback_text": fallback[0],
                "fallback_background": fallback[1],
                "fallback_foreground": fallback[2],
            },
        }
        self._participant_profiles[participant_id] = profile
        return participant_id, name

    def _write_participant_avatars(self) -> None:
        for participant_id, participant in self._participants.items():
            self.check_cancelled()
            profile = self._participant_profiles.get(participant_id)
            if profile is None or self.avatar_resolver is None:
                continue
            try:
                resolved = _call_resolver(self.avatar_resolver, profile)
            except (OSError, RuntimeError, ValueError) as error:
                self._warn(f"{participant['name']} 的头像解析失败：{error}")
                continue
            if resolved is None or _value(resolved, "available", None) is False:
                reason = str(_value(resolved, "reason", "") or "")
                if reason:
                    participant["avatar"]["reason"] = reason  # type: ignore[index]
                continue
            extension = _media_extension(resolved, "avatar")
            if not extension:
                continue
            stored = self._store_media_payload("avatars", resolved, extension)
            if stored is None:
                continue
            path, digest, byte_count = stored
            participant["avatar"].update(  # type: ignore[union-attr]
                {
                    "status": "exported",
                    "path": path,
                    "mime_type": _media_mime_type(resolved, extension, "avatar"),
                    "bytes": byte_count,
                    "sha256": digest,
                }
            )

    def _store_resource(
        self,
        category: str,
        data: bytes,
        extension: str,
    ) -> tuple[str, str]:
        digest = hashlib.sha256(data).hexdigest()
        key = (category, digest)
        existing = self._resource_paths.get(key)
        if existing:
            return existing, digest
        safe_extension = _safe_extension(extension) or "bin"
        target = self.resources_root / category / f"{digest}.{safe_extension}"
        if not target.exists():
            target.write_bytes(data)
        relative = target.relative_to(self.root).as_posix()
        self._resource_paths[key] = relative
        self._checksums[relative] = digest
        return relative, digest

    def _store_media_payload(
        self,
        category: str,
        item: object,
        extension: str,
    ) -> tuple[str, str, int] | None:
        data = _media_inline_bytes(item)
        if data:
            path, digest = self._store_resource(category, data, extension)
            return path, digest, len(data)
        source = _media_local_path(item)
        if source is None:
            return None
        return self._store_resource_path(category, source, extension)

    def _store_resource_path(
        self,
        category: str,
        source: Path,
        extension: str,
    ) -> tuple[str, str, int] | None:
        """Copy a local resource once while hashing it, without loading it in RAM."""

        safe_extension = _safe_extension(extension) or "bin"
        target_root = self.resources_root / category
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=".resource-",
            suffix=".tmp",
            dir=target_root,
        )
        os.close(descriptor)
        temporary = Path(temporary_name)
        digest = hashlib.sha256()
        byte_count = 0
        try:
            try:
                input_stream = source.open("rb")
            except OSError:
                temporary.unlink(missing_ok=True)
                return None
            source_failed = False
            with input_stream, temporary.open("wb") as output_stream:
                while True:
                    self.check_cancelled()
                    try:
                        block = input_stream.read(1024 * 1024)
                    except OSError:
                        source_failed = True
                        break
                    if not block:
                        break
                    digest.update(block)
                    output_stream.write(block)
                    byte_count += len(block)
            if source_failed:
                temporary.unlink(missing_ok=True)
                return None
            if byte_count <= 0:
                temporary.unlink(missing_ok=True)
                return None

            digest_value = digest.hexdigest()
            key = (category, digest_value)
            existing = self._resource_paths.get(key)
            if existing:
                temporary.unlink(missing_ok=True)
                return existing, digest_value, byte_count

            target = target_root / f"{digest_value}.{safe_extension}"
            if target.exists():
                temporary.unlink(missing_ok=True)
            else:
                temporary.replace(target)
            relative = target.relative_to(self.root).as_posix()
            self._resource_paths[key] = relative
            self._checksums[relative] = digest_value
            return relative, digest_value, byte_count
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise

    def _flush_chunk(self) -> None:
        self.check_cancelled()
        chunk_number = len(self._chunks) + 1
        chunk_id = f"messages-{chunk_number:06d}"
        records = self._chunk_records
        filename = f"{chunk_id}.js"
        target = self.data_root / filename
        self._write_text(
            target,
            f"window.WeChatArchive.registerChunk({json.dumps(chunk_id)},"
            + _javascript_json(records)
            + ");\n",
        )
        first = records[0] if records else None
        last = records[-1] if records else None
        self._chunks.append(
            {
                "id": chunk_id,
                "path": f"data/{filename}",
                "count": len(records),
                "first_timestamp": first["timestamp"] if first else None,
                "last_timestamp": last["timestamp"] if last else None,
                "first_date": first["date"] if first else None,
                "last_date": last["date"] if last else None,
            }
        )
        self._chunk_records = []

    def _manifest_payload(self) -> dict[str, object]:
        first_datetime, first_date, _ = _message_times(self._first_timestamp or 0)
        last_datetime, last_date, _ = _message_times(self._last_timestamp or 0)
        del first_datetime, last_datetime
        options = asdict(self.options)
        return {
            "schema": CHAT_HTML_SCHEMA,
            "schema_version": CHAT_HTML_SCHEMA_VERSION,
            "generated_at": self.generated_at,
            "conversation": {
                "id": self.conversation.username,
                "name": self.conversation.display_name,
                "type": "group" if self.conversation.is_group else "contact",
            },
            "summary": {
                "messages": self._message_count,
                "participants": len(self._participants),
                "chunks": len(self._chunks),
                "media_exported": self._exported_media_count,
                "media_missing": self._missing_media_count,
                "first_date": first_date if self._first_timestamp else None,
                "last_date": last_date if self._last_timestamp else None,
            },
            "settings": options,
            "participants": list(self._participants.values()),
            "message_types": [
                {"type": key, "label": _type_label(key), "count": value}
                for key, value in sorted(self._message_types.items())
            ],
            "chunks": self._chunks,
        }

    def _write_checksum_manifest(self, target: Path) -> None:
        files = sorted(
            (
                path
                for path in self.root.rglob("*")
                if path.is_file() and path != target
            ),
            key=lambda path: path.relative_to(self.root).as_posix(),
        )
        target.write_text(
            "".join(
                f"{self._checksums.get(path.relative_to(self.root).as_posix()) or _file_sha256(path)} "
                f"*{path.relative_to(self.root).as_posix()}\n"
                for path in files
            ),
            encoding="utf-8",
        )

    def _write_text(self, target: Path, value: str) -> None:
        data = value.encode("utf-8")
        target.write_bytes(data)
        self._remember_checksum(target, hashlib.sha256(data).hexdigest())

    def _remember_checksum(self, target: Path, digest: str) -> None:
        self._checksums[target.relative_to(self.root).as_posix()] = digest

    def _message_id(self, value: str) -> str:
        """Return a unique traceable id using O(1) writer state."""

        base = value or "message"
        return f"{base}#{self._message_count + 1}"

    def _warn(self, message: str) -> None:
        if len(self._warnings) < MAX_WARNING_DETAILS:
            self._warnings.append(message)
        else:
            self._suppressed_warning_count += 1

    def _result_warnings(self) -> tuple[str, ...]:
        values = list(self._warnings)
        if self._suppressed_warning_count:
            values.append(
                f"另有 {self._suppressed_warning_count} 条重复或同类详细警告已省略；"
                "缺失媒体总数仍已记入归档摘要。"
            )
        return tuple(values)

    def _report_progress(self, *, force: bool = False) -> None:
        if self.progress is None:
            return
        processed = self._processed_count
        since_last = processed - self._last_progress_count
        if not force and processed != 1:
            if since_last < PROGRESS_PROBE_INTERVAL:
                return
            now = time.perf_counter()
            if (
                since_last < PROGRESS_MESSAGE_INTERVAL
                and now - self._last_progress_at < PROGRESS_TIME_INTERVAL_SECONDS
            ):
                return
        else:
            now = time.perf_counter()
        self.progress(processed, self.total_messages)
        self._last_progress_count = processed
        self._last_progress_at = now


def export_chat_html_archive(
    *,
    root: Path,
    conversation: Conversation,
    messages: Iterable[Message],
    options: HtmlExportOptions | None = None,
    profiles: object | None = None,
    avatar_resolver: object | None = None,
    media_resolver: object | None = None,
    attachment_resolver: object | None = None,
    display_text: Callable[[str], str | object] | None = None,
    check_cancelled: Callable[[], None] | None = None,
    progress: Callable[[int, int | None], None] | None = None,
    chunk_size: int = DEFAULT_CHUNK_SIZE,
) -> ChatHtmlArchiveResult:
    """Create one complete offline HTML archive below an empty ``root``.

    ``profiles`` may be a mapping or a store exposing ``profile_for``.
    Resolvers may be callables or objects exposing ``resolve``.  This keeps the
    archive layer independent from database and network policy while ensuring
    that only resolved local bytes enter the output.
    """

    total = len(messages) if isinstance(messages, Sized) else None
    writer: ChatHtmlArchiveWriter | None = None
    try:
        writer = ChatHtmlArchiveWriter(
            root,
            conversation,
            options=options,
            profiles=profiles,
            avatar_resolver=avatar_resolver,
            media_resolver=media_resolver,
            attachment_resolver=attachment_resolver,
            display_text=display_text,
            check_cancelled=check_cancelled,
            progress=progress,
            chunk_size=chunk_size,
            total_messages=total,
        )
        for message in messages:
            writer.write(message)
        return writer.finish()
    except BaseException:
        if writer is not None:
            writer.abort()
        raise


def _lookup_profile(store: object | None, username: str, group_id: str) -> object | None:
    if store is None:
        return None
    profile_for = getattr(store, "profile_for", None)
    if callable(profile_for):
        try:
            return profile_for(username, group_id=group_id)
        except TypeError:
            return profile_for(username)
    if isinstance(store, Mapping):
        return store.get(username)
    profiles = getattr(store, "profiles", None)
    if isinstance(profiles, Mapping):
        return profiles.get(username)
    for method_name in ("get_profile", "get"):
        method = getattr(store, method_name, None)
        if callable(method):
            return method(username)
    return None


def _profile_display_name(
    profile: object | None,
    *,
    fallback: str,
    prefer_group_name: bool,
) -> str:
    if profile is None:
        return fallback.strip() or "未知成员"
    candidates: list[object] = []
    if prefer_group_name:
        candidates.extend((_value(profile, "group_alias", ""), _value(profile, "group_name", "")))
    candidates.extend(
        (
            _value(profile, "remark", ""),
            _value(profile, "nickname", ""),
            _value(profile, "display_name", ""),
            _value(profile, "alias", ""),
            fallback,
            _value(profile, "username", ""),
        )
    )
    return next((str(value).strip() for value in candidates if str(value or "").strip()), "未知成员")


def _avatar_fallback(
    profile: object | None,
    participant_id: str,
    display_name: str,
) -> tuple[str, str, str]:
    fallback = _value(profile, "avatar_fallback", None) if profile is not None else None
    text = str(_value(fallback, "text", "") or "").strip()
    background = str(_value(fallback, "background", "") or "").strip()
    foreground = str(_value(fallback, "foreground", "") or "").strip()
    if not text:
        visible = [character for character in display_name if not character.isspace()]
        text = (visible[0] if visible else "?").upper()
    if not background:
        digest = hashlib.sha256(participant_id.encode("utf-8", "replace")).digest()
        hue = int.from_bytes(digest[:2], "big") % 360
        background = f"hsl({hue} 44% 44%)"
    return text[:2], background, foreground or "#ffffff"


def _call_resolver(resolver: object | None, value: object) -> object | None:
    if resolver is None:
        return None
    if callable(resolver):
        return resolver(value)
    method = getattr(resolver, "resolve", None)
    if callable(method):
        return method(value)
    raise TypeError("resolver must be callable or expose resolve()")


def _normalize_display_text(
    normalizer: Callable[[str], str | object],
    source: str,
) -> tuple[str, str | None]:
    result = normalizer(source)
    if isinstance(result, str):
        return result, source if result != source else None
    text = str(_value(result, "text", source) or "")
    raw_text = _value(result, "raw_text", None)
    if raw_text is None and text != source:
        raw_text = source
    return text, str(raw_text) if raw_text is not None else None


def _resource_kind(message_kind: str, message: Message) -> str:
    if message_kind == "image" or message.message_type == 3:
        return "image"
    if message_kind == "sticker" or message.message_type == 47:
        return "sticker"
    if message_kind == "video" or message.message_type == 43:
        return "video"
    return ""


def _is_system_message(message: Message) -> bool:
    return message.message_type in _SYSTEM_TYPES or understandable_message_type(message) == "system"


def _as_media_sequence(value: object | None) -> tuple[object, ...]:
    if value is None:
        return ()
    if isinstance(value, Sequence) and not isinstance(
        value, (str, bytes, bytearray, memoryview)
    ):
        return tuple(value)
    return (value,)


def _media_inline_bytes(item: object) -> bytes:
    if isinstance(item, bytes):
        return item
    if isinstance(item, (bytearray, memoryview)):
        return bytes(item)
    return _bytes_value(_value(item, "data", b""))


def _media_local_path(item: object) -> Path | None:
    path_value = _value(item, "path", None)
    if isinstance(path_value, Path):
        path = path_value
    elif isinstance(path_value, str) and path_value and "://" not in path_value:
        path = Path(path_value)
    else:
        return None
    try:
        return path if path.is_file() and path.stat().st_size > 0 else None
    except OSError:
        return None


def _media_extension(item: object, kind: str) -> str:
    extension = _safe_extension(str(_value(item, "extension", "") or ""))
    if extension:
        return extension
    image_format = str(_value(item, "image_format", "") or "").upper()
    if image_format in _IMAGE_EXTENSIONS:
        return _IMAGE_EXTENSIONS[image_format]
    mime_type = str(_value(item, "mime_type", "") or "").lower()
    guessed = mimetypes.guess_extension(mime_type, strict=False) if mime_type else ""
    if guessed:
        extension = _safe_extension(guessed)
        if extension:
            return extension
    if kind == "file":
        return _safe_extension(str(_value(item, "filename", "")).rsplit(".", 1)[-1])
    return ""


def _media_mime_type(item: object, extension: str, kind: str) -> str:
    mime_type = str(_value(item, "mime_type", "") or "").strip().lower()
    if mime_type and re.fullmatch(r"[a-z0-9.+-]+/[a-z0-9.+-]+", mime_type):
        if mime_type != "image/svg+xml":
            return mime_type
    if extension in _IMAGE_MIME_TYPES:
        return _IMAGE_MIME_TYPES[extension]
    if kind == "video" and extension in _VIDEO_EXTENSIONS:
        return mimetypes.guess_type(f"x.{extension}")[0] or "video/mp4"
    return "application/octet-stream"


def _safe_extension(value: str) -> str:
    cleaned = str(value or "").strip().lower().lstrip(".")
    return cleaned if _SAFE_EXTENSION.fullmatch(cleaned) else ""


def _bytes_value(value: object) -> bytes:
    if isinstance(value, bytes):
        return value
    if isinstance(value, (bytearray, memoryview)):
        return bytes(value)
    return b""


def _value(item: object | None, name: str, default: object) -> object:
    if item is None:
        return default
    if isinstance(item, Mapping):
        return item.get(name, default)
    try:
        return getattr(item, name)
    except (AttributeError, TypeError):
        return default


def _message_times(timestamp: int) -> tuple[str, str, str]:
    if timestamp <= 0:
        return "时间未知", "日期未知", "时间未知"
    try:
        value = datetime.fromtimestamp(timestamp).astimezone()
    except (OSError, OverflowError, ValueError):
        return "时间未知", "日期未知", "时间未知"
    return (
        value.isoformat(timespec="seconds"),
        value.strftime("%Y-%m-%d"),
        value.strftime("%H:%M:%S"),
    )


def _omitted_reason(kind: str) -> str:
    return {
        "image": "导出设置未包含图片",
        "sticker": "导出设置未包含动态表情",
        "video": "导出设置未包含视频",
    }.get(kind, "导出设置未包含此媒体")


def _missing_reason(kind: str) -> str:
    return {
        "image": "图片在本机不可用",
        "sticker": "动态表情在本机不可用",
        "video": "视频在本机不可用",
    }.get(kind, "媒体在本机不可用")


def _type_label(value: str) -> str:
    return {
        "text": "文字",
        "image": "图片",
        "sticker": "动态表情",
        "voice": "语音",
        "video": "视频",
        "file": "文件",
        "quote": "引用",
        "system": "系统消息",
        "chat_history": "聊天记录",
        "link": "链接",
        "location": "位置",
        "contact_card": "名片",
        "call": "通话",
        "mini_program": "小程序",
        "red_packet": "红包",
        "transfer": "转账",
        "unknown": "其他",
    }.get(value, value)


def _javascript_json(value: object) -> str:
    # U+2028/U+2029 are valid JSON but used to terminate lines in older JS
    # parsers.  Escaping '<' also prevents accidental HTML interpretation if a
    # data file is ever embedded by a downstream packager.
    return (
        json.dumps(
            value,
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        )
        .replace("<", "\\u003c")
        .replace("\u2028", "\\u2028")
        .replace("\u2029", "\\u2029")
    )


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
