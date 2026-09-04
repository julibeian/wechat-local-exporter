from __future__ import annotations

import hashlib
import io
import struct
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from PIL import Image
from pypdf import PdfReader

from wechat_exporter.exporters import PdfTranscriptWriter
from wechat_exporter.media import (
    MediaResolver,
    decrypt_image_dat,
    derive_image_keys,
    extract_media_reference,
)
from wechat_exporter.models import (
    AccountLocation,
    Conversation,
    MediaReference,
    Message,
    PdfImage,
)
from wechat_exporter.service import _iter_messages_with_images


def _png_bytes(size: tuple[int, int] = (96, 64)) -> bytes:
    stream = io.BytesIO()
    Image.new("RGB", size, (30, 160, 90)).save(stream, format="PNG")
    return stream.getvalue()


def _jpeg_bytes(size: tuple[int, int] = (120, 80)) -> bytes:
    stream = io.BytesIO()
    Image.new("RGB", size, (40, 90, 180)).save(stream, format="JPEG", quality=93)
    return stream.getvalue()


def _encrypt_v2(plaintext: bytes, aes_key: bytes, xor_key: int) -> bytes:
    aes_size = min(48, len(plaintext) - 8)
    xor_size = 8
    padder = padding.PKCS7(128).padder()
    padded = padder.update(plaintext[:aes_size]) + padder.finalize()
    encryptor = Cipher(algorithms.AES(aes_key), modes.ECB()).encryptor()
    encrypted = encryptor.update(padded) + encryptor.finalize()
    raw = plaintext[aes_size:-xor_size]
    tail = bytes(value ^ xor_key for value in plaintext[-xor_size:])
    return (
        b"\x07\x08\x56\x32\x08\x07"
        + struct.pack("<II", aes_size, xor_size)
        + b"\x01"
        + encrypted
        + raw
        + tail
    )


def test_media_references_cover_normal_images_and_emoticons() -> None:
    packed_md5 = "0123456789abcdef0123456789abcdef"
    image = extract_media_reference(
        3,
        '<msg><img md5="ffffffffffffffffffffffffffffffff"/></msg>',
        b"\x08\x04\x1a\x22" + packed_md5.encode(),
    )
    assert image == MediaReference(kind="image", md5=packed_md5)

    emoticon = extract_media_reference(
        47,
        '<msg><emoji md5="aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa" '
        'aeskey="bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb" '
        'cdnurl="https://mmbiz.qpic.cn/demo?a=1&amp;b=2"/></msg>',
    )
    assert emoticon is not None
    assert emoticon.kind == "emoticon"
    assert emoticon.cdn_url.endswith("a=1&b=2")

    video = extract_media_reference(
        43,
        '<msg><videomsg md5="cccccccccccccccccccccccccccccccc" '
        'length="2048" playlength="12.5" cdnvideourl="https://vweixinf.tc.qq.com/v.mp4"/></msg>',
    )
    assert video == MediaReference(
        kind="video",
        md5="c" * 32,
        cdn_url="https://vweixinf.tc.qq.com/v.mp4",
        size=2048,
        duration_seconds=12.5,
    )


def test_local_only_media_resolver_never_calls_the_network(tmp_path) -> None:
    calls = []
    resolver = MediaResolver(
        AccountLocation(tmp_path / "account", "wxid_self", "test"),
        "wxid_self",
        allow_network=False,
        download=lambda url: calls.append(url) or b"unexpected",
    )
    message = Message(
        1,
        1_788_330_000,
        47,
        "wxid_friend",
        "好友",
        False,
        "[动画表情]",
        conversation_id="wxid_friend",
        media=MediaReference(
            "emoticon",
            md5="d" * 32,
            cdn_url="https://mmbiz.qpic.cn/emoji",
        ),
    )
    assert resolver.resolve(message) is None
    assert calls == []


def test_v2_image_decrypt_is_byte_exact() -> None:
    plaintext = _png_bytes()
    aes_key, xor_key = derive_image_keys(2_840_026_734, "wxid_self")
    encrypted = _encrypt_v2(plaintext, aes_key, xor_key)
    assert decrypt_image_dat(encrypted, aes_key, xor_key) == plaintext


def test_local_image_resolver_prefers_original_and_preserves_bytes(tmp_path) -> None:
    account_dir = tmp_path / "wxid_self_abcd"
    account = AccountLocation(account_dir, "wxid_self_abcd", "test")
    conversation_id = "wxid_friend"
    media_md5 = "1234567890abcdef1234567890abcdef"
    timestamp = int(datetime(2026, 8, 25, 12, 30).timestamp())
    target = (
        account_dir
        / "msg"
        / "attach"
        / hashlib.md5(conversation_id.encode()).hexdigest()
        / "2026-08"
        / "Img"
        / f"{media_md5}.dat"
    )
    target.parent.mkdir(parents=True)
    original = _png_bytes((160, 90))
    keys = derive_image_keys(123456, "wxid_self")
    target.write_bytes(_encrypt_v2(original, *keys))
    message = Message(
        local_id=1,
        timestamp=timestamp,
        message_type=3,
        sender_id="wxid_self",
        sender_name="我",
        is_outgoing=True,
        content="[图片]",
        conversation_id=conversation_id,
        media=MediaReference(kind="image", md5=media_md5),
    )
    resolver = MediaResolver(account, "wxid_self", image_keys=keys)
    resolved = resolver.resolve(message)
    assert resolved is not None
    assert resolved.data == original
    assert (resolved.width, resolved.height) == (160, 90)
    assert not resolved.is_thumbnail


