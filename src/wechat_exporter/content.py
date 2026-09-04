from __future__ import annotations

import html
import math
import re
from collections.abc import Iterable
from xml.etree import ElementTree

from .models import WECHAT_FILE_MESSAGE_TYPES


TYPE_LABELS = {
    3: "[图片]",
    34: "[语音]",
    42: "[名片]",
    43: "[视频]",
    47: "[动画表情]",
    48: "[位置]",
    49: "[链接/文件]",
    50: "[通话]",
    10000: "[系统消息]",
    10002: "[撤回消息]",
    244813135921: "[引用消息]",
    266287972401: "[拍一拍]",
    81604378673: "[聊天记录]",
    154618822705: "[小程序]",
    8594229559345: "[红包]",
    8589934592049: "[转账]",
    34359738417: "[文件]",
    103079215153: "[文件]",
    25769803825: "[文件]",
}

_CHAT_HISTORY_ITEM_LABELS = {
    1: "[文本]",
    2: "[图片]",
    3: "[语音]",
    4: "[视频]",
    5: "[链接]",
    6: "[位置]",
    7: "[音乐]",
    8: "[文件]",
    14: "[聊天记录]",
    16: "[小程序]",
}

_ZSTD_MAGIC = b"\x28\xb5\x2f\xfd"
WECHAT_VOICE_TEXT_PREFIX = "[微信语音转文字] "


def decode_database_content(message_content: object, compress_content: object = None) -> str:
    for raw in (compress_content, message_content):
        decoded = _decode_one(raw)
        if decoded:
            return decoded
    return ""


def _decode_one(raw: object) -> str:
    if raw is None:
        return ""
    if isinstance(raw, str):
        # SQLite returns actual encoded/compressed payloads as BLOB bytes.
        # Treating arbitrary user text as hex/Base64 would silently corrupt
        # messages that merely happen to match those alphabets.
        return _clean_text(raw)
    if isinstance(raw, memoryview):
        raw = raw.tobytes()
    if isinstance(raw, (bytes, bytearray)):
        return _decode_bytes(bytes(raw))
    return _clean_text(str(raw))


def _decode_bytes(data: bytes) -> str:
    if not data:
        return ""
    if data.startswith(_ZSTD_MAGIC):
        decompressed = _decompress_zstd(data)
        if decompressed is not None:
            data = decompressed
    return _clean_text(data.decode("utf-8", errors="replace"))


def _decompress_zstd(data: bytes) -> bytes | None:
    try:
        from compression import zstd  # type: ignore[attr-defined]

        return zstd.decompress(data)
    except (ImportError, ValueError, OSError):
        pass
    try:
        import zstandard  # type: ignore[import-not-found]
    except ImportError:
        return None
    try:
        return zstandard.ZstdDecompressor().decompress(data)
    except (ValueError, OSError, zstandard.ZstdError):
        return None


def parse_message_text(local_type: int, raw_content: str) -> str:
    content = _clean_text(raw_content)
    if local_type == 1:
        return content
    if local_type == 34:
        transcript = _first_xml_attribute(content, "voicetrans", "transtext")
        if not transcript:
            transcript = _first_xml_value(content, ("transtext", "voicetrans"))
        return f"{WECHAT_VOICE_TEXT_PREFIX}{transcript}" if transcript else "[语音]"
    if local_type == 48:
        label = _first_xml_value(content, ("label", "poiname"))
        return f"[位置] {label}".strip()
    if local_type == 244813135921:
        return _parse_quoted_message(content)
    if local_type == 81604378673:
        return _parse_chat_history(content)
    if local_type in (49, 154618822705):
        title = _first_xml_value(content, ("title", "filename", "des", "displayname"))
        label = TYPE_LABELS.get(local_type, "[链接/文件]")
        return f"{label} {title}".strip()
    if local_type in WECHAT_FILE_MESSAGE_TYPES:
        title = _first_xml_value(content, ("title", "filename", "displayname"))
        return f"[文件] {title}".strip()
    if local_type == 42:
        name = _first_xml_value(content, ("nickname", "displayname", "alias"))
        return f"[名片] {name}".strip()
    if local_type == 266287972401:
        title = _first_xml_value(content, ("title",))
        return title or "[拍一拍]"
    if local_type in (50, 8594229559345, 8589934592049):
        label = TYPE_LABELS[local_type]
        detail = _first_xml_value(
            content,
            (
                "title",
                "des",
                "wording",
                "pay_memo",
                "receiverdes",
                "feedesc",
            ),
        )
        if not detail:
            stripped = _strip_xml(content)
            detail = stripped if stripped != content or not content.startswith("<") else ""
        return _labeled_item(label, detail)
    if local_type in (10000, 10002):
        stripped = _strip_xml(content)
        return stripped or TYPE_LABELS[local_type]
    known = TYPE_LABELS.get(local_type)
    if known:
        return known
    visible = _readable_unknown_text(content)
    label = f"[消息类型 {local_type}]"
    return f"{label} {visible}".strip() if visible else label


