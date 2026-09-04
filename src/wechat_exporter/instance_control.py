"""Windows single-instance and local-update coordination.

The named objects live only in the current Windows logon session. A second
normal launch asks the primary instance to show itself, while the local build
script uses a separate event to request a safe shutdown before installation.
"""
from __future__ import annotations

import ctypes
import os
import threading
from collections.abc import Callable
from ctypes import wintypes


INSTANCE_NAMESPACE = "WeChatChatExporter.v1"
UPDATE_EXIT_EVENT_NAME = rf"Local\{INSTANCE_NAMESPACE}.UpdateExit"

_ERROR_ALREADY_EXISTS = 183
_WAIT_OBJECT_0 = 0
_INFINITE = 0xFFFFFFFF
_EVENT_MODIFY_STATE = 0x0002


def _object_names(namespace: str) -> tuple[str, str, str]:
    prefix = rf"Local\{namespace}"
    return f"{prefix}.Mutex", f"{prefix}.Show", f"{prefix}.UpdateExit"


class NoopInstanceCoordinator:
    """Non-Windows fallback with the same lifecycle surface."""

    def start(
        self,
        *,
        on_show: Callable[[], None],
        on_update_exit: Callable[[], None],
    ) -> None:
        del on_show, on_update_exit

    def close(self) -> None:
        pass


class WindowsInstanceCoordinator:
    def __init__(
        self,
        *,
        kernel,
        mutex_handle: int,
        show_handle: int,
        update_exit_handle: int,
    ) -> None:
        self._kernel = kernel
        self._mutex_handle = mutex_handle
        self._show_handle = show_handle
        self._update_exit_handle = update_exit_handle
        self._stop_handle = int(kernel.CreateEventW(None, True, False, None) or 0)
        self._thread: threading.Thread | None = None
        self._closed = False
        if not self._stop_handle:
            self.close()
            raise ctypes.WinError(ctypes.get_last_error())

    @classmethod
    def claim(
        cls,
        namespace: str = INSTANCE_NAMESPACE,
    ) -> WindowsInstanceCoordinator | None:
        kernel = _configured_kernel32()
        mutex_name, show_name, update_exit_name = _object_names(namespace)
        show_handle = int(kernel.CreateEventW(None, False, False, show_name) or 0)
        update_exit_handle = int(
            kernel.CreateEventW(None, False, False, update_exit_name) or 0
        )
        if not show_handle or not update_exit_handle:
            if show_handle:
                kernel.CloseHandle(show_handle)
            if update_exit_handle:
                kernel.CloseHandle(update_exit_handle)
            raise ctypes.WinError(ctypes.get_last_error())

        ctypes.set_last_error(0)
        mutex_handle = int(kernel.CreateMutexW(None, False, mutex_name) or 0)
        error = ctypes.get_last_error()
        if not mutex_handle:
            kernel.CloseHandle(show_handle)
            kernel.CloseHandle(update_exit_handle)
            raise ctypes.WinError(error)
        if error == _ERROR_ALREADY_EXISTS:
            kernel.SetEvent(show_handle)
            kernel.CloseHandle(mutex_handle)
            kernel.CloseHandle(show_handle)
            kernel.CloseHandle(update_exit_handle)
            return None
        return cls(
            kernel=kernel,
            mutex_handle=mutex_handle,
            show_handle=show_handle,
            update_exit_handle=update_exit_handle,
        )

    def start(
        self,
        *,
        on_show: Callable[[], None],
        on_update_exit: Callable[[], None],
    ) -> None:
        if self._closed or self._thread is not None:
            return

        def wait_for_requests() -> None:
            handles = (wintypes.HANDLE * 3)(
                self._show_handle,
                self._update_exit_handle,
                self._stop_handle,
            )
            while True:
                result = int(
                    self._kernel.WaitForMultipleObjects(3, handles, False, _INFINITE)
                )
                if result == _WAIT_OBJECT_0:
                    try:
                        on_show()
                    except BaseException:
                        pass
                elif result == _WAIT_OBJECT_0 + 1:
                    try:
                        on_update_exit()
                    except BaseException:
                        pass
                else:
                    return

        self._thread = threading.Thread(
            target=wait_for_requests,
            name="wechat-exporter-instance-control",
            daemon=True,
        )
        self._thread.start()

    def close(self) -> None:
        if getattr(self, "_closed", False):
            return
        self._closed = True
        stop_handle = getattr(self, "_stop_handle", 0)
        if stop_handle:
            self._kernel.SetEvent(stop_handle)
        thread = getattr(self, "_thread", None)
        if thread is not None and threading.current_thread() is not thread:
            thread.join(timeout=3)
        for attribute in (
            "_stop_handle",
            "_update_exit_handle",
            "_show_handle",
            "_mutex_handle",
        ):
            handle = getattr(self, attribute, 0)
            if handle:
                self._kernel.CloseHandle(handle)
                setattr(self, attribute, 0)


def claim_primary_instance(
    namespace: str = INSTANCE_NAMESPACE,
) -> WindowsInstanceCoordinator | NoopInstanceCoordinator | None:
    """Return a coordinator for the primary process; signal and return None otherwise."""
    if os.name != "nt":
        return NoopInstanceCoordinator()
    return WindowsInstanceCoordinator.claim(namespace)


def signal_named_event(name: str) -> bool:
    """Signal an existing coordinator event without creating a false primary."""
    if os.name != "nt":
        return False
    kernel = _configured_kernel32()
    handle = int(kernel.OpenEventW(_EVENT_MODIFY_STATE, False, name) or 0)
    if not handle:
        return False
    try:
        return bool(kernel.SetEvent(handle))
    finally:
        kernel.CloseHandle(handle)


def _configured_kernel32():
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
    kernel.CreateMutexW.restype = wintypes.HANDLE
    kernel.CreateEventW.argtypes = [
        ctypes.c_void_p,
        wintypes.BOOL,
        wintypes.BOOL,
        wintypes.LPCWSTR,
    ]
    kernel.CreateEventW.restype = wintypes.HANDLE
    kernel.OpenEventW.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR]
    kernel.OpenEventW.restype = wintypes.HANDLE
    kernel.SetEvent.argtypes = [wintypes.HANDLE]
    kernel.SetEvent.restype = wintypes.BOOL
    kernel.WaitForMultipleObjects.argtypes = [
        wintypes.DWORD,
        ctypes.POINTER(wintypes.HANDLE),
        wintypes.BOOL,
        wintypes.DWORD,
    ]
    kernel.WaitForMultipleObjects.restype = wintypes.DWORD
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
    return kernel
