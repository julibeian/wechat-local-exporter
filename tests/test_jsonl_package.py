from __future__ import annotations

import hashlib
import io
import json
import zipfile
from datetime import datetime

from PIL import Image

from wechat_exporter.jsonl_package import ChatVideoLocator, export_conversation_jsonl_package
from wechat_exporter.models import (
    AccountLocation,
    AttachmentReference,
    Conversation,
    JsonlPackageRequest,
    MediaReference,
    Message,
    PdfImage,
)
from wechat_exporter.service import ExportCancelled, ExporterService


def _message(
    local_id: int,
    message_type: int,
    content: str,
    *,
    media: MediaReference | None = None,
    attachment: AttachmentReference | None = None,
    raw_content: str = "",
    conversation_id: str = "wxid_friend",
) -> Message:
    return Message(
        local_id=local_id,
        timestamp=int(datetime(2026, 9, 2, 9, local_id).timestamp()),
        message_type=message_type,
        sender_id="wxid_friend",
        sender_name="好友",
        is_outgoing=False,
        content=content,
        source_db="message/message_0.db",
        conversation_id=conversation_id,
        media=media,
        attachment=attachment,
        raw_content=raw_content,
    )


class _Images:
    def resolve(self, message: Message) -> PdfImage | None:
        if message.media and message.media.md5 == "image":
            return PdfImage(
                data=b"\x89PNG\r\n\x1a\nimage",
                image_format="PNG",
                width=1,
                height=1,
                source="本机原图缓存",
            )
        return None


def test_package_contains_jsonl_manifest_media_and_explicit_missing_states(tmp_path) -> None:
    account_dir = tmp_path / "account"
    account = AccountLocation(account_dir, "wxid_self", "test")
    conversation = Conversation("wxid_friend", "张老师")
    video_md5 = "b" * 32
    chat_hash = hashlib.md5(conversation.username.encode()).hexdigest()
    video = account_dir / "msg" / "attach" / chat_hash / "2026-09" / "Video" / f"{video_md5}.mp4"
    video.parent.mkdir(parents=True)
    video.write_bytes(b"\0\0\0\x18ftypisom" + b"v" * 32)
    messages = (
        _message(1, 1, "明天十点见"),
        _message(2, 3, "[图片]", media=MediaReference("image", md5="image")),
        _message(3, 47, "[动画表情]", media=MediaReference("emoticon", md5="missing")),
        _message(4, 43, "[视频]", media=MediaReference("video", md5=video_md5)),
        _message(
            5,
            49,
            "[文件] 讲义.pdf",
            attachment=AttachmentReference("讲义.pdf", extension="pdf", size=1024),
        ),
        _message(6, 34, "[微信语音转文字] 明天带讲义"),
    )
    request = JsonlPackageRequest((conversation,), tmp_path / "output")
    result = export_conversation_jsonl_package(
        account=account,
        self_wxid="wxid_self",
        conversation=conversation,
        messages=messages,
        output_dir=request.output_dir,
        request=request,
        image_resolver=_Images(),  # type: ignore[arg-type]
    )

    assert result.message_count == 6
    with zipfile.ZipFile(result.path) as archive:
        names = set(archive.namelist())
        assert {"messages.jsonl", "manifest.json", "导出说明.txt"} <= names
        assert "media/images/" in names
        assert "media/stickers/" in names
        assert "media/videos/" in names
        assert not any(name.startswith("media/audio") for name in names)
        rows = [json.loads(line) for line in archive.read("messages.jsonl").decode().splitlines()]
        manifest = json.loads(archive.read("manifest.json"))

    assert len(rows) == 6
    assert rows[1]["media"][0]["status"] == "exported"
    assert rows[1]["media"][0]["path"].startswith("media/images/")
    assert rows[2]["media"][0]["status"] == "not_available_locally"
    assert rows[3]["media"][0]["status"] == "exported"
    assert rows[3]["media"][0]["path"].startswith("media/videos/")
    assert rows[4]["attachment"]["included"] is False
    assert rows[4]["attachment"]["export_via"] == "batch_chat_files"
    assert rows[5]["transcript"]["source"] == "wechat"
    assert rows[5]["media"] == []
    assert manifest["settings"]["voice_policy"] == "wechat_transcript_only"
    assert manifest["settings"]["ordinary_file_policy"] == "metadata_only"
    assert manifest["settings"]["local_media_only"] is True