def extract_message_details(local_type: int, raw_content: str) -> dict[str, object] | None:
    """Return reliable, AI-friendly fields without exposing an opaque XML dump.

    WeChat has used several XML layouts for the same visible card.  This helper
    therefore keeps only values that can be named with confidence and silently
    omits fields that are absent in a particular client version.
    """

    content = _clean_text(raw_content)
    if not content:
        return None

    if local_type == 48:
        root = _parse_xml(content)
        location = _find_xml_node(root, "location")
        attributes = _node_attributes(location)
        details: dict[str, object] = {}
        _put(
            details,
            "name",
            attributes.get("poiname")
            or attributes.get("label")
            or _first_xml_value(content, ("poiname", "label")),
        )
        _put(
            details,
            "address",
            attributes.get("label") or _first_xml_value(content, ("label",)),
        )
        _put_number(
            details,
            "latitude",
            attributes.get("x")
            or attributes.get("latitude")
            or _first_xml_value(content, ("x", "latitude")),
        )
        _put_number(
            details,
            "longitude",
            attributes.get("y")
            or attributes.get("longitude")
            or _first_xml_value(content, ("y", "longitude")),
        )
        _put(details, "scale", attributes.get("scale"))
        return details or None

    if local_type == 42:
        root = _parse_xml(content)
        card = _find_xml_node(root, "msg")
        if card is None:
            card = root
        attributes = _node_attributes(card)
        details = {}
        _put(
            details,
            "name",
            attributes.get("nickname")
            or attributes.get("displayname")
            or _first_xml_value(content, ("nickname", "displayname")),
        )
        _put(
            details,
            "alias",
            attributes.get("alias") or _first_xml_value(content, ("alias",)),
        )
        _put(
            details,
            "wechat_id",
            attributes.get("username")
            or attributes.get("usernametext")
            or _first_xml_value(content, ("username", "usernametext")),
        )
        return details or None

    if local_type == 244813135921:
        root = _parse_xml(content)
        refermsg = _find_xml_node(root, "refermsg")
        details = {}
        _put(details, "reply_text", _first_xml_value(content, ("title",)))
        if refermsg is not None:
            _put(
                details,
                "quoted_sender",
                _child_xml_value(
                    refermsg,
                    ("displayname", "sourcename", "fromusr"),
                ),
            )
            referenced_type = _child_xml_value(refermsg, ("type",))
            _put_integer(details, "quoted_message_type", referenced_type)
            _put(
                details,
                "quoted_message_id",
                _child_xml_value(refermsg, ("svrid", "msgid", "newmsgid")),
            )
            referenced_content = _xml_node_content(_find_xml_node(refermsg, "content"))
            try:
                type_number = int(referenced_type)
            except ValueError:
                type_number = 0
            _put(details, "quoted_text", _parse_referenced_content(type_number, referenced_content))
        return details or None

    if local_type == 81604378673:
        return {
            "title": _first_xml_value(content, ("title",)),
            "representation": "flattened_text",
        }

    if local_type in (49, 154618822705) or local_type in WECHAT_FILE_MESSAGE_TYPES:
        details = {}
        appmsg_type = _first_xml_value(content, ("type",))
        _put_integer(details, "app_message_type", appmsg_type)
        _put(
            details,
            "title",
            _first_xml_value(content, ("title", "filename", "displayname")),
        )
        _put(
            details,
            "description",
            _first_xml_value(content, ("des", "description")),
        )
        _put(details, "url", _first_xml_value(content, ("url", "weburl")))
        _put(
            details,
            "source",
            _first_xml_value(content, ("sourcedisplayname", "appname")),
        )
        _put(details, "app_id", _first_xml_value(content, ("appid", "weappappid")))
        _put(
            details,
            "page_path",
            _first_xml_value(content, ("pagepath", "weappinfo_pagepath")),
        )
        cover_urls = _unique_nonempty(
            _first_xml_value(content, (name,))
            for name in ("thumburl", "cdnthumburl", "cdnthumbaurl", "coverurl")
        )
        if cover_urls:
            details["cover_urls"] = cover_urls
        return details or None

    if local_type in (
        50,
        8594229559345,
        8589934592049,
        10000,
        10002,
        266287972401,
    ):
        visible_text = parse_message_text(local_type, content)
        details = {"visible_text": visible_text} if visible_text else {}
        _put(
            details,
            "status_text",
            _first_xml_value(
                content,
                ("title", "des", "wording", "pay_memo", "receiverdes", "feedesc"),
            ),
        )
        if local_type == 50:
            _put_number(
                details,
                "duration_seconds",
                _first_xml_value(content, ("duration", "voiplength")),
            )
        return details or None

    return None


