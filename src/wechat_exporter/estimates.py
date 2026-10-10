"""Option-sensitive disk estimates from real serialization and measured media.

Only local resources are read. Image conversion lengths are cached separately
from message profiles. Unknown network resources are counted, not assigned a
speculative maximum. Profiles retain byte totals rather than message bodies.
"""
from __future__ import annotations

import io
import json
import mimetypes
import zlib
from collections.abc import Callable, Iterable
from dataclasses import dataclass, replace
from pathlib import Path

from .attachments import AttachmentResolver, attachment_category
from .chat_html_archive import ChatHtmlArchiveWriter, _ASSET_ROOT, _missing_reason, _omitted_reason
from .content import WECHAT_VOICE_TEXT_PREFIX
from .exporters import PdfTranscriptWriter, TxtTranscriptWriter
from .json_exporter import JsonTextTranscriptWriter
from .jsonl_package import ChatVideoLocator, _media_filename, package_message_record
from .image_probe import LocalImageSizeProbe
from .media import MediaResolver
from .native_face_assets import NATIVE_FACE_ROOT
from .models import AccountLocation, Conversation, ExportWorkload, HtmlExportOptions, Moment


@dataclass(frozen=True, slots=True)
class SizeEstimate:
    lower_bytes: int = 0
    upper_bytes: int = 0
    item_count: int = 0
    unknown_local: int = 0
    unknown_network: int = 0
    approximate: bool = False

    @property
    def byte_count(self) -> int:
        return (self.lower_bytes + self.upper_bytes) // 2

    @property
    def text(self) -> str:
        prefix = "已核算" if self.unknown_local else "已知资源" if self.unknown_network else ""
        about = "约 " if self.approximate or self.lower_bytes != self.upper_bytes else ""
        result = f"预计大小 {prefix}{about}{format_bytes(self.byte_count)}"
        if self.unknown_local:
            result += f" · {self.unknown_local:,} 项本机媒体未计入"
        if self.unknown_network:
            result += f" · {self.unknown_network:,} 项联网媒体大小待定"
        return result


def format_bytes(value: int) -> str:
    amount = float(max(0, value))
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if amount < 1024 or unit == "TB":
            return f"{amount:.0f} B" if unit == "B" else f"{amount:.1f} {unit}"
        amount /= 1024
    raise AssertionError("unreachable")


@dataclass(frozen=True, slots=True)
class _MediaSize:
    kind: str
    identity: str
    lower: int
    upper: int
    local: bool = True
    unknown: bool = False
    system: bool = False
    html_bytes: int = 0
    pdf_bytes: int = 0
    pdf_identity: str = ""
    conversation_id: str = ""
    placeholder_bytes: int = 0


@dataclass(frozen=True, slots=True)
class _FileSize:
    category: str
    declared_size: int | None
    local_size: int | None
    compressed_lower: int
    compressed_upper: int
    identity: str = ""
    system: bool = False
    conversation_id: str = ""


