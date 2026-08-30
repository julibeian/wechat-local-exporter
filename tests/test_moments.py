from __future__ import annotations

import io
import json
import sqlite3
import struct
from datetime import datetime
from pathlib import Path

from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from PIL import Image
from pypdf import PdfReader

from wechat_exporter.archive import WeChatArchive
from wechat_exporter.crypto import DatabaseKeys, DecryptedWorkspace
from wechat_exporter.exporters import MomentsPdfWriter
from wechat_exporter.media import (
    MediaResolver,
    _isaac64_keystream,
    _isaac64_xor,
    _with_token,
    derive_image_keys,
)
from wechat_exporter.moments import parse_moment_xml
from wechat_exporter.moments_archive import MomentsArchiveWriter
from wechat_exporter.models import (
    AccountLocation,
    Conversation,
    Moment,
    MomentMedia,
    MomentMediaFile,
    PdfImage,
)
from wechat_exporter.service import ExporterService, _publish_directory


def _jpeg_bytes(size: tuple[int, int] = (1200, 800)) -> bytes:
    stream = io.BytesIO()
    Image.new("RGB", size, (52, 115, 180)).save(stream, format="JPEG", quality=96)
    return stream.getvalue()


def _gif_bytes() -> bytes:
    stream = io.BytesIO()
    first = Image.new("RGB", (12, 12), (220, 30, 30))
    final = Image.new("RGB", (12, 12), (20, 40, 220))
    first.save(
        stream,
        format="GIF",
        save_all=True,
        append_images=[final],
        duration=[80, 600],
        loop=0,
    )
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


def test_archive_loads_pinned_and_dated_contact_moments(tmp_path) -> None:
    workspace = DecryptedWorkspace(
        tmp_path / "encrypted",
        DatabaseKeys({"sns\\sns.db": b"s" * 32}),
    )
    path = workspace.decrypted_path("sns\\sns.db")
    path.parent.mkdir(parents=True)
    connection = sqlite3.connect(path)
    connection.execute(
        "CREATE TABLE SnsTimeLine(tid INTEGER PRIMARY KEY, user_name TEXT, content TEXT, is_top INTEGER)"
    )
    pinned_time = int(datetime(2025, 1, 2, 9, 30).timestamp())
    recent_time = int(datetime(2026, 8, 27, 18, 0).timestamp())
    pinned_xml = f"""
    <SnsDataItem><id>100</id><username>wxid_friend</username>
      <createTime>{pinned_time}</createTime><contentDesc>置顶内容 &amp; 说明</contentDesc>
      <isTop>1</isTop><location poiName="公园" city="南京" />
      <ContentObject><mediaList><media>
        <url md5="{'a' * 32}" token="original-token" encIdx="7">https://mmbiz.qpic.cn/original.jpg</url>
        <thumb token="thumb-token">https://mmbiz.qpic.cn/thumb.jpg</thumb>
        <size width="4032" height="3024" totalSize="1234567" />
      </media></mediaList></ContentObject>
    </SnsDataItem>
    """
    recent_xml = f"""
    <SnsDataItem><id>101</id><username>wxid_friend</username>
      <createTime>{recent_time}</createTime><contentDesc>最近内容</contentDesc>
    </SnsDataItem>
    """
    other_xml = recent_xml.replace("wxid_friend", "wxid_other")
    connection.executemany(
        "INSERT INTO SnsTimeLine VALUES(?,?,?,?)",
        [
            (100, "wxid_friend", pinned_xml, 1),
            (101, "wxid_friend", recent_xml, 0),
            (102, "wxid_other", other_xml, 0),
        ],
    )
    connection.commit()
    connection.close()

    account = AccountLocation(tmp_path / "wxid_self_abcd", "wxid_self_abcd", "test")
    archive = WeChatArchive(account, workspace)
    moments = archive.contact_moments(Conversation("wxid_friend", "好友"))

    assert [item.post_id for item in moments] == ["100", "101"]
    assert moments[0].is_pinned
    assert moments[0].content == "置顶内容 & 说明"
    assert moments[0].location == "公园 · 南京"
    assert moments[0].media == (
        MomentMedia(
            md5="a" * 32,
            original_url="https://mmbiz.qpic.cn/original.jpg",
            thumbnail_url="https://mmbiz.qpic.cn/thumb.jpg",
            token="original-token",
            thumbnail_token="thumb-token",
            enc_idx="7",
            width=4032,
            height=3024,
            total_size=1234567,
            month="2025-01",
        ),
    )
    workspace.close()


