from __future__ import annotations

import hashlib
import io
import json
import os
import shutil
import struct
import zipfile
from datetime import datetime

import pytest
from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from PIL import Image
from pypdf import PdfReader

from wechat_exporter import media, service as service_module
from wechat_exporter.database_cache import AccountDatabaseCache
from wechat_exporter.media import MediaResolver, derive_image_keys, extract_media_reference
from wechat_exporter.models import (
    AccountLocation, Conversation, ExportRequest, ExportWorkload,
    HtmlExportOptions, JsonlPackageRequest, MediaReference, Message,
    MomentMedia,
)
from wechat_exporter.service import ExporterService


def _cache(account, root):
    # Portable test protector preserves the production account entropy contract.
    def unprotect(data, *, entropy):
        if not data.startswith(entropy + b"\0"):
            raise OSError("different account")
        return data[len(entropy) + 1:]

    return AccountDatabaseCache(
        account, root=root,
        protect=lambda data, *, entropy: entropy + b"\0" + data,
        unprotect=unprotect,
    )


def _png():
    stream = io.BytesIO()
    Image.new("RGB", (48, 32), "#95ec69").save(stream, format="PNG")
    return stream.getvalue()


def _encrypted(data, keys):
    aes_key, xor_key = keys
    aes_size, xor_size = 32, 8
    padder = padding.PKCS7(128).padder()
    padded = padder.update(data[:aes_size]) + padder.finalize()
    cipher = Cipher(algorithms.AES(aes_key), modes.ECB()).encryptor()
    return (
        b"\x07\x08\x56\x32\x08\x07" + struct.pack("<II", aes_size, xor_size)
        + b"\x01" + cipher.update(padded) + cipher.finalize()
        + data[aes_size:-xor_size]
        + bytes(value ^ xor_key for value in data[-xor_size:])
    )


def _fixture(tmp_path, *, original=None):
    account = AccountLocation(tmp_path / "account", "wxid_self", "test")
    conversation = Conversation("family@chatroom", "家", is_group=True)
    message = Message(
        1, int(datetime(2026, 10, 3, 12).timestamp()), 3,
        "wxid_self", "我", True, "[图片]",
        conversation_id=conversation.username,
        media=MediaReference("image", md5="a" * 32),
    )
    keys = derive_image_keys(123456, "wxid_self")
    path = (account.account_dir / "msg/attach"
            / hashlib.md5(conversation.username.encode()).hexdigest()
            / "2026-10/Img" / f"{'a' * 32}.dat")
    path.parent.mkdir(parents=True)
    path.write_bytes(_encrypted(original or _png(), keys))
    return account, conversation, message, keys, path


def test_image_keys_are_captured_at_connection_and_reused_with_wechat_closed(tmp_path, monkeypatch):
    account, _, message, keys, _ = _fixture(tmp_path)
    cache = _cache(account, tmp_path / "cache")
    monkeypatch.setattr(media, "discover_account_image_keys", lambda *_a, **_k: keys)
    online = MediaResolver(account, "wxid_self", image_key_cache=cache)
    assert online.prepare_local_image_keys()
    assert cache.image_keys_path.is_file()

    def no_live_process(*_a, **_k):
        pytest.fail("offline export must use its validated protected cache")

    monkeypatch.setattr(media, "discover_account_image_keys", no_live_process)
    offline = MediaResolver(account, "wxid_self", image_key_cache=cache)
    result = offline.resolve(message)
    assert result is not None and result.data == _png()
    assert not result.is_thumbnail


def test_moments_local_image_uses_the_same_offline_key_cache(tmp_path, monkeypatch):
    account, _, _, keys, image_path = _fixture(tmp_path)
    cache = _cache(account, tmp_path / "cache")
    assert cache.save_image_keys("wxid_self", keys)
    md5 = "a" * 32
    moment_path = account.account_dir / "cache/2026-10/Sns/Img" / md5[:2] / md5[2:]
    moment_path.parent.mkdir(parents=True)
    shutil.copyfile(image_path, moment_path)
    monkeypatch.setattr(media, "discover_account_image_keys",
                        lambda *_a, **_k: pytest.fail("local Moments image must use its cached key"))
    resolver = MediaResolver(account, "wxid_self", image_key_cache=cache, allow_network=False)
    resolved = resolver.resolve_moment_file(MomentMedia(md5=md5))
    assert resolved is not None and resolved.data == _png()
    assert resolved.mime_type == "image/png"


