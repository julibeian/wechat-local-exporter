from __future__ import annotations

import os
import subprocess
import sys
import time
import ctypes
import ctypes.wintypes as wt


def _child() -> None:
    import tkinter as tk

    root = tk.Tk()
    root.title("WeChat exporter close-flow test")
    root.geometry("240x80-10000-10000")
    root.after(30_000, root.destroy)
    root.mainloop()


def _parent() -> None:
    if os.name != "nt":
        print("skipped: Windows only")
        return
    from wechat_exporter import windows
    from wechat_exporter.windows import ProcessInfo

    child = subprocess.Popen(
        [sys.executable, __file__, "--child"],
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    messages: list[str] = []
    try:
        time.sleep(1.5)
        matches: list[tuple[int, int, str]] = []
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        callback_type = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)

        @callback_type
        def inspect_window(hwnd: int, _lparam: int) -> bool:
            pid = wt.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            length = user32.GetWindowTextLengthW(hwnd)
            buffer = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(hwnd, buffer, len(buffer))
            if buffer.value == "WeChat exporter close-flow test":
                matches.append((int(pid.value), int(hwnd), buffer.value))
            return True

        user32.EnumWindows(inspect_window, 0)
        if not matches:
            raise RuntimeError(f"no top-level dummy window found; launcher pid {child.pid}")
        print(f"dummy windows: {matches}")
        window_pid = matches[0][0]

        def dummy_processes() -> list[ProcessInfo]:
            if child.poll() is not None:
                return []
            return [ProcessInfo(window_pid, "dummy-window.exe", 1)]

        windows.list_wechat_processes = dummy_processes
        windows.request_wechat_exit(
            graceful_seconds=5.0,
            timeout_seconds=8.0,
            progress=messages.append,
        )
        child.wait(timeout=2)
        if any("托盘" in message for message in messages):
            raise RuntimeError("dummy window required forced termination")
        print("WM_CLOSE integration check: passed")
    finally:
        if child.poll() is None:
            child.terminate()
            child.wait(timeout=3)


if __name__ == "__main__":
    if "--child" in sys.argv:
        _child()
    else:
        _parent()
