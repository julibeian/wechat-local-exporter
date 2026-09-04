from __future__ import annotations

from wechat_exporter import tray


class _FallbackUser32:
    def KillTimer(self, *_args):
        return True

    def DefWindowProcW(self, *_args):
        return 99


def _icon():
    icon = object.__new__(tray.WindowsTrayIcon)
    icon._user32 = _FallbackUser32()
    return icon


def test_tray_mouse_and_notification_events_have_distinct_actions():
    icon = _icon()
    actions = []
    icon.on_show = lambda: actions.append("show")
    icon.on_exit = lambda: actions.append("exit")
    icon._show_context_menu = lambda hwnd: actions.append(("menu", hwnd))

    for event in (
        tray._WM_LBUTTONUP,
        tray._WM_LBUTTONDBLCLK,
        tray._NIN_BALLOONUSERCLICK,
    ):
        assert icon._window_proc(123, tray._TRAY_CALLBACK, 0, event) == 0
    assert icon._window_proc(123, tray._TRAY_CALLBACK, 0, tray._WM_RBUTTONUP) == 0

    assert actions == ["show", "show", "show", ("menu", 123)]


def test_native_notification_events_use_win32_user_message_values():
    assert tray._NIN_SELECT == 0x0400
    assert tray._NIN_KEYSELECT == 0x0401
    assert tray._NIN_BALLOONSHOW == 0x0402
    assert tray._NIN_BALLOONHIDE == 0x0403
    assert tray._NIN_BALLOONTIMEOUT == 0x0404
    assert tray._NIN_BALLOONUSERCLICK == 0x0405


def test_balloon_is_withdrawn_three_seconds_after_it_is_actually_shown():
    timer_calls = []
    cleared = []

    class FakeUser32:
        def SetTimer(self, hwnd, timer_id, duration_ms, callback):
            timer_calls.append((hwnd, timer_id, duration_ms, callback))
            return timer_id

        def KillTimer(self, hwnd, timer_id):
            timer_calls.append(("kill", hwnd, timer_id))
            return True

        def DefWindowProcW(self, *_args):
            return 99

    icon = object.__new__(tray.WindowsTrayIcon)
    icon._user32 = FakeUser32()
    icon._clear_notification = lambda: cleared.append(True)
    icon.on_show = lambda: None
    icon.on_exit = lambda: None

    assert icon._window_proc(
        123,
        tray._TRAY_CALLBACK,
        0,
        tray._NIN_BALLOONSHOW,
    ) == 0
    assert timer_calls == [
        (123, tray._BALLOON_TIMER_ID, 3_000, None),
    ]

    assert icon._window_proc(
        123,
        tray._WM_TIMER,
        tray._BALLOON_TIMER_ID,
        0,
    ) == 0
    assert timer_calls[-1] == ("kill", 123, tray._BALLOON_TIMER_ID)
    assert cleared == [True]


def test_clearing_notification_keeps_icon_and_sends_empty_info_text():
    calls = []

    class FakeShell32:
        def Shell_NotifyIconW(self, action, pointer):
            data = pointer._obj
            calls.append((action, data.uFlags, data.szInfo, data.szInfoTitle))
            return True

    icon = object.__new__(tray.WindowsTrayIcon)
    icon._hwnd = 123
    icon._icon = 456
    icon.tooltip = "微信导出工具"
    icon._shell32 = FakeShell32()

    icon._clear_notification()

    assert calls == [(tray._NIM_MODIFY, tray._NIF_INFO, "", "")]


def test_context_menu_dispatches_true_exit_without_restoring_window():
    appended = []

    class FakeUser32:
        def CreatePopupMenu(self):
            return 7

        def AppendMenuW(self, _menu, flags, command, label):
            appended.append((flags, command, label))
            return True

        def GetCursorPos(self, pointer):
            pointer._obj.x = 10
            pointer._obj.y = 20
            return True

        def SetForegroundWindow(self, _hwnd):
            return True

        def TrackPopupMenuEx(self, *_args):
            return tray._COMMAND_EXIT

        def PostMessageW(self, *_args):
            return True

        def DestroyMenu(self, _menu):
            return True

    icon = object.__new__(tray.WindowsTrayIcon)
    icon._user32 = FakeUser32()
    actions = []
    icon.on_show = lambda: actions.append("show")
    icon.on_exit = lambda: actions.append("exit")

    icon._show_context_menu(123)

    assert actions == ["exit"]
    assert (tray._MF_STRING, tray._COMMAND_SHOW, "打开主窗口") in appended
    assert (tray._MF_STRING, tray._COMMAND_EXIT, "真正退出") in appended
