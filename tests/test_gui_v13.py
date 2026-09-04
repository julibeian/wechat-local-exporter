import threading
import time
import tkinter as tk
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import pytest

from wechat_exporter import gui, update_ui
from wechat_exporter.models import AccountLocation, Conversation, ExportResult
from wechat_exporter.service import RestartRequired
from wechat_exporter.update import CheckResult, Release


@pytest.fixture(scope="module")
def tk_host():
    try:
        root = tk.Tk()
    except tk.TclError as error:
        pytest.skip(f"Tk display unavailable: {error}")
    root.withdraw()
    gui._configure_native_fonts(root)
    yield root
    root.destroy()


@pytest.fixture
def app(tmp_path, monkeypatch, tk_host):
    root = tk.Toplevel(tk_host)
    root.withdraw()
    monkeypatch.setattr(gui.ExporterApp, "_detect", lambda self: ("test", tmp_path / "Weixin.exe"))
    monkeypatch.setattr(update_ui.UpdateManager, "check", lambda self, **kw: self.result)
    auto_connect_calls = []

    def auto_connect(_self, **kwargs):
        auto_connect_calls.append(kwargs)
        raise RestartRequired()

    monkeypatch.setattr(gui.ConnectionManager, "connect", auto_connect)
    monkeypatch.setattr(gui.messagebox, "askyesno", lambda *a, **kw: False)

    class FakeTray:
        def __init__(self, **kwargs):
            self.on_show = kwargs["on_show"]
            self.on_exit = kwargs["on_exit"]
            self.started = False
            self.stopped = False
            self.notifications = []

        def start(self):
            self.started = True
            return True

        def notify(self, title, message, *, error=False):
            self.notifications.append((title, message, error))
            return True

        def stop(self):
            self.stopped = True

    trays = []

    def create_fake_tray(**kwargs):
        tray = FakeTray(**kwargs)
        trays.append(tray)
        return tray

    monkeypatch.setattr(gui, "create_tray_icon", create_fake_tray)
    instance = gui.ExporterApp(root)
    # Detection completes first; it then starts the automatic connection
    # attempt. Drain both workers before yielding the app to each test.
    for _ in range(3):
        for thread in list(instance._threads):
            thread.join(timeout=2)
        instance._poll_events()
    instance._auto_connect_calls = auto_connect_calls
    instance._test_trays = trays
    yield instance
    if not instance._closed:
        instance.updates.close()
        root.destroy()
    for identifier in tk_host.tk.call("after", "info"):
        tk_host.after_cancel(identifier)


def fake_service(tmp_path):
    archive = SimpleNamespace(self_conversation=lambda: Conversation("a", "我自己", is_self=True), conversations=lambda: [])
    return SimpleNamespace(account=AccountLocation(tmp_path, "wxid_B", "微信进程"), archive=archive, close=lambda: None)


def drain_worker(app):
    for thread in list(app._threads):
        thread.join(timeout=2)
    app._poll_events()


def select_conversations(app, *conversations):
    app.conversations = list(conversations)
    app._filter_conversations()
    item_ids = tuple(app._conversation_by_iid)
    app.tree.selection_set(*item_ids)
    app._selection_changed()
    app.root.update_idletasks()


def export_layout_snapshot(app):
    return (
        app.middle.sashpos(0),
        app.sessions_frame.winfo_height(),
        app.export_frame.winfo_height(),
        app.export_button.winfo_rootx(),
        app.export_button.winfo_rooty(),
        app.export_button.winfo_width(),
        app.export_button.winfo_height(),
    )


def test_startup_automatically_attempts_connection_without_locking_an_account(app):
    assert app.account is None
    assert app.account_var.get() == "尚未确认当前登录账号"
    assert not app._worker_active
    assert len(app._auto_connect_calls) == 1
    assert app._auto_connect_calls[0]["allow_restart"] is False