def test_stale_image_keys_are_rejected_and_reacquired(tmp_path, monkeypatch):
    account, _, message, keys, _ = _fixture(tmp_path)
    cache = _cache(account, tmp_path / "cache")
    cache.save_image_keys("wxid_self", derive_image_keys(999, "wxid_self"))
    calls = []
    monkeypatch.setattr(media, "discover_account_image_keys",
                        lambda *_a, **kw: calls.append(kw["probe"]) or keys)
    result = MediaResolver(account, "wxid_self", image_key_cache=cache).resolve(message)
    assert result is not None and result.data == _png()
    assert len(calls) == 1 and len(calls[0]) == 16
    assert cache.load_image_keys("wxid_self") == keys


@pytest.mark.parametrize("with_cache", [False, True])
def test_legacy_image_keys_work_without_any_v2_probe(tmp_path, monkeypatch, with_cache):
    account, _, message, keys, path = _fixture(tmp_path)
    legacy = b"\x07\x08\x05\x56\x02\x05" + bytes(16)
    path.write_bytes(legacy + bytes(value ^ keys[1] for value in _png()))
    cache = _cache(account, tmp_path / "cache")
    if with_cache:
        assert cache.save_image_keys("wxid_self", keys)
    monkeypatch.setattr(media, "discover_account_image_keys",
                        lambda *_a, **_k: pytest.fail("cached legacy key should be reused") if with_cache else keys)
    result = MediaResolver(account, "wxid_self", image_key_cache=cache).resolve(message)
    assert result is not None and result.data == _png()
    assert cache.load_image_keys("wxid_self") == keys


@pytest.mark.parametrize("failure", ["corrupt", "account", "path", "wxid"])
def test_unusable_image_cache_is_not_reused(tmp_path, monkeypatch, failure):
    account, _, message, keys, _ = _fixture(tmp_path)
    cache = _cache(account, tmp_path / "cache")
    assert cache.save_image_keys("wxid_self", keys)
    if failure == "corrupt":
        cache.image_keys_path.write_bytes(b"broken cache")
    elif failure in {"account", "path"}:
        other = AccountLocation(
            tmp_path / "other", "wxid_other" if failure == "account" else "wxid_self", "test",
        )
        foreign = _cache(other, tmp_path / "cache")
        assert foreign.save_image_keys("wxid_self", keys)
        shutil.copyfile(foreign.image_keys_path, cache.image_keys_path)
    else:
        assert cache.save_image_keys("wxid_other", keys)
    monkeypatch.setattr(media, "discover_account_image_keys", lambda *_a, **_k: None)
    resolver = MediaResolver(account, "wxid_self", image_key_cache=cache)
    assert resolver.resolve(message) is None
    assert resolver.stats.missing == 1
    assert any("登录对应微信账号" in issue for issue in resolver.stats.issues)


@pytest.mark.skipif(os.name != "nt", reason="Windows DPAPI")
def test_image_keys_are_dpapi_protected_and_account_bound(tmp_path):
    account, _, _, keys, _ = _fixture(tmp_path)
    cache = AccountDatabaseCache(account, root=tmp_path / "cache")
    assert cache.save_image_keys("wxid_self", keys)
    protected = cache.image_keys_path.read_bytes()
    assert keys[0] not in protected
    assert keys[0].hex().encode() not in protected
    assert cache.load_image_keys("wxid_self") == keys
    assert cache.load_image_keys("wxid_other") is None


