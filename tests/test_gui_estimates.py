import time
import tkinter.font as tkfont
from dataclasses import replace
from datetime import date, timedelta
from types import SimpleNamespace

import pytest

from test_export_size_estimates import preview_data
from test_gui_v13 import app, select_conversations, tk_host
from wechat_exporter import gui
from wechat_exporter.models import Conversation, Moment, MomentMedia


def connect_preview(app, data):
    account, conversation, archive = data
    archive.contact_moments = lambda contact: [Moment("1", contact.username, 1791500000, "周末见", (
        MomentMedia(total_size=500000), MomentMedia(kind="video", total_size=3000000),
    ))]
    app.service = SimpleNamespace(account=account, archive=archive, close=lambda: None)
    app.account = account
    app._default_selection_pending = False
    select_conversations(app, conversation)


def await_estimate(app, task=None):
    variable = app.task_estimate_vars[task or app.task_var.get()]
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        app.root.update()
        text = variable.get()
        if "预计大小" in text and ":" not in text and "：" not in text:
            return text
        assert "暂时无法" not in text, text
        time.sleep(.02)
    raise AssertionError(variable.get())


def test_every_task_has_its_own_visible_simsun_estimate_and_no_duplicate_dates(app, preview_data):
    connect_preview(app, preview_data)
    app.root.deiconify()
    app.root.attributes("-alpha", 0)
    labels = {
        "chat": app.chat_estimate_label,
        "jsonl_package": app.package_estimate_label,
        "chat_files": app.chat_files_estimate_label,
        "moments": app.moments_estimate_label,
    }
    assert len({str(variable) for variable in app.task_estimate_vars.values()}) == 4
    for task, label in labels.items():
        app.task_buttons[task].invoke()
        text = await_estimate(app, task)
        assert "预计大小" in text and "无媒体读取" not in text
        assert " " not in text and "·" not in text and "–" not in text
        assert "-" in text
        app.root.update_idletasks()
        assert label.winfo_viewable(), (task, text)
        assert label.winfo_rooty() + label.winfo_height() < app.export_button.winfo_rooty()
        assert tkfont.Font(root=app.root, font=label.cget("font")).actual("family") in {"SimSun", "宋体"}
        if task != "moments":
            assert app.range_var.get() not in app.confirmation_var.get()
            assert app.confirm_step_label.cget("text") == "5. 确认"


def test_format_media_and_file_buttons_immediately_recalculate_from_cached_profile(app, preview_data):
    connect_preview(app, preview_data)
    html = await_estimate(app)
    initial_reads = len(preview_data[2].calls)
    app.html_videos_var.set(True)
    html_video = app.estimate_var.get()
    assert html != html_video
    app.chat_format_var.set("json")
    json_text = app.estimate_var.get()
    app.chat_format_var.set("txt")
    assert app.estimate_var.get() != json_text
    app.chat_format_var.set("pdf")
    pdf_quick = app.estimate_var.get()
    app.pdf_images_var.set(True)
    assert app.estimate_var.get() != pdf_quick
    app.task_buttons["jsonl_package"].invoke()
    package_video = app.package_estimate_var.get()
    app.package_include_videos_var.set(False)
    package_no_video = app.package_estimate_var.get()
    assert package_video != package_no_video
    app.package_video_limit_var.set("0.001")
    app.package_include_videos_var.set(True)
    assert app.package_estimate_var.get() == package_no_video
    app.task_buttons["chat_files"].invoke()
    both = app.chat_files_estimate_var.get()
    app.chat_file_category_vars["other"].set(False)
    assert app.chat_files_estimate_var.get() != both
    pdf_only = app.chat_files_estimate_var.get()
    app.chat_file_limit_var.set("0.001")
    assert app.chat_files_estimate_var.get() != pdf_only
    # Format switches use cached byte totals instead of rereading all messages.
    assert len(preview_data[2].calls) == initial_reads


