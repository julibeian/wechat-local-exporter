from __future__ import annotations

import hashlib
import io
import json
import os
import shutil
import zipfile
from types import SimpleNamespace

import pytest
from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from PIL import Image
from pypdf import PdfReader

from wechat_exporter import image_probe, media, service as service_module
from wechat_exporter.database_cache import AccountDatabaseCache
from wechat_exporter.estimates import build_chat_size_profile
from wechat_exporter.media import MediaResolver, decrypt_emoticon_cache, derive_emoticon_key, extract_media_reference
from wechat_exporter.models import (
    AccountLocation, Conversation, ExportRequest, ExportWorkload,
    JsonlPackageRequest, MediaReference, Message,
)
from wechat_exporter.service import ExporterService


def _encrypted(data, key, *, cbc=True):
    padder = padding.PKCS7(128).padder()
    padded = padder.update(data) + padder.finalize()
    cipher = Cipher(algorithms.AES(key), modes.CBC(key) if cbc else modes.ECB()).encryptor()
    return cipher.update(padded) + cipher.finalize()


def _cache(account, root):
    def unprotect(data, *, entropy):
        if not data.startswith(entropy + b"\0"):
            raise OSError("different account")
        return data[len(entropy) + 1:]
    return AccountDatabaseCache(account, root=root,
        protect=lambda data, *, entropy: entropy + b"\0" + data, unprotect=unprotect)


@pytest.fixture
def sticker(tmp_path):
    account = AccountLocation(tmp_path / "account", "wxid_self", "test")
    conversation = Conversation("friend", "表情缓存验收")
    stream = io.BytesIO()
    Image.new("RGB", (48, 32), "red").save(stream, format="GIF", save_all=True,
        append_images=[Image.new("RGB", (48, 32), "green")], duration=150, loop=0)
    data = stream.getvalue()
    md5 = hashlib.md5(data).hexdigest()
    key = derive_emoticon_key(2**40 + 123456, "wxid_self")
    path = account.account_dir / "business/emoticon/Persist" / md5[:2] / md5
    path.parent.mkdir(parents=True)
    path.write_bytes(_encrypted(data, key))
    message = Message(1, 1791500000, 47, "wxid_self", "我", True, "[动画表情]",
        conversation_id=conversation.username, media=MediaReference("emoticon", md5=md5))
    return account, conversation, message, data, key, path


def test_local_sticker_key_is_raw_digest_with_full_uin(sticker):
    assert sticker[4] == hashlib.md5(b"1099511751232wxid_selfEMOTICON").digest()
    assert decrypt_emoticon_cache(sticker[5].read_bytes(), sticker[4]) == sticker[3]
    with pytest.raises(ValueError):
        decrypt_emoticon_cache(sticker[5].read_bytes(), b"wrong-key-1234567")


def test_key_discovery_requires_matching_account_and_valid_file(sticker, monkeypatch):
    account, _, _, data, key, path = sticker
    calls = []
    def candidates(pid, *, full_uin=False):
        calls.append((pid, full_uin))
        return [(2**40 + 123456, "wxid_other"), (123, account.wxid), (2**40 + 123456, account.wxid)]
    monkeypatch.setattr(media, "_read_cfg_candidates", candidates)
    assert media.discover_account_emoticon_key(account.wxid, path.read_bytes(), [SimpleNamespace(pid=1)]) == key
    assert calls == [(1, True)]
    assert media.discover_account_emoticon_key("wxid_unknown", path.read_bytes(), [SimpleNamespace(pid=2)]) is None