@pytest.mark.parametrize("cache_results", [True, False])
def test_transient_image_key_failure_recovers_after_login(tmp_path, monkeypatch, cache_results):
    account, _, message, keys, _ = _fixture(tmp_path)
    available = [False]
    now = [0.0]
    calls = []
    monkeypatch.setattr(media.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(media, "discover_account_image_keys",
                        lambda *_a, **_k: calls.append(1) or (keys if available[0] else None))
    resolver = MediaResolver(account, "wxid_self", image_key_cache=_cache(account, tmp_path / "cache"),
                             cache_message_results=cache_results)
    assert resolver.resolve(message) is None
    available[0], now[0] = True, 1.0
    assert resolver.resolve(message) is None
    assert len(calls) == 1
    now[0] = 3.0
    result = resolver.resolve(message)
    assert result is not None and result.data == _png()
    assert len(calls) == 2


@pytest.mark.parametrize("original", [b"wxgf" + bytes(100), b"corrupt original cache"])
def test_image_decode_failure_falls_back_to_usable_local_thumbnail(tmp_path, original):
    account, conversation, message, keys, path = _fixture(tmp_path, original=original)
    path.with_stem(path.stem + "_t").write_bytes(_encrypted(_png(), keys))
    resolver = MediaResolver(account, "wxid_self", image_keys=keys, ffmpeg_executable="")
    resolved = resolver.resolve(message)
    assert resolved is not None and resolved.data == _png()
    assert resolved.is_thumbnail and resolver.stats.thumbnails == 1

    from wechat_exporter.chat_html_archive import export_chat_html_archive
    html = export_chat_html_archive(root=tmp_path / "html", conversation=conversation,
                                    messages=(message,), media_resolver=resolver)
    script = next((html.root / "data").glob("messages-*.js")).read_text(encoding="utf-8")
    records = json.loads(script[script.index(",") + 1:script.rindex(")")])
    assert records[0]["media"][0]["is_thumbnail"] is True
    assert html.exported_media_count == 1 and html.missing_media_count == 0


def test_offline_shared_media_pipeline_exports_html_pdf_and_ai_package(tmp_path, monkeypatch):
    account, conversation, image_message, keys, _ = _fixture(tmp_path)
    cache = _cache(account, tmp_path / "cache")
    assert cache.save_image_keys("wxid_self", keys)
    video_bytes = b"\x00\x00\x00\x18ftypisom" + bytes(64)
    local_md5, content_md5 = "b" * 32, "c" * 32
    video = account.account_dir / "msg/video/2026-10" / f"{local_md5}.mp4"
    video.parent.mkdir(parents=True)
    video.write_bytes(video_bytes)
    reference = extract_media_reference(
        43, f'<msg><videomsg md5="{content_md5}" length="{len(video_bytes)}"/></msg>',
        b"\x08\x3d\x10\x01\x22\x22" + local_md5.encode(),
    )
    assert reference is not None and reference.md5 == content_md5
    assert reference.local_md5 == local_md5
    video_message = Message(2, image_message.timestamp + 1, 43, "wxid_self", "我", True,
                            "[视频]", conversation_id=conversation.username, media=reference)
    messages = (image_message, video_message)

    class Archive:
        self_wxid = "wxid_self"
        profile_store = None
        avatar_resolver = None

        def export_workload(self, *_a, **_k):
            return ExportWorkload(message_count=2, image_count=1)

        def iter_messages(self, *_a, **_k):
            yield from messages

    def resolver_factory(account, wxid, **kwargs):
        return MediaResolver(account, wxid, image_key_cache=cache, **kwargs)

    monkeypatch.setattr(service_module, "MediaResolver", resolver_factory)
    monkeypatch.setattr(media, "discover_account_image_keys",
                        lambda *_a, **_k: pytest.fail("offline pipeline must not require WeChat"))
    service = ExporterService(account)
    service.archive = Archive()
    output = tmp_path / "exports"
    html = service.export(ExportRequest((conversation,), output, include_txt=False,
                         include_html=True, html_options=HtmlExportOptions(include_videos=True)))
    manifest = json.loads((html.files[0].parent / "data/manifest.json").read_text(encoding="utf-8"))
    assert manifest["summary"]["media_exported"] == 2
    assert manifest["summary"]["media_missing"] == 0
    assert next((html.files[0].parent / "resources/videos").glob("*.mp4")).read_bytes() == video_bytes

    pdf = service.export(ExportRequest((conversation,), output, include_txt=False,
                        include_pdf=True, include_pdf_images=True))
    assert sum(len(page.images) for page in PdfReader(pdf.files[0]).pages) == 1

    package = service.export_jsonl_package(JsonlPackageRequest((conversation,), output))
    with zipfile.ZipFile(package.files[0]) as archive:
        rows = [json.loads(line) for line in archive.read("messages.jsonl").decode().splitlines()]
        assert [r["media"][0]["status"] for r in rows] == ["exported", "exported"]
        assert archive.read(rows[0]["media"][0]["path"]) == _png()
        assert archive.read(rows[1]["media"][0]["path"]) == video_bytes
