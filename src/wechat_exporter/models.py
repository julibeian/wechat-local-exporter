from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path


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


@dataclass(frozen=True, slots=True)
class MediaReference:
    """Read-only pointer to image data described by a WeChat message."""

    kind: str
    md5: str = ""
    aes_key: str = ""
    cdn_url: str = ""
    encrypted_url: str = ""
    thumbnail_url: str = ""


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

    @property
    def datetime(self) -> datetime:
        return datetime.fromtimestamp(self.timestamp)


@dataclass(frozen=True, slots=True)
class ExportRequest:
    conversations: tuple[Conversation, ...]
    output_dir: Path
    include_txt: bool = True
    include_pdf: bool = True
    include_pdf_images: bool = True
    include_wechat_voice_text: bool = True
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
    message_counts: dict[str, int] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    duration_seconds: float = 0.0
