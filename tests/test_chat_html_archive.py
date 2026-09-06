from __future__ import annotations

import io
import json
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image

from wechat_exporter.chat_html_archive import (
    ChatArchiveMedia,
    export_chat_html_archive,
)
from wechat_exporter.models import (
    AccountLocation,
    AttachmentReference,
    Conversation,
    ExportRequest,
    ExportWorkload,
    HtmlExportOptions,
    MediaReference,
    Message,
    PdfImage,
)
from wechat_exporter.profiles import AvatarFallback, ResolvedAvatar
from wechat_exporter.service import ExportCancelled, ExporterService


def _image_bytes(image_format: str, color: str) -> bytes:
    stream = io.BytesIO()
    Image.new("RGB", (12, 10), color=color).save(stream, format=image_format)
    return stream.getvalue()


def _messages(conversation_id: str) -> tuple[Message, ...]:
    return (
        Message(
            1,
            1_780_000_000,
            1,
            "wxid_friend",
            "旧昵称",
            False,
            "收到[爱心] <script>alert(1)</script>",
            conversation_id=conversation_id,
        ),
        Message(
            2,
            1_780_000_010,
            3,
            "wxid_self",
            "我",
            True,
            "[图片]",
            conversation_id=conversation_id,
            media=MediaReference(kind="image", md5="a" * 32),
        ),
        Message(
            3,
            1_780_000_020,
            47,
            "wxid_friend",
            "旧昵称",
            False,
            "[动画表情]",
            conversation_id=conversation_id,
            media=MediaReference(kind="emoticon", md5="b" * 32),
        ),
        Message(
            4,
            1_780_000_030,
            49,
            "wxid_friend",
            "旧昵称",
            False,
            "[文件]",
            conversation_id=conversation_id,
            attachment=AttachmentReference(
                filename="课程表.xlsx",
                extension="xlsx",
                size=7,
            ),
        ),
    )


def test_chat_html_archive_is_offline_safe_and_preserves_visible_content(tmp_path) -> None:
    conversation = Conversation("room@chatroom", "课程群", is_group=True)
    png = _image_bytes("PNG", "#2f80ed")
    gif = _image_bytes("GIF", "#e25555")
    profile = SimpleNamespace(
        username="wxid_friend",
        group_alias="小吕",
        remark="吕新颜",
        nickname="昵称",
        alias="",
        display_name="小吕",
        avatar_fallback=AvatarFallback("吕", "#315c91"),
    )
    profiles = {"wxid_friend": profile}

    def resolve_media(message: Message) -> object | None:
        if message.message_type == 3:
            return PdfImage(png, "PNG", 12, 10, "synthetic")
        if message.message_type == 47:
            return ChatArchiveMedia(
                kind="sticker",
                data=gif,
                extension="gif",
                mime_type="image/gif",
                is_animated=True,
            )
        return None

    result = export_chat_html_archive(
        root=tmp_path / "archive",
        conversation=conversation,
        messages=_messages(conversation.username),
        options=HtmlExportOptions(include_file_binaries=True),
        profiles=profiles,
        avatar_resolver=lambda _profile: ResolvedAvatar(
            status="available",
            data=png,
            mime_type="image/png",
            extension="png",
            width=12,
            height=10,
            source="synthetic",
        ),
        media_resolver=resolve_media,
        attachment_resolver=lambda _message: ChatArchiveMedia(
            kind="file",
            data=b"workbook",
            extension="xlsx",
            filename="课程表.xlsx",
        ),
        chunk_size=2,
    )

    assert result.entrypoint.is_file()
    assert result.manifest.is_file()
    assert result.checksum_manifest.is_file()
    assert result.message_count == 4
    assert result.chunk_count == 2
    assert result.exported_media_count == 3  # image, sticker and file; avatars are summarized separately
    assert result.missing_media_count == 0

    manifest = json.loads(result.manifest.read_text(encoding="utf-8"))
    assert manifest["schema"] == "wechat-chat-offline-archive"
    assert manifest["conversation"]["name"] == "课程群"
    assert manifest["participants"][0]["name"] == "小吕"
    assert manifest["participants"][0]["avatar"]["status"] == "exported"
    assert manifest["summary"]["media_exported"] == 3

    chunks = "\n".join(
        path.read_text(encoding="utf-8")
        for path in sorted((result.root / "data").glob("messages-*.js"))
    )
    assert "收到❤️" in chunks
    assert '"text":"收到[爱心]' not in chunks
    assert '"raw_text":"收到[爱心] ' in chunks
    assert "<script>" not in chunks
    assert "\\u003cscript>alert(1)\\u003c/script>" in chunks
    assert "课程表.xlsx" in chunks
    assert '"animated":true' in chunks

    exported_text = "\n".join(
        path.read_text(encoding="utf-8")
        for path in result.root.rglob("*")
        if path.is_file() and path.suffix in {".html", ".css", ".js", ".json", ".txt"}
    )
    assert "http://" not in exported_text
    assert "https://" not in exported_text
    assert "搜索全部记录" in result.entrypoint.read_text(encoding="utf-8")
    assert "@media" in (result.root / "assets" / "viewer.css").read_text(
        encoding="utf-8"
    )
    assert "[hidden]" in (result.root / "assets" / "viewer.css").read_text(
        encoding="utf-8"
    )
    checksum_lines = result.checksum_manifest.read_text(encoding="utf-8").splitlines()
    assert any(line.endswith("*index.html") for line in checksum_lines)
    assert any("resources/avatars/" in line for line in checksum_lines)


