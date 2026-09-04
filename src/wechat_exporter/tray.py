"""Small native Windows notification-area icon without an extra dependency."""
from __future__ import annotations

import ctypes
import os
import sys
import threading
from collections.abc import Callable
from ctypes import wintypes


_WM_CLOSE = 0x0010
_WM_DESTROY = 0x0002
_WM_NULL = 0x0000
_WM_TIMER = 0x0113
_WM_USER = 0x0400
_WM_APP = 0x8000
_WM_LBUTTONUP = 0x0202
_WM_LBUTTONDBLCLK = 0x0203
_WM_RBUTTONUP = 0x0205
_NIN_SELECT = _WM_USER
_NIN_KEYSELECT = _WM_USER + 1
_NIN_BALLOONSHOW = _WM_USER + 2
_NIN_BALLOONHIDE = _WM_USER + 3
_NIN_BALLOONTIMEOUT = _WM_USER + 4
_NIN_BALLOONUSERCLICK = _WM_USER + 5
_TRAY_CALLBACK = _WM_APP + 37
_NIM_ADD = 0x00000000
_NIM_MODIFY = 0x00000001
_NIM_DELETE = 0x00000002
_NIM_SETVERSION = 0x00000004
_NOTIFYICON_VERSION_4 = 4
_NIF_MESSAGE = 0x00000001
_NIF_ICON = 0x00000002
_NIF_TIP = 0x00000004
_NIF_INFO = 0x00000010
_NIIF_INFO = 0x00000001
_NIIF_ERROR = 0x00000003
_IMAGE_ICON = 1
_LR_DEFAULTSIZE = 0x00000040
_LR_SHARED = 0x00008000
_IDI_APPLICATION = 32512
_MF_STRING = 0x0000
_MF_SEPARATOR = 0x0800
_TPM_RIGHTBUTTON = 0x0002
_TPM_RETURNCMD = 0x0100
_COMMAND_SHOW = 1001
_COMMAND_EXIT = 1002
_BALLOON_TIMER_ID = 1
_BALLOON_DURATION_MS = 3_000


class _Guid(ctypes.Structure):
    _fields_ = [
        ("Data1", wintypes.DWORD),
        ("Data2", wintypes.WORD),
        ("Data3", wintypes.WORD),
        ("Data4", ctypes.c_ubyte * 8),
    ]


class _NotifyIconDataW(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("hWnd", wintypes.HWND),
        ("uID", wintypes.UINT),
        ("uFlags", wintypes.UINT),
        ("uCallbackMessage", wintypes.UINT),
        ("hIcon", wintypes.HICON),
        ("szTip", wintypes.WCHAR * 128),
        ("dwState", wintypes.DWORD),
        ("dwStateMask", wintypes.DWORD),
        ("szInfo", wintypes.WCHAR * 256),
        ("uVersion", wintypes.UINT),
        ("szInfoTitle", wintypes.WCHAR * 64),
        ("dwInfoFlags", wintypes.DWORD),
        ("guidItem", _Guid),
        ("hBalloonIcon", wintypes.HICON),
    ]


_WNDPROC = ctypes.WINFUNCTYPE(
    ctypes.c_ssize_t,
    wintypes.HWND,
    wintypes.UINT,
    wintypes.WPARAM,
    wintypes.LPARAM,
)


class _WindowClassW(ctypes.Structure):
    _fields_ = [
        ("style", wintypes.UINT),
        ("lpfnWndProc", _WNDPROC),
        ("cbClsExtra", ctypes.c_int),
        ("cbWndExtra", ctypes.c_int),
        ("hInstance", wintypes.HINSTANCE),
        ("hIcon", wintypes.HICON),
        ("hCursor", wintypes.HANDLE),
        ("hbrBackground", wintypes.HBRUSH),
        ("lpszMenuName", wintypes.LPCWSTR),
        ("lpszClassName", wintypes.LPCWSTR),
    ]


