"""Measure the bytes actually exported from local images, with a stat cache."""
from __future__ import annotations

import hashlib
import io
import json
import os
import re
import struct
import tempfile
import threading
import zlib
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path

from PIL import Image

from .database_cache import AccountDatabaseCache
from .media import MediaResolver, _V1_MAGIC, _V2_MAGIC, _decrypt_cdn_blob, decrypt_emoticon_cache, decrypt_image_dat
from .models import AccountLocation


@dataclass(frozen=True, slots=True)
class MeasuredImage:
    byte_count: int
    digest: str
    image_format: str
    pdf_bytes: int
    pdf_identity: str


_CACHE_LOCK = threading.Lock()
_CACHE_VERSION = 2  # PNG output uses the same FFmpeg command as MediaResolver.


class LocalImageSizeProbe:
    def __init__(self, account: AccountLocation, wxid: str, *, cache_root: Path | None = None):
        self._cache = AccountDatabaseCache(account)
        self._keys: tuple[bytes, int] | None = None
        self._keys_checked = False
        self._emoticon_key: bytes | None = None
        self._emoticon_key_checked = False
        self._load_keys: Callable[[], tuple[bytes, int] | None] = lambda: self._cache.load_image_keys(wxid)
        self._root = account.account_dir.resolve()
        self._path = (cache_root or self._cache.root) / "export-image-sizes.json"
        self._entries = self._read_cache()
        self._pending: dict[str, dict] = {}
        self._resolver = MediaResolver(account, wxid, allow_network=False, cache_message_results=False)
        self._failed: set[tuple] = set()

    def _read_cache(self) -> dict[str, dict]:
        try:
            if self._path.stat().st_size > 16 * 1024**2:
                return {}
            payload = json.loads(self._path.read_text(encoding="utf-8"))
            if payload.get("version") == _CACHE_VERSION and isinstance(payload.get("images"), dict):
                return payload["images"]
        except (OSError, ValueError, TypeError, AttributeError):
            pass
        return {}

    def measure(self, path: Path, *, cdn_key: str = "",
                check_cancelled: Callable[[], None] | None = None,
                cached_only: bool = False) -> MeasuredImage | None:
        """Decode exactly as export does, discarding pixels after counting bytes.

        No downloads or process/key acquisition. Changed source files are
        remeasured; failures are never persisted, so later keys can repair them.
        The cache contains lengths, formats and digests, never media or keys.
        """
        if check_cancelled:
            check_cancelled()
        try:
            resolved = path.resolve()
            if not resolved.is_relative_to(self._root):
                return None
            stat = path.stat()
            signature = [stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns]
            identity = hashlib.sha256(str(resolved.relative_to(self._root)).casefold().encode()).hexdigest()
            # Different CDN keys must not reuse a failed in-memory lookup.
            failure_key = (identity, *signature, cdn_key)
            entry = self._entries.get(identity)
            if isinstance(entry, dict) and entry.get("source") == signature:
                try:
                    measured = MeasuredImage(**entry["result"])
                    if (type(measured.byte_count) is int and measured.byte_count > 0
                            and type(measured.pdf_bytes) is int and measured.pdf_bytes > 0
                            and re.fullmatch(r"[0-9a-f]{64}", measured.digest)
                            and re.fullmatch(r"[0-9a-f]{64}", measured.pdf_identity)
                            and measured.image_format in {"JPEG", "PNG", "GIF", "WEBP", "BMP", "AVIF"}):
                        return measured
                except (KeyError, ValueError, TypeError):
                    pass
            if cached_only or failure_key in self._failed:
                return None
            data = path.read_bytes()
            if data.startswith((_V1_MAGIC, _V2_MAGIC)):
                if not self._keys_checked:
                    self._keys = self._load_keys()
                    self._keys_checked = True
                if self._keys is None:
                    self._failed.add(failure_key)
                    return None
                data = decrypt_image_dat(data, *self._keys)
            else:
                if not self._emoticon_key_checked:
                    self._emoticon_key = self._cache.load_emoticon_key(self._resolver.wxid)
                    self._emoticon_key_checked = True
                if self._emoticon_key is not None:
                    try:
                        data = decrypt_emoticon_cache(data, self._emoticon_key)
                    except ValueError:
                        pass
                if cdn_key:
                    try:
                        data = _decrypt_cdn_blob(data, cdn_key)
                    except ValueError:
                        pass
                data = decrypt_image_dat(data, b"", 0)
            if data.startswith(b"wxgf"):
                data = self._resolver._wxgf_to_png(data)
                if data is None:
                    self._failed.add(failure_key)
                    return None
            if check_cancelled:
                check_cancelled()
            with Image.open(io.BytesIO(data)) as image:
                image_format = (image.format or "PNG").upper()
                rgb = image.convert("RGB").tobytes()
                pdf_identity = hashlib.sha256(rgb)
                # ReportLab stores JPEG unchanged, others as deflated RGB,
                # then ASCII85. Transparency gets a separate deflated mask.
                pdf_bytes = (len(data) if image_format == "JPEG" else len(zlib.compress(rgb)))
                if "A" in image.getbands() or "transparency" in image.info:
                    alpha = image.convert("RGBA").getchannel("A").tobytes()
                    pdf_identity.update(alpha)
                    pdf_bytes += len(zlib.compress(alpha))
                measured = MeasuredImage(len(data), hashlib.sha256(data).hexdigest(), image_format,
                                         round(pdf_bytes * 1.25) + 400, pdf_identity.hexdigest())
            after = path.stat()
            if signature != [after.st_size, after.st_mtime_ns, after.st_ctime_ns]:
                return None
            entry = {"source": signature, "result": asdict(measured)}
            self._entries[identity] = self._pending[identity] = entry
            return measured
        except (OSError, ValueError, RuntimeError, struct.error, Image.DecompressionBombError):
            return None

    def estimate(self, path: Path, file_size: int) -> tuple[int, int]:
        measured = self.measure(path)
        size = measured.byte_count if measured else 0
        return size, size

    def flush(self) -> None:
        if not self._pending:
            return
        temporary: str | None = None
        try:
            with _CACHE_LOCK:
                entries = self._read_cache()
                entries.update(self._pending)
                self._path.parent.mkdir(parents=True, exist_ok=True)
                with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", delete=False,
                                                 dir=self._path.parent, suffix=".tmp") as stream:
                    temporary = stream.name
                    json.dump({"version": _CACHE_VERSION, "images": dict(list(entries.items())[-20_000:])},
                              stream, separators=(",", ":"))
                os.replace(temporary, self._path)
                self._pending.clear()
        except (OSError, ValueError, TypeError):
            pass
        finally:
            if temporary and Path(temporary).exists():
                Path(temporary).unlink(missing_ok=True)


