from __future__ import annotations

import ctypes
import hashlib
import html
import io
import os
import re
import shutil
import struct
import subprocess
import tempfile
import threading
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from xml.etree import ElementTree

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from PIL import Image

from .models import (
    AccountLocation,
    MediaReference,
    Message,
    MomentMedia,
    MomentMediaFile,
    PdfImage,
)
from .windows import ProcessMemory, ProcessInfo, list_wechat_processes


_V1_MAGIC = b"\x07\x08\x05\x56\x02\x05"
_V2_MAGIC = b"\x07\x08\x56\x32\x08\x07"
_CFG_LANDMARK = b"global_config"
_HEX_MD5_RE = re.compile(rb"(?i)([0-9a-f]{32})")
_SUPPORTED_CDN_SUFFIXES = (
    ".qq.com",
    ".qpic.cn",
    ".wechat.com",
    ".weixin.qq.com",
)
_MAX_DOWNLOAD_BYTES = 512 * 1024 * 1024
_VIDEO_ENCRYPTED_PREFIX_BYTES = 128 * 1024
_MOMENT_USER_AGENTS = (
    "MicroMessenger Client",
    "Mozilla/5.0 WeChat-Archive-Exporter",
)


@dataclass(slots=True)
class MediaStats:
    requested: int = 0
    embedded: int = 0
    local_originals: int = 0
    cdn_originals: int = 0
    thumbnails: int = 0
    emoticons: int = 0
    wxgf_converted: int = 0
    moment_images: int = 0
    moment_videos: int = 0
    missing: int = 0
    issues: set[str] = field(default_factory=set)

    def summary(self) -> str:
        return (
            f"PDF 图片：已嵌入 {self.embedded}/{self.requested} 张"
            f"（本机原图 {self.local_originals}、缩略图 {self.thumbnails}、"
            f"表情图片 {self.emoticons}），缺失 {self.missing} 张。"
        )

    def moments_summary(self, *, fallback_count: int = 0) -> str:
        unavailable = max(0, self.missing - max(0, fallback_count))
        return (
            f"朋友圈媒体：已归档 {self.embedded}/{self.requested} 个"
            f"（照片 {self.moment_images}、视频 {self.moment_videos}；"
            f"微信 CDN 原文件 {self.cdn_originals}、本机缓存 {self.local_originals}、"
            f"缩略图兜底 {self.thumbnails}），"
            f"静态主图兜底 {max(0, fallback_count)} 个，仍缺失 {unavailable} 个。"
        )


def extract_media_reference(
    message_type: int,
    raw_content: str,
    packed_info: object = None,
) -> MediaReference | None:
    if message_type == 3:
        md5 = _first_md5(packed_info)
        if not md5:
            md5 = _xml_attribute(raw_content, "img", "md5")
        if not md5:
            return None
        return MediaReference(kind="image", md5=md5.lower())
    if message_type == 43:
        attributes = _xml_attributes(raw_content, "videomsg")
        md5 = (
            attributes.get("md5", "")
            or attributes.get("newmd5", "")
            or _first_md5(packed_info)
        ).lower()
        return MediaReference(
            kind="video",
            md5=md5,
            aes_key=attributes.get("aeskey", ""),
            cdn_url=attributes.get("cdnvideourl", ""),
            thumbnail_url=(
                attributes.get("cdnthumburl", "")
                or attributes.get("thumburl", "")
            ),
            size=_optional_int(attributes.get("length")),
            duration_seconds=_optional_float(attributes.get("playlength")),
            filename=attributes.get("filename", ""),
        )
    if message_type != 47:
        return None

    attributes = _xml_attributes(raw_content, "emoji")
    md5 = attributes.get("md5", "").lower()
    return MediaReference(
        kind="emoticon",
        md5=md5,
        aes_key=attributes.get("aeskey", ""),
        cdn_url=attributes.get("cdnurl", ""),
        encrypted_url=attributes.get("encrypturl", ""),
        thumbnail_url=attributes.get("thumburl", ""),
    )


def derive_image_keys(cfg_dword: int, wxid: str) -> tuple[bytes, int]:
    """Derive the account-level AES and XOR image keys used by WeChat 4.x."""
    aes_key = hashlib.md5(f"{cfg_dword}{wxid}".encode("utf-8")).hexdigest()[:16]
    return aes_key.encode("ascii"), cfg_dword & 0xFF


def discover_account_image_keys(
    account_dir: Path,
    wxid: str,
    processes: Iterable[ProcessInfo] | None = None,
) -> tuple[bytes, int] | None:
    """Read cfgDword from the logged-in process and validate the derived key."""
    probe = _find_v2_probe(account_dir)
    for process in processes if processes is not None else list_wechat_processes():
        try:
            candidates = _read_cfg_candidates(process.pid)
        except (OSError, PermissionError, ValueError):
            continue
        for cfg_dword, remote_wxid in candidates:
            if remote_wxid and remote_wxid != wxid:
                continue
            aes_key, xor_key = derive_image_keys(cfg_dword, wxid)
            if probe is None or _validate_v2_key(probe, aes_key):
                return aes_key, xor_key
    return None