class WindowsTrayIcon:
    """Own a hidden Win32 message window and one notification-area icon."""

    def __init__(
        self,
        *,
        tooltip: str,
        on_show: Callable[[], None],
        on_exit: Callable[[], None],
    ):
        self.tooltip = tooltip
        self.on_show = on_show
        self.on_exit = on_exit
        self._ready = threading.Event()
        self._stopped = threading.Event()
        self._thread: threading.Thread | None = None
        self._hwnd: int | None = None
        self._icon: int | None = None
        self._owns_icon = False
        self._error: BaseException | None = None
        self._wndproc = _WNDPROC(self._window_proc)
        self._class_name = f"WeChatExporterTray_{os.getpid()}_{id(self):x}"

    def start(self) -> bool:
        if os.name != "nt":
            return False
        if self._thread is None:
            self._thread = threading.Thread(
                target=self._run,
                name="wechat-exporter-tray",
                daemon=True,
            )
            self._thread.start()
        self._ready.wait(3)
        return self._hwnd is not None and self._error is None

    def notify(self, title: str, message: str, *, error: bool = False) -> bool:
        hwnd = self._hwnd
        if hwnd is None:
            return False
        data = self._data(_NIF_INFO)
        data.szInfoTitle = title[:63]
        data.szInfo = message[:255]
        data.dwInfoFlags = _NIIF_ERROR if error else _NIIF_INFO
        return bool(self._shell32.Shell_NotifyIconW(_NIM_MODIFY, ctypes.byref(data)))

    def stop(self) -> None:
        hwnd = self._hwnd
        if hwnd is not None:
            self._user32.KillTimer(hwnd, _BALLOON_TIMER_ID)
            self._user32.PostMessageW(hwnd, _WM_CLOSE, 0, 0)
        if self._thread is not None and threading.current_thread() is not self._thread:
            self._stopped.wait(3)

    def _data(self, flags: int) -> _NotifyIconDataW:
        data = _NotifyIconDataW()
        data.cbSize = ctypes.sizeof(_NotifyIconDataW)
        data.hWnd = self._hwnd
        data.uID = 1
        data.uFlags = flags
        data.uCallbackMessage = _TRAY_CALLBACK
        data.hIcon = self._icon
        data.szTip = self.tooltip[:127]
        return data

    def _run(self) -> None:
        try:
            self._user32 = ctypes.WinDLL("user32", use_last_error=True)
            self._shell32 = ctypes.WinDLL("shell32", use_last_error=True)
            self._kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            self._configure_functions()
            instance = self._kernel32.GetModuleHandleW(None)
            window_class = _WindowClassW(
                0,
                self._wndproc,
                0,
                0,
                instance,
                None,
                None,
                None,
                None,
                self._class_name,
            )
            if not self._user32.RegisterClassW(ctypes.byref(window_class)):
                raise ctypes.WinError(ctypes.get_last_error())
            hwnd = self._user32.CreateWindowExW(
                0,
                self._class_name,
                self.tooltip,
                0,
                0,
                0,
                0,
                0,
                None,
                None,
                instance,
                None,
            )
            if not hwnd:
                raise ctypes.WinError(ctypes.get_last_error())
            self._hwnd = int(hwnd)
            self._icon, self._owns_icon = self._load_icon()
            data = self._data(_NIF_MESSAGE | _NIF_ICON | _NIF_TIP)
            if not self._shell32.Shell_NotifyIconW(_NIM_ADD, ctypes.byref(data)):
                raise ctypes.WinError(ctypes.get_last_error())
            version = self._data(0)
            version.uVersion = _NOTIFYICON_VERSION_4
            self._shell32.Shell_NotifyIconW(_NIM_SETVERSION, ctypes.byref(version))
            self._ready.set()
            message = wintypes.MSG()
            while self._user32.GetMessageW(ctypes.byref(message), None, 0, 0) > 0:
                self._user32.TranslateMessage(ctypes.byref(message))
                self._user32.DispatchMessageW(ctypes.byref(message))
        except BaseException as error:
            self._error = error
            self._ready.set()
        finally:
            hwnd = self._hwnd
            if hwnd is not None:
                data = self._data(0)
                self._shell32.Shell_NotifyIconW(_NIM_DELETE, ctypes.byref(data))
            if self._icon and self._owns_icon:
                self._user32.DestroyIcon(self._icon)
            self._hwnd = None
            self._stopped.set()

    def _window_proc(self, hwnd, message, wparam, lparam):
        if message == _WM_TIMER and int(wparam) == _BALLOON_TIMER_ID:
            self._user32.KillTimer(hwnd, _BALLOON_TIMER_ID)
            self._clear_notification()
            return 0
        if message == _TRAY_CALLBACK:
            event = int(lparam) & 0xFFFF
            if event == _NIN_BALLOONSHOW:
                self._user32.SetTimer(
                    hwnd,
                    _BALLOON_TIMER_ID,
                    _BALLOON_DURATION_MS,
                    None,
                )
                return 0
            if event in {
                _NIN_BALLOONHIDE,
                _NIN_BALLOONTIMEOUT,
                _NIN_BALLOONUSERCLICK,
            }:
                self._user32.KillTimer(hwnd, _BALLOON_TIMER_ID)
            if event in {
                _WM_LBUTTONUP,
                _WM_LBUTTONDBLCLK,
                _NIN_SELECT,
                _NIN_KEYSELECT,
                _NIN_BALLOONUSERCLICK,
            }:
                try:
                    self.on_show()
                except BaseException:
                    pass
            elif event == _WM_RBUTTONUP:
                self._show_context_menu(hwnd)
            return 0
        if message == _WM_CLOSE:
            self._user32.DestroyWindow(hwnd)
            return 0
        if message == _WM_DESTROY:
            self._user32.PostQuitMessage(0)
            return 0
        return self._user32.DefWindowProcW(hwnd, message, wparam, lparam)

    def _clear_notification(self) -> None:
        """Withdraw the currently visible balloon without removing the tray icon."""
        if self._hwnd is None:
            return
        data = self._data(_NIF_INFO)
        data.szInfo = ""
        data.szInfoTitle = ""
        self._shell32.Shell_NotifyIconW(_NIM_MODIFY, ctypes.byref(data))

    def _show_context_menu(self, hwnd: int) -> None:
        menu = self._user32.CreatePopupMenu()
        if not menu:
            return
        try:
            self._user32.AppendMenuW(menu, _MF_STRING, _COMMAND_SHOW, "打开主窗口")
            self._user32.AppendMenuW(menu, _MF_SEPARATOR, 0, None)
            self._user32.AppendMenuW(menu, _MF_STRING, _COMMAND_EXIT, "真正退出")
            point = wintypes.POINT()
            if not self._user32.GetCursorPos(ctypes.byref(point)):
                return
            self._user32.SetForegroundWindow(hwnd)
            command = self._user32.TrackPopupMenuEx(
                menu,
                _TPM_RIGHTBUTTON | _TPM_RETURNCMD,
                point.x,
                point.y,
                hwnd,
                None,
            )
            self._user32.PostMessageW(hwnd, _WM_NULL, 0, 0)
            callback = {
                _COMMAND_SHOW: self.on_show,
                _COMMAND_EXIT: self.on_exit,
            }.get(int(command))
            if callback is not None:
                try:
                    callback()
                except BaseException:
                    pass
        finally:
            self._user32.DestroyMenu(menu)

    def _load_icon(self) -> tuple[int, bool]:
        large = wintypes.HICON()
        small = wintypes.HICON()
        self._shell32.ExtractIconExW(
            str(sys.executable),
            0,
            ctypes.byref(large),
            ctypes.byref(small),
            1,
        )
        selected = int(small.value or large.value or 0)
        if selected:
            if large.value and large.value != selected:
                self._user32.DestroyIcon(large)
            return selected, True
        icon = self._user32.LoadImageW(
            None,
            ctypes.c_void_p(_IDI_APPLICATION),
            _IMAGE_ICON,
            0,
            0,
            _LR_DEFAULTSIZE | _LR_SHARED,
        )
        if not icon:
            raise ctypes.WinError(ctypes.get_last_error())
        return int(icon), False

    def _configure_functions(self) -> None:
        self._kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
        self._kernel32.GetModuleHandleW.restype = wintypes.HMODULE
        self._user32.RegisterClassW.argtypes = [ctypes.POINTER(_WindowClassW)]
        self._user32.RegisterClassW.restype = wintypes.ATOM
        self._user32.CreateWindowExW.argtypes = [
            wintypes.DWORD,
            wintypes.LPCWSTR,
            wintypes.LPCWSTR,
            wintypes.DWORD,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            wintypes.HWND,
            wintypes.HMENU,
            wintypes.HINSTANCE,
            ctypes.c_void_p,
        ]
        self._user32.CreateWindowExW.restype = wintypes.HWND
        self._user32.DestroyWindow.argtypes = [wintypes.HWND]
        self._user32.SetTimer.argtypes = [
            wintypes.HWND,
            ctypes.c_size_t,
            wintypes.UINT,
            ctypes.c_void_p,
        ]
        self._user32.SetTimer.restype = ctypes.c_size_t
        self._user32.KillTimer.argtypes = [wintypes.HWND, ctypes.c_size_t]
        self._user32.KillTimer.restype = wintypes.BOOL
        self._user32.PostMessageW.argtypes = [
            wintypes.HWND,
            wintypes.UINT,
            wintypes.WPARAM,
            wintypes.LPARAM,
        ]
        self._user32.CreatePopupMenu.restype = wintypes.HMENU
        self._user32.AppendMenuW.argtypes = [
            wintypes.HMENU,
            wintypes.UINT,
            wintypes.WPARAM,
            wintypes.LPCWSTR,
        ]
        self._user32.AppendMenuW.restype = wintypes.BOOL
        self._user32.GetCursorPos.argtypes = [ctypes.POINTER(wintypes.POINT)]
        self._user32.GetCursorPos.restype = wintypes.BOOL
        self._user32.SetForegroundWindow.argtypes = [wintypes.HWND]
        self._user32.SetForegroundWindow.restype = wintypes.BOOL
        self._user32.TrackPopupMenuEx.argtypes = [
            wintypes.HMENU,
            wintypes.UINT,
            ctypes.c_int,
            ctypes.c_int,
            wintypes.HWND,
            ctypes.c_void_p,
        ]
        self._user32.TrackPopupMenuEx.restype = wintypes.UINT
        self._user32.DestroyMenu.argtypes = [wintypes.HMENU]
        self._user32.DestroyMenu.restype = wintypes.BOOL
        self._user32.GetMessageW.argtypes = [
            ctypes.POINTER(wintypes.MSG),
            wintypes.HWND,
            wintypes.UINT,
            wintypes.UINT,
        ]
        self._user32.DefWindowProcW.argtypes = [
            wintypes.HWND,
            wintypes.UINT,
            wintypes.WPARAM,
            wintypes.LPARAM,
        ]
        self._user32.DefWindowProcW.restype = ctypes.c_ssize_t
        self._user32.TranslateMessage.argtypes = [ctypes.POINTER(wintypes.MSG)]
        self._user32.DispatchMessageW.argtypes = [ctypes.POINTER(wintypes.MSG)]
        self._user32.DispatchMessageW.restype = ctypes.c_ssize_t
        self._user32.LoadImageW.restype = wintypes.HANDLE
        self._user32.DestroyIcon.argtypes = [wintypes.HICON]
        self._shell32.Shell_NotifyIconW.argtypes = [
            wintypes.DWORD,
            ctypes.POINTER(_NotifyIconDataW),
        ]
        self._shell32.Shell_NotifyIconW.restype = wintypes.BOOL
        self._shell32.ExtractIconExW.argtypes = [
            wintypes.LPCWSTR,
            ctypes.c_int,
            ctypes.POINTER(wintypes.HICON),
            ctypes.POINTER(wintypes.HICON),
            wintypes.UINT,
        ]
        self._shell32.ExtractIconExW.restype = wintypes.UINT


def create_tray_icon(
    *,
    tooltip: str,
    on_show: Callable[[], None],
    on_exit: Callable[[], None],
) -> WindowsTrayIcon | None:
    if os.name != "nt":
        return None
    return WindowsTrayIcon(tooltip=tooltip, on_show=on_show, on_exit=on_exit)
