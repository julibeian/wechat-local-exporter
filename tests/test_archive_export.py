from __future__ import annotations

import hashlib
import sqlite3
import threading
from datetime import datetime
from pathlib import Path

import pytest
from pypdf import PdfReader

from wechat_exporter import service as service_module
from wechat_exporter.archive import WeChatArchive
from wechat_exporter.crypto import DatabaseKeys, DecryptedWorkspace
from wechat_exporter.exporters import PdfTranscriptWriter, TxtTranscriptWriter
from wechat_exporter.models import (
    AccountLocation,
    Conversation,
    ExportRequest,
    ExportWorkload,
    MediaReference,
    Message,
    Moment,
)
from wechat_exporter.service import (
    ExportCancelled,
    ExporterService,
    estimate_export_seconds,
    estimate_moments_export_seconds,
    format_duration,
)


SELF = "wxid_self"
FRIEND = "wxid_friend"


def test_cancelled_chat_export_removes_only_this_run_artifacts(tmp_path) -> None:
    conversation = Conversation(FRIEND, "好友")
    messages = (
        Message(1, 1, 1, SELF, "我", True, "第一条", conversation_id=FRIEND),
        Message(2, 2, 1, FRIEND, "好友", False, "第二条", conversation_id=FRIEND),
    )

    class Archive:
        self_wxid = SELF

        def export_workload(self, *_args, **_kwargs):
            return ExportWorkload(message_count=len(messages))

        def iter_messages(self, *_args, **_kwargs):
            yield from messages

    output_dir = tmp_path / "chat-output"
    output_dir.mkdir()
    keep = output_dir / "用户原有文件.txt"
    keep.write_text("keep", encoding="utf-8")
    cancelled = threading.Event()
    service = ExporterService(AccountLocation(tmp_path, SELF, "test"))
    service.archive = Archive()  # type: ignore[assignment]

    def stop_after_first_message(_message: str, fraction: float) -> None:
        if fraction > 0:
            cancelled.set()

    with pytest.raises(ExportCancelled):
        service.export(
            ExportRequest(
                conversations=(conversation,),
                output_dir=output_dir,
                include_txt=True,
                include_pdf=False,
            ),
            progress=stop_after_first_message,
            cancelled=cancelled,
        )

    assert keep.read_text(encoding="utf-8") == "keep"
    assert [path for path in output_dir.rglob("*") if path.is_file()] == [keep]


def test_cancelled_moments_export_removes_temporary_data(tmp_path) -> None:
    conversation = Conversation(FRIEND, "好友")
    moments = (
        Moment("1", FRIEND, 1, "第一条朋友圈"),
        Moment("2", FRIEND, 2, "第二条朋友圈"),
    )

    class Archive:
        self_wxid = SELF

        def contact_moments(self, *_args, **_kwargs):
            return moments

    output_dir = tmp_path / "moments-output"
    output_dir.mkdir()
    keep = output_dir / "用户原有文件.txt"
    keep.write_text("keep", encoding="utf-8")
    cancelled = threading.Event()
    service = ExporterService(AccountLocation(tmp_path, SELF, "test"))
    service.archive = Archive()  # type: ignore[assignment]

    def stop_after_first_post(message: str, _fraction: float) -> None:
        if "1/2" in message:
            cancelled.set()

    with pytest.raises(ExportCancelled):
        service.export_moments_archive(
            conversation,
            output_dir,
            progress=stop_after_first_post,
            cancelled=cancelled,
        )

    assert keep.read_text(encoding="utf-8") == "keep"
    assert [path for path in output_dir.rglob("*") if path.is_file()] == [keep]


def test_cancelled_moments_export_rolls_back_an_already_published_archive(
    tmp_path,
    monkeypatch,
) -> None:
    conversation = Conversation(FRIEND, "好友")

    class Archive:
        self_wxid = SELF

        def contact_moments(self, *_args, **_kwargs):
            return (Moment("1", FRIEND, 1, "朋友圈"),)

    output_dir = tmp_path / "moments-output"
    output_dir.mkdir()
    keep = output_dir / "用户原有文件.txt"
    keep.write_text("keep", encoding="utf-8")
    cancelled = threading.Event()
    original_publish = service_module._publish_directory

    def publish_then_cancel(source, destination, **kwargs):
        result = original_publish(source, destination, **kwargs)
        cancelled.set()
        return result

    monkeypatch.setattr(service_module, "_publish_directory", publish_then_cancel)
    service = ExporterService(AccountLocation(tmp_path, SELF, "test"))
    service.archive = Archive()  # type: ignore[assignment]

    with pytest.raises(ExportCancelled):
        service.export_moments_archive(
            conversation,
            output_dir,
            cancelled=cancelled,
        )

    assert [path for path in output_dir.rglob("*") if path.is_file()] == [keep]