@dataclass(frozen=True, slots=True)
class ChatSizeProfile:
    workload: ExportWorkload
    conversation_count: int
    txt_bytes: int
    json_bytes: int
    structured_bytes: int
    compressed_bytes: int
    media: tuple[_MediaSize, ...] = ()
    files: tuple[_FileSize, ...] = ()
    selected_conversation_count: int = 0
    html_bytes: tuple[int, ...] = ()
    pdf_bytes: int = 0
    images_measured: bool = True

    def estimate(
        self, task: str, *, chat_format: str = "html", include_images: bool = True,
        include_videos: bool = False, allow_network: bool = False,
        include_files: bool = False, video_limit: int = 0,
        categories: frozenset[str] = frozenset(), file_limit: int = 0,
        include_system: bool = True, include_file_metadata: bool = True,
    ) -> SizeEstimate:
        count, conversations = self.workload.message_count, self.conversation_count
        if task == "chat_files":
            # Index, manifest and instructions are deflated in the ZIP too.
            lower = self.selected_conversation_count * 1100 + len(self.files) * 75
            upper = self.selected_conversation_count * 1300 + len(self.files) * 175
            exported = 0
            for item in self.files:
                if item.category not in categories or item.local_size is None:
                    continue
                if file_limit and max(item.local_size, item.declared_size or 0) > file_limit:
                    continue
                lower += item.compressed_lower + 200
                upper += item.compressed_upper + 300
                exported += 1
            return SizeEstimate(lower, upper, exported, approximate=True)
        if not count:
            return SizeEstimate()
        if task == "chat" and chat_format in {"txt", "json"}:
            total = self.txt_bytes if chat_format == "txt" else self.json_bytes
            return SizeEstimate(total, total)
        html = task == "chat" and chat_format == "html"
        pdf = task == "chat" and chat_format == "pdf"
        if task == "jsonl_package":
            # Text is deflated; images and video are ZIP_STORED.
            total = self.compressed_bytes + conversations * 1800
            lower, upper = max(0, total - conversations * 600), total + conversations * 600
        elif pdf:
            lower = upper = self.pdf_bytes
        else:
            total = self.html_bytes[(0 if include_system else 2) + (0 if include_file_metadata else 1)]
            lower = max(0, total - conversations * 1000)
            upper = total + conversations * 1000
        seen: set[tuple[str, str]] = set()
        unknown_local = unknown_network = 0
        for item in self.media:
            if pdf and item.kind == "video":
                continue
            if item.kind == "cover" and task != "jsonl_package":
                continue
            if html and not include_system and item.system:
                continue
            kind = "sticker" if item.kind == "emoticon" else item.kind
            enabled = (include_images if kind in {"image", "sticker"} else include_videos
                       if kind == "video" else task == "jsonl_package")
            if html:
                omitted = len(_json({"kind": kind, "status": "omitted_by_settings", "reason": _omitted_reason(kind)}))
                metadata = (item.html_bytes if enabled else omitted) - item.placeholder_bytes
                lower += metadata
                upper += metadata
            if not enabled:
                continue
            if item.kind == "cover" and (task != "jsonl_package" or not allow_network):
                continue
            if not item.local:
                if allow_network:
                    unknown_network += 1
                continue
            if item.kind == "video" and video_limit and item.upper > video_limit:
                continue
            if item.unknown:
                unknown_local += 1
                continue
            identity = (kind, item.conversation_id + ":" + (item.pdf_identity if pdf else item.identity))
            if html or pdf:
                if identity in seen:
                    continue
                seen.add(identity)
            if pdf:
                # Same RGB/alpha compression as ReportLab, plus page/object overhead.
                lower += item.pdf_bytes
                upper += item.pdf_bytes + 800
            else:
                lower += item.lower
                upper += item.upper
                if html and item.upper:
                    lower += 175  # resource checksum entry
                    upper += 175
                if task == "jsonl_package":
                    # Entry names and media status fields in the deflated JSONL.
                    lower += 180
                    upper += 300
        if html and include_files and include_file_metadata:
            for item in self.files:
                if not include_system and item.system:
                    continue
                lower += 250
                upper += 350
                identity = ("file", item.conversation_id + ":" + item.identity)
                if item.local_size and identity not in seen:
                    lower += item.local_size
                    upper += item.local_size
                    seen.add(identity)
        return SizeEstimate(lower, max(lower, upper), unknown_local=unknown_local,
                            unknown_network=unknown_network, approximate=html or pdf or task == "jsonl_package")


class _TextByteCounter:
    def __init__(self, *, bom: bool = False):
        self.byte_count = 3 if bom else 0
        self.closed = False

    def write(self, value: str) -> int:
        self.byte_count += len(value.encode("utf-8"))
        return len(value)

    def close(self) -> None:
        self.closed = True


