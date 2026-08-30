from __future__ import annotations

import datetime
import html
import re
import sqlite3
from pathlib import Path
from xml.etree import ElementTree

from .content import decode_database_content
from .crypto import DecryptedWorkspace
from .models import Conversation, Moment, MomentMedia


_PIN_TAGS = (
    "isTop",
    "is_top",
    "isPinned",
    "is_pinned",
    "pinned",
    "sticky",
    "topFlag",
    "top_flag",
)


def load_contact_moments(
    workspace: DecryptedWorkspace,
    conversation: Conversation,
) -> list[Moment]:
    """Load every locally synced Moments row for one personal contact."""
    if conversation.is_group:
        raise ValueError("朋友圈导出只支持单个联系人，不支持群聊。")

    path = workspace.decrypted_path("sns\\sns.db")
    if not path.is_file():
        raise RuntimeError(
            "当前连接没有可用的朋友圈数据库。请在微信中打开朋友圈后重新连接。"
        )

    connection = _connect_readonly(path)
    try:
        table = _find_table(connection, "SnsTimeLine")
        if table is None:
            raise RuntimeError("sns.db 中没有找到朋友圈时间线表。")
        columns = _column_map(connection, table)
        user_column = _pick_column(columns, "user_name", "username", "user")
        content_column = _pick_column(columns, "content", "xml", "content_xml")
        if user_column is None or content_column is None:
            raise RuntimeError("当前微信版本的朋友圈表缺少发布者或内容字段。")

        tid_column = _pick_column(columns, "tid", "id", "sns_id")
        time_column = _pick_column(columns, "create_time", "createtime", "timestamp")
        pin_column = _pick_column(
            columns,
            "is_top",
            "is_pinned",
            "pinned",
            "sticky",
            "top_flag",
        )
        quoted_table = _quote_identifier(table)
        quoted_user = _quote_identifier(user_column)
        rows = connection.execute(
            f"SELECT * FROM {quoted_table} WHERE {quoted_user} = ?",
            (conversation.username,),
        ).fetchall()
    finally:
        connection.close()

    moments: list[Moment] = []
    seen: set[str] = set()
    for row in rows:
        raw_content = row[content_column]
        xml = decode_database_content(raw_content)
        fallback_id = str(row[tid_column] or "") if tid_column else ""
        fallback_timestamp = _as_timestamp(row[time_column]) if time_column else 0
        pinned_hint = _as_bool(row[pin_column]) if pin_column else False
        moment = parse_moment_xml(
            xml,
            fallback_user=conversation.username,
            fallback_post_id=fallback_id,
            fallback_timestamp=fallback_timestamp,
            pinned_hint=pinned_hint,
        )
        identity = moment.post_id or f"{moment.timestamp}:{moment.content}:{len(moment.media)}"
        if identity in seen:
            continue
        seen.add(identity)
        moments.append(moment)

    moments.sort(
        key=lambda item: (1 if item.is_pinned else 0, item.timestamp, item.post_id),
        reverse=True,
    )
    return moments


def parse_moment_xml(
    xml: str,
    *,
    fallback_user: str = "",
    fallback_post_id: str = "",
    fallback_timestamp: int = 0,
    pinned_hint: bool = False,
) -> Moment:
    """Parse the stable fields used by the archive exporter from SnsDataItem XML."""
    normalized = (xml or "").replace("\x00", "").strip()
    root = _parse_xml(normalized)

    post_id = _first_text(root, normalized, "id") or fallback_post_id
    username = _first_text(root, normalized, "username", "user_name") or fallback_user
    timestamp = _as_timestamp(
        _first_text(root, normalized, "createTime", "create_time")
    ) or fallback_timestamp
    content = html.unescape(
        _first_text(root, normalized, "contentDesc", "content_desc")
    ).strip()
    location = _location_text(root, normalized)
    is_pinned = pinned_hint or _is_pinned(root, normalized)
    visibility = _visibility(root, normalized)
    media = tuple(_parse_media(root, normalized, timestamp))

    return Moment(
        post_id=post_id,
        username=username,
        timestamp=timestamp,
        content=content,
        media=media,
        is_pinned=is_pinned,
        location=location,
        visibility=visibility,
    )


