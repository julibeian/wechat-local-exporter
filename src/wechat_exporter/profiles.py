from __future__ import annotations

import hashlib
import io
import re
import sqlite3
import urllib.parse
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from types import MappingProxyType
from typing import Literal, Protocol

from PIL import Image, UnidentifiedImageError

from .models import AccountLocation


_MAX_AVATAR_BYTES = 5 * 1024 * 1024
_MAX_AVATAR_PIXELS = 16_000_000
_SUPPORTED_AVATAR_FORMATS = frozenset({"JPEG", "PNG", "GIF", "WEBP", "BMP"})
_AVATAR_FILE_TYPES = {
    "JPEG": ("jpg", "image/jpeg"),
    "PNG": ("png", "image/png"),
    "GIF": ("gif", "image/gif"),
    "WEBP": ("webp", "image/webp"),
    "BMP": ("bmp", "image/bmp"),
}
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


class _Workspace(Protocol):
    def decrypted_path(self, relative_path: str) -> Path: ...


@dataclass(frozen=True, slots=True)
class AvatarReference:
    """Untrusted avatar hints read from local WeChat metadata.

    Paths and image bytes are deliberately not considered safe until they have
    passed :class:`AvatarResolver`.  Remote URLs are retained only as a future
    opt-in retrieval hint; they must never be copied directly into exported
    HTML.
    """

    local_hint: str = ""
    remote_url: str = ""
    embedded_data: bytes = b""


@dataclass(frozen=True, slots=True)
class AvatarFallback:
    text: str
    background: str
    foreground: str = "#ffffff"


@dataclass(frozen=True, slots=True)
class ContactProfile:
    username: str
    group_alias: str = ""
    remark: str = ""
    nickname: str = ""
    alias: str = ""
    avatar: AvatarReference | None = None
    is_personal: bool = False

    @property
    def display_name(self) -> str:
        """Use only verified metadata, in the product-defined priority order."""

        return next(
            (
                value.strip()
                for value in (
                    self.group_alias,
                    self.remark,
                    self.nickname,
                    self.alias,
                    self.username,
                )
                if value and value.strip()
            ),
            "未知成员",
        )

    @property
    def avatar_fallback(self) -> AvatarFallback:
        name = self.display_name.strip()
        visible = [character for character in name if not character.isspace()]
        text = (visible[0] if visible else "?").upper()
        digest = hashlib.sha256(self.username.encode("utf-8", "replace")).digest()
        hue = int.from_bytes(digest[:2], "big") % 360
        return AvatarFallback(text=text, background=f"hsl({hue} 48% 44%)")


@dataclass(frozen=True, slots=True)
class ResolvedAvatar:
    status: Literal[
        "available",
        "missing",
        "rejected",
        "network_disabled",
        "network_unavailable",
    ]
    data: bytes = b""
    path: Path | None = None
    mime_type: str = ""
    extension: str = ""
    width: int = 0
    height: int = 0
    sha256: str = ""
    source: str = ""
    reason: str = ""

    @property
    def available(self) -> bool:
        return self.status == "available"