def test_json_text_is_default_and_jsonl_is_a_separate_advanced_task(app):
    assert app.chat_format_var.get() == "json"
    assert app.task_var.get() == ""
    texts = []
    stack = [app.export_frame]
    while stack:
        widget = stack.pop()
        stack.extend(widget.winfo_children())
        try:
            texts.append(str(widget.cget("text")))
        except tk.TclError:
            pass
    assert any("JSON（最快）" in text for text in texts)
    assert any(text == "AI 完整资料包" for text in texts)


def test_real_cold_start_restores_conversation_pane_without_manual_sash_reset(app):
    app.root.attributes("-alpha", 0)
    app.root.geometry("1060x900")
    app.root.deiconify()
    deadline = time.monotonic() + 1
    while time.monotonic() < deadline and app.middle.sashpos(0) < 100:
        app.root.update()
        time.sleep(0.02)

    assert app.middle.winfo_height() >= 120
    assert app.middle.sashpos(0) >= 100
    assert app.sessions_frame.winfo_height() >= 100


def test_conversation_and_export_panes_use_fixed_chat_text_reference_height(app):
    select_conversations(app, Conversation("wxid_friend", "好友"))
    app.root.attributes("-alpha", 0)
    app.root.geometry("1060x900")
    app.root.deiconify()
    app.root.update()

    app._fit_export_pane()
    app.root.update_idletasks()

    assert app.middle.sashpos(0) == pytest.approx(
        app.middle.winfo_height()
        - app._chat_export_pane_height
        - gui.PANE_SASH_ALLOWANCE,
        abs=2,
    )
    assert app.export_frame.winfo_height() == pytest.approx(
        app._chat_export_pane_height,
        abs=gui.PANE_SASH_ALLOWANCE,
    )
    assert app.sessions_frame.winfo_height() >= 60


@pytest.mark.parametrize("size", ["900x800", "1060x900"])
def test_task_and_format_switches_keep_panes_and_primary_action_fixed(app, size):
    select_conversations(app, Conversation("wxid_friend", "好友"))
    app.root.attributes("-alpha", 0)
    app.root.geometry(size)
    app.root.deiconify()
    app.task_var.set("chat")
    app.chat_format_var.set("json")
    app.root.update()

    baseline = export_layout_snapshot(app)
    assert app.pdf_mode_frame.winfo_manager() == ""

    states = (
        ("chat", "txt", False),
        ("chat", "pdf", False),
        ("chat", "pdf", True),
        ("jsonl_package", "pdf", True),
        ("chat_files", "pdf", True),
        ("moments", "pdf", True),
        ("chat", "json", False),
    )
    for task, chat_format, pdf_images in states:
        app.task_var.set(task)
        app.chat_format_var.set(chat_format)
        app.pdf_images_var.set(pdf_images)
        app.root.update()

        current = export_layout_snapshot(app)
        for actual, expected in zip(current, baseline):
            assert actual == pytest.approx(expected, abs=1), (size, task, current, baseline)
        assert bool(app.pdf_mode_frame.winfo_ismapped()) is (
            task == "chat" and chat_format == "pdf"
        )
        assert (
            app.export_button.winfo_rooty() + app.export_button.winfo_height()
            <= app.export_frame.winfo_rooty() + app.export_frame.winfo_height()
        )


def test_cancel_action_does_not_move_primary_export_action(app):
    select_conversations(app, Conversation("wxid_friend", "好友"))
    app.root.attributes("-alpha", 0)
    app.root.geometry("1060x900")
    app.root.deiconify()
    app.root.update()
    baseline = export_layout_snapshot(app)

    app.cancel_export_button.pack(side="right", padx=(0, 10))
    app.root.update_idletasks()
    current = export_layout_snapshot(app)

    assert current[3:] == baseline[3:]


