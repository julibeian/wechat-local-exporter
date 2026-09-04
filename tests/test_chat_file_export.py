from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import zipfile
from dataclasses import replace
from datetime import datetime
from pathlib import Path

import pytest

from wechat_exporter import attachments as attachment_module
from wechat_exporter.archive import WeChatArchive
from wechat_exporter.attachments import (
    AttachmentResolver,
    attachment_category,
    export_conversation_attachments,
    extract_attachment_reference,
)
from wechat_exporter.models import (
    CHAT_FILE_CATEGORIES,
    AccountLocation,
    AttachmentReference,
    ChatFileExportRequest,
    Conversation,
    Message,
)
from wechat_exporter.service import ExporterService
from wechat_exporter.service import ExportCancelled


FILE_XML = """
<msg><appmsg>
  <title>操作系统实验要求.pdf</title><type>6</type>
  <appattach>
    <totallen>1837422</totallen><attachid>attach-123</attachid>
    <fileext>.PDF</fileext><cdnattachurl>https://example.invalid/a</cdnattachurl>
    <aeskey>aes-value</aeskey><filekey>file-key</filekey>
    <fileuploadtoken>upload-token</fileuploadtoken>
  </appattach>
  <md5>aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa</md5>
</appmsg></msg>
"""


def _message(
    local_id: int,
    filename: str,
    *,
    size: int | None = None,
    extension: str | None = None,
    sender_name: str = "班长",
    conversation_id: str = "room@chatroom",
    timestamp: int | None = None,
) -> Message:
    suffix = extension if extension is not None else Path(filename).suffix.lstrip(".")
    return Message(
        local_id=local_id,
        timestamp=timestamp or int(datetime(2026, 9, 1, 20, 31, local_id).timestamp()),
        message_type=49,
        sender_id="wxid_monitor",
        sender_name=sender_name,
        is_outgoing=False,
        content=f"[链接/文件] {filename}",
        sort_seq=1000 + local_id,
        source_db="message\\message_0.db",
        conversation_id=conversation_id,
        attachment=AttachmentReference(filename, suffix, size),
    )


def _zip_records(path: Path) -> tuple[str, list[dict[str, object]], set[str]]:
    with zipfile.ZipFile(path) as archive:
        names = set(archive.namelist())
        root = next(name.split("/", 1)[0] for name in names if "/" in name)
        records = [
            json.loads(line)
            for line in archive.read(f"{root}/files.jsonl").decode("utf-8").splitlines()
        ]
    return root, records, names


def test_file_appmsg_metadata_is_extracted_but_web_link_is_not() -> None:
    reference = extract_attachment_reference(49, FILE_XML)
    assert reference == AttachmentReference(
        filename="操作系统实验要求.pdf",
        extension="pdf",
        size=1837422,
        md5="a" * 32,
        attachment_id="attach-123",
        cdn_url="https://example.invalid/a",
        aes_key="aes-value",
        file_key="file-key",
        file_upload_token="upload-token",
    )
    web_link = "<msg><appmsg><title>课程网页</title><type>5</type><url>https://example.com</url></appmsg></msg>"
    mini_program = "<msg><appmsg><title>课程小程序</title><type>33</type></appmsg></msg>"
    assert extract_attachment_reference(49, web_link) is None
    assert extract_attachment_reference(49, mini_program) is None


def test_explicit_wechat_file_type_is_recognised_without_type49() -> None:
    reference = extract_attachment_reference(
        34359738417,
        "<msg><appmsg><title>明确文件.docx</title><appattach><totallen>12</totallen></appattach></appmsg></msg>",
    )
    assert reference is not None
    assert reference.filename == "明确文件.docx"
    assert reference.extension == "docx"
    assert reference.size == 12