class _Bits:
    def __init__(self, data: bytes):
        self.data, self.position = data, 0

    def read(self, count: int) -> int:
        if count < 0 or self.position + count > len(self.data) * 8:
            raise ValueError("truncated SPS")
        value = 0
        for _ in range(count):
            value = value * 2 + ((self.data[self.position // 8] >> (7 - self.position % 8)) & 1)
            self.position += 1
        return value

    def ue(self) -> int:
        zeros = 0
        while self.read(1) == 0:
            zeros += 1
            if zeros > 31:
                raise ValueError("invalid Exp-Golomb value")
        return (1 << zeros) - 1 + self.read(zeros)


def hevc_picture_dimensions(data: bytes) -> tuple[int, int, int] | None:
    """Read the small prefix of a normal Annex-B HEVC sequence parameter set.

    Only metadata is parsed. Syntax reference: the HEVC SPS/PTL reader in
    github.com/FFmpeg/FFmpeg/blob/master/libavcodec/hevc/ps.c.
    """
    starts = tuple(re.finditer(b"\x00\x00(?:\x00)?\x01", data))
    for index, match in enumerate(starts):
        end = starts[index + 1].start() if index + 1 < len(starts) else len(data)
        nal = data[match.end():end]
        if len(nal) < 3 or nal[0] & 0x80 or (nal[0] >> 1) & 63 != 33 or not nal[1] & 7:
            continue
        try:
            bits = _Bits(nal[2:].replace(b"\x00\x00\x03", b"\x00\x00"))
            bits.read(4)  # video_parameter_set_id
            sub_layers = bits.read(3)
            bits.read(1)  # temporal_id_nesting_flag
            bits.read(96)  # general_profile_tier_level, including level_idc
            flags = [(bits.read(1), bits.read(1)) for _ in range(sub_layers)]
            if sub_layers:
                bits.read((8 - sub_layers) * 2)
            for profile, level in flags:
                if profile:
                    bits.read(88)
                if level:
                    bits.read(8)
            bits.ue()  # seq_parameter_set_id
            chroma = bits.ue()
            if chroma > 3:
                raise ValueError("invalid chroma format")
            separate = bits.read(1) if chroma == 3 else 0
            width, height = bits.ue(), bits.ue()
            if bits.read(1):
                left, right, top, bottom = (bits.ue() for _ in range(4))
                width -= (left + right) * (2 if chroma in {1, 2} and not separate else 1)
                height -= (top + bottom) * (2 if chroma == 1 and not separate else 1)
            bit_depth = max(bits.ue(), bits.ue()) + 8
            if width <= 0 or height <= 0 or width * height > 100_000_000 or bit_depth > 16:
                raise ValueError("invalid HEVC picture dimensions")
            return width, height, bit_depth
        except ValueError:
            continue
    return None
