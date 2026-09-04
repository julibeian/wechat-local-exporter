from __future__ import annotations

import hashlib
import json
import mimetypes
import os
import shutil
import tempfile
import zipfile
from collections import Counter
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from uuid import uuid4

from .exporters import safe_filename
from .jsonl_exporter import SCHEMA_VERSION, message_to_json_record
from .media import MediaResolver
from .models import AccountLocation, Conversation, JsonlPackageRequest, Message


_VIDEO_SUFFIXES = frozenset({".mp4", ".mov", ".m4v", ".avi", ".mkv", ".webm"})
_IMAGE_EXTENSIONS = {
    "JPEG": ("jpg", "image/jpeg"),
    "PNG": ("png", "image/png"),
    "GIF": ("gif", "image/gif"),
    "WEBP": ("webp", "image/webp"),
    "BMP": ("bmp", "image/bmp"),
}


@dataclass(frozen=True, slots=True)
class JsonlPackageArchive:
    path: Path
    message_count: int
    status_counts: dict[str, int]


class ChatVideoLocator:
    """One-pass, account-scoped lookup for already cached chat video files."""

    def __init__(self, account: AccountLocation):
        self.account = account
        self._indexes: dict[str, tuple[Path, ...]] = {}
        self._global_files: tuple[Path, ...] | None = None

    def find(self, message: Message) -> Path | None:
        reference = message.media
        if reference is None or reference.kind != "video" or not message.conversation_id:
            return None
        chat_hash = hashlib.md5(message.conversation_id.encode("utf-8")).hexdigest()
        files = self._indexes.get(chat_hash)
        if files is None:
            files = self._build_index(chat_hash)
            self._indexes[chat_hash] = files

        names = {
            value.casefold()
            for value in (
                reference.md5,
                Path(reference.filename).name,
                Path(reference.filename).stem,
                str(message.server_id) if message.server_id > 0 else "",
                str(message.local_id),
            )
            if value
        }
        exact: list[Path] = []
        partial: list[Path] = []
        for candidate in files:
            filename = candidate.name.casefold()
            stem = candidate.stem.casefold()
            if filename in names or stem in names:
                exact.append(candidate)
            elif any(len(name) >= 8 and name in filename for name in names):
                partial.append(candidate)
        candidates = exact or partial
        if not candidates:
            return None
        if reference.size is not None:
            sized = []
            for candidate in candidates:
                try:
                    if candidate.stat().st_size == reference.size:
                        sized.append(candidate)
                except OSError:
                    continue
            if not sized:
                return None
            candidates = sized
        if len(candidates) > 1:
            month = message.datetime.strftime("%Y-%m").casefold()
            candidates.sort(key=lambda path: (month not in str(path).casefold(), str(path)))
        return candidates[0] if candidates else None

    def _build_index(self, chat_hash: str) -> tuple[Path, ...]:
        chat_files = self._scan_roots(
            (self.account.account_dir / "msg" / "attach" / chat_hash,)
        )
        if self._global_files is None:
            self._global_files = self._scan_roots(
                (
                    self.account.account_dir / "msg" / "Video",
                    self.account.account_dir / "msg" / "video",
                )
            )
        return tuple(dict.fromkeys((*chat_files, *self._global_files)))

    def _scan_roots(self, roots: Iterable[Path]) -> tuple[Path, ...]:
        account_root = self.account.account_dir.resolve()
        found: list[Path] = []
        seen: set[Path] = set()
        for root in roots:
            if not root.is_dir():
                continue
            try:
                candidates = root.rglob("*")
                for candidate in candidates:
                    try:
                        if not candidate.is_file() or candidate.suffix.casefold() not in _VIDEO_SUFFIXES:
                            continue
                        resolved = candidate.resolve()
                        if not resolved.is_relative_to(account_root) or resolved in seen:
                            continue
                    except OSError:
                        continue
                    seen.add(resolved)
                    found.append(candidate)
            except OSError:
                continue
        return tuple(found)