def test_archive_row_populates_shared_attachment_reference(tmp_path) -> None:
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    row = connection.execute(
        "SELECT 1 AS local_id, 49 AS local_type, ? AS create_time, "
        "10 AS sort_seq, 2 AS real_sender_id, ? AS message_content, "
        "NULL AS compress_content, 0 AS computed_is_send, 99 AS server_id, "
        "NULL AS packed_info_data",
        (int(datetime(2026, 9, 1).timestamp()), FILE_XML),
    ).fetchone()
    archive = object.__new__(WeChatArchive)
    archive.self_wxid = "wxid_self"
    archive.contacts = {"wxid_friend": "好友"}
    archive._calibrations = {}
    message = archive._row_to_message(
        "message\\message_0.db",
        row,
        Conversation("wxid_friend", "好友"),
        {2: "wxid_friend"},
    )
    assert message.attachment is not None
    assert message.attachment.filename == "操作系统实验要求.pdf"
    assert message.attachment.extension == "pdf"
    assert message.stable_id == "server:99"
    connection.close()


@pytest.mark.parametrize(
    ("extension", "category"),
    (
        ("pdf", "pdf"),
        ("docx", "word"),
        ("xlsx", "excel"),
        ("pptx", "powerpoint"),
        ("zip", "archive"),
        ("7Z", "archive"),
        ("py", "other"),
        ("", "other"),
    ),
)
def test_attachment_extension_categories(extension: str, category: str) -> None:
    assert attachment_category(extension) == category


def test_resolver_indexes_account_file_roots_once_and_finds_by_metadata(tmp_path) -> None:
    account_dir = tmp_path / "account"
    cached = account_dir / "msg" / "file" / "2026-09" / "资料" / "实验要求.pdf"
    cached.parent.mkdir(parents=True)
    cached.write_bytes(b"cached attachment")
    decoy = account_dir / "unrelated" / "实验要求.pdf"
    decoy.parent.mkdir(parents=True)
    decoy.write_bytes(b"cached attachment")
    conversation = Conversation("room@chatroom", "课程群", is_group=True)
    message = _message(1, "实验要求.pdf", size=cached.stat().st_size)
    resolver = AttachmentResolver(
        AccountLocation(account_dir, "wxid_self", "test"),
        (conversation,),
    )
    assert resolver.index_build_count == 1
    assert resolver.indexed_file_count == 1
    assert resolver.resolve(message) == cached
    missing = _message(2, "附件3.pdf", size=999_999)
    assert resolver.resolve(missing) is None
    assert resolver.index_build_count == 1


def test_resolver_uses_selected_conversation_attach_file_root(tmp_path) -> None:
    account_dir = tmp_path / "account"
    conversation = Conversation("room@chatroom", "课程群", is_group=True)
    chat_hash = hashlib.md5(conversation.username.encode()).hexdigest()
    cached = account_dir / "msg" / "attach" / chat_hash / "2026-09" / "File" / "群文件.xlsx"
    cached.parent.mkdir(parents=True)
    cached.write_bytes(b"excel")
    resolver = AttachmentResolver(
        AccountLocation(account_dir, "wxid_self", "test"),
        (conversation,),
    )
    assert resolver.resolve(_message(1, "群文件.xlsx", size=5)) == cached


def test_resolver_does_not_guess_when_recorded_md5_disagrees(tmp_path) -> None:
    account_dir = tmp_path / "account"
    cached = account_dir / "msg" / "file" / "2026-09" / "同名.pdf"
    cached.parent.mkdir(parents=True)
    cached.write_bytes(b"actual")
    conversation = Conversation("wxid_friend", "好友")
    message = _message(1, "同名.pdf", size=len(b"actual"), conversation_id="wxid_friend")
    message = replace(
        message,
        attachment=AttachmentReference(
            "同名.pdf",
            "pdf",
            len(b"actual"),
            md5="f" * 32,
        ),
    )
    resolver = AttachmentResolver(
        AccountLocation(account_dir, "wxid_self", "test"),
        (conversation,),
    )
    assert resolver.resolve(message) is None