def test_service_exports_atomic_html_entrypoint_and_keeps_raw_face_text(tmp_path) -> None:
    conversation = Conversation("wxid_friend", "好友")
    messages = _messages(conversation.username)[:1]

    class Archive:
        self_wxid = "wxid_self"
        profile_store = None
        avatar_resolver = None

        def export_workload(self, *_args, **_kwargs):
            return ExportWorkload(message_count=len(messages))

        def iter_messages(self, *_args, **_kwargs):
            yield from messages

    service = ExporterService(
        AccountLocation(tmp_path / "account", "wxid_self", "test")
    )
    service.archive = Archive()  # type: ignore[assignment]
    output = tmp_path / "exports"
    request = ExportRequest(
        conversations=(conversation,),
        output_dir=output,
        include_txt=False,
        include_html=True,
        html_options=HtmlExportOptions(
            include_images=False,
            include_stickers=False,
        ),
    )

    first = service.export(request)
    second = service.export(request)

    assert len(first.files) == 1
    assert first.files[0].name == "index.html"
    assert first.files[0].is_file()
    assert second.files[0].parent.name.endswith("(2)")
    assert first.message_counts == {conversation.username: 1}
    assert first.file_categories[first.files[0]] == "chat"
    chunk = next((first.files[0].parent / "data").glob("messages-*.js"))
    chunk_text = chunk.read_text(encoding="utf-8")
    assert "收到❤️" in chunk_text
    assert '"raw_text":"收到[爱心] ' in chunk_text


def test_cancelled_service_html_export_removes_only_current_artifacts(tmp_path) -> None:
    conversation = Conversation("wxid_friend", "好友")
    messages = _messages(conversation.username)[:2]

    class Archive:
        self_wxid = "wxid_self"
        profile_store = None
        avatar_resolver = None

        def export_workload(self, *_args, **_kwargs):
            return ExportWorkload(message_count=len(messages))

        def iter_messages(self, *_args, **_kwargs):
            yield from messages

    service = ExporterService(
        AccountLocation(tmp_path / "account", "wxid_self", "test")
    )
    service.archive = Archive()  # type: ignore[assignment]
    output = tmp_path / "exports"
    output.mkdir()
    keep = output / "用户原有文件.txt"
    keep.write_text("keep", encoding="utf-8")
    cancelled = threading.Event()

    def cancel_after_progress(_message: str, fraction: float) -> None:
        if fraction > 0:
            cancelled.set()

    with pytest.raises(ExportCancelled):
        service.export(
            ExportRequest(
                conversations=(conversation,),
                output_dir=output,
                include_txt=False,
                include_html=True,
                html_options=HtmlExportOptions(
                    include_images=False,
                    include_stickers=False,
                ),
            ),
            progress=cancel_after_progress,
            cancelled=cancelled,
        )

    assert keep.read_text(encoding="utf-8") == "keep"
    assert [path for path in output.rglob("*") if path.is_file()] == [keep]