def app_message_semantic_type(local_type: int, raw_content: str) -> str | None:
    """Refine known app-card subtypes while leaving uncertain values alone."""

    if local_type == 154618822705:
        return "mini_program"
    if local_type != 49:
        return None
    try:
        subtype = int(_first_xml_value(raw_content, ("type",)))
    except ValueError:
        return None
    return {
        3: "music",
        4: "video_card",
        5: "link",
        6: "file",
        19: "chat_history",
        33: "mini_program",
        36: "mini_program",
        57: "quote",
    }.get(subtype)


def split_group_sender(content: str) -> tuple[str, str]:
    if ":\n" not in content:
        return "", content
    possible_sender, text = content.split(":\n", 1)
    if re.fullmatch(r"(?:wxid_[A-Za-z0-9_-]+|[A-Za-z0-9_-]+@openim)", possible_sender):
        return possible_sender, text
    return "", content


def _parse_quoted_message(content: str) -> str:
    """Render both the reply text and the original message embedded by WeChat."""
    title = _first_xml_value(content, ("title",))
    heading = f"[引用消息] {title}".strip()
    root = _parse_xml(content)
    if root is None:
        return heading
    refermsg = _find_xml_node(root, "refermsg")
    if refermsg is None:
        return heading

    sender = _child_xml_value(refermsg, ("displayname", "sourcename", "fromusr"))
    raw_type = _child_xml_value(refermsg, ("type",))
    try:
        referenced_type = int(raw_type)
    except ValueError:
        referenced_type = 0
    referenced_content = _xml_node_content(_find_xml_node(refermsg, "content"))
    quoted = _parse_referenced_content(referenced_type, referenced_content)
    if not quoted:
        quoted = TYPE_LABELS.get(referenced_type, "[原消息内容不可用]")
    label = f"引用原文（{sender}）" if sender else "引用原文"
    return f"{heading}\n{label}：{quoted}"


def _parse_referenced_content(message_type: int, content: str) -> str:
    if not content:
        return TYPE_LABELS.get(message_type, "")
    if message_type == 49:
        app_type = _first_xml_value(content, ("type",))
        try:
            subtype = int(app_type)
        except ValueError:
            subtype = 0
        if subtype == 19:
            return _parse_chat_history(content)
        if subtype == 57:
            return _parse_quoted_message(content)
    return parse_message_text(message_type, content)