@dataclass(slots=True)
class ProfileStore:
    account: AccountLocation
    _profiles: dict[str, ContactProfile] = field(default_factory=dict, repr=False)
    _group_aliases: dict[tuple[str, str], str] = field(default_factory=dict, repr=False)
    personal_usernames: frozenset[str] = frozenset()

    @classmethod
    def empty(cls, account: AccountLocation) -> ProfileStore:
        return cls(account=account)

    @property
    def profiles(self) -> Mapping[str, ContactProfile]:
        return MappingProxyType(self._profiles)

    def profile_for(self, username: str, *, group_id: str = "") -> ContactProfile:
        normalized = str(username or "").strip()
        base = self._profiles.get(normalized)
        if base is None:
            base = ContactProfile(username=normalized or "未知成员")
        group_alias = self._group_aliases.get((group_id, normalized), "") if group_id else ""
        return replace(base, group_alias=group_alias) if group_alias else base

    def get_profile(self, username: str, *, group_id: str = "") -> ContactProfile:
        """Compatibility alias for callers that use repository-style naming."""

        return self.profile_for(username, group_id=group_id)

    def get(self, username: str, default: ContactProfile | None = None) -> ContactProfile | None:
        """Provide mapping-like global lookup without applying a group alias."""

        return self._profiles.get(username, default)

    def participants_for(
        self,
        conversation_id: str,
        sender_ids: Iterable[str] | None = None,
    ) -> tuple[ContactProfile, ...]:
        """Return deterministic participants without inventing group aliases.

        When no explicit sender list is supplied, only members evidenced by a
        recognized group-member metadata table are returned.  With a sender
        list, missing group metadata safely falls back to the global contact
        profile (or finally the raw wxid).
        """

        if sender_ids is None:
            usernames = {
                username
                for group_id, username in self._group_aliases
                if group_id == conversation_id
            }
        else:
            usernames = {str(value or "").strip() for value in sender_ids}
            usernames.discard("")
        return tuple(
            self.profile_for(username, group_id=conversation_id)
            for username in sorted(usernames, key=str.casefold)
        )