class _HtmlMetadataCounter(ChatHtmlArchiveWriter):
    """Use the actual writer's text serialization, without creating an archive."""
    def _prepare_root(self) -> None:
        self.byte_count = 0
        for source in [*(_ASSET_ROOT / "icons").glob("*"),
                       _ASSET_ROOT / "viewer.css", _ASSET_ROOT / "viewer.js"]:
            self.byte_count += source.stat().st_size
            self._checksums[(Path("assets") / source.relative_to(_ASSET_ROOT)).as_posix()] = "0" * 64

    def _message_media(self, message, message_kind):
        return []

    def _copy_native_face(self, filename: str) -> None:
        self.byte_count += (NATIVE_FACE_ROOT / filename).stat().st_size
        self._checksums["assets/faces/" + filename] = "0" * 64
        if "assets/faces/NOTICE.txt" not in self._checksums:
            self.byte_count += (NATIVE_FACE_ROOT / "NOTICE.txt").stat().st_size
            self._checksums["assets/faces/NOTICE.txt"] = "0" * 64

    def _write_text(self, target: Path, value: str) -> None:
        self.byte_count += len(value.encode("utf-8"))
        self._remember_checksum(target, "0" * 64)

    def _write_checksum_manifest(self, target: Path) -> None:
        self.byte_count += sum(len(("0" * 64 + " *" + path + "\n").encode()) for path in self._checksums)