def test_chat_file_zip_has_fixed_structure_safe_names_and_all_skip_statuses(tmp_path) -> None:
    account_dir = tmp_path / "account"
    source = account_dir / "msg" / "file" / "2026-09" / "source.pdf"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"pdf")
    conversation = Conversation("room@chatroom", "计算机:23级群😀", is_group=True)
    resolver = AttachmentResolver(
        AccountLocation(account_dir, "wxid_self", "test"),
        (conversation,),
    )
    messages = (
        _message(1, "实验:要求?.pdf", size=3, sender_name="班:长😀"),
        _message(2, "实验:要求?.pdf", size=3, sender_name="班:长😀"),
        _message(3, "附件3.pdf", size=99),
        _message(4, "超大文件.pdf", size=101),
        _message(5, "报名表.docx", size=20),
    )
    result = export_conversation_attachments(
        output_dir=tmp_path / "output",
        conversation=conversation,
        messages=messages,
        resolver=resolver,
        categories=frozenset({"pdf"}),
        max_file_size_bytes=100,
        start_timestamp=int(datetime(2026, 8, 20).timestamp()),
        end_timestamp=int(datetime(2026, 9, 2, 23, 59, 59).timestamp()),
    )
    assert result.path.is_file()
    root, records, names = _zip_records(result.path)
    assert result.path.stem == root
    assert f"{root}/导出说明.txt" in names
    assert f"{root}/files.jsonl" in names
    assert f"{root}/files/" in names
    statuses = [record["status"] for record in records]
    assert statuses == [
        "exported",
        "exported",
        "not_available_locally",
        "too_large",
        "unsupported",
    ]
    exported_paths = [str(record["path"]) for record in records if record["status"] == "exported"]
    assert len(exported_paths) == len(set(exported_paths)) == 2
    assert all(":" not in path and "?" not in path for path in exported_paths)
    assert all(len(Path(path).name) <= 140 for path in exported_paths)
    assert all(path.startswith("files/2026-09-01_2031") for path in exported_paths)
    assert all(f"{root}/{path}" in names for path in exported_paths)
    assert records[2]["path"] is None
    assert records[2]["reason"] == "本机未找到可读取的附件实体"
    with zipfile.ZipFile(result.path) as archive:
        explanation = archive.read(f"{root}/导出说明.txt").decode("utf-8")
    assert "找到文件消息：5" in explanation
    assert "成功导出：2" in explanation
    assert "因过大跳过：1" in explanation
    assert "本机找不到：1" in explanation


def test_copy_read_error_stays_in_index(tmp_path, monkeypatch) -> None:
    account_dir = tmp_path / "account"
    source = account_dir / "msg" / "file" / "2026-09" / "bad.pdf"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"bad")
    conversation = Conversation("wxid_friend", "好友")
    resolver = AttachmentResolver(AccountLocation(account_dir, "wxid_self", "test"), (conversation,))

    def fail_copy(*_args, **_kwargs):
        raise OSError("simulated read failure")

    monkeypatch.setattr(attachment_module, "_copy_file", fail_copy)
    result = export_conversation_attachments(
        output_dir=tmp_path / "output",
        conversation=conversation,
        messages=(_message(1, "bad.pdf", size=3, conversation_id="wxid_friend"),),
        resolver=resolver,
        categories=frozenset({"pdf"}),
        max_file_size_bytes=0,
    )
    _root, records, _names = _zip_records(result.path)
    assert records[0]["status"] == "read_error"
    assert "simulated read failure" in str(records[0]["reason"])


def test_existing_zip_uses_safe_suffix_and_matching_internal_root(tmp_path) -> None:
    conversation = Conversation("wxid_friend", "好友")
    resolver = AttachmentResolver(AccountLocation(tmp_path / "account", "wxid_self", "test"))
    first = export_conversation_attachments(
        output_dir=tmp_path / "output",
        conversation=conversation,
        messages=(),
        resolver=resolver,
        categories=CHAT_FILE_CATEGORIES,
        max_file_size_bytes=0,
    )
    second = export_conversation_attachments(
        output_dir=tmp_path / "output",
        conversation=conversation,
        messages=(),
        resolver=resolver,
        categories=CHAT_FILE_CATEGORIES,
        max_file_size_bytes=0,
    )
    assert first.path != second.path
    assert second.path.stem.endswith("(2)")
    second_root, _records, _names = _zip_records(second.path)
    assert second_root == second.path.stem