class AvatarResolver:
    """Resolve and validate an avatar without escaping the WeChat account root."""

    def __init__(
        self,
        account: AccountLocation,
        *,
        workspace: _Workspace | None = None,
        allow_network: bool = False,
        downloader: Callable[[str, int], bytes] | None = None,
        max_bytes: int = _MAX_AVATAR_BYTES,
    ) -> None:
        self.account = account
        self.workspace = workspace
        self.allow_network = allow_network
        self._downloader = downloader
        self.max_bytes = max(1, int(max_bytes))
        self._account_root = account.account_dir.resolve()

    def resolve(self, profile: ContactProfile) -> ResolvedAvatar:
        # WeChat 4 stores the actual pictures separately from contact metadata.
        # Query only the requested identity, and validate bytes before exporting.
        databases: list[Path] = []
        if self.workspace is not None:
            try:
                databases.append(self.workspace.decrypted_path("head_image\\head_image.db"))
            except (OSError, ValueError):
                pass
        # Existing local tools may have already decrypted this same account's
        # avatar cache. This is optional metadata, never a source of messages.
        local_cache = self._account_root / "db_storage_decrypted" / "head_image" / "head_image.db"
        if local_cache.resolve().is_relative_to(self._account_root):
            databases.append(local_cache)
        for database in databases:
            try:
                if database.is_file():
                    connection = _connect_readonly(database)
                    try:
                        row = connection.execute(
                            "SELECT image_buffer FROM head_image WHERE username=? "
                            "AND length(image_buffer) BETWEEN 1 AND ?",
                            (profile.username, self.max_bytes),
                        ).fetchone()
                    finally:
                        connection.close()
                    if row and isinstance(row[0], bytes):
                        result = self._validate_data(row[0], source="head_image.db 本机头像")
                        if result.available:
                            return result
            except (OSError, sqlite3.Error, ValueError):
                pass
        reference = profile.avatar or AvatarReference()

        rejected_reason = ""
        if reference.embedded_data:
            result = self._validate_data(reference.embedded_data, source="contact.db 内嵌头像")
            if result.available:
                return result
            rejected_reason = result.reason

        if reference.local_hint:
            candidate, reason = self._safe_local_path(reference.local_hint)
            if candidate is not None:
                result = self._read_local(candidate)
                if result.available:
                    return result
                rejected_reason = result.reason
            elif reason:
                rejected_reason = reason

        for candidate in self._deterministic_cache_candidates(profile.username):
            result = self._read_local(candidate)
            if result.available:
                return result

        if reference.remote_url:
            if not self.allow_network:
                return ResolvedAvatar(
                    status="network_disabled",
                    reason="头像仅有远程线索，联网补全默认关闭",
                )
            if self._downloader is None:
                return ResolvedAvatar(
                    status="network_unavailable",
                    reason="未配置受限制的头像下载器",
                )
            if not _safe_remote_avatar_url(reference.remote_url):
                return ResolvedAvatar(status="rejected", reason="远程头像地址不安全")
            try:
                data = self._downloader(reference.remote_url, self.max_bytes)
            except (OSError, RuntimeError, ValueError):
                return ResolvedAvatar(status="network_unavailable", reason="头像下载失败")
            return self._validate_data(data, source="联网补全头像")

        if rejected_reason:
            return ResolvedAvatar(status="rejected", reason=rejected_reason)
        return ResolvedAvatar(status="missing", reason="本机头像不可用")

    def _safe_local_path(self, hint: str) -> tuple[Path | None, str]:
        cleaned = str(hint or "").strip().strip('"')
        if not cleaned or "\x00" in cleaned:
            return None, "头像路径为空或无效"
        parsed = urllib.parse.urlparse(cleaned)
        if parsed.scheme and not re.fullmatch(r"[A-Za-z]", parsed.scheme):
            return None, "头像路径不是本机文件"
        path = Path(cleaned)
        if not path.is_absolute():
            path = self._account_root / path
        try:
            resolved = path.resolve()
        except OSError:
            return None, "头像路径无法解析"
        if not resolved.is_relative_to(self._account_root):
            return None, "头像路径超出当前微信账号目录"
        return resolved, ""

    def _deterministic_cache_candidates(self, username: str) -> Iterable[Path]:
        """Yield exact-name candidates only; never pick an unrelated cache file."""

        normalized = username.strip()
        if not normalized:
            return
        digest = hashlib.md5(normalized.encode("utf-8")).hexdigest()
        names = (normalized, digest)
        roots = [
            self._account_root / "avatar",
            self._account_root / "Avatar",
            self._account_root / "headimg",
            self._account_root / "HeadImg",
        ]
        cache_root = self._account_root / "cache"
        if cache_root.is_dir():
            try:
                for month in sorted(cache_root.iterdir(), reverse=True):
                    if not month.is_dir():
                        continue
                    roots.extend(
                        month / folder
                        for folder in ("avatar", "Avatar", "headimg", "HeadImg")
                    )
            except OSError:
                pass
        seen: set[Path] = set()
        for root in roots:
            for name in names:
                candidates = (
                    root / name,
                    root / name[:2] / name[2:],
                    *(root / f"{name}.{extension}" for extension in ("jpg", "jpeg", "png", "webp", "gif")),
                )
                for candidate in candidates:
                    if candidate in seen:
                        continue
                    seen.add(candidate)
                    try:
                        if candidate.is_file() and candidate.resolve().is_relative_to(self._account_root):
                            yield candidate.resolve()
                    except OSError:
                        continue

    def _read_local(self, path: Path) -> ResolvedAvatar:
        try:
            resolved = path.resolve()
            if not resolved.is_relative_to(self._account_root):
                return ResolvedAvatar(status="rejected", reason="头像路径越界")
            size = resolved.stat().st_size
            if size <= 0 or size > self.max_bytes:
                return ResolvedAvatar(status="rejected", reason="头像文件大小无效")
            with resolved.open("rb") as stream:
                data = stream.read(self.max_bytes + 1)
        except OSError:
            return ResolvedAvatar(status="missing", reason="头像文件无法读取")
        return self._validate_data(data, source="本机微信头像缓存", path=resolved)

    def _validate_data(
        self,
        data: bytes,
        *,
        source: str,
        path: Path | None = None,
    ) -> ResolvedAvatar:
        if not data or len(data) > self.max_bytes:
            return ResolvedAvatar(status="rejected", reason="头像数据大小无效")
        try:
            with Image.open(io.BytesIO(data)) as image:
                image_format = str(image.format or "").upper()
                width, height = image.size
                if image_format not in _SUPPORTED_AVATAR_FORMATS:
                    raise ValueError("不支持的头像格式")
                if width <= 0 or height <= 0 or width * height > _MAX_AVATAR_PIXELS:
                    raise ValueError("头像尺寸无效")
                image.verify()
        except (Image.DecompressionBombError, UnidentifiedImageError, OSError, ValueError):
            return ResolvedAvatar(status="rejected", reason="头像不是可用的图片")
        extension, mime_type = _AVATAR_FILE_TYPES[image_format]
        return ResolvedAvatar(
            status="available",
            data=data,
            path=path,
            mime_type=mime_type,
            extension=extension,
            width=width,
            height=height,
            sha256=hashlib.sha256(data).hexdigest(),
            source=source,
        )