def test_moments_local_cache_image_is_decrypted_without_reencoding(tmp_path) -> None:
    account_dir = tmp_path / "wxid_self_abcd"
    account = AccountLocation(account_dir, "wxid_self_abcd", "test")
    md5 = "1234567890abcdef1234567890abcdef"
    original = _jpeg_bytes((640, 480))
    keys = derive_image_keys(123456, "wxid_self")
    cached = account_dir / "cache" / "2026-08" / "Sns" / "Img" / md5[:2] / md5[2:]
    cached.parent.mkdir(parents=True)
    cached.write_bytes(_encrypt_v2(original, *keys))

    resolver = MediaResolver(account, "wxid_self", image_keys=keys)
    image = resolver.resolve_moment(MomentMedia(md5=md5))

    assert image is not None
    assert image.data == original
    assert image.source == "本机朋友圈缓存（原始字节）"
    assert not image.is_thumbnail


def test_animated_moment_keeps_original_and_generates_final_stopped_frame(tmp_path) -> None:
    animated = _gif_bytes()
    resolver = MediaResolver(
        AccountLocation(tmp_path, "self", "test"),
        "self",
        download=lambda _url: animated,
        ffmpeg_executable="",
    )

    resolved = resolver.resolve_moment_file(
        MomentMedia(original_url="https://mmbiz.qpic.cn/animated.gif")
    )

    assert resolved is not None
    assert resolved.extension == "gif"
    assert resolved.data == animated
    assert resolved.is_animated
    assert resolved.fallback_extension == "png"
    with Image.open(io.BytesIO(resolved.fallback_data)) as stopped:
        assert stopped.convert("RGB").getpixel((0, 0)) == (20, 40, 220)


def test_live_video_tries_its_exact_tokenized_path_before_image_style_path(tmp_path) -> None:
    requested: list[str] = []
    video = b"\x00\x00\x00\x18ftypisom" + b"\x00" * 64

    def download(url: str) -> bytes:
        requested.append(url)
        return video

    resolver = MediaResolver(
        AccountLocation(tmp_path, "self", "test"),
        "self",
        download=download,
        ffmpeg_executable="",
    )
    resolved = resolver.resolve_moment_file(
        MomentMedia(
            kind="video",
            role="live_photo_video",
            original_url="https://vweixinf.tc.qq.com/live/150",
            token="video-token",
            enc_idx="3",
        )
    )

    assert resolved is not None
    assert requested[0].startswith("https://vweixinf.tc.qq.com/live/150?")
    assert "token=video-token" in requested[0]
    assert "/0?" not in requested[0]


def test_moments_pdf_groups_dates_and_links_to_full_image_page(tmp_path) -> None:
    timestamp = int(datetime(2026, 8, 27, 18, 30).timestamp())
    media = MomentMedia(md5="b" * 32)
    moment = Moment(
        post_id="100",
        username="wxid_friend",
        timestamp=timestamp,
        content="测试朋友圈正文",
        media=(media,),
        is_pinned=True,
    )
    jpeg = _jpeg_bytes()
    image = PdfImage(
        data=jpeg,
        image_format="JPEG",
        width=1200,
        height=800,
        source="微信官方 CDN 原图",
    )
    path = tmp_path / "moments.pdf"
    with MomentsPdfWriter(path, Conversation("wxid_friend", "好友")) as writer:
        writer.write(moment, ((media, image),))

    reader = PdfReader(path)
    extracted = "\n".join(page.extract_text() or "" for page in reader.pages)
    assert "微信朋友圈公开内容" in extracted
    assert "置顶" in extracted
    assert "2026 年 08 月 27 日" in extracted
    assert "测试朋友圈正文" in extracted
    assert len(reader.pages) == 2
    assert any(page.get("/Annots") for page in reader.pages)
    dimensions = []
    for page in reader.pages:
        resources = page.get("/Resources")
        if not resources or "/XObject" not in resources:
            continue
        xobjects = resources["/XObject"].get_object()
        for value in xobjects.values():
            obj = value.get_object()
            if obj.get("/Subtype") == "/Image":
                dimensions.append((obj["/Width"], obj["/Height"]))
    assert (1200, 800) in dimensions