def test_emoticon_resolver_downloads_message_cdn_image(tmp_path) -> None:
    data = _png_bytes((48, 48))
    account = AccountLocation(tmp_path / "wxid_self_abcd", "wxid_self_abcd", "test")
    reference = MediaReference(
        kind="emoticon",
        md5="a" * 32,
        cdn_url="https://mmbiz.qpic.cn/demo",
    )
    message = Message(
        local_id=2,
        timestamp=int(datetime(2026, 8, 25, 13, 0).timestamp()),
        message_type=47,
        sender_id="wxid_friend",
        sender_name="好友",
        is_outgoing=False,
        content="[动画表情]",
        conversation_id="wxid_friend",
        media=reference,
    )
    resolver = MediaResolver(account, "wxid_self", download=lambda _url: data)
    resolved = resolver.resolve(message)
    assert resolved is not None
    assert resolved.data == data
    assert resolver.stats.emoticons == 1


def test_parallel_resolver_downloads_duplicate_emoticon_once(tmp_path) -> None:
    data = _png_bytes((48, 48))
    account = AccountLocation(tmp_path / "wxid_self_abcd", "wxid_self_abcd", "test")
    calls = 0
    calls_lock = threading.Lock()

    def download(_url: str) -> bytes:
        nonlocal calls
        with calls_lock:
            calls += 1
        time.sleep(0.03)
        return data

    message = Message(
        local_id=2,
        timestamp=int(datetime(2026, 8, 25, 13, 0).timestamp()),
        message_type=47,
        sender_id="wxid_friend",
        sender_name="好友",
        is_outgoing=False,
        content="[动画表情]",
        conversation_id="wxid_friend",
        media=MediaReference(
            kind="emoticon",
            md5="a" * 32,
            cdn_url="https://mmbiz.qpic.cn/demo",
        ),
    )
    resolver = MediaResolver(account, "wxid_self", download=download)
    with ThreadPoolExecutor(max_workers=4) as executor:
        resolved = list(executor.map(resolver.resolve, [message] * 4))

    assert all(item is not None and item.data == data for item in resolved)
    assert calls == 1
    assert resolver.stats.requested == 4
    assert resolver.stats.embedded == 4


def test_media_prefetch_overlaps_work_and_preserves_message_order() -> None:
    class SlowResolver:
        def __init__(self) -> None:
            self.active = 0
            self.peak = 0
            self.lock = threading.Lock()

        def resolve(self, message: Message) -> None:
            with self.lock:
                self.active += 1
                self.peak = max(self.peak, self.active)
            time.sleep(0.03)
            with self.lock:
                self.active -= 1
            return None

    messages = [
        Message(
            local_id=index,
            timestamp=index,
            message_type=3,
            sender_id="wxid_self",
            sender_name="我",
            is_outgoing=True,
            content="[图片]",
            conversation_id="wxid_friend",
            media=MediaReference(kind="image", md5=f"{index:032x}"),
        )
        for index in range(8)
    ]
    resolver = SlowResolver()
    resolved = list(
        _iter_messages_with_images(
            messages,
            resolver,  # type: ignore[arg-type]
            max_workers=4,
            max_pending=8,
        )
    )

    assert [message.local_id for message, _ in resolved] == list(range(8))
    assert resolver.peak >= 2


def test_pdf_contains_full_resolution_image_xobject(tmp_path) -> None:
    jpeg = _jpeg_bytes((120, 80))
    image = PdfImage(
        data=jpeg,
        image_format="JPEG",
        width=120,
        height=80,
        source="合成测试原图",
    )
    conversation = Conversation("wxid_friend", "好友")
    message = Message(
        local_id=1,
        timestamp=int(datetime(2026, 8, 25, 14, 0).timestamp()),
        message_type=3,
        sender_id="wxid_self",
        sender_name="我",
        is_outgoing=True,
        content="[图片]",
    )
    output = tmp_path / "image.pdf"
    with PdfTranscriptWriter(output, conversation) as writer:
        writer.write(message, image=image)

    reader = PdfReader(output)
    xobjects = reader.pages[0]["/Resources"]["/XObject"].get_object()
    images = [obj.get_object() for obj in xobjects.values() if obj.get_object().get("/Subtype") == "/Image"]
    assert len(images) == 1
    assert images[0]["/Width"] == 120
    assert images[0]["/Height"] == 80
    assert "/DCTDecode" in str(images[0]["/Filter"])