def build_chat_size_profile(
    archive: object, account: AccountLocation, conversations: tuple[Conversation, ...],
    *, start_timestamp: int = 0, end_timestamp: int = 0,
    check_cancelled: Callable[[], None] | None = None,
    progress: Callable[[int, int], None] | None = None,
    measure_images: bool = True,
) -> ChatSizeProfile:
    self_wxid = getattr(archive, "self_wxid", account.wxid)
    resolver = MediaResolver(account, self_wxid, allow_network=False)
    image_probe = LocalImageSizeProbe(account, self_wxid)
    videos = ChatVideoLocator(account)
    attachments: AttachmentResolver | None = None
    media: list[_MediaSize] = []
    files: list[_FileSize] = []
    count = images = emoticons = txt_bytes = json_bytes = structured_bytes = pdf_bytes = 0
    conversation_count = 0
    html_bytes = [0] * 4
    compressed_bytes = 0
    compression_samples: dict[Path, tuple[int, int]] = {}
    try:
        for conversation in conversations:
            local_count = 0
            compressor = zlib.compressobj()
            txt_counter, json_counter = _TextByteCounter(bom=True), _TextByteCounter()
            txt = TxtTranscriptWriter(Path("preview.txt"), conversation, start_timestamp=start_timestamp,
                                      end_timestamp=end_timestamp, output_stream=txt_counter)
            json_writer = JsonTextTranscriptWriter(Path("preview.json"), conversation, start_timestamp=start_timestamp,
                                                   end_timestamp=end_timestamp, output_stream=json_counter)
            pdf_buffer = io.BytesIO()
            pdf_writer = PdfTranscriptWriter(Path("preview.pdf"), conversation, start_timestamp=start_timestamp,
                                             end_timestamp=end_timestamp, output_stream=pdf_buffer)
            counters = [_HtmlMetadataCounter(Path("preview-html"), conversation,
                        options=HtmlExportOptions(include_system_messages=system, include_file_metadata=metadata),
                        profiles=getattr(archive, "profile_store", None), check_cancelled=check_cancelled)
                        for system, metadata in ((True, True), (True, False), (False, True), (False, False))]
            for message in archive.iter_messages(conversation, start_timestamp=start_timestamp, end_timestamp=end_timestamp):
                if check_cancelled:
                    check_cancelled()
                count += 1
                local_count += 1
                images += message.message_type == 3
                emoticons += message.message_type == 47
                if message.message_type == 34 and not message.content.startswith(WECHAT_VOICE_TEXT_PREFIX):
                    message = replace(message, content="[语音]（微信尚未生成转文字）")
                txt.write(message)
                json_writer.write(message)
                pdf_writer.write(message)
                for counter in counters:
                    counter.write(message)
                record = package_message_record(message, conversation)
                package_media = []
                reference = message.media
                system = message.message_type in {10000, 10002, 266287972401}
                if reference is not None:
                    path = None
                    measured = None
                    thumbnail = False
                    candidates = []
                    if reference.kind == "image":
                        candidates = resolver._find_image_dats(message, reference.md5)
                    elif reference.kind == "emoticon":
                        candidates = resolver._find_emoticon_candidates(reference)
                    elif reference.kind == "video":
                        path = videos.find(message)
                    for candidate, small in candidates:
                        measured = image_probe.measure(candidate, cdn_key=reference.aes_key,
                                                       check_cancelled=check_cancelled, cached_only=not measure_images)
                        if measured:
                            path, thumbnail = candidate, small
                            break
                    size = measured.byte_count if measured else _local_size(path, account) if reference.kind == "video" else None
                    kind = "sticker" if reference.kind == "emoticon" else reference.kind
                    if size is not None:
                        extension = ({"JPEG": "jpg", "PNG": "png", "GIF": "gif", "WEBP": "webp", "BMP": "bmp", "AVIF": "avif"}.get(measured.image_format, "png")
                                     if measured else path.suffix.lstrip("."))
                        digest = measured.digest if measured else str(path)
                        resource = f"resources/{ {'image':'images', 'sticker':'stickers', 'video':'videos'}[kind] }/{'0'*64}.{extension}"
                        media_record = {"kind": kind, "status": "exported", "path": resource,
                                        "mime_type": mimetypes.guess_type("x." + extension)[0] or "application/octet-stream",
                                        "bytes": size, "sha256": "0"*64, "animated": extension == "gif", "is_thumbnail": thumbnail}
                        metadata_size = len(_json(media_record))
                        media.append(_MediaSize(reference.kind, digest, size, size, system=system,
                                     html_bytes=metadata_size, pdf_bytes=measured.pdf_bytes if measured else 0,
                                     pdf_identity=measured.pdf_identity if measured else "", conversation_id=conversation.username,
                                     placeholder_bytes=len(message.content.encode()) if message.content in {"[图片]", "[动画表情]", "[视频]"} else 0))
                        folder = {"image": "images", "sticker": "stickers", "video": "videos"}[kind]
                        package_item = {"kind": kind, "status": "exported",
                                        "path": f"media/{folder}/{_media_filename(local_count, message, extension)}",
                                        "mime_type": media_record["mime_type"], "size": size,
                                        "source": "本机微信视频缓存" if kind == "video" else "本机表情缓存" if kind == "sticker" else "本机缩略图" if thumbnail else "本机原图缓存"}
                        if kind == "video":
                            package_item.update(duration_seconds=reference.duration_seconds, original_size=size)
                        else:
                            package_item.update(is_thumbnail=thumbnail, animated=extension == "gif")
                        package_media.append(package_item)
                    else:
                        network = reference.kind == "emoticon" and any((reference.cdn_url, reference.encrypted_url, reference.thumbnail_url))
                        pending = bool(candidates)
                        metadata_size = len(_json({"kind": kind, "status": "missing", "reason": _missing_reason(kind)}))
                        media.append(_MediaSize(reference.kind, reference.md5, 0, 0, local=pending or not network,
                                               unknown=pending or bool(network), system=system, html_bytes=metadata_size,
                                               conversation_id=conversation.username,
                                               placeholder_bytes=len(message.content.encode()) if message.content in {"[图片]", "[动画表情]", "[视频]"} else 0))
                        package_media.append({"kind": kind, "status": "not_available_locally", "path": None,
                            "reason": "当前微信账号的本机缓存中未找到视频；不会扫描账号目录之外的位置。" if kind == "video" else "本机缓存中未找到；默认未联网补全。"})
                details = record.get("details")
                if isinstance(details, dict) and details.get("cover_urls"):
                    media.append(_MediaSize("cover", message.stable_id, 0, 0, False, True))
                attachment = message.attachment
                if attachment is not None:
                    if attachments is None:
                        attachments = AttachmentResolver(account, conversations)
                    source = attachments.locate_for_estimate(message)
                    size = _local_size(source, account)
                    if source is not None and size is not None:
                        if source not in compression_samples:
                            compression_samples[source] = _compressed_file_range(source, size)
                        low, high = compression_samples[source]
                    else:
                        low = high = 0
                    files.append(_FileSize(attachment_category(attachment.extension), attachment.size, size,
                                          low, high, str(source or ""), system, conversation.username))
                record["media"] = package_media
                encoded = _json(record) + b"\n"
                structured_bytes += len(encoded)
                compressed_bytes += len(compressor.compress(encoded))
                if progress and (count == 1 or count % 20 == 0):
                    progress(count, images)
            compressed_bytes += len(compressor.flush())
            txt.close()
            json_writer.close()
            pdf_writer.close()
            if local_count:
                conversation_count += 1
                txt_bytes += txt_counter.byte_count
                json_bytes += json_counter.byte_count
                pdf_bytes += pdf_buffer.tell()
                for index, counter in enumerate(counters):
                    counter.finish()
                    html_bytes[index] += counter.byte_count
    finally:
        image_probe.flush()
    return ChatSizeProfile(ExportWorkload(count, images, emoticons), conversation_count, txt_bytes, json_bytes,
                           structured_bytes, compressed_bytes, tuple(media), tuple(files), len(conversations),
                           tuple(html_bytes), pdf_bytes, measure_images)


