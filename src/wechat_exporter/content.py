from __future__ import annotations

import html
import re
from xml.etree import ElementTree


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
    if local_type in (49, 244813135921, 81604378673, 154618822705):
        title = _first_xml_value(content, ("title", "filename", "des", "displayname"))
        label = TYPE_LABELS.get(local_type, "[链接/文件]")
        return f"{label} {title}".strip()
    if local_type == 42:
        name = _first_xml_value(content, ("nickname", "displayname", "alias"))
        return f"[名片] {name}".strip()
    if local_type == 266287972401:
        title = _first_xml_value(content, ("title",))
        return title or "[拍一拍]"
    if local_type in (10000, 10002):
        stripped = _strip_xml(content)
        return stripped or TYPE_LABELS[local_type]
    return TYPE_LABELS.get(local_type, f"[消息类型 {local_type}]")


def split_group_sender(content: str) -> tuple[str, str]:
    if ":\n" not in content:
        return "", content
    possible_sender, text = content.split(":\n", 1)
    if re.fullmatch(r"(?:wxid_[A-Za-z0-9_-]+|[A-Za-z0-9_-]+@openim)", possible_sender):
        return possible_sender, text
    return "", content


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


def _strip_xml(content: str) -> str:
    value = html.unescape(content)
    value = re.sub(r"<[^>]+>", " ", value)
    return _clean_text(value)


def _clean_text(value: str) -> str:
    value = value.replace("\x00", "")
    value = value.replace("\r\n", "\n").replace("\r", "\n")
    value = re.sub(r"[\u0001-\u0008\u000b\u000c\u000e-\u001f\u007f]", "", value)
    return value.strip()