def test_two_conversations_create_two_zips_and_receive_date_range(tmp_path) -> None:
    account_dir = tmp_path / "account"
    file_root = account_dir / "msg" / "file" / "2026-09"
    file_root.mkdir(parents=True)
    (file_root / "a.pdf").write_bytes(b"a")
    (file_root / "b.docx").write_bytes(b"bb")
    conversations = (
        Conversation("wxid_friend", "好友"),
        Conversation("room@chatroom", "课程群", is_group=True),
    )
    start = int(datetime(2026, 8, 20).timestamp())
    end = int(datetime(2026, 9, 2, 23, 59, 59).timestamp())
    calls: list[tuple[str, int, int]] = []

    class Archive:
        self_wxid = "wxid_self"

        def iter_messages(self, conversation, *, start_timestamp, end_timestamp):
            calls.append((conversation.username, start_timestamp, end_timestamp))
            if conversation.is_group:
                yield _message(2, "b.docx", size=2, conversation_id=conversation.username)
            else:
                yield _message(1, "a.pdf", size=1, conversation_id=conversation.username)

    service = ExporterService(AccountLocation(account_dir, "wxid_self", "test"))
    service.archive = Archive()  # type: ignore[assignment]
    result = service.export_chat_files(
        ChatFileExportRequest(
            conversations=conversations,
            output_dir=tmp_path / "output",
            categories=CHAT_FILE_CATEGORIES,
            max_file_size_bytes=100 * 1024 * 1024,
            start_timestamp=start,
            end_timestamp=end,
        )
    )
    assert len(result.files) == 2
    assert all(path.suffix == ".zip" and zipfile.is_zipfile(path) for path in result.files)
    assert len({path.parent for path in result.files}) == 2
    assert calls == [
        ("wxid_friend", start, end),
        ("room@chatroom", start, end),
    ]
    assert set(result.file_categories.values()) == {"chat_files"}


def test_failed_zip_publish_leaves_no_final_or_temporary_zip(tmp_path, monkeypatch) -> None:
    conversation = Conversation("wxid_friend", "好友")
    output_dir = tmp_path / "output"
    resolver = AttachmentResolver(AccountLocation(tmp_path / "account", "wxid_self", "test"))

    def fail_replace(_source, _destination):
        raise OSError("simulated publish failure")

    monkeypatch.setattr(attachment_module.os, "replace", fail_replace)
    with pytest.raises(OSError, match="publish failure"):
        export_conversation_attachments(
            output_dir=output_dir,
            conversation=conversation,
            messages=(),
            resolver=resolver,
            categories=CHAT_FILE_CATEGORIES,
            max_file_size_bytes=0,
        )
    assert not list(output_dir.glob("*.zip"))
    assert not list(output_dir.glob("*.tmp"))
    assert not list(output_dir.glob(".聊天文件构建-*"))


def test_cancelled_chat_file_export_removes_zip_and_temporary_build_data(tmp_path) -> None:
    account_dir = tmp_path / "account"
    file_root = account_dir / "msg" / "file" / "2026-09"
    file_root.mkdir(parents=True)
    (file_root / "a.pdf").write_bytes(b"a")
    (file_root / "b.pdf").write_bytes(b"bb")
    conversation = Conversation("wxid_friend", "好友")

    class Archive:
        self_wxid = "wxid_self"

        def iter_messages(self, *_args, **_kwargs):
            yield _message(1, "a.pdf", size=1, conversation_id="wxid_friend")
            yield _message(2, "b.pdf", size=2, conversation_id="wxid_friend")

    service = ExporterService(AccountLocation(account_dir, "wxid_self", "test"))
    service.archive = Archive()  # type: ignore[assignment]
    cancelled = threading.Event()
    output_dir = tmp_path / "output"

    def cancel_after_first(message: str, _fraction: float) -> None:
        if "已处理 1/2" in message:
            cancelled.set()

    with pytest.raises(ExportCancelled):
        service.export_chat_files(
            ChatFileExportRequest(
                conversations=(conversation,),
                output_dir=output_dir,
            ),
            progress=cancel_after_first,
            cancelled=cancelled,
        )
    assert not list(output_dir.rglob("*.zip"))
    assert not list(output_dir.rglob("*.tmp"))
    assert not [path for path in output_dir.rglob("*") if path.is_file()]