def export_conversation_jsonl_package(
    *,
    account: AccountLocation,
    self_wxid: str,
    conversation: Conversation,
    messages: Iterable[Message],
    output_dir: Path,
    request: JsonlPackageRequest,
    image_resolver: MediaResolver | None = None,
    video_locator: ChatVideoLocator | None = None,
    progress: Callable[[str, float], None] | None = None,
    check_cancelled: Callable[[], None] | None = None,
) -> JsonlPackageArchive:
    """Build one atomic ZIP whose JSONL rows point to media inside that ZIP."""

    if request.include_videos and request.max_video_size_bytes <= 0:
        raise ValueError("包含视频时，单个视频最大体积必须大于 0。")
    output_dir.mkdir(parents=True, exist_ok=True)
    resolver = image_resolver or MediaResolver(
        account,
        self_wxid,
        allow_network=request.allow_network_media,
    )
    locator = video_locator or ChatVideoLocator(account)
    final_path = _available_package_path(output_dir, conversation, request)
    temporary_zip = output_dir / f".{final_path.name}.{uuid4().hex}.tmp"
    status_counts: Counter[str] = Counter()
    message_count = 0

    try:
        with tempfile.TemporaryDirectory(prefix=".聊天资料包构建-", dir=output_dir) as temporary:
            root = Path(temporary)
            for relative in (
                "media/images",
                "media/stickers",
                "media/videos",
                "media/cards",
            ):
                (root / relative).mkdir(parents=True, exist_ok=True)

            messages_path = root / "messages.jsonl"
            with messages_path.open("w", encoding="utf-8", newline="\n") as stream:
                for index, message in enumerate(messages, start=1):
                    _check(check_cancelled)
                    record = message_to_json_record(message, conversation)
                    media_items = _export_message_media(
                        root=root,
                        index=index,
                        message=message,
                        record=record,
                        resolver=resolver,
                        locator=locator,
                        request=request,
                    )
                    record["media"] = media_items
                    if record.get("attachment") is not None:
                        original_attachment = dict(record["attachment"])  # type: ignore[arg-type]
                        attachment = {
                            key: original_attachment[key]
                            for key in (
                                "filename",
                                "extension",
                                "size",
                                "md5",
                                "attachment_id",
                            )
                            if key in original_attachment
                        }
                        attachment.update(
                            {
                                "included": False,
                                "export_via": "batch_chat_files",
                                "reason": "普通文件实体不装入资料包；请使用“批量导出聊天文件”。",
                            }
                        )
                        record["attachment"] = attachment
                    stream.write(
                        json.dumps(
                            record,
                            ensure_ascii=False,
                            separators=(",", ":"),
                            allow_nan=False,
                        )
                    )
                    stream.write("\n")
                    message_count = index
                    for item in media_items:
                        status_counts[str(item["status"])] += 1
                    if progress and (index == 1 or index % 100 == 0):
                        progress(f"已整理 {index} 条消息", 0.0)

            manifest = {
                "format": "wechat-jsonl-media-package",
                "format_version": 1,
                "jsonl_schema_version": SCHEMA_VERSION,
                "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
                "conversation": {
                    "id": conversation.username,
                    "name": conversation.display_name,
                    "type": "group" if conversation.is_group else "contact",
                },
                "range": {
                    "start_timestamp": request.start_timestamp or None,
                    "end_timestamp": request.end_timestamp or None,
                },
                "settings": {
                    "local_media_only": not request.allow_network_media,
                    "network_completion": request.allow_network_media,
                    "include_videos": request.include_videos,
                    "max_video_size_bytes": request.max_video_size_bytes,
                    "voice_policy": "wechat_transcript_only",
                    "ordinary_file_policy": "metadata_only",
                },
                "counts": {
                    "messages": message_count,
                    "media_status": dict(sorted(status_counts.items())),
                },
                "entrypoints": {
                    "messages": "messages.jsonl",
                    "instructions": "导出说明.txt",
                },
            }
            (root / "manifest.json").write_text(
                json.dumps(manifest, ensure_ascii=False, indent=2),
                encoding="utf-8",
                newline="\n",
            )
            (root / "导出说明.txt").write_text(
                _instructions(request),
                encoding="utf-8",
                newline="\n",
            )
            _check(check_cancelled)
            if progress:
                progress("正在压缩 JSONL 与媒体文件", 0.96)
            _write_zip(root, temporary_zip)
            _check(check_cancelled)
            os.replace(temporary_zip, final_path)
    except BaseException:
        temporary_zip.unlink(missing_ok=True)
        raise

    return JsonlPackageArchive(
        path=final_path,
        message_count=message_count,
        status_counts=dict(status_counts),
    )