def _visibility(root: ElementTree.Element | None, xml: str) -> str:
    """Classify explicit visibility markers without excluding any post."""
    if _as_bool(_first_text(root, xml, "private", "isPrivate", "is_private")):
        return "private"

    selected_tags = (
        "groupUser",
        "group_user",
        "whiteList",
        "allowList",
        "visibleUserList",
        "withUserList",
    )
    if any(_first_text(root, xml, name) for name in selected_tags):
        return "selected"

    excluded_tags = (
        "blackList",
        "notVisibleUserList",
        "withoutUserList",
        "excludedUserList",
    )
    if any(_first_text(root, xml, name) for name in excluded_tags):
        return "excluded"
    return "visible"


def _parse_media(
    root: ElementTree.Element | None, xml: str, timestamp: int = 0
) -> list[MomentMedia]:
    month = (
        datetime.datetime.fromtimestamp(timestamp).strftime("%Y-%m")
        if timestamp
        else ""
    )
    result: list[MomentMedia] = []
    if root is not None:
        global_video_key = _global_video_key(root)
        for node in root.iter():
            if _tag_name(node) != "media":
                continue
            outer = _media_from_node(
                node,
                month=month,
                fallback_key=global_video_key,
            )
            if outer is not None:
                result.append(outer)

            # A WeChat live photo is represented by an ordinary still image
            # plus a nested livePhoto/liveMedia video.  Treat both as first-
            # class files so the motion part is not silently discarded.
            for descendant in node.iter():
                if descendant is node or _tag_name(descendant) not in {
                    "livephoto",
                    "livemedia",
                }:
                    continue
                live = _media_from_node(
                    descendant,
                    month=month,
                    fallback_key=(
                        global_video_key
                        or (outer.aes_key if outer is not None else "")
                    ),
                    forced_kind="video",
                    role="live_photo_video",
                )
                if live is not None:
                    result.append(live)
        return result

    global_video_key = _regex_global_video_key(xml)
    for block in re.findall(r"<media\b[^>]*>([\s\S]*?)</media>", xml, re.I):
        live_match = re.search(
            r"<(?:livePhoto|liveMedia)\b[^>]*>([\s\S]*?)</(?:livePhoto|liveMedia)>",
            block,
            re.I,
        )
        outer_block = block[: live_match.start()] if live_match else block
        outer = _media_from_regex(
            outer_block,
            month=month,
            fallback_key=global_video_key,
        )
        if outer is not None:
            result.append(outer)
        if live_match:
            live = _media_from_regex(
                live_match.group(1),
                month=month,
                fallback_key=(
                    global_video_key
                    or (outer.aes_key if outer is not None else "")
                ),
                forced_kind="video",
                role="live_photo_video",
            )
            if live is not None:
                result.append(live)
    return result


def _media_from_node(
    node: ElementTree.Element,
    *,
    month: str,
    fallback_key: str = "",
    forced_kind: str = "",
    role: str = "ordinary",
) -> MomentMedia | None:
    url_node = _first_descendant(node, "url")
    thumb_node = _first_descendant(node, "thumb")
    original_url = _node_text(url_node)
    thumbnail_url = _node_text(thumb_node)
    url_attributes = _lower_attributes(url_node)
    thumb_attributes = _lower_attributes(thumb_node)
    md5 = (
        url_attributes.get("md5")
        or thumb_attributes.get("md5")
        or _first_descendant_text(node, "md5")
    ).lower()
    if not (md5 or original_url or thumbnail_url):
        return None
    kind = forced_kind or _media_kind(node, original_url)
    node_key = (
        url_attributes.get("key")
        or url_attributes.get("aeskey")
        or thumb_attributes.get("key")
        or thumb_attributes.get("aeskey")
        or ""
    )
    key = (fallback_key or node_key) if kind == "video" else node_key
    return MomentMedia(
        md5=md5,
        original_url=html.unescape(original_url),
        thumbnail_url=html.unescape(thumbnail_url),
        token=url_attributes.get("token", ""),
        thumbnail_token=thumb_attributes.get("token", ""),
        aes_key=key,
        kind=kind,
        enc_idx=(
            url_attributes.get("enc_idx")
            or url_attributes.get("encidx")
            or thumb_attributes.get("enc_idx")
            or thumb_attributes.get("encidx")
            or ""
        ),
        width=_size_value(node, "width"),
        height=_size_value(node, "height"),
        total_size=_size_value(node, "totalsize"),
        month=month,
        role=role,
    )