@pytest.mark.parametrize("store", ["Persist", "ThumbStore"])
def test_sticker_key_captured_at_connection_reused_offline_with_animation(sticker, tmp_path, monkeypatch, store):
    account, _, message, data, key, original = sticker
    if store == "ThumbStore":
        thumbnail = account.account_dir / "business/emoticon/ThumbStore" / (original.name + ".gif")
        thumbnail.parent.mkdir(parents=True)
        original.rename(thumbnail)
    cache = _cache(account, tmp_path / "cache")
    monkeypatch.setattr(media, "discover_account_emoticon_key", lambda *_a, **_k: key)
    MediaResolver(account, account.wxid, image_key_cache=cache).prepare_local_media_keys()
    assert cache.load_emoticon_key(account.wxid) == key
    monkeypatch.setattr(media, "discover_account_emoticon_key", lambda *_a, **_k: pytest.fail("offline key acquisition"))
    monkeypatch.setattr(MediaResolver, "_download", lambda *_a: pytest.fail("offline download"))
    resolved = MediaResolver(account, account.wxid, image_key_cache=cache).resolve(message)
    assert resolved is not None and resolved.data == data and resolved.is_animated
    assert resolved.is_thumbnail == (store == "ThumbStore")


@pytest.mark.parametrize("failure", ["stale", "corrupt", "account", "path", "wxid"])
def test_invalid_sticker_cache_is_validated_before_reuse(sticker, tmp_path, monkeypatch, failure):
    account, _, message, data, key, _ = sticker
    cache = _cache(account, tmp_path / "cache")
    assert cache.save_emoticon_key(account.wxid, key)
    if failure == "stale":
        assert cache.save_emoticon_key(account.wxid, b"incorrect-key-12")
    elif failure == "corrupt":
        cache.emoticon_key_path.write_bytes(b"broken")
    elif failure in {"account", "path"}:
        other = AccountLocation(tmp_path / "other", "wxid_other" if failure == "account" else account.wxid, "test")
        foreign = _cache(other, tmp_path / "cache")
        assert foreign.save_emoticon_key(account.wxid, key)
        shutil.copyfile(foreign.emoticon_key_path, cache.emoticon_key_path)
    else:
        assert cache.save_emoticon_key("wxid_other", key)
    calls = []
    monkeypatch.setattr(media, "discover_account_emoticon_key", lambda *_a, **_k: calls.append(1) or key)
    resolved = MediaResolver(account, account.wxid, image_key_cache=cache).resolve(message)
    assert resolved is not None and resolved.data == data
    assert calls == [1] and cache.load_emoticon_key(account.wxid) == key


@pytest.mark.skipif(os.name != "nt", reason="Windows DPAPI")
def test_sticker_key_cache_is_user_protected_and_separate_from_image_keys(sticker, tmp_path):
    account, _, _, _, key, _ = sticker
    cache = AccountDatabaseCache(account, root=tmp_path / "protected")
    assert cache.save_emoticon_key(account.wxid, key)
    protected = cache.emoticon_key_path.read_bytes()
    assert key not in protected and key.hex().encode() not in protected
    assert cache.load_emoticon_key(account.wxid) == key
    assert cache.load_emoticon_key("wxid_other") is None
    assert not cache.image_keys_path.exists()


def test_android_identifier_and_local_thumbnail_fallback(sticker):
    account, _, message, data, key, original = sticker
    alternate = original.name
    reference = extract_media_reference(47, f'<msg><emoji md5="{"a"*32}" androidmd5="{alternate}"/></msg>')
    assert reference is not None and reference.local_md5 == alternate
    original.write_bytes(b"corrupt cached original")
    thumbnail = account.account_dir / "business/emoticon/Thumb" / alternate[:2] / alternate
    thumbnail.parent.mkdir(parents=True)
    thumbnail.write_bytes(_encrypted(data, key))
    from dataclasses import replace
    resolved = MediaResolver(account, account.wxid, emoticon_key=key).resolve(replace(message, media=reference))
    assert resolved is not None and resolved.data == data and resolved.is_thumbnail
    assert not MediaResolver(account, account.wxid)._find_emoticon_candidates(MediaReference("emoticon", md5="../outside"))