class MediaResolver:
    """Resolve image messages to in-memory PDF attachments, without DB writes."""

    def __init__(
        self,
        account: AccountLocation,
        wxid: str,
        *,
        image_keys: tuple[bytes, int] | None = None,
        download: Callable[[str], bytes] | None = None,
        ffmpeg_executable: str | None = None,
        allow_network: bool = True,
    ):
        self.account = account
        self.wxid = wxid
        self.stats = MediaStats()
        self._image_keys = image_keys
        self._image_keys_checked = image_keys is not None
        self._download_override = download
        self.allow_network = allow_network
        self._ffmpeg_executable = ffmpeg_executable
        self._ffmpeg_checked = ffmpeg_executable is not None
        self._cache: dict[tuple[str, ...], PdfImage | None] = {}
        self._moment_file_cache: dict[tuple[str, ...], MomentMediaFile | None] = {}
        self._moment_file_inflight: set[tuple[str, ...]] = set()
        self._inflight: set[tuple[str, ...]] = set()
        self._attach_indexes: dict[str, dict[str, Path]] = {}
        self._sns_image_index: dict[str, Path] | None = None
        self._sns_size_index: dict[tuple[int, int, int], Path] | None = None
        self._cache_condition = threading.Condition()
        self._stats_lock = threading.Lock()
        self._image_key_lock = threading.Lock()
        self._attach_index_lock = threading.Lock()
        self._ffmpeg_lock = threading.Lock()

    def resolve(self, message: Message) -> PdfImage | None:
        reference = message.media
        if reference is None or reference.kind not in {"image", "emoticon"}:
            return None
        with self._stats_lock:
            self.stats.requested += 1
        cache_key = (
            message.conversation_id,
            reference.kind,
            reference.md5,
            reference.aes_key,
            reference.cdn_url,
            reference.encrypted_url,
            reference.thumbnail_url,
        )
        with self._cache_condition:
            while cache_key in self._inflight and cache_key not in self._cache:
                self._cache_condition.wait()
            if cache_key in self._cache:
                result = self._cache[cache_key]
                should_resolve = False
            else:
                self._inflight.add(cache_key)
                result = None
                should_resolve = True

        if should_resolve:
            try:
                result = (
                    self._resolve_local_image(message, reference)
                    if reference.kind == "image"
                    else self._resolve_emoticon(reference)
                )
            except BaseException:
                with self._cache_condition:
                    self._inflight.discard(cache_key)
                    self._cache_condition.notify_all()
                raise
            with self._cache_condition:
                self._cache[cache_key] = result
                self._inflight.discard(cache_key)
                self._cache_condition.notify_all()

        with self._stats_lock:
            if result is None:
                self.stats.missing += 1
                return None
            self.stats.embedded += 1
            if reference.kind == "emoticon":
                self.stats.emoticons += 1
            elif result.is_thumbnail:
                self.stats.thumbnails += 1
            else:
                self.stats.local_originals += 1
        return result

    def resolve_card_cover(self, urls: Iterable[str]) -> PdfImage | None:
        """Best-effort fetch for explicitly enabled, official WeChat card covers."""

        if not self.allow_network:
            return None
        for url in urls:
            if not url:
                continue
            try:
                data = self._download(url)
                if _image_format(data):
                    return _pdf_image(data, source="微信官方卡片封面")
            except (OSError, ValueError, RuntimeError):
                continue
        return None

    def resolve_moment(self, media: MomentMedia) -> PdfImage | None:
        """Resolve a Moments photo for the legacy PDF writer."""
        if media.kind != "image":
            return None
        resolved = self.resolve_moment_file(media)
        if resolved is None or not resolved.mime_type.startswith("image/"):
            return None
        try:
            return _pdf_image(
                resolved.data,
                source=resolved.source,
                is_thumbnail=resolved.is_thumbnail,
            )
        except ValueError:
            return None

    def resolve_moment_file(self, media: MomentMedia) -> MomentMediaFile | None:
        """Resolve an original Moments image/video for a static archive."""
        with self._stats_lock:
            self.stats.requested += 1
        cache_key = (
            "moment-file",
            media.kind,
            media.role,
            media.md5,
            media.original_url,
            media.thumbnail_url,
            media.token,
            media.thumbnail_token,
            media.aes_key,
            media.enc_idx,
        )
        with self._cache_condition:
            while (
                cache_key in self._moment_file_inflight
                and cache_key not in self._moment_file_cache
            ):
                self._cache_condition.wait()
            if cache_key in self._moment_file_cache:
                result = self._moment_file_cache[cache_key]
                should_resolve = False
            else:
                self._moment_file_inflight.add(cache_key)
                result = None
                should_resolve = True

        if should_resolve:
            try:
                result = self._resolve_moment_file(media)
            except BaseException:
                with self._cache_condition:
                    self._moment_file_inflight.discard(cache_key)
                    self._cache_condition.notify_all()
                raise
            with self._cache_condition:
                self._moment_file_cache[cache_key] = result
                self._moment_file_inflight.discard(cache_key)
                self._cache_condition.notify_all()

        with self._stats_lock:
            if result is None:
                self.stats.missing += 1
                return None
            self.stats.embedded += 1
            if media.kind == "video":
                self.stats.moment_videos += 1
            else:
                self.stats.moment_images += 1
            if result.is_thumbnail:
                self.stats.thumbnails += 1
            elif result.source.startswith("微信官方 CDN"):
                self.stats.cdn_originals += 1
            else:
                self.stats.local_originals += 1
        return result

    def _resolve_moment_file(self, media: MomentMedia) -> MomentMediaFile | None:
        if media.kind == "video":
            return self._resolve_moment_video(media)
        image = self._resolve_moment_image(media)
        if image is None:
            return None
        extension, mime_type = _image_file_type(image.data)
        fallback_data = (
            _animated_final_frame_png(image.data) if image.is_animated else None
        )
        return MomentMediaFile(
            data=image.data,
            extension=extension,
            mime_type=mime_type,
            source=image.source,
            is_thumbnail=image.is_thumbnail,
            is_animated=image.is_animated,
            fallback_data=fallback_data or b"",
            fallback_extension="png" if fallback_data else "",
            fallback_mime_type="image/png" if fallback_data else "",
            fallback_source="动画最后停止画面" if fallback_data else "",
        )

    def _add_issue(self, issue: str) -> None:
        with self._stats_lock:
            self.stats.issues.add(issue)

    def _resolve_local_image(
        self, message: Message, reference: MediaReference
    ) -> PdfImage | None:
        found = self._find_image_dat(message, reference.md5)
        if found is None:
            self._add_issue("部分图片尚未缓存在本机")
            return None
        dat_path, is_thumbnail = found
        try:
            encrypted = dat_path.read_bytes()
        except OSError:
            self._add_issue("部分本机图片文件无法读取")
            return None
        try:
            if _image_format(encrypted):
                image_data = encrypted
            else:
                keys = self._account_image_keys()
                if encrypted.startswith(_V2_MAGIC) and keys is None:
                    self._add_issue("未能从当前微信登录进程读取图片密钥")
                    return None
                aes_key, xor_key = keys or (b"", 0)
                image_data = decrypt_image_dat(encrypted, aes_key, xor_key)
            if image_data.startswith(b"wxgf"):
                converted = self._wxgf_to_png(image_data)
                if converted is None:
                    self._add_issue("缺少可用的 HEVC 解码器，部分 wxgf 图片未导出")
                    return None
                image_data = converted
                with self._stats_lock:
                    self.stats.wxgf_converted += 1
            source = "本机缩略图" if is_thumbnail else "本机原图缓存"
            return _pdf_image(image_data, source=source, is_thumbnail=is_thumbnail)
        except (OSError, ValueError, RuntimeError):
            self._add_issue("部分本机图片解密或解码失败")
            return None

    def _resolve_emoticon(self, reference: MediaReference) -> PdfImage | None:
        local = self._find_local_emoticon(reference.md5)
        if local is not None:
            try:
                data = local.read_bytes()
                if not _image_format(data) and reference.aes_key:
                    data = _decrypt_cdn_blob(data, reference.aes_key)
                if _image_format(data):
                    return _pdf_image(data, source="本机表情缓存")
            except (OSError, ValueError):
                pass

        if not self.allow_network:
            self._add_issue("部分表情图片尚未缓存在本机（未启用联网补全）")
            return None

        candidates = (
            (reference.cdn_url, False),
            (reference.encrypted_url, False),
            (reference.thumbnail_url, True),
        )
        for url, is_thumbnail in candidates:
            if not url:
                continue
            try:
                data = self._download(url)
                if not _image_format(data) and reference.aes_key:
                    data = _decrypt_cdn_blob(data, reference.aes_key)
                if not _image_format(data):
                    continue
                return _pdf_image(
                    data,
                    source="微信官方 CDN 缩略图" if is_thumbnail else "微信官方 CDN",
                    is_thumbnail=is_thumbnail,
                )
            except (OSError, ValueError, RuntimeError):
                continue
        self._add_issue("部分表情图片在本机和微信 CDN 均不可用")
        return None

    def _resolve_moment_image(self, media: MomentMedia) -> PdfImage | None:
        original_url = _with_token(
            media.original_url,
            media.token,
            media.enc_idx,
            original=True,
        )
        if original_url:
            image = self._download_moment_image(
                original_url,
                aes_key=media.aes_key,
                source="微信官方 CDN 原图",
                is_thumbnail=False,
            )
            if image is not None:
                return image

        local = self._find_sns_image(media.md5, media)
        if local is not None:
            try:
                data = local.read_bytes()
                image = self._decode_image_blob(
                    data,
                    aes_key=media.aes_key,
                    source="本机朋友圈缓存（原始字节）",
                    is_thumbnail=False,
                )
                if image is not None:
                    return image
            except OSError:
                self._add_issue("部分本机朋友圈图片文件无法读取")

        thumbnail_url = _with_token(
            media.thumbnail_url, media.thumbnail_token or media.token, media.enc_idx
        )
        if thumbnail_url:
            image = self._download_moment_image(
                thumbnail_url,
                aes_key=media.aes_key,
                source="微信官方 CDN 缩略图",
                is_thumbnail=True,
            )
            if image is not None:
                self._add_issue("部分朋友圈原图不可用，归档中已明确标注缩略图兜底")
                return image

        self._add_issue("部分朋友圈照片的原图和缩略图均不可用")
        return None

    def _resolve_moment_video(self, media: MomentMedia) -> MomentMediaFile | None:
        candidates = (
            _with_token(
                media.original_url,
                media.token,
                media.enc_idx,
                token_first=True,
            ),
            _with_token(
                media.original_url,
                media.token,
                media.enc_idx,
                original=True,
                token_first=True,
            ),
            media.original_url,
        )
        tried: set[str] = set()
        for original_url in candidates:
            if not original_url or original_url in tried:
                continue
            tried.add(original_url)
            try:
                data = self._download(original_url)
                resolved = self._decode_video_blob(
                    data,
                    key=media.aes_key,
                    source="微信官方 CDN 原视频",
                    generate_fallback=media.role != "live_photo_video",
                )
                if resolved is not None:
                    return resolved
            except (OSError, ValueError, RuntimeError):
                continue

        local = self._find_sns_video(media)
        if local is not None:
            try:
                resolved = self._decode_video_blob(
                    local.read_bytes(),
                    key=media.aes_key,
                    source="本机朋友圈视频缓存",
                    generate_fallback=media.role != "live_photo_video",
                )
                if resolved is not None:
                    return resolved
            except OSError:
                self._add_issue("部分本机朋友圈视频文件无法读取")

        self._add_issue("部分朋友圈视频在微信 CDN 和本机缓存中均不可用")
        return None

    def _decode_video_blob(
        self,
        data: bytes,
        *,
        key: str,
        source: str,
        generate_fallback: bool = False,
    ) -> MomentMediaFile | None:
        extension, mime_type = _video_file_type(data)
        if not extension and key:
            prefix_size = min(len(data), _VIDEO_ENCRYPTED_PREFIX_BYTES)
            stream = _isaac64_keystream(key, prefix_size)
            if stream is not None:
                prefix = bytes(
                    value ^ stream[index]
                    for index, value in enumerate(data[:prefix_size])
                )
                candidate = prefix + data[prefix_size:]
                extension, mime_type = _video_file_type(candidate)
                if extension:
                    data = candidate
        if not extension:
            return None
        fallback_data = (
            self._video_final_frame_png(data, extension=extension)
            if generate_fallback
            else None
        )
        return MomentMediaFile(
            data=data,
            extension=extension,
            mime_type=mime_type,
            source=source,
            fallback_data=fallback_data or b"",
            fallback_extension="png" if fallback_data else "",
            fallback_mime_type="image/png" if fallback_data else "",
            fallback_source="视频最后停止画面" if fallback_data else "",
        )

    def _download_moment_image(
        self,
        url: str,
        *,
        aes_key: str,
        source: str,
        is_thumbnail: bool,
    ) -> PdfImage | None:
        try:
            data = self._download(url)
        except (OSError, ValueError, RuntimeError):
            return None
        return self._decode_image_blob(
            data,
            aes_key=aes_key,
            source=source,
            is_thumbnail=is_thumbnail,
        )

    def _decode_image_blob(
        self,
        data: bytes,
        *,
        aes_key: str,
        source: str,
        is_thumbnail: bool,
    ) -> PdfImage | None:
        try:
            if not _image_format(data) and aes_key:
                try:
                    data = _decrypt_cdn_blob(data, aes_key)
                except ValueError:
                    pass
            if not _image_format(data) and aes_key:
                decrypted = _isaac64_xor(data, aes_key)
                if decrypted is not None:
                    data = decrypted
            if not _image_format(data):
                keys = self._account_image_keys()
                if data.startswith(_V2_MAGIC) and keys is None:
                    self._add_issue("未能从当前微信登录进程读取朋友圈图片密钥")
                    return None
                key, xor_key = keys or (b"", 0)
                data = decrypt_image_dat(data, key, xor_key)
            if data.startswith(b"wxgf"):
                converted = self._wxgf_to_png(data)
                if converted is None:
                    self._add_issue("缺少可用的 HEVC 解码器，部分朋友圈照片未导出")
                    return None
                data = converted
                with self._stats_lock:
                    self.stats.wxgf_converted += 1
            return _pdf_image(data, source=source, is_thumbnail=is_thumbnail)
        except (OSError, ValueError, RuntimeError):
            return None

    def _account_image_keys(self) -> tuple[bytes, int] | None:
        if self._image_keys_checked:
            return self._image_keys
        with self._image_key_lock:
            if not self._image_keys_checked:
                self._image_keys_checked = True
                self._image_keys = discover_account_image_keys(
                    self.account.account_dir, self.wxid
                )
        return self._image_keys

    def _find_image_dat(self, message: Message, md5: str) -> tuple[Path, bool] | None:
        if not message.conversation_id or not md5:
            return None
        chat_hash = hashlib.md5(message.conversation_id.encode("utf-8")).hexdigest()
        chat_root = self.account.account_dir / "msg" / "attach" / chat_hash
        month = message.datetime.strftime("%Y-%m")
        image_dir = chat_root / month / "Img"
        for filename, thumbnail in ((f"{md5}.dat", False), (f"{md5}_t.dat", True)):
            candidate = image_dir / filename
            if candidate.is_file():
                return candidate, thumbnail

        index = self._attach_indexes.get(chat_hash)
        if index is None:
            with self._attach_index_lock:
                index = self._attach_indexes.get(chat_hash)
                if index is None:
                    index = {}
                    if chat_root.is_dir():
                        for candidate in chat_root.rglob("*.dat"):
                            index.setdefault(candidate.name.lower(), candidate)
                    self._attach_indexes[chat_hash] = index
        original = index.get(f"{md5}.dat".lower())
        if original is not None:
            return original, False
        thumbnail = index.get(f"{md5}_t.dat".lower())
        return (thumbnail, True) if thumbnail is not None else None

    def _find_local_emoticon(self, md5: str) -> Path | None:
        if not md5:
            return None
        direct = self.account.account_dir / "business" / "emoticon" / "Persist" / md5[:2] / md5
        if direct.is_file():
            return direct
        cache_root = self.account.account_dir / "cache"
        if cache_root.is_dir():
            for month in sorted(cache_root.iterdir(), reverse=True):
                candidate = month / "Emoticon" / md5[:2] / md5
                if candidate.is_file():
                    return candidate
        return None

    def _find_sns_image(
        self, md5: str, media: MomentMedia | None = None
    ) -> Path | None:
        normalized = md5.strip().lower()
        if not re.fullmatch(r"[0-9a-f]{32}", normalized):
            normalized = ""
        cache_root = self.account.account_dir / "cache"
        if not cache_root.is_dir():
            return None

        if normalized:
            for month in sorted(cache_root.iterdir(), reverse=True):
                image_root = month / "Sns" / "Img"
                for candidate in (
                    image_root / normalized[:2] / normalized[2:],
                    image_root / normalized[:2] / normalized,
                    image_root / normalized,
                ):
                    if candidate.is_file():
                        return candidate

            with self._attach_index_lock:
                if self._sns_image_index is None:
                    index: dict[str, Path] = {}
                    for image_root in cache_root.glob("*/Sns/Img"):
                        if not image_root.is_dir():
                            continue
                        for candidate in image_root.rglob("*"):
                            if not candidate.is_file():
                                continue
                            name = candidate.name.lower()
                            index.setdefault(name, candidate)
                            index.setdefault(candidate.parent.name.lower() + name, candidate)
                    self._sns_image_index = index
                found = self._sns_image_index.get(normalized)
            if found is not None:
                return found

        if media is not None and (media.total_size or (media.width and media.height)):
            return self._find_sns_image_by_size(media)
        return None

    def _find_sns_image_by_size(self, media: MomentMedia) -> Path | None:
        """Fallback cache lookup by (decrypted length, width, height).

        WeChat 4.x names the Sns cache files with a hash that cannot be
        derived from the XML record, so the original-size attributes are used
        to match decrypted cache files instead.
        """
        with self._attach_index_lock:
            if self._sns_size_index is None:
                index: dict[tuple[int, int, int], Path] = {}
                keys = self._account_image_keys()
                if keys is not None:
                    aes_key, xor_key = keys
                    for image_root in self.account.account_dir.glob("cache/*/Sns/Img"):
                        if not image_root.is_dir():
                            continue
                        for candidate in image_root.rglob("*"):
                            if not candidate.is_file():
                                continue
                            try:
                                plain = decrypt_image_dat(
                                    candidate.read_bytes(), aes_key, xor_key
                                )
                                if not plain.startswith(b"\xff\xd8") and not plain.startswith(
                                    b"\x89PNG"
                                ):
                                    continue
                                width, height = Image.open(io.BytesIO(plain)).size
                            except (OSError, ValueError, RuntimeError):
                                continue
                            index.setdefault((len(plain), width, height), candidate)
                            index.setdefault((len(plain), 0, 0), candidate)
                self._sns_size_index = index
            hit = self._sns_size_index.get(
                (media.total_size, media.width, media.height)
            )
            if hit is None and media.total_size:
                hit = self._sns_size_index.get((media.total_size, 0, 0))
            return hit

    def _find_sns_video(self, media: MomentMedia) -> Path | None:
        """Best-effort lookup for already cached Moments video bytes."""
        cache_root = self.account.account_dir / "cache"
        if not cache_root.is_dir():
            return None
        normalized = media.md5.strip().lower()
        names = {normalized, normalized[2:]} if normalized else set()
        for pattern in ("*/Sns/Video", "*/Sns/Media"):
            for video_root in cache_root.glob(pattern):
                if not video_root.is_dir():
                    continue
                for candidate in video_root.rglob("*"):
                    if not candidate.is_file():
                        continue
                    lowered = candidate.name.lower()
                    if lowered in names or any(name and name in lowered for name in names):
                        return candidate
        return None

    def _download(self, url: str) -> bytes:
        if self._download_override is not None:
            return self._download_override(url)
        normalized = html.unescape(url).strip()
        parsed = urllib.parse.urlparse(normalized)
        hostname = (parsed.hostname or "").lower()
        if parsed.scheme not in {"http", "https"} or not hostname.endswith(
            _SUPPORTED_CDN_SUFFIXES
        ):
            raise ValueError("表情地址不是微信官方 CDN")
        errors: list[OSError] = []
        for user_agent in _MOMENT_USER_AGENTS:
            request = urllib.request.Request(
                normalized,
                headers={"User-Agent": user_agent},
            )
            try:
                with urllib.request.urlopen(request, timeout=15) as response:
                    final_host = (
                        urllib.parse.urlparse(response.geturl()).hostname or ""
                    ).lower()
                    if not final_host.endswith(_SUPPORTED_CDN_SUFFIXES):
                        raise ValueError("表情下载被重定向到非微信域名")
                    declared = int(response.headers.get("Content-Length") or 0)
                    if declared > _MAX_DOWNLOAD_BYTES:
                        raise ValueError("表情图片超过大小限制")
                    data = response.read(_MAX_DOWNLOAD_BYTES + 1)
                if len(data) > _MAX_DOWNLOAD_BYTES:
                    raise ValueError("表情图片超过大小限制")
                return data
            except urllib.error.HTTPError as error:
                errors.append(error)
        raise (errors[-1] if errors else OSError("表情图片下载失败"))

    def _ffmpeg_path(self) -> str | None:
        with self._ffmpeg_lock:
            if not self._ffmpeg_checked:
                self._ffmpeg_executable = _find_ffmpeg()
                self._ffmpeg_checked = True
            return self._ffmpeg_executable

    def _video_final_frame_png(self, data: bytes, *, extension: str) -> bytes | None:
        executable = self._ffmpeg_path()
        if not executable:
            return None
        try:
            with tempfile.TemporaryDirectory(prefix="wechat-moments-frame-") as temp:
                source = Path(temp) / f"source.{extension}"
                source.write_bytes(data)
                command = [
                    executable,
                    "-v",
                    "error",
                    "-sseof",
                    "-0.05",
                    "-i",
                    str(source),
                    "-frames:v",
                    "1",
                    "-f",
                    "image2pipe",
                    "-vcodec",
                    "png",
                    "pipe:1",
                ]
                completed = subprocess.run(
                    command,
                    capture_output=True,
                    timeout=45,
                    check=False,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
        except (OSError, subprocess.TimeoutExpired):
            return None
        return (
            completed.stdout
            if completed.returncode == 0
            and completed.stdout.startswith(b"\x89PNG\r\n\x1a\n")
            else None
        )

    def _wxgf_to_png(self, data: bytes) -> bytes | None:
        start = data.find(b"\x00\x00\x00\x01")
        if start < 0:
            return None
        executable = self._ffmpeg_path()
        if not executable:
            return None
        command = [
            executable,
            "-v",
            "error",
            "-f",
            "hevc",
            "-i",
            "pipe:0",
            "-frames:v",
            "1",
            "-f",
            "image2pipe",
            "-vcodec",
            "png",
            "pipe:1",
        ]
        try:
            completed = subprocess.run(
                command,
                input=data[start:],
                capture_output=True,
                timeout=30,
                check=False,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except (OSError, subprocess.TimeoutExpired):
            return None
        return completed.stdout if completed.returncode == 0 and completed.stdout.startswith(b"\x89PNG") else None


def decrypt_image_dat(data: bytes, aes_key: bytes, xor_key: int) -> bytes:
    if _image_format(data):
        return data
    if data.startswith(_V2_MAGIC):
        if len(aes_key) != 16 or len(data) < 31:
            raise ValueError("微信图片 AES 密钥或文件头无效")
        aes_size, xor_size = struct.unpack_from("<II", data, 6)
        aes_block_size = aes_size + (16 - aes_size % 16 if aes_size % 16 else 16)
        start = 15
        encrypted_end = start + aes_block_size
        tail_start = len(data) - xor_size if xor_size else len(data)
        if encrypted_end > tail_start or tail_start < start:
            raise ValueError("微信图片分段长度无效")
        decryptor = Cipher(algorithms.AES(aes_key), modes.ECB()).decryptor()
        plaintext = decryptor.update(data[start:encrypted_end]) + decryptor.finalize()
        plaintext = _pkcs7_unpad(plaintext)
        raw = data[encrypted_end:tail_start]
        tail = bytes(value ^ (xor_key & 0xFF) for value in data[tail_start:])
        result = plaintext + raw + tail
        if not _image_format(result):
            raise ValueError("微信图片解密结果不是受支持的图像")
        return result
    if data.startswith(_V1_MAGIC):
        result = bytes(value ^ (xor_key & 0xFF) for value in data[22:])
        if _image_format(result):
            return result
    for signature in (b"\xff\xd8\xff", b"\x89PNG", b"GIF8", b"RIFF"):
        candidate = data[0] ^ signature[0] if data else 0
        result = bytes(value ^ candidate for value in data)
        if result.startswith(signature):
            return result
    raise ValueError("无法识别微信图片加密格式")


def _first_md5(value: object) -> str:
    if isinstance(value, memoryview):
        value = value.tobytes()
    if isinstance(value, str):
        raw = value.encode("utf-8", errors="ignore")
    elif isinstance(value, (bytes, bytearray)):
        raw = bytes(value)
    else:
        return ""
    match = _HEX_MD5_RE.search(raw)
    return match.group(1).decode("ascii").lower() if match else ""


def _optional_int(value: object) -> int | None:
    try:
        parsed = int(str(value).strip())
    except (TypeError, ValueError):
        return None
    return parsed if parsed >= 0 else None


def _optional_float(value: object) -> float | None:
    try:
        parsed = float(str(value).strip())
    except (TypeError, ValueError):
        return None
    return parsed if parsed >= 0 else None


def _xml_attributes(content: str, tag: str) -> dict[str, str]:
    normalized = html.unescape(content or "").replace("\x00", "")
    try:
        root = ElementTree.fromstring(normalized)
    except ElementTree.ParseError:
        match = re.search(fr"<{tag}\b([^>]*)>", normalized, re.I | re.S)
        if not match:
            return {}
        return {
            key.lower(): html.unescape(value)
            for key, _, value in re.findall(
                r"([\w:.-]+)\s*=\s*([\"'])(.*?)\2", match.group(1), re.S
            )
        }
    for node in root.iter():
        if str(node.tag).lower() == tag.lower():
            return {str(key).lower(): html.unescape(value) for key, value in node.attrib.items()}
    return {}


def _xml_attribute(content: str, tag: str, name: str) -> str:
    return _xml_attributes(content, tag).get(name.lower(), "")


def _image_format(data: bytes) -> str:
    if data.startswith(b"\xff\xd8\xff"):
        return "JPEG"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "PNG"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "GIF"
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return "WEBP"
    if data.startswith(b"BM"):
        return "BMP"
    if data.startswith(b"wxgf"):
        return "WXGF"
    return ""


def _image_file_type(data: bytes) -> tuple[str, str]:
    return {
        "JPEG": ("jpg", "image/jpeg"),
        "PNG": ("png", "image/png"),
        "GIF": ("gif", "image/gif"),
        "WEBP": ("webp", "image/webp"),
        "BMP": ("bmp", "image/bmp"),
    }.get(_image_format(data), ("", ""))


def _video_file_type(data: bytes) -> tuple[str, str]:
    if len(data) >= 12 and data[4:8] == b"ftyp":
        if data[8:12] == b"qt  ":
            return "mov", "video/quicktime"
        return "mp4", "video/mp4"
    if data.startswith(b"\x1a\x45\xdf\xa3"):
        return "webm", "video/webm"
    if data.startswith(b"RIFF") and data[8:12] == b"AVI ":
        return "avi", "video/x-msvideo"
    return "", ""


def _pdf_image(data: bytes, *, source: str, is_thumbnail: bool = False) -> PdfImage:
    image_format = _image_format(data)
    if not image_format or image_format == "WXGF":
        raise ValueError("不是可嵌入 PDF 的图像")
    with Image.open(io.BytesIO(data)) as image:
        width, height = image.size
        animated = bool(getattr(image, "is_animated", False))
    if width <= 0 or height <= 0:
        raise ValueError("图像尺寸无效")
    return PdfImage(
        data=data,
        image_format=image_format,
        width=width,
        height=height,
        source=source,
        is_thumbnail=is_thumbnail,
        is_animated=animated,
    )


def _animated_final_frame_png(data: bytes) -> bytes | None:
    """Render the final settled frame of an animated GIF/WebP as PNG."""
    try:
        with Image.open(io.BytesIO(data)) as image:
            if not bool(getattr(image, "is_animated", False)):
                return None
            frame_count = int(getattr(image, "n_frames", 1))
            image.seek(max(0, frame_count - 1))
            frame = image.convert("RGBA")
            output = io.BytesIO()
            frame.save(output, format="PNG")
            return output.getvalue()
    except (EOFError, OSError, ValueError):
        return None


def _decrypt_cdn_blob(data: bytes, aes_key_hex: str) -> bytes:
    try:
        key = bytes.fromhex(aes_key_hex.strip())
    except ValueError as error:
        raise ValueError("表情 AES 密钥格式无效") from error
    if len(key) != 16 or len(data) % 16:
        raise ValueError("表情密文长度无效")
    decryptor = Cipher(algorithms.AES(key), modes.ECB()).decryptor()
    return _pkcs7_unpad(decryptor.update(data) + decryptor.finalize())


_ISAAC64_MASK = (1 << 64) - 1


def _isaac64_mix(values: tuple[int, ...]) -> tuple[int, ...]:
    """Run the reference ISAAC-64 initialization mix with 64-bit wrapping."""
    a, b, c, d, e, f, g, h = values
    mask = _ISAAC64_MASK
    a = (a - e) & mask
    f ^= h >> 9
    h = (h + a) & mask
    b = (b - f) & mask
    g ^= (a << 9) & mask
    a = (a + b) & mask
    c = (c - g) & mask
    h ^= b >> 23
    b = (b + c) & mask
    d = (d - h) & mask
    a ^= (c << 15) & mask
    c = (c + d) & mask
    e = (e - a) & mask
    b ^= d >> 14
    d = (d + e) & mask
    f = (f - b) & mask
    c ^= (e << 20) & mask
    e = (e + f) & mask
    g = (g - c) & mask
    d ^= f >> 17
    f = (f + g) & mask
    h = (h - d) & mask
    e ^= (g << 14) & mask
    g = (g + h) & mask
    return tuple(value & mask for value in (a, b, c, d, e, f, g, h))


def _isaac64_keystream(key_text: str, size: int) -> bytes | None:
    """Generate WeChat's numeric-key ISAAC-64 stream in big-endian order."""
    text = (key_text or "").strip()
    if not text.isdecimal() or size <= 0:
        return None
    seed = int(text) & _ISAAC64_MASK
    results = [0] * 256
    results[0] = seed
    memory = [0] * 256
    # Bob Jenkins' ISAAC-64 reference uses ...7c13.  The previous ...7c15
    # typo produces an entirely different stream and makes valid WeChat CDN
    # payloads look corrupt after XOR.
    state = (0x9E3779B97F4A7C13,) * 8
    for _ in range(4):
        state = _isaac64_mix(state)
    for offset in range(0, 256, 8):
        state = tuple(
            (state[index] + results[offset + index]) & _ISAAC64_MASK
            for index in range(8)
        )
        state = _isaac64_mix(state)
        memory[offset : offset + 8] = state
    for offset in range(0, 256, 8):
        state = tuple(
            (state[index] + memory[offset + index]) & _ISAAC64_MASK
            for index in range(8)
        )
        state = _isaac64_mix(state)
        memory[offset : offset + 8] = state

    a = b = c = 0
    output = bytearray()
    while len(output) < size:
        c = (c + 1) & _ISAAC64_MASK
        b = (b + c) & _ISAAC64_MASK
        for index in range(256):
            x = memory[index]
            branch = index & 3
            if branch == 0:
                a ^= (~(a << 21)) & _ISAAC64_MASK
            elif branch == 1:
                a ^= a >> 5
            elif branch == 2:
                a ^= (a << 12) & _ISAAC64_MASK
            else:
                a ^= a >> 33
            a = (a + memory[(index + 128) & 255]) & _ISAAC64_MASK
            y = (memory[(x >> 3) & 255] + a + b) & _ISAAC64_MASK
            memory[index] = y
            b = (memory[(y >> 11) & 255] + x) & _ISAAC64_MASK
            results[index] = b
        for value in reversed(results):
            output.extend(value.to_bytes(8, "big"))
            if len(output) >= size:
                break
    return bytes(output[:size])


def _isaac64_xor(data: bytes, key_text: str) -> bytes | None:
    """Decrypt WeChat 4.x CDN-encrypted Moments media with ISAAC-64 XOR.

    The XML key is a decimal 64-bit seed.  WeChat consumes each generated
    block in reverse result order and big-endian byte order.  Returns bytes
    only when the decrypted payload has a recognized image signature.
    """
    if not data or not key_text:
        return None
    stream = _isaac64_keystream(key_text, len(data))
    if stream is None:
        return None
    plain = bytes(a ^ b for a, b in zip(data, stream, strict=True))
    return plain if _image_format(plain) else None


def _with_token(
    url: str,
    token: str,
    idx: str = "",
    *,
    original: bool = False,
    token_first: bool = False,
) -> str:
    normalized = html.unescape(url or "").strip()
    if normalized.startswith("//"):
        normalized = "https:" + normalized
    if not normalized:
        return normalized
    parsed = urllib.parse.urlparse(normalized)
    scheme = "https" if parsed.scheme == "http" else parsed.scheme
    path = re.sub(r"/\d+$", "/0", parsed.path) if original else parsed.path
    query = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
    if token or idx:
        query = [
            (key, value)
            for key, value in query
            if key.lower() not in {"token", "idx"}
        ]
        auth_query = []
        if token:
            auth_query.append(("token", token))
        auth_query.append(("idx", idx or "1"))
        query = auth_query + query if token_first else query + auth_query
    return urllib.parse.urlunparse(
        parsed._replace(
            scheme=scheme,
            path=path,
            query=urllib.parse.urlencode(query),
        )
    )


def _pkcs7_unpad(data: bytes) -> bytes:
    if not data:
        raise ValueError("空的 PKCS7 数据")
    pad = data[-1]
    if not 1 <= pad <= 16 or data[-pad:] != bytes([pad]) * pad:
        raise ValueError("PKCS7 填充无效")
    return data[:-pad]


def _find_v2_probe(account_dir: Path) -> bytes | None:
    roots = [account_dir / "msg" / "attach"]
    cache_root = account_dir / "cache"
    if cache_root.is_dir():
        roots.extend(path for path in cache_root.glob("*/Sns/Img") if path.is_dir())
    for root in roots:
        if not root.is_dir():
            continue
        for candidate in root.rglob("*"):
            if not candidate.is_file():
                continue
            try:
                with candidate.open("rb") as stream:
                    head = stream.read(31)
            except OSError:
                continue
            if head.startswith(_V2_MAGIC) and len(head) >= 31:
                return head[15:31]
    return None


def _validate_v2_key(probe: bytes, aes_key: bytes) -> bool:
    if len(probe) != 16 or len(aes_key) != 16:
        return False
    try:
        decryptor = Cipher(algorithms.AES(aes_key), modes.ECB()).decryptor()
        plaintext = decryptor.update(probe) + decryptor.finalize()
    except ValueError:
        return False
    return bool(_image_format(plaintext))


def _read_cfg_candidates(pid: int) -> list[tuple[int, str]]:
    module = _find_weixin_module(pid)
    if module is None:
        return []
    base, size = module
    if size <= 0 or size >= 512 * 1024 * 1024:
        return []
    with ProcessMemory(pid) as memory:
        image = memory.read(base, size)
        if len(image) != size:
            return []
        positions: list[int] = []
        cursor = len(image)
        while True:
            cursor = image.rfind(_CFG_LANDMARK, 0, cursor)
            if cursor < 0:
                break
            if cursor + 28 <= len(image):
                stored_size = struct.unpack_from("<I", image, cursor + 16)[0]
                capacity = struct.unpack_from("<I", image, cursor + 24)[0]
                if stored_size == len(_CFG_LANDMARK) and capacity and (capacity | 0xF) == 0xF:
                    positions.append(cursor)
            if cursor == 0:
                break
        results: list[tuple[int, str]] = []
        seen: set[int] = set()
        for string_position in positions:
            size_position = string_position + 16
            for back in (0x138, 0x130):
                pointer = memory.read(base + size_position - back, 8)
                if len(pointer) != 8:
                    continue
                owner = struct.unpack("<Q", pointer)[0]
                cfg_pointer = memory.read(owner + 0x68, 8)
                if len(cfg_pointer) != 8:
                    continue
                cfg = struct.unpack("<Q", cfg_pointer)[0]
                if not 0x10000 <= cfg < 0x800000000000:
                    continue
                value = memory.read(cfg + 0x40, 4)
                if len(value) != 4:
                    continue
                cfg_dword = struct.unpack("<I", value)[0]
                if not cfg_dword or cfg_dword in seen:
                    continue
                seen.add(cfg_dword)
                results.append((cfg_dword, _read_remote_string(memory, cfg + 0x48)))
        return results


def _read_remote_string(memory: ProcessMemory, address: int) -> str:
    size_bytes = memory.read(address + 16, 8)
    if len(size_bytes) != 8:
        return ""
    size = struct.unpack("<Q", size_bytes)[0]
    if size <= 0 or size > 1024:
        return ""
    if size <= 15:
        data = memory.read(address, size)
    else:
        pointer = memory.read(address, 8)
        data = memory.read(struct.unpack("<Q", pointer)[0], size) if len(pointer) == 8 else b""
    return data.decode("utf-8", errors="replace") if len(data) == size else ""


def _find_weixin_module(pid: int) -> tuple[int, int] | None:
    if os.name != "nt":
        return None
    from ctypes import wintypes

    class ModuleEntry32W(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD),
            ("th32ModuleID", wintypes.DWORD),
            ("th32ProcessID", wintypes.DWORD),
            ("GlblcntUsage", wintypes.DWORD),
            ("ProccntUsage", wintypes.DWORD),
            ("modBaseAddr", ctypes.c_void_p),
            ("modBaseSize", wintypes.DWORD),
            ("hModule", wintypes.HMODULE),
            ("szModule", ctypes.c_wchar * 256),
            ("szExePath", ctypes.c_wchar * 260),
        ]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    kernel32.CreateToolhelp32Snapshot.restype = ctypes.c_void_p
    kernel32.Module32FirstW.argtypes = [ctypes.c_void_p, ctypes.POINTER(ModuleEntry32W)]
    kernel32.Module32FirstW.restype = wintypes.BOOL
    kernel32.Module32NextW.argtypes = [ctypes.c_void_p, ctypes.POINTER(ModuleEntry32W)]
    kernel32.Module32NextW.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    snapshot = kernel32.CreateToolhelp32Snapshot(0x08 | 0x10, pid)
    if not snapshot or snapshot == ctypes.c_void_p(-1).value:
        return None
    try:
        entry = ModuleEntry32W()
        entry.dwSize = ctypes.sizeof(entry)
        ok = kernel32.Module32FirstW(snapshot, ctypes.byref(entry))
        while ok:
            if entry.szModule.lower() == "weixin.dll":
                return int(entry.modBaseAddr or 0), int(entry.modBaseSize)
            entry.dwSize = ctypes.sizeof(entry)
            ok = kernel32.Module32NextW(snapshot, ctypes.byref(entry))
    finally:
        kernel32.CloseHandle(snapshot)
    return None


def _find_ffmpeg() -> str | None:
    executable = shutil.which("ffmpeg")
    if executable:
        return executable
    candidates: list[Path] = []
    for drive in ("C:/", "D:/", "E:/"):
        candidates.extend(Path(drive).glob("Octave/Octave-*/mingw64/bin/ffmpeg.exe"))
    if candidates:
        return str(max(candidates, key=lambda path: path.stat().st_mtime_ns))
    try:
        import imageio_ffmpeg  # type: ignore[import-not-found]
    except ImportError:
        return None
    try:
        return imageio_ffmpeg.get_ffmpeg_exe()
    except (OSError, RuntimeError):
        return None