def test_metadata_stage_feasibility_and_incomplete_workload_boundary(tmp_path):
    """Evidence for the deferred lazy-decryption design, not a production switch."""
    archive, workspace = _build_archive(tmp_path)
    try:
        for _name, path in archive._message_databases():
            path.unlink()
        archive.load_metadata()
        conversations = archive.conversations()
        assert conversations  # Contact/session alone can render the initial list.
        # Existing workload code would silently count zero without a preparation gate.
        assert archive.export_workload(conversations, start_timestamp=0, end_timestamp=0).message_count == 0
    finally:
        workspace.close()


def _create_database(path: Path, statements: list[str], rows: list[tuple[str, tuple]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    try:
        for statement in statements:
            connection.execute(statement)
        for statement, values in rows:
            connection.execute(statement, values)
        connection.commit()
    finally:
        connection.close()


def _build_archive(tmp_path: Path) -> tuple[WeChatArchive, DecryptedWorkspace]:
    keys = DatabaseKeys(
        {
            "contact\\contact.db": b"a" * 32,
            "session\\session.db": b"b" * 32,
            "message\\message_0.db": b"c" * 32,
            "message\\message_1.db": b"d" * 32,
        }
    )
    workspace = DecryptedWorkspace(tmp_path / "encrypted", keys)
    _create_database(
        workspace.decrypted_path("contact\\contact.db"),
        ["CREATE TABLE contact(username TEXT, remark TEXT, nick_name TEXT, alias TEXT)"],
        [
            ("INSERT INTO contact VALUES(?,?,?,?)", (SELF, "", "本人", "")),
            ("INSERT INTO contact VALUES(?,?,?,?)", (FRIEND, "好友备注", "好友昵称", "")),
        ],
    )
    now = int(datetime(2026, 8, 23, 12, 0).timestamp())
    _create_database(
        workspace.decrypted_path("session\\session.db"),
        [
            "CREATE TABLE SessionTable(username TEXT, last_timestamp INTEGER, summary TEXT, last_msg_type INTEGER)"
        ],
        [("INSERT INTO SessionTable VALUES(?,?,?,?)", (FRIEND, now, "最近消息", 1))],
    )
    table = "Msg_" + hashlib.md5(FRIEND.encode()).hexdigest()
    schema = [
        "CREATE TABLE Name2Id(user_name TEXT)",
        f"CREATE TABLE {table}(local_id INTEGER, local_type INTEGER, create_time INTEGER, sort_seq INTEGER, real_sender_id INTEGER, message_content BLOB, compress_content BLOB)",
    ]
    _create_database(
        workspace.decrypted_path("message\\message_0.db"),
        schema,
        [
            ("INSERT INTO Name2Id(rowid,user_name) VALUES(?,?)", (1, SELF)),
            ("INSERT INTO Name2Id(rowid,user_name) VALUES(?,?)", (2, FRIEND)),
            (f"INSERT INTO {table} VALUES(?,?,?,?,?,?,?)", (1, 1, now - 30, 1, 2, "你好", None)),
            (f"INSERT INTO {table} VALUES(?,?,?,?,?,?,?)", (2, 3, now - 20, 2, 1, "binary", None)),
        ],
    )
    _create_database(
        workspace.decrypted_path("message\\message_1.db"),
        schema,
        [
            ("INSERT INTO Name2Id(rowid,user_name) VALUES(?,?)", (10, SELF)),
            ("INSERT INTO Name2Id(rowid,user_name) VALUES(?,?)", (11, FRIEND)),
            (f"INSERT INTO {table} VALUES(?,?,?,?,?,?,?)", (10, 1, now - 60, 1, 1, "这是我以前发的", None)),
            (f"INSERT INTO {table} VALUES(?,?,?,?,?,?,?)", (11, 1, now - 50, 2, 2, "这是对方以前发的", None)),
        ],
    )
    account = AccountLocation(tmp_path / "wxid_self_abcd", "wxid_self_abcd", "test")
    archive = WeChatArchive(account, workspace)
    archive.load_metadata()
    return archive, workspace


def test_archive_merge_and_sender_calibration(tmp_path) -> None:
    archive, workspace = _build_archive(tmp_path)
    try:
        conversations = archive.conversations()
        assert len(conversations) == 1
        assert conversations[0].display_name == "好友备注"
        self_conversation = archive.self_conversation()
        assert self_conversation.username == SELF
        assert self_conversation.is_self
        assert self_conversation.display_name == "我自己（本人）"
        samples = archive.calibration_samples(conversations[0], limit_per_sender=1)
        assert {(sample.source_db, sample.sender_id) for sample in samples} == {
            ("message\\message_1.db", 1),
            ("message\\message_1.db", 2),
        }
        archive.set_calibration("message\\message_1.db", 1, "self")
        archive.set_calibration("message\\message_1.db", 2, "other")
        messages = list(archive.iter_messages(conversations[0]))
        assert [message.content for message in messages] == [
            "这是我以前发的",
            "这是对方以前发的",
            "你好",
            "[图片]",
        ]
        assert [message.sender_name for message in messages] == ["我", "好友备注", "好友备注", "我"]
        workload = archive.export_workload(conversations)
        assert workload == ExportWorkload(
            message_count=4,
            image_count=1,
            emoticon_count=0,
        )
        recent_workload = archive.export_workload(
            conversations,
            start_timestamp=int(datetime(2026, 8, 23, 11, 59, 25).timestamp()),
        )
        assert recent_workload == ExportWorkload(
            message_count=2,
            image_count=1,
            emoticon_count=0,
        )
    finally:
        workspace.close()


def test_txt_and_pdf_are_searchable(tmp_path) -> None:
    archive, workspace = _build_archive(tmp_path)
    try:
        conversation = archive.conversations()[0]
        archive.set_calibration("message\\message_1.db", 1, "self")
        archive.set_calibration("message\\message_1.db", 2, "other")
        messages = list(archive.iter_messages(conversation))
        txt_path = tmp_path / "chat.txt"
        pdf_path = tmp_path / "chat.pdf"
        with TxtTranscriptWriter(txt_path, conversation) as writer:
            for message in messages:
                writer.write(message)
        with PdfTranscriptWriter(pdf_path, conversation) as writer:
            for message in messages:
                writer.write(message)
        assert "这是我以前发的" in txt_path.read_text(encoding="utf-8-sig")
        extracted = "\n".join(page.extract_text() or "" for page in PdfReader(pdf_path).pages)
        assert "这是我以前发的" in extracted
        assert "好友备注" in extracted
    finally:
        workspace.close()


def test_conversations_only_include_personal_contacts_and_groups(tmp_path) -> None:
    keys = DatabaseKeys(
        {
            "contact\\contact.db": b"a" * 32,
            "session\\session.db": b"b" * 32,
        }
    )
    workspace = DecryptedWorkspace(tmp_path / "encrypted-filter", keys)
    _create_database(
        workspace.decrypted_path("contact\\contact.db"),
        [
            "CREATE TABLE contact(username TEXT, local_type INTEGER, verify_flag INTEGER, delete_flag INTEGER, remark TEXT, nick_name TEXT)"
        ],
        [
            ("INSERT INTO contact VALUES(?,?,?,?,?,?)", ("wxid_friend", 1, 0, 0, "好友", "")),
            ("INSERT INTO contact VALUES(?,?,?,?,?,?)", ("alice_custom", 1, 0, 0, "", "Alice")),
            ("INSERT INTO contact VALUES(?,?,?,?,?,?)", ("gh_school", 1, 24, 0, "", "学校公众号")),
            ("INSERT INTO contact VALUES(?,?,?,?,?,?)", ("service_account", 1, 8, 0, "", "认证服务")),
            ("INSERT INTO contact VALUES(?,?,?,?,?,?)", ("weixin_pay", 1, 0, 0, "", "微信支付")),
            ("INSERT INTO contact VALUES(?,?,?,?,?,?)", ("wxid_stranger", 0, 0, 0, "", "临时会话")),
            ("INSERT INTO contact VALUES(?,?,?,?,?,?)", ("wxid_deleted", 1, 0, 1, "", "已删除")),
        ],
    )
    now = int(datetime(2026, 8, 23, 12, 0).timestamp())
    usernames = (
        "wxid_friend",
        "alice_custom",
        "123456@chatroom",
        "gh_school",
        "service_account",
        "weixin_pay",
        "wxid_stranger",
        "wxid_deleted",
        "missing_contact",
    )
    _create_database(
        workspace.decrypted_path("session\\session.db"),
        [
            "CREATE TABLE SessionTable(username TEXT, last_timestamp INTEGER, summary TEXT, last_msg_type INTEGER)"
        ],
        [
            ("INSERT INTO SessionTable VALUES(?,?,?,?)", (username, now - index, "摘要", 1))
            for index, username in enumerate(usernames)
        ],
    )
    archive = WeChatArchive(
        AccountLocation(tmp_path / "wxid_self_abcd", "wxid_self_abcd", "test"),
        workspace,
    )
    try:
        archive.load_metadata()
        conversations = archive.conversations()
        assert [item.username for item in conversations] == [
            "wxid_friend",
            "alice_custom",
            "123456@chatroom",
        ]
        assert [item.is_group for item in conversations] == [False, False, True]
    finally:
        workspace.close()


def test_wechat_voice_transcript_is_exported_without_media_decode(tmp_path) -> None:
    keys = DatabaseKeys(
        {
            "contact\\contact.db": b"a" * 32,
            "session\\session.db": b"b" * 32,
            "message\\message_0.db": b"c" * 32,
        }
    )
    workspace = DecryptedWorkspace(tmp_path / "encrypted-voice", keys)
    _create_database(
        workspace.decrypted_path("contact\\contact.db"),
        [
            "CREATE TABLE contact(username TEXT, local_type INTEGER, verify_flag INTEGER, delete_flag INTEGER, remark TEXT, nick_name TEXT)"
        ],
        [
            ("INSERT INTO contact VALUES(?,?,?,?,?,?)", (FRIEND, 1, 0, 0, "好友备注", "")),
        ],
    )
    now = int(datetime(2026, 8, 23, 12, 0).timestamp())
    _create_database(
        workspace.decrypted_path("session\\session.db"),
        [
            "CREATE TABLE SessionTable(username TEXT, last_timestamp INTEGER, summary TEXT, last_msg_type INTEGER)"
        ],
        [("INSERT INTO SessionTable VALUES(?,?,?,?)", (FRIEND, now, "[语音]", 34))],
    )
    table = "Msg_" + hashlib.md5(FRIEND.encode()).hexdigest()
    _create_database(
        workspace.decrypted_path("message\\message_0.db"),
        [
            "CREATE TABLE Name2Id(user_name TEXT)",
            f"CREATE TABLE {table}(local_id INTEGER, local_type INTEGER, create_time INTEGER, sort_seq INTEGER, real_sender_id INTEGER, message_content BLOB, compress_content BLOB, computed_is_send INTEGER, server_id INTEGER)",
        ],
        [
            ("INSERT INTO Name2Id(rowid,user_name) VALUES(?,?)", (1, SELF)),
            ("INSERT INTO Name2Id(rowid,user_name) VALUES(?,?)", (2, FRIEND)),
            (
                f"INSERT INTO {table} VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    1,
                    34,
                    now,
                    1,
                    2,
                    '<msg><voicemsg/><voicetrans transtext="今天下午三点见" istransend="1"/></msg>',
                    None,
                    0,
                    9001,
                ),
            ),
            (
                f"INSERT INTO {table} VALUES(?,?,?,?,?,?,?,?,?)",
                (2, 34, now + 1, 2, 2, "<msg><voicemsg/></msg>", None, 0, 9002),
            ),
        ],
    )
    account = AccountLocation(tmp_path / "wxid_self_abcd", "wxid_self_abcd", "test")
    archive = WeChatArchive(account, workspace)
    archive.load_metadata()
    service = ExporterService(account)
    service.archive = archive
    output_dir = tmp_path / "voice-output"
    conversation = archive.conversations()[0]
    try:
        messages = list(archive.iter_messages(conversation))
        assert messages[0].server_id == 9001
        assert messages[0].content == "[微信语音转文字] 今天下午三点见"
        assert messages[1].content == "[语音]"
        progress_updates: list[tuple[str, float]] = []
        txt_result = service.export(
            ExportRequest(
                conversations=(conversation,),
                output_dir=output_dir / "txt",
                include_txt=True,
                include_pdf=False,
                include_wechat_voice_text=True,
            ),
            progress=lambda message, fraction: progress_updates.append(
                (message, fraction)
            ),
        )
        pdf_result = service.export(
            ExportRequest(
                conversations=(conversation,),
                output_dir=output_dir / "pdf",
                include_txt=False,
                include_pdf=True,
                include_pdf_images=False,
                include_wechat_voice_text=True,
            )
        )
        txt_path = txt_result.files[0]
        pdf_path = pdf_result.files[0]
        assert txt_path.parent.parent.name == "联系人"
        assert pdf_path.parent.parent.name == "联系人"
        txt = txt_path.read_text(encoding="utf-8-sig")
        assert "[微信语音转文字] 今天下午三点见" in txt
        assert "[语音]（微信尚未生成转文字）" in txt
        extracted = "\n".join(page.extract_text() or "" for page in PdfReader(pdf_path).pages)
        assert "微信语音转文字" in extracted
        assert "今天下午三点见" in extracted
        assert txt_result.warnings == [
            "微信语音转文字：已写入 1 条，微信尚未生成 1 条。"
        ]
        assert pdf_result.warnings == txt_result.warnings
        assert txt_result.duration_seconds > 0
        assert any("50%" in message for message, _fraction in progress_updates)
        assert progress_updates[-1][1] == 1.0
        assert "实际用时" in progress_updates[-1][0]
    finally:
        workspace.close()


def test_fast_pdf_skips_all_media_resolution(tmp_path, monkeypatch) -> None:
    conversation = Conversation("wxid_friend", "好友")
    message = Message(
        local_id=1,
        timestamp=int(datetime(2026, 8, 25, 14, 0).timestamp()),
        message_type=3,
        sender_id=SELF,
        sender_name="我",
        is_outgoing=True,
        content="[图片]",
        conversation_id=FRIEND,
        media=MediaReference(kind="image", md5="a" * 32),
    )

    class SingleImageArchive:
        self_wxid = SELF

        def export_workload(self, *_args, **_kwargs):
            return ExportWorkload(message_count=1, image_count=1)

        def iter_messages(self, *_args, **_kwargs):
            yield message

    class UnexpectedMediaResolver:
        def __init__(self, *_args, **_kwargs):
            raise AssertionError("快速 PDF 不应创建图片解析器")

    monkeypatch.setattr("wechat_exporter.service.MediaResolver", UnexpectedMediaResolver)
    account = AccountLocation(tmp_path / "wxid_self_abcd", "wxid_self_abcd", "test")
    service = ExporterService(account)
    service.archive = SingleImageArchive()  # type: ignore[assignment]
    result = service.export(
        ExportRequest(
            conversations=(conversation,),
            output_dir=tmp_path / "fast-output",
            include_txt=False,
            include_pdf=True,
            include_pdf_images=False,
        )
    )

    pdf_path = result.files[0]
    extracted = "\n".join(page.extract_text() or "" for page in PdfReader(pdf_path).pages)
    assert "[图片]" in extracted
    assert result.warnings == [
        "快速 PDF：跳过 1 张图片/表情的读取，已用可搜索占位文字写入。"
    ]


def test_export_estimate_accounts_for_selected_formats_and_media() -> None:
    workload = ExportWorkload(
        message_count=10_000,
        image_count=100,
        emoticon_count=20,
    )
    quick = estimate_export_seconds(
        workload,
        conversation_count=2,
        include_txt=True,
        include_pdf=True,
        include_pdf_images=False,
    )
    complete = estimate_export_seconds(
        workload,
        conversation_count=2,
        include_txt=True,
        include_pdf=True,
        include_pdf_images=True,
    )

    assert 0 < quick[0] <= quick[1]
    assert complete[0] > quick[0]
    assert complete[1] > quick[1]
    assert estimate_export_seconds(
        workload,
        conversation_count=2,
        include_txt=False,
        include_pdf=False,
        include_pdf_images=False,
    ) == (0.0, 0.0)
    assert format_duration(65) == "1 分 5 秒"


def test_moments_estimate_uses_a_separate_media_sensitive_range() -> None:
    small = estimate_moments_export_seconds(post_count=10, media_count=5)
    media_heavy = estimate_moments_export_seconds(post_count=10, media_count=100)

    assert 0 < small[0] <= small[1]
    assert media_heavy[0] > small[0]
    assert media_heavy[1] > small[1]
    assert estimate_moments_export_seconds(post_count=0, media_count=100) == (
        0.0,
        0.0,
    )
