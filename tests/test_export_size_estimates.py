from __future__ import annotations

import hashlib
import io
from dataclasses import replace
from pathlib import Path

import pytest
from PIL import Image

from wechat_exporter.attachments import AttachmentResolver
from wechat_exporter.estimates import (
    SizeEstimate, build_chat_size_profile, build_moments_size_profile,
)
from wechat_exporter.models import (
    AccountLocation, AttachmentReference, ChatFileExportRequest, Conversation,
    ExportRequest, JsonlPackageRequest, MediaReference, Message, Moment, MomentMedia,
)
from wechat_exporter.service import ExporterService


class PreviewArchive:
    self_wxid = "self"
    profiles = {}
    profile_store = None

    def __init__(self, messages):
        self.messages = messages
        self.calls = []

    def iter_messages(self, conversation, *, start_timestamp=0, end_timestamp=0):
        self.calls.append((conversation.username, start_timestamp, end_timestamp))
        return (m for m in self.messages if m.conversation_id == conversation.username
                and (not start_timestamp or m.timestamp >= start_timestamp)
                and (not end_timestamp or m.timestamp <= end_timestamp))

    def export_workload(self, conversations, **kwargs):
        from wechat_exporter.models import ExportWorkload
        messages = [m for c in conversations for m in self.iter_messages(c, **kwargs)]
        return ExportWorkload(len(messages), sum(m.message_type == 3 for m in messages),
                              sum(m.message_type == 47 for m in messages))


@pytest.fixture
def preview_data(tmp_path):
    account = AccountLocation(tmp_path / "account", "self", "test")
    conversation = Conversation("friend", "预览联系人")
    timestamp = 1791500000
    def message(index, kind=1, content="收到[玫瑰] 🌹", **kw):
        return Message(index, timestamp + index, kind, "self", "我", True, content,
                       conversation_id="friend", **kw)
    image_hash, video_hash = "a" * 32, "b" * 32
    chat_hash = hashlib.md5(b"friend").hexdigest()
    image_dir = account.account_dir / "msg" / "attach" / chat_hash / message(1).datetime.strftime("%Y-%m") / "Img"
    image_dir.mkdir(parents=True, exist_ok=True)
    buffer = io.BytesIO()
    Image.new("RGB", (80, 70), "red").save(buffer, format="PNG")
    (image_dir / f"{image_hash}.dat").write_bytes(buffer.getvalue())
    video_dir = account.account_dir / "msg" / "video"
    video_dir.mkdir(parents=True, exist_ok=True)
    (video_dir / f"{video_hash}.mp4").write_bytes(b"video-content" * 1000)
    file_dir = account.account_dir / "msg" / "file"
    file_dir.mkdir(parents=True, exist_ok=True)
    messages = [message(1), message(2, 3, "[图片]", media=MediaReference("image", md5=image_hash)),
                message(3, 3, "[图片]", media=MediaReference("image", md5=image_hash)),
                message(4, 43, "[视频]", media=MediaReference("video", md5="c"*32, local_md5=video_hash))]
    for index, name in ((5, "课程.pdf"), (6, "笔记.txt")):
        data = (name.encode("utf-8") + b"data") * 900
        (file_dir / name).write_bytes(data)
        messages.append(message(index, 49, f"[文件] {name}", attachment=AttachmentReference(
            name, Path(name).suffix[1:], len(data), hashlib.md5(data).hexdigest(),
        )))
    return account, conversation, PreviewArchive(messages)


def profile_for(data, **kwargs):
    account, conversation, archive = data
    return build_chat_size_profile(archive, account, (conversation,), **kwargs)


def test_options_change_size_and_apply_actual_local_video_hash_and_limit(preview_data):
    profile = profile_for(preview_data)
    without = profile.estimate("jsonl_package")
    with_video = profile.estimate("jsonl_package", include_videos=True)
    assert len(b"video-content" * 1000) <= with_video.byte_count - without.byte_count <= len(b"video-content" * 1000) + 300
    assert profile.estimate("jsonl_package", include_videos=True, video_limit=1) == without
    html_images = profile.estimate("chat", include_images=True)
    html_no_images = profile.estimate("chat", include_images=False)
    # Two occurrences share one HTML resource, but are both kept in an AI ZIP.
    assert with_video.upper_bytes > without.upper_bytes
    assert html_images.upper_bytes > html_no_images.upper_bytes
    assert profile.estimate("chat", chat_format="txt").upper_bytes < html_images.lower_bytes
    assert profile.estimate("chat", chat_format="pdf", include_images=True).upper_bytes > profile.estimate(
        "chat", chat_format="pdf", include_images=False).upper_bytes