def _export_message_media(
    *,
    root: Path,
    index: int,
    message: Message,
    record: dict[str, object],
    resolver: MediaResolver,
    locator: ChatVideoLocator,
    request: JsonlPackageRequest,
) -> list[dict[str, object]]:
    reference = message.media
    if reference is not None and reference.kind in {"image", "emoticon"}:
        folder = "images" if reference.kind == "image" else "stickers"
        kind = "image" if reference.kind == "image" else "sticker"
        resolved = resolver.resolve(message)
        if resolved is None:
            return [
                {
                    "kind": kind,
                    "status": "not_available_locally",
                    "path": None,
                    "reason": (
                        "本机缓存中未找到；默认未联网补全。"
                        if not request.allow_network_media
                        else "本机缓存和可用微信媒体地址均未取得。"
                    ),
                }
            ]
        extension, mime_type = _IMAGE_EXTENSIONS.get(
            resolved.image_format.upper(),
            ("bin", "application/octet-stream"),
        )
        filename = _media_filename(index, message, extension)
        relative = Path("media") / folder / filename
        (root / relative).write_bytes(resolved.data)
        return [
            {
                "kind": kind,
                "status": "exported",
                "path": relative.as_posix(),
                "mime_type": mime_type,
                "size": len(resolved.data),
                "source": resolved.source,
                "is_thumbnail": resolved.is_thumbnail,
                "animated": resolved.is_animated,
            }
        ]

    if reference is not None and reference.kind == "video":
        item: dict[str, object] = {
            "kind": "video",
            "path": None,
            "duration_seconds": reference.duration_seconds,
        }
        if not request.include_videos:
            item.update(status="not_requested", reason="本次导出未选择视频。")
            return [item]
        source = locator.find(message)
        if source is None:
            item.update(
                status="not_available_locally",
                reason="当前微信账号的本机缓存中未找到视频；不会扫描账号目录之外的位置。",
            )
            return [item]
        try:
            size = source.stat().st_size
        except OSError as error:
            item.update(status="read_error", reason=f"无法读取本机视频：{error}")
            return [item]
        item["original_size"] = size
        if request.max_video_size_bytes and size > request.max_video_size_bytes:
            item.update(
                status="too_large",
                reason=f"单个视频超过 {request.max_video_size_bytes} 字节上限。",
            )
            return [item]
        extension = source.suffix.casefold().lstrip(".") or "mp4"
        filename = _media_filename(index, message, extension)
        relative = Path("media") / "videos" / filename
        try:
            shutil.copyfile(source, root / relative)
        except OSError as error:
            item.update(status="read_error", reason=f"复制本机视频失败：{error}")
            return [item]
        item.update(
            status="exported",
            path=relative.as_posix(),
            mime_type=mimetypes.guess_type(source.name)[0] or "video/mp4",
            size=size,
            source="本机微信视频缓存",
        )
        return [item]

    details = record.get("details")
    if isinstance(details, dict) and details.get("cover_urls"):
        raw_urls = details["cover_urls"]
        urls = (
            [str(value) for value in raw_urls]
            if isinstance(raw_urls, list)
            else [str(raw_urls)]
        )
        cover = resolver.resolve_card_cover(urls)
        if cover is not None:
            extension, mime_type = _IMAGE_EXTENSIONS.get(
                cover.image_format.upper(),
                ("bin", "application/octet-stream"),
            )
            filename = _media_filename(index, message, extension)
            relative = Path("media") / "cards" / filename
            (root / relative).write_bytes(cover.data)
            return [
                {
                    "kind": "card_cover",
                    "status": "exported",
                    "path": relative.as_posix(),
                    "mime_type": mime_type,
                    "size": len(cover.data),
                    "source": cover.source,
                }
            ]
        return [
            {
                "kind": "card_cover",
                "status": "not_downloaded",
                "path": None,
                "reason": (
                    "未能从可用媒体地址取得卡片封面。"
                    if request.allow_network_media
                    else "默认不联网下载外部卡片封面。"
                ),
            }
        ]
    return []


