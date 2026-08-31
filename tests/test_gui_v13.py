import threading
import time
import tkinter as tk
from pathlib import Path
from types import SimpleNamespace

import pytest

from wechat_exporter import gui, update_ui
from wechat_exporter.models import AccountLocation, Conversation
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
    instance = gui.ExporterApp(root)
    for thread in instance._threads:
        thread.join(timeout=2)
    instance._poll_events()
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


def test_startup_does_not_prompt_or_lock_an_account(app):
    assert app.account is None
    assert app.account_var.get() == "尚未确认当前登录账号"
    assert not app._worker_active


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
    controller.manager.result = CheckResult("available", "1.4.0", (Release("1.4.0", "2026-09-01", "新增功能"),))
    controller.refresh()
    assert controller.banner.winfo_manager() == "pack"
    controller.dismiss()
    assert controller.banner.winfo_manager() == ""
    assert "1.4.0" in controller.status.get()
    controller.show()
    assert "新增功能" in controller.notes.get("1.0", "end")
    controller.manager.result = CheckResult("unavailable")
    controller.refresh()
    assert "# v1.3.0" in controller.notes.get("1.0", "end")


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


@pytest.mark.parametrize("size", ["900x800", "1060x900"])
def test_controls_fit_with_update_banner_at_supported_window_sizes(app, size):
    app.root.attributes("-alpha", 0)
    app.root.geometry(size)
    app.root.deiconify()
    app.updates.manager.result = CheckResult("available", "1.4.0")
    app.updates.refresh()
    app.root.update()
    app._set_initial_sash()
    app.root.update_idletasks()
    for widget in (app.connect_button, app.export_button, app.moments_button, app.progress):
        bottom = widget.winfo_rooty() - app.root.winfo_rooty() + widget.winfo_height()
        right = widget.winfo_rootx() - app.root.winfo_rootx() + widget.winfo_width()
        assert bottom <= app.root.winfo_height(), (size, str(widget), bottom, app.middle.winfo_height(), app.export_frame.winfo_reqheight(), app.source_frame.winfo_reqheight())
        assert right <= app.root.winfo_width(), (size, str(widget), right)
        child = widget
        while child.master is not app.root:
            parent = child.master
            assert child.winfo_y() + child.winfo_height() <= parent.winfo_height(), (size, str(child))
            child = parent