def _parse_chat_history(content: str) -> str:
    """Expand a combined-forward ChatHistory payload into searchable lines."""
    outer_root = _parse_xml(content)
    outer_title = _first_xml_value(content, ("title",))
    record_root = _record_info_root(outer_root)
    record_title = _child_xml_value(record_root, ("title",)) if record_root is not None else ""
    heading_title = outer_title or record_title
    heading = f"[聊天记录] {heading_title}".strip()
    if record_root is None:
        summary = _first_xml_value(content, ("des", "info"))
        return f"{heading}\n{summary}" if summary and summary != heading_title else heading

    lines = [heading]
    for item in _record_data_items(record_root):
        rendered = _render_chat_history_item(item)
        if rendered:
            lines.append(rendered)
    if len(lines) == 1:
        summary = _child_xml_value(record_root, ("desc", "info"))
        if summary and summary != heading_title:
            lines.append(summary)
    return "\n".join(lines)


def _record_info_root(outer_root: ElementTree.Element | None) -> ElementTree.Element | None:
    if outer_root is None:
        return None
    if _xml_tag(outer_root) == "recordinfo":
        return outer_root
    existing = _find_xml_node(outer_root, "recordinfo")
    if existing is not None:
        return existing
    recorditem = _find_xml_node(outer_root, "recorditem")
    if recorditem is None:
        return None
    for child in recorditem:
        if _xml_tag(child) == "recordinfo":
            return child
    return _parse_xml(_xml_node_content(recorditem))


def _render_chat_history_item(item: ElementTree.Element) -> str:
    raw_type = item.attrib.get("datatype", "") or _child_xml_value(item, ("datatype",))
    try:
        item_type = int(raw_type)
    except ValueError:
        item_type = 0
    sender = _child_xml_value(item, ("sourcename", "displayname"))
    source_time = _child_xml_value(item, ("sourcetime",))
    description = _child_xml_value(item, ("datadesc",))
    title = _child_xml_value(item, ("datatitle",))

    if item_type == 1:
        body = description or title or "[文本]"
    elif item_type == 6:
        location = _child_xml_value(item, ("label", "poiname"))
        body = _labeled_item("[位置]", location or title or description)
    elif item_type == 14:
        nested = _find_xml_node(item, "recordinfo")
        if nested is None:
            nested_node = _find_xml_node(item, "recordxml")
            if nested_node is None:
                nested_node = _find_xml_node(item, "recorditem")
            nested = _parse_xml(_xml_node_content(nested_node))
        body = _render_record_info(nested) if nested is not None else "[聊天记录]"
    else:
        label = _CHAT_HISTORY_ITEM_LABELS.get(item_type, f"[消息类型 {item_type}]" if item_type else "[消息]")
        detail = title or description
        if item_type == 8 and not detail:
            detail = _child_xml_value(item, ("datafmt",))
        body = _labeled_item(label, detail)

    prefix_parts = []
    if source_time:
        prefix_parts.append(f"[{source_time}]")
    if sender:
        prefix_parts.append(f"{sender}：")
    prefix = " ".join(prefix_parts)
    if not prefix:
        return body
    body_lines = body.splitlines() or [""]
    first = f"{prefix} {body_lines[0]}".rstrip()
    return "\n".join([first, *(f"    {line}" for line in body_lines[1:])])


def _render_record_info(root: ElementTree.Element) -> str:
    title = _child_xml_value(root, ("title",))
    heading = f"[聊天记录] {title}".strip()
    lines = [heading]
    for item in _record_data_items(root):
        rendered = _render_chat_history_item(item)
        if rendered:
            lines.append(rendered)
    return "\n".join(lines)


def _record_data_items(root: ElementTree.Element) -> list[ElementTree.Element]:
    """Return only this record's items, leaving nested records to their parent item."""
    datalist = _find_xml_node(root, "datalist")
    if datalist is None:
        return []
    return [child for child in datalist if _xml_tag(child) == "dataitem"]


def _labeled_item(label: str, detail: str) -> str:
    if not detail or detail == label:
        return label
    return f"{label} {detail}"


def _parse_xml(content: str) -> ElementTree.Element | None:
    if not content:
        return None
    normalized = content.replace("\x00", "").strip()
    for candidate in (normalized, html.unescape(normalized)):
        try:
            return ElementTree.fromstring(candidate)
        except ElementTree.ParseError:
            continue
    return None