def test_date_buttons_update_counts_and_size_for_all_three_chat_tasks(app, preview_data):
    archive = preview_data[2]
    archive.messages[0] = replace(archive.messages[0], timestamp=archive.messages[0].timestamp - 40 * 86400)
    connect_preview(app, preview_data)
    today = date.fromtimestamp(archive.messages[-1].timestamp)
    for task in ("chat", "jsonl_package", "chat_files"):
        app.task_buttons[task].invoke()
        app._set_quick_date_range(None)
        full = await_estimate(app, task)
        app._set_quick_date_range(7, today=today)
        recent = await_estimate(app, task)
        if task != "chat_files":
            assert "6条" in full and "5条" in recent
            assert full != recent
        app._set_date_range(today + timedelta(days=1), None)
        empty = await_estimate(app, task)
        assert empty != full
        assert app.range_var.get() not in app.confirmation_var.get()


def test_late_preview_result_cannot_overwrite_a_new_selection(app, preview_data):
    connect_preview(app, preview_data)
    await_estimate(app)
    old_generation = app._estimate_generation
    old_key, old_profile = next(iter(app._estimate_cache.items()))
    selected = Conversation("someone-else", "另一联系人")
    select_conversations(app, selected)
    assert "0B" in await_estimate(app)
    current = app.estimate_var.get()
    app.events.put(("estimate:ok", (old_generation, old_key, old_profile, 1)))
    app._poll_events()
    assert app.estimate_var.get() == current


@pytest.mark.parametrize("source,expected", [
    ("谢谢[玫瑰]【花】[花]", "谢谢[玫瑰]【花】[花]"),
    ("[OK][咖啡] 好的👌", "[OK][咖啡] 好的👌"),
    ("[图片]【未知】[用户说明]", "[图片]【未知】[用户说明]"),
])
def test_recent_message_faces_keep_native_labels_and_original_unicode(app, source, expected):
    conversation = Conversation("friend", "联系人", summary=source)
    select_conversations(app, conversation)
    values = app.tree.item(app.tree.selection()[0], "values")
    assert values[-1] == expected
    assert conversation.summary == source
    # The accessible row retains source labels; original artwork is a separate cell.
    assert "image" not in app.tree.item(app.tree.selection()[0]) or not app.tree.item(app.tree.selection()[0], "image")


def test_text_and_file_preview_do_not_decode_images_and_html_refines_the_cache(app, preview_data, monkeypatch):
    from wechat_exporter.image_probe import LocalImageSizeProbe
    original = LocalImageSizeProbe.measure
    reads = []
    def measure(self, path, **kwargs):
        reads.append(kwargs.get("cached_only", False))
        return original(self, path, **kwargs)
    monkeypatch.setattr(LocalImageSizeProbe, "measure", measure)
    app.chat_format_var.set("json")
    connect_preview(app, preview_data)
    assert "预计大小" in await_estimate(app)
    assert reads and all(reads)
    app.task_buttons["chat_files"].invoke()
    assert "预计大小" in await_estimate(app)
    assert all(reads)
    app.chat_format_var.set("html")
    app.task_buttons["chat"].invoke()
    assert "预计大小" in await_estimate(app)
    assert any(not value for value in reads)


def test_export_completion_refreshes_the_estimate_after_local_media_changes(app, preview_data, monkeypatch):
    from PIL import Image
    connect_preview(app, preview_data)
    original = await_estimate(app)
    reads = len(preview_data[2].calls)
    image_path = next(preview_data[0].account_dir.rglob("*.dat"))
    Image.effect_noise((180, 180), 100).save(image_path, format="PNG")
    monkeypatch.setattr(gui, "append_export_history", lambda *_a, **_k: None)
    app._active_export_generation = 1
    app.events.put(("export:ok", (1, SimpleNamespace(files=[], warnings=[], duration_seconds=.1))))
    app._poll_events()
    refreshed = await_estimate(app)
    assert refreshed != original
    assert len(preview_data[2].calls) > reads