def test_service_moments_export_copies_archive_when_windows_blocks_rename(
    tmp_path, monkeypatch
) -> None:
    account = AccountLocation(tmp_path / "wxid_self_abcd", "wxid_self_abcd", "test")
    service = ExporterService(account)
    conversation = Conversation("wxid_friend", "好友")
    moment = Moment(
        post_id="1",
        username=conversation.username,
        timestamp=int(datetime(2026, 8, 27, 8, 0).timestamp()),
        content="纯文字朋友圈",
    )

    class FakeArchive:
        self_wxid = "wxid_self"

        @staticmethod
        def contact_moments(_conversation: Conversation) -> list[Moment]:
            return [moment]

    service.archive = FakeArchive()  # type: ignore[assignment]
    real_replace = Path.replace
    blocked_attempts = 0

    def deny_temporary_directory_rename(path: Path, target: Path) -> Path:
        nonlocal blocked_attempts
        if path.name.startswith(".朋友圈归档构建-"):
            blocked_attempts += 1
            raise PermissionError(13, "Windows 正在占用归档文件", str(path))
        return real_replace(path, target)

    monkeypatch.setattr(Path, "replace", deny_temporary_directory_rename)
    monkeypatch.setattr("wechat_exporter.service.time.sleep", lambda _delay: None)
    result = service.export_moments_archive(conversation, tmp_path / "exports")

    assert blocked_attempts > 1
    assert {path.name for path in result.files} == {
        "index.html",
        "moments.json",
        "manifest-sha256.txt",
    }
    assert result.files[0].parent.parent.name == "朋友圈"
    assert result.files[0].parent.parent.parent.parent.name == "联系人"
    assert not list((tmp_path / "exports").rglob("*.pdf"))
    assert not list((tmp_path / "exports").rglob(".朋友圈归档构建-*"))


def test_publish_directory_retries_a_transient_access_error(
    tmp_path, monkeypatch
) -> None:
    source = tmp_path / "source"
    destination = tmp_path / "destination"
    source.mkdir()
    (source / "archive.txt").write_text("完整归档", encoding="utf-8")
    real_replace = Path.replace
    attempts = 0

    def fail_twice(path: Path, target: Path) -> Path:
        nonlocal attempts
        attempts += 1
        if attempts <= 2:
            raise PermissionError(13, "Windows 正在占用归档文件", str(path))
        return real_replace(path, target)

    monkeypatch.setattr(Path, "replace", fail_twice)
    published = _publish_directory(source, destination, retry_delays=(0.0, 0.0))

    assert attempts == 3
    assert published == destination
    assert (destination / "archive.txt").read_text(encoding="utf-8") == "完整归档"
    assert not source.exists()


def test_isaac64_xor_roundtrip() -> None:
    """ISAAC-64 CDN decryption round-trips a JPEG with the fixed algorithm."""
    key = "7400016519024241812"
    plain = _jpeg_bytes((16, 16))
    stream = _isaac64_keystream(key, len(plain))
    assert stream is not None
    cipher = bytes(a ^ b for a, b in zip(plain, stream, strict=True))
    assert cipher != plain

    decrypted = _isaac64_xor(cipher, key)
    assert decrypted == plain


def test_isaac64_keystream_fixed_vector() -> None:
    stream = _isaac64_keystream("7400016519024241812", 64)
    assert stream is not None
    assert stream.hex() == (
        "d11f5124938a6ef489102d6f92e1e820"
        "563e87b6f8a84aef78ca759c91a7e543"
        "a20a5ff048379652b36bdb3c0e08c949"
        "45ae63fcf9b02e451cb02307cebded25"
    )


def test_isaac64_xor_rejects_non_image_output() -> None:
    assert _isaac64_xor(b"\x00" * 64, "123") is None
    assert _isaac64_xor(b"", "123") is None
    assert _isaac64_xor(b"\xff\xd8\xff\xe0" + b"\x00" * 64, "") is None


