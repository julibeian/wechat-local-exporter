from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path


WECHAT_FILE_MESSAGE_TYPES = frozenset({34359738417, 103079215153, 25769803825})


@dataclass(frozen=True, slots=True)
class AccountLocation:
    account_dir: Path
    wxid: str
    source: str

    @property
    def db_dir(self) -> Path:
        return self.account_dir / "db_storage"


@dataclass(frozen=True, slots=True)
class Conversation:
    username: str
    display_name: str
    last_timestamp: int = 0
    summary: str = ""
    is_group: bool = False
    is_self: bool = False


@dataclass(frozen=True, slots=True)
class MediaReference:
    """Read-only pointer to image data described by a WeChat message."""

    kind: str
    md5: str = ""
    aes_key: str = ""
    cdn_url: str = ""
    encrypted_url: str = ""
    thumbnail_url: str = ""
    size: int | None = None
    duration_seconds: float | None = None
    filename: str = ""


@dataclass(frozen=True, slots=True)
class AttachmentReference:
    """Metadata needed to describe and locate one WeChat file attachment."""

    filename: str
    extension: str = ""
    size: int | None = None
    md5: str = ""
    attachment_id: str = ""
    cdn_url: str = ""
    aes_key: str = ""
    file_key: str = ""
    file_upload_token: str = ""


@dataclass(frozen=True, slots=True)
class PdfImage:
    """Image bytes to embed in a PDF without an extra lossy re-encode."""

    data: bytes
    image_format: str
    width: int
    height: int
    source: str
    is_thumbnail: bool = False
    is_animated: bool = False


@dataclass(frozen=True, slots=True)
class MomentMedia:
    """An original media reference stored in a Moments XML record."""

    md5: str = ""
    original_url: str = ""
    thumbnail_url: str = ""
    token: str = ""
    thumbnail_token: str = ""
    aes_key: str = ""
    kind: str = "image"
    enc_idx: str = ""
    width: int = 0
    height: int = 0
    total_size: int = 0
    month: str = ""
    role: str = "ordinary"


@dataclass(frozen=True, slots=True)
class MomentMediaFile:
    """Decrypted original Moments media ready for an offline archive."""

    data: bytes
    extension: str
    mime_type: str
    source: str
    is_thumbnail: bool = False
    is_animated: bool = False
    fallback_data: bytes = b""
    fallback_extension: str = ""
    fallback_mime_type: str = ""
    fallback_source: str = ""


@dataclass(frozen=True, slots=True)
class Moment:
    """One Moments post visible to the currently logged-in WeChat account."""

    post_id: str
    username: str
    timestamp: int
    content: str = ""
    media: tuple[MomentMedia, ...] = ()
    is_pinned: bool = False
    location: str = ""
    visibility: str = "visible"

    @property
    def datetime(self) -> datetime:
        return datetime.fromtimestamp(self.timestamp)


@dataclass(frozen=True, slots=True)
class Message:
    local_id: int
    timestamp: int
    message_type: int
    sender_id: str
    sender_name: str
    is_outgoing: bool | None
    content: str
    sort_seq: int = 0
    source_db: str = ""
    server_id: int = 0
    conversation_id: str = ""
    media: MediaReference | None = None
    attachment: AttachmentReference | None = None
    raw_content: str = ""

    @property
    def datetime(self) -> datetime:
        return datetime.fromtimestamp(self.timestamp)

    @property
    def stable_id(self) -> str:
        if self.server_id > 0:
            return f"server:{self.server_id}"
        source = self.source_db.replace("/", "\\") or "unknown"
        return f"local:{source}:{self.local_id}"


@dataclass(frozen=True, slots=True)
class ExportRequest:
    conversations: tuple[Conversation, ...]
    output_dir: Path
    include_txt: bool = True
    include_pdf: bool = False
    include_pdf_images: bool = False
    include_jsonl: bool = False
    include_json: bool = False
    include_wechat_voice_text: bool = True
    start_timestamp: int = 0
    end_timestamp: int = 0


CHAT_FILE_CATEGORIES = frozenset(
    {"pdf", "word", "excel", "powerpoint", "archive", "other"}
)


@dataclass(frozen=True, slots=True)
class ChatFileExportRequest:
    conversations: tuple[Conversation, ...]
    output_dir: Path
    categories: frozenset[str] = CHAT_FILE_CATEGORIES
    max_file_size_bytes: int = 100 * 1024 * 1024
    start_timestamp: int = 0
    end_timestamp: int = 0


@dataclass(frozen=True, slots=True)
class JsonlPackageRequest:
    """Settings for one AI-oriented JSONL + media ZIP per conversation."""

    conversations: tuple[Conversation, ...]
    output_dir: Path
    include_videos: bool = True
    max_video_size_bytes: int = 100 * 1024 * 1024
    allow_network_media: bool = False
    start_timestamp: int = 0
    end_timestamp: int = 0


@dataclass(frozen=True, slots=True)
class ExportWorkload:
    message_count: int = 0
    image_count: int = 0
    emoticon_count: int = 0

    @property
    def media_count(self) -> int:
        return self.image_count + self.emoticon_count


@dataclass(slots=True)
class ExportResult:
    files: list[Path] = field(default_factory=list)
    file_conversations: dict[Path, Conversation] = field(default_factory=dict)
    file_categories: dict[Path, str] = field(default_factory=dict)
    message_counts: dict[str, int] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    duration_seconds: float = 0.0