def test_encrypted_wxgf_sticker_uses_the_export_decoder(sticker, monkeypatch):
    account, _, message, _, key, original = sticker
    wxgf = b"wxgf" + bytes(128)
    stream = io.BytesIO()
    Image.new("RGBA", (48, 32), "#95ec69").save(stream, format="PNG")
    png = stream.getvalue()
    original.write_bytes(_encrypted(wxgf, key))
    seen = []
    monkeypatch.setattr(MediaResolver, "_wxgf_to_png", lambda self, data: seen.append(data) or png)
    resolved = MediaResolver(account, account.wxid, emoticon_key=key).resolve(message)
    assert resolved is not None and resolved.data == png and resolved.image_format == "PNG"
    assert seen == [wxgf]


@pytest.mark.parametrize("cbc", [True, False], ids=["current-cbc", "legacy-ecb"])
def test_cdn_sticker_decryption_works_for_current_and_legacy_modes(sticker, monkeypatch, cbc):
    account, _, message, data, key, path = sticker
    path.unlink()
    reference = MediaReference("emoticon", md5=message.media.md5, encrypted_url="https://wx.qpic.cn/synthetic", aes_key=key.hex())
    downloaded = []
    monkeypatch.setattr(MediaResolver, "_download", lambda self, url: downloaded.append(url) or _encrypted(data, key, cbc=cbc))
    from dataclasses import replace
    resolved = MediaResolver(account, account.wxid, allow_network=True).resolve(replace(message, media=reference))
    assert resolved is not None and resolved.data == data and resolved.is_animated
    assert downloaded == [reference.encrypted_url]
    with pytest.raises(ValueError):
        media._decrypt_cdn_blob(_encrypted(b"not a media file", key, cbc=cbc), key.hex())


def test_encrypted_stickers_export_and_estimate_consistently_in_all_rich_formats(sticker, tmp_path, monkeypatch):
    account, conversation, message, data, key, _ = sticker
    cache = _cache(account, tmp_path / "cache")
    assert cache.save_emoticon_key(account.wxid, key)
    monkeypatch.setattr(image_probe, "AccountDatabaseCache", lambda _account: cache)
    monkeypatch.setattr(service_module, "MediaResolver", lambda a, w, **kw: MediaResolver(a, w, image_key_cache=cache, **kw))
    monkeypatch.setattr(media, "discover_account_emoticon_key", lambda *_a, **_k: pytest.fail("offline key acquisition"))
    monkeypatch.setattr(MediaResolver, "_download", lambda *_a: pytest.fail("offline download"))
    archive = SimpleNamespace(self_wxid=account.wxid, profile_store=None,
        iter_messages=lambda *_a, **_k: iter((message,)),
        export_workload=lambda *_a, **_k: ExportWorkload(1, emoticon_count=1))
    service = ExporterService(account)
    service.archive = archive
    profile = build_chat_size_profile(archive, account, (conversation,))
    estimate = profile.estimate("chat")
    assert estimate.unknown_local == 0
    html = service.export(ExportRequest((conversation,), tmp_path / "html", include_txt=False, include_html=True))
    root = html.files[0].parent
    actual = sum(p.stat().st_size for p in root.rglob("*") if p.is_file())
    assert abs(estimate.byte_count - actual) < 500
    script = next((root / "data").glob("messages-*.js")).read_text(encoding="utf-8")
    row = json.loads(script[script.index(",") + 1:script.rindex(")")])[0]
    assert row["media"][0]["animated"] is True
    assert (root / row["media"][0]["path"]).read_bytes() == data
    pdf = service.export(ExportRequest((conversation,), tmp_path / "pdf", include_txt=False, include_pdf=True, include_pdf_images=True))
    assert sum(len(page.images) for page in PdfReader(pdf.files[0]).pages) == 1
    package = service.export_jsonl_package(JsonlPackageRequest((conversation,), tmp_path / "ai"))
    with zipfile.ZipFile(package.files[0]) as packed:
        row = json.loads(packed.read("messages.jsonl"))
        assert row["media"][0]["status"] == "exported"
        assert packed.read(row["media"][0]["path"]) == data