def test_parser_keeps_live_photo_video_and_global_key() -> None:
    xml = """
    <SnsDataItem><id>live-1</id><username>wxid_friend</username>
      <createTime>1787800000</createTime><enc key="2136343393" />
      <ContentObject><mediaList><media><type>2</type>
        <url md5="aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa" key="9988">https://shmmsns.qpic.cn/still/150</url>
        <thumb>https://shmmsns.qpic.cn/still/thumb</thumb>
        <livePhoto><url token="video-token" enc_idx="3">https://vweixinf.tc.qq.com/video/live</url></livePhoto>
      </media></mediaList></ContentObject>
    </SnsDataItem>
    """
    moment = parse_moment_xml(xml)

    assert len(moment.media) == 2
    assert moment.media[0].kind == "image"
    assert moment.media[1].kind == "video"
    assert moment.media[1].role == "live_photo_video"
    assert moment.media[1].aes_key == "2136343393"
    assert moment.media[1].token == "video-token"
    assert moment.media[1].enc_idx == "3"


def test_parser_labels_private_and_group_visibility_without_filtering() -> None:
    private = parse_moment_xml(
        "<SnsDataItem><id>1</id><private>1</private><contentDesc>私密日记</contentDesc></SnsDataItem>"
    )
    selected = parse_moment_xml(
        "<SnsDataItem><id>2</id><private>0</private><groupUser><username>a</username></groupUser></SnsDataItem>"
    )
    excluded = parse_moment_xml(
        "<SnsDataItem><id>3</id><blackList><username>b</username></blackList></SnsDataItem>"
    )

    assert private.visibility == "private"
    assert private.content == "私密日记"
    assert selected.visibility == "selected"
    assert excluded.visibility == "excluded"


def test_video_decrypts_only_128k_prefix_and_preserves_tail(tmp_path) -> None:
    key = "2136343393"
    plain = (
        b"\x00\x00\x00\x18ftypisom" + b"\x00" * (140 * 1024)
    )
    prefix_size = 128 * 1024
    stream = _isaac64_keystream(key, prefix_size)
    assert stream is not None
    encrypted = bytes(
        value ^ stream[index] for index, value in enumerate(plain[:prefix_size])
    ) + plain[prefix_size:]
    resolver = MediaResolver(
        AccountLocation(tmp_path, "self", "test"), "self"
    )

    result = resolver._decode_video_blob(encrypted, key=key, source="test")

    assert result is not None
    assert result.extension == "mp4"
    assert result.data == plain


def test_archive_html_json_and_manifest_keep_original_media(tmp_path) -> None:
    conversation = Conversation("wxid_friend", "好友")
    jpeg = _jpeg_bytes((2400, 1600))
    video = b"\x00\x00\x00\x18ftypisom" + b"\x00" * 64
    image_ref = MomentMedia(kind="image", role="ordinary")
    video_ref = MomentMedia(kind="video", role="live_photo_video")
    moment = Moment(
        post_id="post-1",
        username=conversation.username,
        timestamp=int(datetime(2026, 8, 27, 18, 30).timestamp()),
        content="原图与实况视频",
        media=(image_ref, video_ref),
        is_pinned=True,
    )
    writer = MomentsArchiveWriter(tmp_path / "archive", conversation)
    writer.write(
        moment,
        (
            (
                image_ref,
                MomentMediaFile(jpeg, "jpg", "image/jpeg", "CDN 原图"),
            ),
            (
                video_ref,
                MomentMediaFile(video, "mp4", "video/mp4", "CDN 原视频"),
            ),
        ),
    )
    html_path, json_path, manifest_path = writer.finish()

    rendered = html_path.read_text(encoding="utf-8")
    payload = json.loads(json_path.read_text(encoding="utf-8"))
    assert 'loading="lazy"' in rendered
    assert '<video controls preload="metadata"' in rendered
    assert 'target="_blank"' in rendered
    assert payload["summary"] == {
        "posts": 1,
        "media_exported": 2,
        "images": 1,
        "videos": 1,
        "media_missing": 0,
        "media_fallbacks": 0,
        "visibility": {"当前账号可见": 1},
    }
    image_path = tmp_path / "archive" / payload["posts"][0]["media"][0]["path"]
    assert image_path.read_bytes() == jpeg
    assert len(manifest_path.read_text(encoding="utf-8").splitlines()) == 4