def _media_filename(index: int, message: Message, extension: str) -> str:
    digest = hashlib.sha256(message.stable_id.encode("utf-8")).hexdigest()[:12]
    return f"{index:08d}_{digest}.{extension}"


def _available_package_path(
    output_dir: Path,
    conversation: Conversation,
    request: JsonlPackageRequest,
) -> Path:
    date_label = _date_label(request.start_timestamp, request.end_timestamp)
    base = safe_filename(
        f"{conversation.display_name}_完整聊天资料_{date_label}"
    )[:150]
    candidate = output_dir / f"{base}.zip"
    suffix = 2
    while candidate.exists():
        candidate = output_dir / f"{base} ({suffix}).zip"
        suffix += 1
    return candidate


def _date_label(start_timestamp: int, end_timestamp: int) -> str:
    if not start_timestamp and not end_timestamp:
        return "全部日期"
    start = datetime.fromtimestamp(start_timestamp).strftime("%Y%m%d") if start_timestamp else "最早"
    end = datetime.fromtimestamp(end_timestamp).strftime("%Y%m%d") if end_timestamp else "最新"
    return f"{start}-{end}"


def _instructions(request: JsonlPackageRequest) -> str:
    network = "已允许尝试微信官方媒体地址" if request.allow_network_media else "仅使用本机已有媒体，未联网补全"
    video = (
        f"包含本机已有且单个不超过 {request.max_video_size_bytes // (1024 * 1024)} MB 的视频"
        if request.include_videos
        else "不包含视频文件"
    )
    return (
        "微信聊天 AI 分析资料包\n"
        "========================\n\n"
        "1. messages.jsonl 每一物理行是一条独立消息，可直接交给支持 JSONL 的 AI 或数据工具。\n"
        "2. 每条消息的 media 数组指向 media/ 内文件；缺失、过大或未选择的媒体仍有明确状态。\n"
        "3. 图片和表情尽量保存本机已有原始字节；本资料包" + network + "。\n"
        "4. " + video + "；超过上限的视频不装入 ZIP，但消息和大小状态会保留。\n"
        "5. 语音只保存微信已经生成的转录文字，永远不装入原始语音文件，也不运行新的语音识别。\n"
        "6. Word、PDF、Excel 等普通文件只保留元数据，不装入资料包；需要实体文件请使用“批量导出聊天文件”。\n"
        "7. 不可可靠解析的消息会保留 type=unknown、原始消息类型、时间、发送者和可理解文字，不猜测缺失信息。\n"
    )


def _write_zip(root: Path, destination: Path) -> None:
    with zipfile.ZipFile(destination, "w", allowZip64=True) as archive:
        for directory in (
            "media/images/",
            "media/stickers/",
            "media/videos/",
            "media/cards/",
        ):
            archive.writestr(directory, b"", compress_type=zipfile.ZIP_STORED)
        for path in sorted(root.rglob("*")):
            if not path.is_file():
                continue
            relative = path.relative_to(root).as_posix()
            compression = (
                zipfile.ZIP_STORED
                if relative.startswith("media/")
                else zipfile.ZIP_DEFLATED
            )
            archive.write(path, relative, compress_type=compression)


def _check(callback: Callable[[], None] | None) -> None:
    if callback is not None:
        callback()