def _media_from_regex(
    block: str,
    *,
    month: str,
    fallback_key: str = "",
    forced_kind: str = "",
    role: str = "ordinary",
) -> MomentMedia | None:
    url, url_attributes = _regex_tag(block, "url")
    thumb, thumb_attributes = _regex_tag(block, "thumb")
    md5 = (
        url_attributes.get("md5")
        or thumb_attributes.get("md5")
        or _regex_text(block, "md5")
    ).lower()
    if not (md5 or url or thumb):
        return None
    media_type = _regex_text(block, "type")
    kind = forced_kind or ("video" if _looks_like_video(url, media_type) else "image")
    node_key = (
        url_attributes.get("key")
        or url_attributes.get("aeskey")
        or thumb_attributes.get("key")
        or thumb_attributes.get("aeskey")
        or ""
    )
    key = (fallback_key or node_key) if kind == "video" else node_key
    return MomentMedia(
        md5=md5,
        original_url=html.unescape(url),
        thumbnail_url=html.unescape(thumb),
        token=url_attributes.get("token", ""),
        thumbnail_token=thumb_attributes.get("token", ""),
        aes_key=key,
        kind=kind,
        enc_idx=(
            url_attributes.get("enc_idx")
            or url_attributes.get("encidx")
            or thumb_attributes.get("enc_idx")
            or thumb_attributes.get("encidx")
            or ""
        ),
        width=_regex_size_value(block, "width"),
        height=_regex_size_value(block, "height"),
        total_size=_regex_size_value(block, "totalsize"),
        month=month,
        role=role,
    )


def _global_video_key(root: ElementTree.Element) -> str:
    for node in root.iter():
        if _tag_name(node) == "enc":
            attributes = _lower_attributes(node)
            return attributes.get("key", "") or _node_text(node)
    return ""


def _regex_global_video_key(xml: str) -> str:
    match = re.search(r"<enc\b([^>]*)>", xml, re.I | re.S)
    if not match:
        return ""
    attributes = {
        key.lower(): html.unescape(value)
        for key, _, value in re.findall(
            r"([\w:.-]+)\s*=\s*([\"'])(.*?)\2", match.group(1), re.S
        )
    }
    return attributes.get("key", "")


def _size_value(node: ElementTree.Element, name: str) -> int:
    for descendant in node.iter():
        if _tag_name(descendant) == "size":
            value = _lower_attributes(descendant).get(name.lower())
            if value is not None:
                try:
                    return int(value)
                except ValueError:
                    return 0
    return 0


def _regex_size_value(block: str, name: str) -> int:
    match = re.search(r'<size\b[^>]*\b' + name + r'="(\d+)"', block, re.I)
    if not match:
        return 0
    try:
        return int(match.group(1))
    except ValueError:
        return 0


def _parse_xml(xml: str) -> ElementTree.Element | None:
    if not xml:
        return None
    try:
        return ElementTree.fromstring(xml)
    except ElementTree.ParseError:
        return None


def _first_text(
    root: ElementTree.Element | None,
    xml: str,
    *names: str,
) -> str:
    lowered = {name.lower() for name in names}
    if root is not None:
        for node in root.iter():
            if _tag_name(node) in lowered:
                value = _node_text(node)
                if value:
                    return value
    for name in names:
        value = _regex_text(xml, name)
        if value:
            return value
    return ""


def _regex_text(xml: str, name: str) -> str:
    match = re.search(
        rf"<{re.escape(name)}\b[^>]*>(?:<!\[CDATA\[)?([\s\S]*?)(?:\]\]>)?</{re.escape(name)}>",
        xml,
        re.I,
    )
    return match.group(1).strip() if match else ""