def _xml_tag(node: ElementTree.Element) -> str:
    return str(node.tag).rsplit("}", 1)[-1].lower()


def _find_xml_node(
    root: ElementTree.Element | None, name: str
) -> ElementTree.Element | None:
    if root is None:
        return None
    expected = name.lower()
    for node in root.iter():
        if _xml_tag(node) == expected:
            return node
    return None


def _child_xml_value(root: ElementTree.Element | None, names: tuple[str, ...]) -> str:
    if root is None:
        return ""
    expected = {name.lower() for name in names}
    for node in root.iter():
        if node is root or _xml_tag(node) not in expected:
            continue
        value = _xml_node_content(node)
        if value:
            return _clean_text(html.unescape(value))
    return ""


def _xml_node_content(node: ElementTree.Element | None) -> str:
    if node is None:
        return ""
    if len(node) == 0:
        return (node.text or "").strip()
    values = [node.text or ""]
    values.extend(ElementTree.tostring(child, encoding="unicode") for child in node)
    return "".join(values).strip()


def _first_xml_value(content: str, names: tuple[str, ...]) -> str:
    if not content:
        return ""
    normalized = html.unescape(content).replace("\x00", "")
    for name in names:
        match = re.search(fr"<{name}>(?:<!\[CDATA\[)?([\s\S]*?)(?:\]\]>)?</{name}>", normalized, re.I)
        if match:
            return _clean_text(html.unescape(match.group(1)))
    try:
        root = ElementTree.fromstring(normalized)
    except ElementTree.ParseError:
        return ""
    for name in names:
        node = root.find(f".//{name}")
        if node is not None and node.text:
            return _clean_text(node.text)
    return ""


def _first_xml_attribute(content: str, tag: str, attribute: str) -> str:
    if not content:
        return ""
    normalized = content.replace("\x00", "")
    match = re.search(
        fr"<{tag}\b[^>]*\b{attribute}\s*=\s*([\"'])(.*?)\1",
        normalized,
        re.I | re.S,
    )
    if match:
        return _clean_text(html.unescape(match.group(2)))
    try:
        root = ElementTree.fromstring(normalized)
    except ElementTree.ParseError:
        return ""
    for node in root.iter():
        if node.tag.lower() == tag.lower():
            return _clean_text(html.unescape(node.attrib.get(attribute, "")))
    return ""


def _node_attributes(node: ElementTree.Element | None) -> dict[str, str]:
    if node is None:
        return {}
    return {
        str(key).rsplit("}", 1)[-1].lower(): _clean_text(html.unescape(str(value)))
        for key, value in node.attrib.items()
    }


def _put(target: dict[str, object], key: str, value: object) -> None:
    if value is None:
        return
    normalized = _clean_text(str(value))
    if normalized:
        target[key] = normalized


def _put_integer(target: dict[str, object], key: str, value: object) -> None:
    try:
        target[key] = int(str(value).strip())
    except (TypeError, ValueError):
        pass


def _put_number(target: dict[str, object], key: str, value: object) -> None:
    try:
        number = float(str(value).strip())
    except (TypeError, ValueError):
        return
    if not math.isfinite(number):
        return
    target[key] = int(number) if number.is_integer() else number


def _unique_nonempty(values: Iterable[object]) -> list[str]:
    result: list[str] = []
    for value in values:
        normalized = _clean_text(str(value))
        if normalized and normalized not in result:
            result.append(normalized)
    return result


def _strip_xml(content: str) -> str:
    value = html.unescape(content)
    value = re.sub(r"<[^>]+>", " ", value)
    return _clean_text(value)


def _readable_unknown_text(content: str) -> str:
    value = _strip_xml(content)
    value = re.sub(r"[A-Za-z0-9+/=_-]{256,}", "[不可读数据]", value)
    if len(value) > 2_000:
        value = value[:2_000].rstrip() + "…"
    return value


def _clean_text(value: str) -> str:
    value = value.replace("\x00", "")
    value = value.replace("\r\n", "\n").replace("\r", "\n")
    value = re.sub(r"[\u0001-\u0008\u000b\u000c\u000e-\u001f\u007f]", "", value)
    return value.strip()