def test_export_options_expand_progressively_and_pdf_mode_only_exists_for_pdf(app):
    contact = Conversation("wxid_friend", "好友")
    select_conversations(app, contact)
    assert app.task_frame.winfo_manager() == "grid"
    assert app.task_var.get() == "chat"
    assert app.date_frame.winfo_manager() == "grid"
    assert app.chat_options_frame.winfo_manager() == "grid"
    assert app.output_frame.winfo_manager() == "grid"
    assert app.package_options_frame.winfo_manager() == ""
    assert app.pdf_mode_frame.winfo_manager() == ""

    app.chat_format_var.set("pdf")
    app.root.update_idletasks()
    assert app.pdf_mode_frame.winfo_manager() == "pack"
    app.pdf_images_var.set(True)
    assert "完整版" in app.confirmation_var.get()

    app.chat_format_var.set("json")
    app.root.update_idletasks()
    assert app.pdf_mode_frame.winfo_manager() == ""
    assert app.pdf_images_var.get() is False

    app.task_var.set("jsonl_package")
    app.root.update_idletasks()
    assert app.package_options_frame.winfo_manager() == "grid"
    assert app.chat_options_frame.winfo_manager() == ""
    assert "语音仅存微信转录" in app.confirmation_var.get()

    app.task_var.set("moments")
    app.root.update_idletasks()
    assert app.date_frame.winfo_manager() == ""
    assert app.moments_options_frame.winfo_manager() == "grid"


def test_first_load_selects_first_a_contact_and_opens_first_task(app):
    app._default_selection_pending = True
    app.task_var.set("")
    app.conversations = [
        Conversation("wxid_self", "我自己", is_self=True),
        Conversation("alpha-group@chatroom", "AAA 群", is_group=True),
        Conversation("wxid_alice", "Alice"),
        Conversation("wxid_aaron", "Aaron"),
        Conversation("wxid_bob", "Bob"),
    ]

    app._filter_conversations()
    app.root.update_idletasks()

    selected = app._selected_conversations()
    assert [item.username for item in selected] == ["wxid_aaron"]
    assert app.task_var.get() == "chat"
    assert app.date_frame.winfo_manager() == "grid"
    assert app.chat_options_frame.winfo_manager() == "grid"


def test_first_load_falls_forward_when_a_has_no_contact(app):
    app._default_selection_pending = True
    app.task_var.set("")
    app.conversations = [
        Conversation("alpha-group@chatroom", "A 群", is_group=True),
        Conversation("wxid_charlie", "Charlie"),
        Conversation("wxid_bob", "Bob"),
    ]

    app._filter_conversations()

    assert [item.username for item in app._selected_conversations()] == ["wxid_bob"]
    assert app.task_var.get() == "chat"


def test_usage_guide_is_one_top_level_secondary_document(app):
    assert app.help_button.cget("text") == "使用说明"
    app._show_usage_guide()
    dialog = app.help_dialog
    try:
        assert dialog is not None
        assert dialog.title() == "使用说明"
        assert dialog.section_list.size() == 6
        assert "聊天文字" in dialog.article.get("1.0", "end")
        dialog.section_list.selection_clear(0, "end")
        dialog.section_list.selection_set(3)
        dialog._section_changed()
        assert "不会自行运行语音识别" in dialog.article.get("1.0", "end")
    finally:
        if dialog is not None:
            dialog.destroy()


def test_ordinary_chat_dispatches_exactly_one_format_and_hides_pdf_flag(
    app, monkeypatch
):
    select_conversations(app, Conversation("wxid_friend", "好友"))
    captured = []
    app.service = SimpleNamespace(
        archive=object(),
        export=lambda request, **_kwargs: captured.append(request),
    )
    monkeypatch.setattr(app, "_calibrate_selected", lambda _items: True)
    monkeypatch.setattr(app, "_run_worker", lambda _name, function, **_kwargs: function())

    app.task_var.set("chat")
    app.chat_format_var.set("pdf")
    app.pdf_images_var.set(True)
    app._export_clicked()
    assert (
        captured[-1].include_json,
        captured[-1].include_jsonl,
        captured[-1].include_txt,
        captured[-1].include_pdf,
        captured[-1].include_pdf_images,
    ) == (False, False, False, True, True)

    app.chat_format_var.set("json")
    app._export_clicked()
    assert (
        captured[-1].include_json,
        captured[-1].include_jsonl,
        captured[-1].include_txt,
        captured[-1].include_pdf,
        captured[-1].include_pdf_images,
    ) == (True, False, False, False, False)