def test_oversized_video_is_not_copied_but_remains_in_jsonl(tmp_path) -> None:
    account_dir = tmp_path / "account"
    account = AccountLocation(account_dir, "wxid_self", "test")
    conversation = Conversation("wxid_friend", "好友")
    video_md5 = "c" * 32
    chat_hash = hashlib.md5(conversation.username.encode()).hexdigest()
    video = account_dir / "msg" / "attach" / chat_hash / "2026-09" / "Video" / f"{video_md5}.mp4"
    video.parent.mkdir(parents=True)
    video.write_bytes(b"x" * 20)
    request = JsonlPackageRequest(
        (conversation,),
        tmp_path / "output",
        max_video_size_bytes=10,
    )
    result = export_conversation_jsonl_package(
        account=account,
        self_wxid="wxid_self",
        conversation=conversation,
        messages=(_message(1, 43, "[视频]", media=MediaReference("video", md5=video_md5)),),
        output_dir=request.output_dir,
        request=request,
        image_resolver=_Images(),  # type: ignore[arg-type]
    )
    with zipfile.ZipFile(result.path) as archive:
        row = json.loads(archive.read("messages.jsonl").decode())
        assert row["media"][0]["status"] == "too_large"
        assert not any(name.startswith("media/videos/") and not name.endswith("/") for name in archive.namelist())


def test_unrelated_video_is_not_matched_by_size_and_month(tmp_path) -> None:
    account_dir = tmp_path / "account"
    account = AccountLocation(account_dir, "wxid_self", "test")
    conversation = Conversation("wxid_friend", "好友")
    chat_hash = hashlib.md5(conversation.username.encode()).hexdigest()
    unrelated = (
        account_dir
        / "msg"
        / "attach"
        / chat_hash
        / "2026-09"
        / "Video"
        / f"{'d' * 32}.mp4"
    )
    unrelated.parent.mkdir(parents=True)
    unrelated.write_bytes(b"same-size-video")
    request = JsonlPackageRequest((conversation,), tmp_path / "output")
    result = export_conversation_jsonl_package(
        account=account,
        self_wxid="wxid_self",
        conversation=conversation,
        messages=(
            _message(
                1,
                43,
                "[视频]",
                media=MediaReference(
                    "video",
                    md5="e" * 32,
                    size=unrelated.stat().st_size,
                ),
            ),
        ),
        output_dir=request.output_dir,
        request=request,
        image_resolver=_Images(),  # type: ignore[arg-type]
    )

    with zipfile.ZipFile(result.path) as archive:
        row = json.loads(archive.read("messages.jsonl").decode())
        assert row["media"][0]["status"] == "not_available_locally"
        assert not any(
            name.startswith("media/videos/") and not name.endswith("/")
            for name in archive.namelist()
        )


def test_named_video_with_conflicting_declared_size_is_not_used(tmp_path) -> None:
    account_dir = tmp_path / "account"
    conversation_id = "wxid_friend"
    media_md5 = "e" * 32
    chat_hash = hashlib.md5(conversation_id.encode()).hexdigest()
    candidate = account_dir / "msg" / "attach" / chat_hash / f"{media_md5}.mp4"
    candidate.parent.mkdir(parents=True)
    candidate.write_bytes(b"wrong-size")
    message = _message(
        1,
        43,
        "[视频]",
        media=MediaReference("video", md5=media_md5, size=999),
        conversation_id=conversation_id,
    )

    assert (
        ChatVideoLocator(AccountLocation(account_dir, "wxid_self", "test")).find(message)
        is None
    )