@dataclass(frozen=True, slots=True)
class MomentsSizeProfile:
    post_count: int
    media_count: int
    size: SizeEstimate


def build_moments_size_profile(moments: Iterable[Moment], account: AccountLocation, *,
                              check_cancelled: Callable[[], None] | None = None) -> MomentsSizeProfile:
    posts = media_count = total = unknown = 0
    resolver = MediaResolver(account, account.wxid, allow_network=False)
    probe = LocalImageSizeProbe(account, account.wxid)
    try:
        for moment in moments:
            if check_cancelled:
                check_cancelled()
            posts += 1
            total += len(moment.content.encode("utf-8")) * 3 + 1000
            for item in moment.media:
                media_count += 1
                path = (resolver._find_sns_video(item) if item.kind == "video"
                        else resolver._find_sns_image(item.md5) if item.md5 else None)
                measured = probe.measure(path, cdn_key=item.aes_key, check_cancelled=check_cancelled) if path and item.kind == "image" else None
                size = measured.byte_count if measured else max(0, item.total_size) or _local_size(path, account) or 0
                if size:
                    total += size + 500
                else:
                    unknown += 1
    finally:
        probe.flush()
    if posts:
        total += 25_000
    return MomentsSizeProfile(posts, media_count, SizeEstimate(total, total, unknown_network=unknown, approximate=bool(posts)))


def _json(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")


def _local_size(path: Path | None, account: AccountLocation) -> int | None:
    if path is not None:
        try:
            if path.resolve().is_relative_to(account.account_dir.resolve()) and path.is_file():
                return path.stat().st_size
        except OSError:
            pass
    return None


def _compressed_file_range(path: Path, size: int) -> tuple[int, int]:
    """Exact for small files; a bounded sample predicts larger ZIP entries."""
    try:
        with path.open("rb") as stream:
            if size <= 1024**2:
                compressor = zlib.compressobj(wbits=-15)
                total = len(compressor.compress(stream.read())) + len(compressor.flush())
                return total, total
            parts = []
            for offset in sorted({0, max(0, size // 2 - 16_384), max(0, size - 32_768)}):
                stream.seek(offset)
                parts.append(stream.read(32_768))
        ratios = [len(zlib.compress(part)) / max(1, len(part)) for part in parts]
        expected = min(size, round(size * sum(ratios) / len(ratios)))
        return max(0, round(expected * .9)), min(round(size * 1.001) + 100, round(expected * 1.1) + 100)
    except OSError:
        return round(size * .9), size + 100