def test_advanced_package_dispatch_uses_confirmed_safe_defaults(app, monkeypatch):
    select_conversations(app, Conversation("wxid_friend", "好友"))
    captured = []
    app.service = SimpleNamespace(
        archive=object(),
        export_jsonl_package=lambda request, **_kwargs: captured.append(request),
    )
    monkeypatch.setattr(app, "_calibrate_selected", lambda _items: True)
    monkeypatch.setattr(app, "_run_worker", lambda _name, function, **_kwargs: function())
    app.task_var.set("jsonl_package")
    app._export_jsonl_package_clicked()

    request = captured[-1]
    assert request.include_videos is True
    assert request.max_video_size_bytes == 100 * 1024 * 1024
    assert request.allow_network_media is False
    app.package_network_var.set(True)
    assert "联网补全表情和卡片封面" in app._confirmation_summary(
        "jsonl_package", (Conversation("wxid_friend", "好友"),)
    )


def test_chat_file_dialog_defaults_to_all_categories_and_100_mb(app):
    dialog = gui.ChatFileExportDialog(app.root)
    try:
        assert all(variable.get() for variable in dialog.category_vars.values())
        assert dialog.max_size_var.get() == "100"
        assert gui._max_file_size_bytes("100") == 100 * 1024 * 1024
        assert gui._max_file_size_bytes("0") == 0
        with pytest.raises(ValueError):
            gui._max_file_size_bytes("-1")
    finally:
        dialog.destroy()


@pytest.mark.parametrize("consent", [False, True])
def test_gui_restart_only_after_explicit_consent(app, tmp_path, monkeypatch, consent):
    calls, prompts = [], []
    def connect(**kwargs):
        calls.append(kwargs["allow_restart"])
        if not kwargs["allow_restart"]:
            raise RestartRequired()
        return fake_service(tmp_path)
    app.connection.connect = connect
    monkeypatch.setattr(gui.messagebox, "askyesno", lambda *a, **kw: prompts.append(a) or consent)
    app._connect_clicked()
    drain_worker(app)
    assert len(prompts) == 1
    if consent:
        drain_worker(app)
        assert calls == [False, True]
        assert "wxid_B" in app.account_var.get()
        assert "已确认" in app.account_var.get()
    else:
        assert calls == [False]


def test_gui_direct_connection_has_no_modal(app, tmp_path, monkeypatch):
    app.connection.connect = lambda **kw: fake_service(tmp_path)
    monkeypatch.setattr(gui.messagebox, "askyesno", lambda *a, **kw: pytest.fail("unexpected modal"))
    app._connect_clicked()
    drain_worker(app)
    assert app.service.account.wxid == "wxid_B"


def test_update_banner_and_offline_history_stay_inside_ui(app, monkeypatch):
    controller = app.updates
    controller.manager.result = CheckResult(
        "available",
        "2.0.0",
        (
            Release(
                "2.0.0",
                "2026-09-01",
                "新增功能\n<!-- wechat-exporter-target-sha256:" + "a" * 64 + " -->",
            ),
        ),
    )
    controller.refresh()
    assert controller.banner.winfo_manager() == "pack"
    controller.dismiss()
    assert controller.banner.winfo_manager() == ""
    assert "2.0.0" in controller.status.get()
    controller.show()
    assert "新增功能" in controller.notes.get("1.0", "end")
    assert "target-sha256" not in controller.notes.get("1.0", "end")
    assert controller.dialog_current.get() == "v1.5.0"
    assert controller.dialog_latest.get() == "v2.0.0"
    assert controller.release_heading.get() == "v2.0.0"
    assert controller.release_list.size() == 1
    controller.manager.result = CheckResult("unavailable")
    controller.refresh()
    assert controller.release_heading.get() == "v1.5.0"
    assert controller.release_list.size() >= 2
    assert "JSONL" in controller.notes.get("1.0", "end")