def _regex_tag(xml: str, name: str) -> tuple[str, dict[str, str]]:
    match = re.search(
        rf"<{re.escape(name)}\b([^>]*)>(?:<!\[CDATA\[)?([\s\S]*?)(?:\]\]>)?</{re.escape(name)}>",
        xml,
        re.I,
    )
    if not match:
        return "", {}
    attributes = {
        key.lower(): html.unescape(value)
        for key, _, value in re.findall(
            r"([\w:.-]+)\s*=\s*([\"'])(.*?)\2",
            match.group(1),
            re.S,
        )
    }
    return match.group(2).strip(), attributes


def _location_text(root: ElementTree.Element | None, xml: str) -> str:
    attributes: dict[str, str] = {}
    if root is not None:
        for node in root.iter():
            if _tag_name(node) == "location":
                attributes = _lower_attributes(node)
                break
    if not attributes:
        match = re.search(r"<location\b([^>]*)>", xml, re.I | re.S)
        if match:
            attributes = {
                key.lower(): html.unescape(value)
                for key, _, value in re.findall(
                    r"([\w:.-]+)\s*=\s*([\"'])(.*?)\2",
                    match.group(1),
                    re.S,
                )
            }
    values = []
    for key in ("poiname", "poiaddressname", "poiaddress", "city", "country"):
        value = attributes.get(key, "").strip()
        if value and value not in values:
            values.append(value)
    return " · ".join(values)


def _is_pinned(root: ElementTree.Element | None, xml: str) -> bool:
    for name in _PIN_TAGS:
        value = _first_text(root, xml, name)
        if _as_bool(value):
            return True
    names = "|".join(re.escape(name) for name in _PIN_TAGS)
    match = re.search(
        rf"\b(?:{names})\s*=\s*([\"'])(.*?)\1",
        xml,
        re.I | re.S,
    )
    return bool(match and _as_bool(match.group(2)))


def _media_kind(node: ElementTree.Element, url: str) -> str:
    media_type = _first_descendant_text(node, "type")
    return "video" if _looks_like_video(url, media_type) else "image"


def _looks_like_video(url: str, media_type: str) -> bool:
    lowered_url = url.lower().split("?", 1)[0]
    return (
        lowered_url.endswith((".mp4", ".mov", ".avi", ".m4v"))
        or any(marker in lowered_url for marker in ("/video/", "video.qq.com", "vweixinf.tc.qq.com"))
        or media_type in {
        "6",
        "15",
        }
    )


def _first_descendant(
    root: ElementTree.Element,
    name: str,
) -> ElementTree.Element | None:
    lowered = name.lower()
    for node in root.iter():
        if node is not root and _tag_name(node) == lowered:
            return node
    return None


def _first_descendant_text(root: ElementTree.Element, name: str) -> str:
    return _node_text(_first_descendant(root, name))


def _node_text(node: ElementTree.Element | None) -> str:
    if node is None:
        return ""
    return "".join(node.itertext()).strip()


def _lower_attributes(node: ElementTree.Element | None) -> dict[str, str]:
    if node is None:
        return {}
    return {str(key).lower(): html.unescape(value) for key, value in node.attrib.items()}


def _tag_name(node: ElementTree.Element) -> str:
    return str(node.tag).rsplit("}", 1)[-1].lower()


def _connect_readonly(path: Path) -> sqlite3.Connection:
    uri = f"file:{path.as_posix()}?mode=ro&immutable=1"
    connection = sqlite3.connect(uri, uri=True)
    connection.row_factory = sqlite3.Row
    return connection


def _find_table(connection: sqlite3.Connection, expected: str) -> str | None:
    row = connection.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND lower(name)=lower(?)",
        (expected,),
    ).fetchone()
    return str(row[0]) if row else None


def _column_map(connection: sqlite3.Connection, table: str) -> dict[str, str]:
    escaped = table.replace('"', '""')
    return {
        str(row[1]).lower(): str(row[1])
        for row in connection.execute(f'PRAGMA table_info("{escaped}")')
    }


def _pick_column(columns: dict[str, str], *names: str) -> str | None:
    for name in names:
        if name.lower() in columns:
            return columns[name.lower()]
    return None


def _quote_identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def _as_timestamp(value: object) -> int:
    try:
        result = int(value or 0)
    except (TypeError, ValueError, OverflowError):
        return 0
    while result > 10_000_000_000:
        result //= 1000
    return max(0, result)


def _as_bool(value: object) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on", "top", "pinned"}
