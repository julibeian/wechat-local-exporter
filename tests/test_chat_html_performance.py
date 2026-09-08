from __future__ import annotations

import hashlib
import json
import tracemalloc
from dataclasses import replace
from pathlib import Path

from wechat_exporter.archive import WeChatArchive
from wechat_exporter.chat_html_archive import (
    MAX_WARNING_DETAILS,
    PROGRESS_PROBE_INTERVAL,
    ChatArchiveMedia,
    export_chat_html_archive,
)
from wechat_exporter.media import MediaResolver
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
from wechat_exporter.service import ExporterService


def _message(index: int, *, content: str = "合成消息") -> Message:
    return Message(
        local_id=index,
        timestamp=1_700_000_000 + index,
        message_type=1,
        sender_id="wxid_friend",
        sender_name="好友",
        is_outgoing=False,
        content=content,
        conversation_id="wxid_friend",
        server_id=7,
    )


def test_large_writer_bounds_progress_callbacks_and_keeps_ids_unique(tmp_path) -> None:
    count = 10_001
    updates: list[tuple[int, int | None]] = []
    result = export_chat_html_archive(
        root=tmp_path / "archive",
        conversation=Conversation("wxid_friend", "压力测试"),
        messages=(_message(index) for index in range(count)),
        options=HtmlExportOptions(include_images=False, include_stickers=False),
        progress=lambda processed, total: updates.append((processed, total)),
        chunk_size=2_000,
    )

    assert result.message_count == count
    assert result.chunk_count == 6
    # A slow machine may legitimately report every probe interval because the
    # time threshold is also reached.  It must still be far below per-message
    # callbacks and can never exceed one update per probe plus endpoints.
    assert 2 <= len(updates) <= (count // PROGRESS_PROBE_INTERVAL) + 3
    assert updates[0] == (1, None)
    assert updates[-1] == (count, None)
    chunks = sorted((result.root / "data").glob("messages-*.js"))
    assert '"id":"server:7#1"' in chunks[0].read_text(encoding="utf-8")
    assert f'"id":"server:7#{count}"' in chunks[-1].read_text(encoding="utf-8")


def test_path_attachment_is_streamed_and_checksum_reused(tmp_path) -> None:
    source = tmp_path / "large.bin"
    block = b"0123456789abcdef" * 65_536  # 1 MiB
    with source.open("wb") as stream:
        for _ in range(12):
            stream.write(block)
    expected_digest = hashlib.sha256(source.read_bytes()).hexdigest()

    message = Message(
        local_id=1,
        timestamp=1_700_000_000,
        message_type=49,
        sender_id="wxid_friend",
        sender_name="好友",
        is_outgoing=False,
        content="[文件]",
        conversation_id="wxid_friend",
        attachment=AttachmentReference(filename="large.bin", extension="bin"),
    )
    tracemalloc.start()
    result = export_chat_html_archive(
        root=tmp_path / "archive",
        conversation=Conversation("wxid_friend", "路径媒体"),
        messages=(message,),
        options=HtmlExportOptions(
            include_images=False,
            include_stickers=False,
            include_file_binaries=True,
        ),
        attachment_resolver=lambda _message: ChatArchiveMedia(
            kind="file",
            path=source,
            extension="bin",
        ),
    )
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()

    exported = result.root / "resources" / "files" / f"{expected_digest}.bin"
    assert exported.stat().st_size == source.stat().st_size
    assert peak < 8 * 1024 * 1024
    assert not list((result.root / "resources" / "files").glob(".resource-*.tmp"))
    checksum = result.checksum_manifest.read_text(encoding="utf-8")
    assert f"{expected_digest} *resources/files/{expected_digest}.bin" in checksum


def test_path_media_that_disappears_after_stat_is_marked_missing(
    tmp_path,
    monkeypatch,
) -> None:
    source = tmp_path / "vanishing.bin"
    source.write_bytes(b"available during resolver lookup")
    original_open = Path.open

    def fail_source_open(path: Path, *args, **kwargs):
        if path == source:
            raise FileNotFoundError(source)
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", fail_source_open)
    message = Message(
        local_id=1,
        timestamp=1_700_000_000,
        message_type=49,
        sender_id="wxid_friend",
        sender_name="好友",
        is_outgoing=False,
        content="[文件]",
        conversation_id="wxid_friend",
        attachment=AttachmentReference(filename="vanishing.bin", extension="bin"),
    )

    result = export_chat_html_archive(
        root=tmp_path / "archive",
        conversation=Conversation("wxid_friend", "路径竞态"),
        messages=(message,),
        options=HtmlExportOptions(
            include_images=False,
            include_stickers=False,
            include_file_binaries=True,
        ),
        attachment_resolver=lambda _message: ChatArchiveMedia(
            kind="file",
            path=source,
            extension="bin",
        ),
    )

    chunk = next((result.root / "data").glob("messages-*.js")).read_text(
        encoding="utf-8"
    )
    assert result.status == "complete"
    assert result.missing_media_count == 1
    assert '"status":"missing"' in chunk
    assert not list((result.root / "resources" / "files").glob(".resource-*.tmp"))


def test_warning_details_are_bounded_for_repeated_resolver_failures(tmp_path) -> None:
    def fail(_message: Message) -> object:
        raise ValueError("broken")

    messages = (
        Message(
            local_id=index,
            timestamp=1_700_000_000 + index,
            message_type=3,
            sender_id="wxid_friend",
            sender_name="好友",
            is_outgoing=False,
            content="[图片]",
            conversation_id="wxid_friend",
        )
        for index in range(MAX_WARNING_DETAILS + 25)
    )
    result = export_chat_html_archive(
        root=tmp_path / "archive",
        conversation=Conversation("wxid_friend", "错误上限"),
        messages=messages,
        media_resolver=fail,
    )

    assert len(result.warnings) == MAX_WARNING_DETAILS + 1
    assert "25 条" in result.warnings[-1]
    manifest = json.loads(result.manifest.read_text(encoding="utf-8"))
    assert manifest["summary"]["media_missing"] == MAX_WARNING_DETAILS + 25


def test_archive_merge_releases_dedup_fingerprints_as_order_advances(monkeypatch) -> None:
    archive = object.__new__(WeChatArchive)
    conversation = Conversation("wxid_friend", "有界去重")
    count = 20_000

    monkeypatch.setattr(
        archive,
        "_message_databases",
        lambda: [("synthetic", Path("synthetic.db"))],
    )

    def rows(*_args, **_kwargs):
        for index in range(count):
            yield _message(index, content=(f"{index}:" + "x" * 512))

    monkeypatch.setattr(archive, "_iter_database_messages", rows)
    tracemalloc.start()
    exported_count = sum(1 for _ in archive.iter_messages(conversation))
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()

    assert exported_count == count
    assert peak < 8 * 1024 * 1024


def test_archive_merge_keeps_dedup_state_for_a_complete_normalized_second(
    monkeypatch,
) -> None:
    archive = object.__new__(WeChatArchive)
    conversation = Conversation("wxid_friend", "毫秒时间去重")
    first = replace(_message(1, content="duplicate"), timestamp=1_700_000_000, sort_seq=20)
    middle = replace(_message(2, content="middle"), timestamp=1_700_000_000, sort_seq=10)
    duplicate = replace(first, local_id=3)
    later = replace(_message(4, content="later"), timestamp=1_700_000_001, sort_seq=1)

    monkeypatch.setattr(
        archive,
        "_message_databases",
        lambda: [("synthetic", Path("synthetic.db"))],
    )
    monkeypatch.setattr(
        archive,
        "_iter_database_messages",
        lambda *_args, **_kwargs: iter((first, middle, duplicate, later)),
    )

    exported = list(archive.iter_messages(conversation))

    assert exported == [first, middle, later]


def test_service_html_does_not_retain_every_unique_image_payload(
    tmp_path,
    monkeypatch,
) -> None:
    count = 40
    payload_size = 512 * 1024
    conversation = Conversation("wxid_friend", "图文压力测试")

    class Archive:
        self_wxid = "wxid_self"
        profile_store = None
        avatar_resolver = None

        def export_workload(self, *_args, **_kwargs):
            return ExportWorkload(message_count=count, image_count=count)

        def iter_messages(self, *_args, **_kwargs):
            for index in range(count):
                yield Message(
                    local_id=index,
                    timestamp=1_700_000_000 + index,
                    message_type=3,
                    sender_id="wxid_friend",
                    sender_name="好友",
                    is_outgoing=False,
                    content="[图片]",
                    conversation_id=conversation.username,
                    media=MediaReference(kind="image", md5=f"{index:032x}"),
                )

    def resolve_unique_image(
        _resolver: MediaResolver,
        message: Message,
        _reference: MediaReference,
    ) -> PdfImage:
        # Allocate distinct bytes for every image.  With the historical
        # unbounded resolver cache this test retains about 20 MiB.
        data = bytes([message.local_id + 1]) * payload_size
        return PdfImage(data, "PNG", 1, 1, "synthetic")

    monkeypatch.setattr(MediaResolver, "_resolve_local_image", resolve_unique_image)
    service = ExporterService(
        AccountLocation(tmp_path / "account", "wxid_self", "test")
    )
    service.archive = Archive()  # type: ignore[assignment]

    tracemalloc.start()
    try:
        result = service.export(
            ExportRequest(
                conversations=(conversation,),
                output_dir=tmp_path / "exports",
                include_txt=False,
                include_html=True,
                html_options=HtmlExportOptions(
                    include_images=True,
                    include_stickers=False,
                ),
            )
        )
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    assert result.message_counts == {conversation.username: count}
    assert any(
        warning.startswith("HTML 图片/表情：") for warning in result.warnings
    )
    assert not any(warning.startswith("PDF 图片：") for warning in result.warnings)
    assert peak < 8 * 1024 * 1024