def test_header_hides_update_status_and_exposes_github_link(app, monkeypatch):
    controller = app.updates
    opened = []
    monkeypatch.setattr(
        update_ui.webbrowser,
        "open",
        lambda url, **kwargs: opened.append((url, kwargs)) or True,
    )

    assert not hasattr(controller, "status_label")
    assert controller.github_link.cget("text") == "GitHub ↗"
    assert controller.open_project() is True
    assert opened == [(update_ui.PROJECT_URL, {"new": 2})]


def test_version_dialog_uses_readable_two_pane_layout_at_minimum_size(app):
    controller = app.updates
    app.root.attributes("-alpha", 0)
    app.root.deiconify()
    app.root.update()
    controller.manager.result = CheckResult(
        "available",
        "2.0.0",
        (
            Release("2.0.0", "2026-09-03", "第一项。\n\n第二项。"),
            Release("1.5.0", "2026-09-02", "旧版说明。"),
        ),
    )
    controller.show()
    controller.dialog.attributes("-alpha", 0)
    controller.dialog.geometry("720x500")
    controller.dialog.deiconify()
    controller.dialog.update()

    assert controller.release_list.size() == 2
    assert controller.release_list.winfo_width() >= 100
    assert controller.notes.winfo_width() >= 300
    assert controller.notes.winfo_rootx() > controller.release_list.winfo_rootx()
    for widget in (
        controller.release_list,
        controller.notes,
        controller.check_button,
        controller.download_button,
    ):
        bottom = (
            widget.winfo_rooty()
            - controller.dialog.winfo_rooty()
            + widget.winfo_height()
        )
        assert bottom <= controller.dialog.winfo_height()


def test_update_network_thread_does_not_block_tk(app):
    controller = app.updates
    waiting, release = threading.Event(), threading.Event()
    def blocked_check(**kwargs):
        waiting.set()
        release.wait(2)
        return CheckResult("unavailable")
    controller.manager.check = blocked_check
    try:
        controller.check()
        assert waiting.wait(1)
        ticks = []
        app.root.after(0, lambda: ticks.append(True))
        app.root.update()
        assert ticks == [True]
        assert not app._worker_active  # Local actions have their own worker state.
    finally:
        release.set()


def test_date_shortcuts_are_on_primary_export_page(app):
    select_conversations(app, Conversation("wxid_friend", "好友"))
    app.task_var.set("chat")
    today = date(2026, 9, 1)
    for button in (
        app.all_dates_button,
        app.seven_days_button,
        app.one_month_button,
        app.custom_dates_button,
    ):
        assert button.winfo_manager() == "pack"

    app._set_quick_date_range(7, today=today)
    assert app.start_var.get() == "2026-08-26"
    assert app.end_var.get() == "2026-09-01"
    app._set_quick_date_range(30, today=today)
    assert app.start_var.get() == "2026-08-03"
    assert app.end_var.get() == "2026-09-01"
    app._set_quick_date_range(None, today=today)
    assert app.range_var.get() == "全部日期"