def test_files_follow_categories_and_limits_without_hashing(preview_data, monkeypatch):
    monkeypatch.setattr(AttachmentResolver, "_file_md5", lambda *args: pytest.fail("preview hashes attachment"))
    profile = profile_for(preview_data)
    both = profile.estimate("chat_files", categories=frozenset({"pdf", "other"}))
    pdf = profile.estimate("chat_files", categories=frozenset({"pdf"}))
    small = profile.estimate("chat_files", categories=frozenset({"pdf", "other"}), file_limit=1)
    assert (both.item_count, pdf.item_count, small.item_count) == (2, 1, 0)
    assert both.upper_bytes > pdf.upper_bytes > small.upper_bytes
    # The real export path continues to require MD5 verification.
    with pytest.raises(pytest.fail.Exception, match="preview hashes"):
        AttachmentResolver(preview_data[0]).resolve(preview_data[2].messages[-1])


def test_date_and_conversation_selection_are_applied_before_size_calculation(preview_data):
    full = profile_for(preview_data)
    restricted = profile_for(preview_data, end_timestamp=preview_data[2].messages[0].timestamp)
    assert restricted.workload.message_count == 1
    assert not restricted.media and not restricted.files
    assert restricted.json_bytes < full.json_bytes
    empty = profile_for(preview_data, start_timestamp=9999999999)
    assert empty.estimate("chat", chat_format="json").upper_bytes == 0
    assert empty.estimate("chat_files", categories=frozenset({"pdf"})).upper_bytes > 0


@pytest.mark.parametrize("task", ["txt", "json", "pdf", "html", "jsonl_package", "chat_files"])
def test_size_predicts_actual_exports_with_small_error(preview_data, tmp_path, task, monkeypatch):
    # PDF creation timestamps and document IDs otherwise change compressed byte
    # lengths between the preview and export. Compare identical metadata here.
    monkeypatch.setattr("reportlab.rl_config.invariant", 1)
    account, conversation, archive = preview_data
    profile = profile_for(preview_data)
    service = ExporterService(account)
    service.archive = archive
    if task == "jsonl_package":
        estimate = profile.estimate(task, include_videos=True)
        result = service.export_jsonl_package(JsonlPackageRequest((conversation,), tmp_path / task))
    elif task == "chat_files":
        estimate = profile.estimate(task, categories=frozenset({"pdf", "other"}))
        result = service.export_chat_files(ChatFileExportRequest((conversation,), tmp_path / task))
    else:
        estimate = profile.estimate("chat", chat_format=task, include_images=task == "html")
        result = service.export(ExportRequest(
            (conversation,), tmp_path / task, include_txt=task == "txt", include_json=task == "json",
            include_pdf=task == "pdf", include_html=task == "html",
        ))
    assert result.files
    actual = sum(p.stat().st_size for p in (tmp_path / task).rglob("*") if p.is_file())
    # A huge range containing the result does not establish useful accuracy.
    assert abs(estimate.byte_count - actual) <= max(500, actual * .08), (task, actual, estimate)
    if task in {"txt", "json", "pdf"}:
        assert estimate.byte_count == actual
    assert "–" not in estimate.text


def test_network_option_accounts_for_missing_stickers_without_downloading(preview_data, monkeypatch):
    from wechat_exporter.media import MediaResolver
    monkeypatch.setattr(MediaResolver, "_download", lambda *args: pytest.fail("preview downloads media"))
    preview_data[2].messages.append(replace(preview_data[2].messages[0], local_id=20, message_type=47,
        media=MediaReference("emoticon", md5="d"*32, cdn_url="https://wx.qpic.cn/test")))
    profile = profile_for(preview_data)
    online = profile.estimate("jsonl_package", allow_network=True)
    offline = profile.estimate("jsonl_package", allow_network=False)
    assert online.unknown_network == 1 and offline.unknown_network == 0
    assert online.byte_count == offline.byte_count
    assert "1 项联网媒体大小待定" in online.text


def test_moments_use_original_lengths_and_empty_archive_is_zero(preview_data, monkeypatch):
    from wechat_exporter.media import MediaResolver
    monkeypatch.setattr(MediaResolver, "resolve_moment_file", lambda *args: pytest.fail("preview decodes media"))
    account = preview_data[0]
    small = build_moments_size_profile([Moment("1", "friend", 1, "动态", (
        MomentMedia(total_size=100000),))], account)
    large = build_moments_size_profile([Moment("1", "friend", 1, "动态", (
        MomentMedia(total_size=1000000), MomentMedia(kind="video", total_size=9000000)))], account)
    assert large.size.lower_bytes > small.size.upper_bytes
    assert (large.post_count, large.media_count) == (1, 2)
    assert build_moments_size_profile([], account).size.upper_bytes == 0


def test_preview_cancellation_stops_streaming_and_does_not_export(preview_data):
    seen = 0
    def stop():
        nonlocal seen
        seen += 1
        if seen == 2:
            raise InterruptedError()
    with pytest.raises(InterruptedError):
        profile_for(preview_data, check_cancelled=stop)
    assert seen == 2


def test_size_units_do_not_show_zero_megabytes_for_small_files():
    assert SizeEstimate(10, 50).text == "预计大小 约 30 B"
    assert "1.0 MB" in SizeEstimate(1024**2, 1024**2).text