def test_missing_live_video_uses_its_static_main_image_instead_of_error(tmp_path) -> None:
    conversation = Conversation("wxid_friend", "好友")
    image_ref = MomentMedia(kind="image", role="ordinary")
    live_video_ref = MomentMedia(kind="video", role="live_photo_video")
    moment = Moment(
        post_id="live-fallback",
        username=conversation.username,
        timestamp=int(datetime(2026, 8, 27, 18, 30).timestamp()),
        content="实况照片静态兜底",
        media=(image_ref, live_video_ref),
    )
    writer = MomentsArchiveWriter(tmp_path / "archive", conversation)
    writer.write(
        moment,
        (
            (
                image_ref,
                MomentMediaFile(_jpeg_bytes(), "jpg", "image/jpeg", "CDN 原图"),
            ),
            (live_video_ref, None),
        ),
    )
    html_path, json_path, _manifest_path = writer.finish()

    rendered = html_path.read_text(encoding="utf-8")
    payload = json.loads(json_path.read_text(encoding="utf-8"))
    video_item = payload["posts"][0]["media"][1]
    assert video_item["status"] == "fallback"
    assert video_item["fallback_path"] == payload["posts"][0]["media"][0]["path"]
    assert payload["summary"]["media_fallbacks"] == 1
    assert payload["summary"]["media_missing"] == 0
    assert "静态主图兜底" in rendered
    assert "实况照片视频未能导出" not in rendered


def test_self_archive_marks_private_scope_and_uses_self_folder(tmp_path) -> None:
    account = AccountLocation(tmp_path / "wxid_self_abcd", "wxid_self_abcd", "test")
    service = ExporterService(account)
    conversation = Conversation("wxid_self", "我自己（本人）", is_self=True)
    moment = Moment(
        post_id="private-1",
        username=conversation.username,
        timestamp=int(datetime(2026, 8, 20, 8, 0).timestamp()),
        content="仅自己可见内容",
        visibility="private",
    )

    class FakeArchive:
        self_wxid = "wxid_self"

        @staticmethod
        def contact_moments(_conversation: Conversation) -> list[Moment]:
            return [moment]

    service.archive = FakeArchive()  # type: ignore[assignment]
    result = service.export_moments_archive(conversation, tmp_path / "exports")
    payload = json.loads(result.files[1].read_text(encoding="utf-8"))

    assert result.files[0].parents[3].name == "本人"
    assert payload["posts"][0]["visibility_label"] == "仅自己可见"
    assert "包含私密和分组可见" in payload["scope"]
    assert any("包括私密" in warning for warning in result.warnings)


def test_with_token_appends_token_and_idx() -> None:
    url = "http://shmmsns.qpic.cn/mmsns/abc/0"
    assert _with_token(url, "tok", "1") == "https://shmmsns.qpic.cn/mmsns/abc/0?token=tok&idx=1"
    assert _with_token(url, "tok", "") == "https://shmmsns.qpic.cn/mmsns/abc/0?token=tok&idx=1"
    assert _with_token(url, "", "1") == "https://shmmsns.qpic.cn/mmsns/abc/0?idx=1"
    existing = "http://shmmsns.qpic.cn/mmsns/abc/0?token=old"
    assert _with_token(existing, "tok", "1") == "https://shmmsns.qpic.cn/mmsns/abc/0?token=tok&idx=1"
    sized = "http://shmmsns.qpic.cn/mmsns/abc/150?foo=bar"
    assert _with_token(sized, "tok", original=True) == (
        "https://shmmsns.qpic.cn/mmsns/abc/0?foo=bar&token=tok&idx=1"
    )


def test_find_sns_image_by_size_matches_decrypted_cache(tmp_path) -> None:
    account_dir = tmp_path / "wxid_self_abcd"
    account = AccountLocation(account_dir, "wxid_self_abcd", "test")
    original = _jpeg_bytes((1200, 800))
    keys = derive_image_keys(123456, "wxid_self")
    cached = account_dir / "cache" / "2026-07" / "Sns" / "Img" / "00" / "3f1b2f5f1bb9ecd8f16f8168906090"
    cached.parent.mkdir(parents=True)
    cached.write_bytes(_encrypt_v2(original, *keys))

    resolver = MediaResolver(account, "wxid_self", image_keys=keys)
    media = MomentMedia(
        md5="",
        original_url="",
        thumbnail_url="",
        width=1200,
        height=800,
        total_size=len(original),
    )
    assert resolver._find_sns_image_by_size(media) == cached
    # 仅 totalSize 也能命中
    media_only_size = MomentMedia(total_size=len(original))
    assert resolver._find_sns_image_by_size(media_only_size) == cached