def test_cancel_button_stops_worker_without_popup(app, monkeypatch):
    release = threading.Event()
    worker = threading.Thread(target=lambda: release.wait(2))
    generation = 1
    app._threads.append(worker)
    app._worker_active = True
    app._active_worker_name = "export"
    app._active_export_generation = generation
    app._export_cancel_events[generation] = threading.Event()
    app._export_threads[generation] = worker
    app._export_started_at = time.perf_counter()
    app.progress["value"] = 54
    app.service = SimpleNamespace(archive=object())
    worker.start()
    monkeypatch.setattr(
        gui.messagebox,
        "showinfo",
        lambda *args, **kwargs: pytest.fail("取消导出不应弹窗"),
    )

    app._cancel_export_clicked()

    assert app._export_cancel_events[generation].is_set()
    assert not app._worker_active
    assert app._active_export_generation is None
    assert str(app.cancel_export_button.cget("state")) == "disabled"
    assert app.cancel_export_button.cget("text") == "取消导出"
    assert app.progress["value"] == 0
    assert str(app.export_button.cget("state")) == "disabled"
    assert str(app.connect_button.cget("state")) == "normal"
    assert "可继续操作" in app.status_var.get()

    # A predecessor's late progress/completion must not overwrite a new page state.
    app._active_export_generation = 2
    app._worker_active = True
    app._active_worker_name = "export"
    app._export_started_at = time.perf_counter()
    app._latest_export_status = "新的导出任务 12%"
    app.status_var.set("新的导出任务 12%")
    app.progress["value"] = 12
    release.set()
    worker.join(2)
    app.events.put(("export:cancelled", (generation, None)))
    app._poll_events()

    assert app.progress["value"] == 12
    assert "新的导出任务" in app.status_var.get()
    assert app.cancel_export_button.cget("text") == "取消导出"
    app._worker_active = False
    app._active_worker_name = None
    app._active_export_generation = None
    app._export_started_at = None


def test_close_waits_for_reader_before_workspace_cleanup(app):
    release = threading.Event()
    worker = threading.Thread(target=lambda: release.wait(2))
    app._background_threads.append(worker)
    worker.start()
    closed = []
    app.service = SimpleNamespace(close=lambda: closed.append(True))
    app._on_close()
    assert not app._closed and not closed
    release.set()
    worker.join(2)
    app._on_close()
    assert app._closed and closed == [True]


def test_window_close_hides_to_tray_and_tray_click_restores(app):
    app.root.attributes("-alpha", 0)
    app.root.deiconify()
    app.root.update()

    app._hide_to_tray()

    tray = app._test_trays[-1]
    assert app._tray_hidden is True
    assert app.root.state() == "withdrawn"
    assert tray.started is True
    assert tray.notifications[-1][0] == "已缩小到系统托盘"
    assert app._closing is False

    tray.on_show()
    app._poll_events()
    assert app._tray_hidden is False
    assert app.root.state() != "withdrawn"


def test_tray_real_exit_closes_idle_application(app):
    closed = []
    app.service = SimpleNamespace(close=lambda: closed.append(True))
    app._hide_to_tray()
    tray = app._test_trays[-1]

    tray.on_exit()
    app._poll_events()

    assert app._closed is True
    assert closed == [True]
    assert tray.stopped is True


def test_tray_real_exit_declined_during_export_keeps_work_running(app, monkeypatch):
    generation = 51
    cancelled = threading.Event()
    app._active_export_generation = generation
    app._export_cancel_events[generation] = cancelled
    app._hide_to_tray()
    tray = app._test_trays[-1]
    monkeypatch.setattr(gui.messagebox, "askyesno", lambda *args, **kwargs: False)

    tray.on_exit()
    app._poll_events()

    assert app._closed is False
    assert app._closing is False
    assert cancelled.is_set() is False


def test_tray_real_exit_confirmed_cancels_then_waits_for_export(app, monkeypatch):
    release = threading.Event()
    worker = threading.Thread(target=lambda: release.wait(2))
    generation = 52
    cancelled = threading.Event()
    closed = []
    app.service = SimpleNamespace(close=lambda: closed.append(True))
    app._active_export_generation = generation
    app._export_cancel_events[generation] = cancelled
    app._export_threads[generation] = worker
    app._threads.append(worker)
    worker.start()
    app._hide_to_tray()
    tray = app._test_trays[-1]
    monkeypatch.setattr(gui.messagebox, "askyesno", lambda *args, **kwargs: True)

    tray.on_exit()
    app._poll_events()

    assert cancelled.is_set() is True
    assert app._closing is True
    assert app._closed is False
    assert closed == []

    release.set()
    worker.join(2)
    deadline = time.monotonic() + 1
    while not app._closed and time.monotonic() < deadline:
        app.root.update()
    assert app._closed is True
    assert closed == [True]