def load_contact_profiles(account: AccountLocation, workspace: _Workspace) -> ProfileStore:
    """Load profile metadata using conservative, case-insensitive schema candidates."""

    store = ProfileStore(account=account)
    path = workspace.decrypted_path("contact\\contact.db")
    connection = _connect_readonly(path)
    try:
        contact_table = _find_table(connection, ("contact", "contacts"))
        personal: set[str] = set()
        if contact_table:
            for row in connection.execute(f'SELECT * FROM "{_quote_identifier(contact_table)}"'):
                values = _normalized_row(row)
                username = _first_text(values, ("username", "user_name", "wxid"))
                if not username:
                    continue
                avatar = _avatar_reference(values)
                profile = ContactProfile(
                    username=username,
                    remark=_first_text(values, ("remark", "remark_name", "con_remark")),
                    nickname=_first_text(values, ("nick_name", "nickname", "display_name")),
                    alias=_first_text(values, ("alias", "user_alias")),
                    avatar=avatar,
                    is_personal=_is_personal_contact(values, username),
                )
                store._profiles[username] = profile
                if profile.is_personal:
                    personal.add(username)
        store.personal_usernames = frozenset(personal)
        store._group_aliases.update(_load_group_aliases(connection))
    finally:
        connection.close()
    return store


def _connect_readonly(path: Path) -> sqlite3.Connection:
    uri = f"file:{path.as_posix()}?mode=ro&immutable=1"
    connection = sqlite3.connect(uri, uri=True)
    connection.row_factory = sqlite3.Row
    return connection