def test_service_creates_one_independent_package_per_conversation(tmp_path) -> None:
    conversations = (
        Conversation("wxid_a", "甲"),
        Conversation("room@chatroom", "乙群", is_group=True),
    )

    class Archive:
        self_wxid = "wxid_self"

        def iter_messages(self, conversation, **_kwargs):
            yield Message(
                1,
                1_788_330_000,
                1,
                conversation.username,
                conversation.display_name,
                False,
                "你好",
                conversation_id=conversation.username,
            )

    service = ExporterService(AccountLocation(tmp_path / "account", "wxid_self", "test"))
    service.archive = Archive()  # type: ignore[assignment]
    result = service.export_jsonl_package(
        JsonlPackageRequest(conversations, tmp_path / "output")
    )
    assert len(result.files) == 2
    assert all(path.suffix == ".zip" for path in result.files)
    assert len({path.parent for path in result.files}) == 2
    assert set(result.file_categories.values()) == {"chat_package"}


def test_service_shared_media_resolver_scopes_image_cache_by_conversation(tmp_path) -> None:
    account_dir = tmp_path / "account"
    conversations = (
        Conversation("wxid_without_image", "无本机图片"),
        Conversation("wxid_with_image", "有本机图片"),
    )
    media_md5 = "a" * 32
    image_stream = io.BytesIO()
    Image.new("RGB", (2, 2), (30, 160, 90)).save(image_stream, format="PNG")
    image_data = image_stream.getvalue()
    chat_hash = hashlib.md5(conversations[1].username.encode()).hexdigest()
    image_path = (
        account_dir
        / "msg"
        / "attach"
        / chat_hash
        / "2026-09"
        / "Img"
        / f"{media_md5}.dat"
    )
    image_path.parent.mkdir(parents=True)
    image_path.write_bytes(image_data)

    class Archive:
        self_wxid = "wxid_self"

        def iter_messages(self, conversation, **_kwargs):
            yield _message(
                1,
                3,
                "[图片]",
                media=MediaReference("image", md5=media_md5),
                conversation_id=conversation.username,
            )

    service = ExporterService(AccountLocation(account_dir, "wxid_self", "test"))
    service.archive = Archive()  # type: ignore[assignment]
    result = service.export_jsonl_package(
        JsonlPackageRequest(conversations, tmp_path / "output")
    )

    packages = {
        result.file_conversations[path].username: path for path in result.files
    }
    with zipfile.ZipFile(packages[conversations[0].username]) as archive:
        missing_row = json.loads(archive.read("messages.jsonl"))
    with zipfile.ZipFile(packages[conversations[1].username]) as archive:
        exported_row = json.loads(archive.read("messages.jsonl"))
        exported_path = exported_row["media"][0]["path"]
        assert archive.read(exported_path) == image_data

    assert missing_row["media"][0]["status"] == "not_available_locally"
    assert exported_row["media"][0]["status"] == "exported"


def test_cancelled_package_never_leaves_a_partial_zip(tmp_path) -> None:
    account = AccountLocation(tmp_path / "account", "wxid_self", "test")
    conversation = Conversation("wxid_friend", "好友")
    request = JsonlPackageRequest((conversation,), tmp_path / "output")
    checks = 0

    def cancel_during_messages() -> None:
        nonlocal checks
        checks += 1
        if checks >= 2:
            raise ExportCancelled()

    try:
        export_conversation_jsonl_package(
            account=account,
            self_wxid="wxid_self",
            conversation=conversation,
            messages=(_message(1, 1, "第一条"), _message(2, 1, "第二条")),
            output_dir=request.output_dir,
            request=request,
            image_resolver=_Images(),  # type: ignore[arg-type]
            check_cancelled=cancel_during_messages,
        )
    except ExportCancelled:
        pass
    else:
        raise AssertionError("expected cancellation")

    assert not list(request.output_dir.glob("*.zip"))
    assert not list(request.output_dir.glob("*.tmp"))
    assert not list(request.output_dir.glob(".聊天资料包构建-*"))