def test_external_update_waits_for_active_work_without_cancelling_it(app):
    release = threading.Event()
    worker = threading.Thread(target=lambda: release.wait(2))
    generation = 53
    cancelled = threading.Event()
    closed = []
    app.service = SimpleNamespace(close=lambda: closed.append(True))
    app._active_export_generation = generation
    app._export_cancel_events[generation] = cancelled
    app._export_threads[generation] = worker
    app._threads.append(worker)
    app._worker_active = True
    worker.start()

    app.events.put(("instance:update-exit", None))
    app._poll_events()

    assert app._external_update_pending is True
    assert cancelled.is_set() is False
    assert app._closing is False

    app._worker_active = False
    app._active_export_generation = None
    release.set()
    worker.join(2)
    app._poll_events()

    assert app._closed is True
    assert closed == [True]


def test_export_continues_in_tray_and_completion_uses_notification(app, monkeypatch):
    generation = 41
    app._active_export_generation = generation
    app._worker_active = True
    app._active_worker_name = "export"
    app._hide_to_tray()
    tray = app._test_trays[-1]
    monkeypatch.setattr(
        gui.messagebox,
        "showinfo",
        lambda *_args, **_kwargs: pytest.fail("hidden export opened a modal dialog"),
    )

    app.events.put(
        (
            "export:ok",
            (generation, ExportResult(duration_seconds=1.25)),
        )
    )
    app._poll_events()

    assert app._closing is False
    assert app._active_export_generation is None
    assert any(title == "导出完成" for title, _message, _error in tray.notifications)


def test_export_failure_in_tray_uses_error_notification(app, monkeypatch):
    generation = 42
    app._active_export_generation = generation
    app._worker_active = True
    app._active_worker_name = "export"
    app._hide_to_tray()
    tray = app._test_trays[-1]
    monkeypatch.setattr(
        gui.messagebox,
        "showerror",
        lambda *_args, **_kwargs: pytest.fail("hidden export opened a modal dialog"),
    )

    app.events.put(("export:error", (generation, RuntimeError("磁盘空间不足"))))
    app._poll_events()

    assert app._active_export_generation is None
    assert tray.notifications[-1] == ("导出失败", "磁盘空间不足", True)


@pytest.mark.parametrize("size", ["900x800", "1060x900"])
def test_controls_fit_with_update_banner_at_supported_window_sizes(app, size):
    select_conversations(app, Conversation("wxid_friend", "好友"))
    app.task_var.set("jsonl_package")
    app.root.attributes("-alpha", 0)
    app.root.geometry(size)
    app.root.deiconify()
    app.updates.manager.result = CheckResult("available", "2.0.0")
    app.updates.refresh()
    app.root.update()
    app._set_initial_sash()
    app.root.update_idletasks()
    for widget in (
        app.connect_button,
        app.export_button,
        app.history_button,
        app.all_dates_button,
        app.seven_days_button,
        app.one_month_button,
        app.custom_dates_button,
        app.progress,
    ):
        bottom = widget.winfo_rooty() - app.root.winfo_rooty() + widget.winfo_height()
        right = widget.winfo_rootx() - app.root.winfo_rootx() + widget.winfo_width()
        assert bottom <= app.root.winfo_height(), (
            size,
            str(widget),
            bottom,
            app.middle.winfo_height(),
            app.export_frame.winfo_reqheight(),
            app.source_frame.winfo_reqheight(),
            widget.winfo_rooty(),
            app.root.winfo_rooty(),
            app.outer.winfo_height(),
            app.outer.winfo_reqheight(),
            app.middle.sashpos(0),
        )
        assert right <= app.root.winfo_width(), (size, str(widget), right)
        child = widget
        while child.master is not app.root:
            parent = child.master
            assert child.winfo_y() + child.winfo_height() <= parent.winfo_height(), (size, str(child))
            child = parent