def _find_table(connection: sqlite3.Connection, names: tuple[str, ...]) -> str | None:
    available = {
        _canonical(str(row[0])): str(row[0])
        for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    for name in names:
        found = available.get(_canonical(name))
        if found:
            return found
    return None


def _load_group_aliases(connection: sqlite3.Connection) -> dict[tuple[str, str], str]:
    allowed_tables = {
        "chatroommember",
        "chatroommembers",
        "chatroommemberinfo",
        "groupmember",
        "groupmembers",
        "roommember",
        "roommembers",
    }
    tables = [
        str(row[0])
        for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        if _canonical(str(row[0])) in allowed_tables
    ]
    result: dict[tuple[str, str], str] = {}
    for table in tables:
        columns = {
            _canonical(str(row[1])): str(row[1])
            for row in connection.execute(
                f'PRAGMA table_info("{_quote_identifier(table)}")'
            )
        }
        group_column = _first_column(
            columns,
            ("chatroom_name", "chatroom_id", "room_id", "group_id", "chatroom_username"),
        )
        member_column = _first_column(
            columns,
            ("member_username", "member_wxid", "user_name", "username", "wxid"),
        )
        alias_column = _first_column(
            columns,
            (
                "display_name",
                "displayname",
                "room_nickname",
                "group_nickname",
                "member_alias",
                "member_display_name",
            ),
        )
        if not group_column or not member_column or not alias_column:
            continue
        selected = ", ".join(
            f'"{_quote_identifier(value)}"'
            for value in (group_column, member_column, alias_column)
        )
        try:
            rows = connection.execute(
                f'SELECT {selected} FROM "{_quote_identifier(table)}" ORDER BY rowid'
            )
            for row in rows:
                group_id = _as_text(row[0])
                username = _as_text(row[1])
                group_alias = _as_text(row[2])
                if not group_id.lower().endswith("@chatroom") or not username or not group_alias:
                    continue
                result.setdefault((group_id, username), group_alias)
        except sqlite3.DatabaseError:
            continue
    return result


def _normalized_row(row: sqlite3.Row) -> dict[str, object]:
    return {_canonical(key): row[key] for key in row.keys()}


def _first_text(values: Mapping[str, object], names: tuple[str, ...]) -> str:
    for name in names:
        value = values.get(_canonical(name))
        text = _as_text(value)
        if text:
            return text
    return ""


def _first_bytes(values: Mapping[str, object], names: tuple[str, ...]) -> bytes:
    for name in names:
        value = values.get(_canonical(name))
        if isinstance(value, bytes) and value:
            return value
    return b""


def _first_column(columns: Mapping[str, str], names: tuple[str, ...]) -> str:
    for name in names:
        column = columns.get(_canonical(name))
        if column:
            return column
    return ""


def _avatar_reference(values: Mapping[str, object]) -> AvatarReference | None:
    local_hint = _first_text(
        values,
        (
            "avatar_path",
            "head_img_path",
            "headimg_path",
            "head_image_path",
            "small_head_path",
            "small_headimg_path",
            "avatar_file",
        ),
    )
    remote_url = _first_text(
        values,
        (
            "avatar_url",
            "head_img_url",
            "headimg_url",
            "headimgurl",
            "small_head_url",
            "big_head_url",
            "head_image_url",
        ),
    )
    embedded_data = _first_bytes(
        values,
        ("avatar_data", "avatar_blob", "head_img_data", "headimg_data"),
    )
    if not local_hint and not remote_url and not embedded_data:
        return None
    return AvatarReference(
        local_hint=local_hint,
        remote_url=remote_url,
        embedded_data=embedded_data,
    )


def _is_personal_contact(values: Mapping[str, object], username: str) -> bool:
    normalized = username.strip().lower()
    if (
        not normalized
        or normalized.startswith("gh_")
        or normalized.endswith("@chatroom")
        or normalized.endswith("@app")
        or normalized in _SYSTEM_CONVERSATION_IDS
    ):
        return False
    if _as_int(_first_value(values, ("verify_flag", "verifyflag"), 0)) != 0:
        return False
    if _as_int(_first_value(values, ("delete_flag", "deleteflag"), 0)) != 0:
        return False
    type_names = ("local_type", "contact_type", "type")
    if any(_canonical(name) in values for name in type_names):
        return _as_int(_first_value(values, type_names, 0)) == 1
    return True


def _first_value(
    values: Mapping[str, object], names: tuple[str, ...], default: object
) -> object:
    for name in names:
        canonical = _canonical(name)
        if canonical in values and values[canonical] is not None:
            return values[canonical]
    return default


def _as_text(value: object) -> str:
    if isinstance(value, str):
        return value.strip().strip("\x00")
    if isinstance(value, bytes):
        for encoding in ("utf-8", "utf-16le"):
            try:
                decoded = value.decode(encoding).strip().strip("\x00")
            except UnicodeDecodeError:
                continue
            if decoded and all(character.isprintable() or character.isspace() for character in decoded):
                return decoded
    return ""


def _as_int(value: object) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError, OverflowError):
        return 0


def _canonical(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", value.casefold())


def _quote_identifier(value: str) -> str:
    return value.replace('"', '""')


def _safe_remote_avatar_url(value: str) -> bool:
    try:
        parsed = urllib.parse.urlparse(value.strip())
    except ValueError:
        return False
    hostname = (parsed.hostname or "").lower()
    return parsed.scheme == "https" and bool(hostname) and (
        hostname == "qpic.cn"
        or hostname.endswith(".qpic.cn")
        or hostname == "wechat.com"
        or hostname.endswith(".wechat.com")
        or hostname == "weixin.qq.com"
        or hostname.endswith(".weixin.qq.com")
    )
