from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import os
import re
import struct
import subprocess
import time
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path

from .models import AccountLocation


if os.name == "nt":
    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
else:  # pragma: no cover - imported for documentation/tests on other systems
    _kernel32 = None


PROCESS_VM_READ = 0x0010
PROCESS_QUERY_INFORMATION = 0x0400
PROCESS_TERMINATE = 0x0001
MEM_COMMIT = 0x1000
PAGE_GUARD = 0x100
PAGE_NOACCESS = 0x01
READABLE_PAGE_TYPES = {0x02, 0x04, 0x08, 0x10, 0x20, 0x40, 0x80}
MAX_USER_ADDRESS = 0x7FFF_FFFF_FFFF


class MemoryBasicInformation(ctypes.Structure):
    _fields_ = [
        ("BaseAddress", ctypes.c_void_p),
        ("AllocationBase", ctypes.c_void_p),
        ("AllocationProtect", wt.DWORD),
        ("PartitionId", wt.WORD),
        ("RegionSize", ctypes.c_size_t),
        ("State", wt.DWORD),
        ("Protect", wt.DWORD),
        ("Type", wt.DWORD),
    ]


@dataclass(frozen=True, slots=True)
class ProcessInfo:
    pid: int
    image_name: str
    memory_kb: int


def ensure_windows() -> None:
    if os.name != "nt":
        raise OSError("直接读取微信仅支持 Windows")


def list_wechat_processes() -> list[ProcessInfo]:
    """Return Weixin.exe processes, largest working set first."""
    ensure_windows()
    completed = subprocess.run(
        ["tasklist", "/FI", "IMAGENAME eq Weixin.exe", "/FO", "CSV", "/NH"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    processes: list[ProcessInfo] = []
    for raw_line in completed.stdout.splitlines():
        line = raw_line.strip()
        if not line or line.upper().startswith("INFO:"):
            continue
        fields = [part.strip('"') for part in re.findall(r'"([^"]*)"', line)]
        if len(fields) < 5:
            continue
        try:
            pid = int(fields[1])
            memory_kb = int(re.sub(r"\D", "", fields[4]) or "0")
        except ValueError:
            continue
        processes.append(ProcessInfo(pid=pid, image_name=fields[0], memory_kb=memory_kb))
    return sorted(processes, key=lambda item: item.memory_kb, reverse=True)


def request_wechat_exit(
    *,
    graceful_seconds: float = 6.0,
    timeout_seconds: float = 20.0,
    progress: Callable[[str], None] | None = None,
) -> None:
    """Ask every top-level WeChat window to close and wait for its processes.

    This first uses WM_CLOSE. If WeChat only minimizes to its tray, remaining
    Weixin.exe processes are ended after the grace period. The caller must
    obtain explicit user confirmation before invoking it.
    """
    ensure_windows()
    processes = list_wechat_processes()
    if not processes:
        return
    if progress:
        progress("正在平稳退出当前微信...")

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    enum_callback = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)
    user32.EnumWindows.argtypes = [enum_callback, wt.LPARAM]
    user32.EnumWindows.restype = wt.BOOL
    user32.GetWindowThreadProcessId.argtypes = [wt.HWND, ctypes.POINTER(wt.DWORD)]
    user32.GetWindowThreadProcessId.restype = wt.DWORD
    user32.PostMessageW.argtypes = [wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM]
    user32.PostMessageW.restype = wt.BOOL
    pids = {process.pid for process in processes}
    wm_close = 0x0010

    @enum_callback
    def close_window(hwnd: int, _lparam: int) -> bool:
        pid = wt.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if int(pid.value) in pids:
            user32.PostMessageW(hwnd, wm_close, 0, 0)
        return True

    if not user32.EnumWindows(close_window, 0):
        raise ctypes.WinError(ctypes.get_last_error())

    started = time.monotonic()
    graceful_deadline = started + min(graceful_seconds, timeout_seconds)
    while time.monotonic() < graceful_deadline:
        if not list_wechat_processes():
            if progress:
                progress("当前微信已退出，准备重新启动...")
            return
        time.sleep(0.25)

    remaining = list_wechat_processes()
    if remaining:
        if progress:
            progress("微信仍在托盘运行，正在结束剩余微信进程...")
        assert _kernel32 is not None
        _kernel32.OpenProcess.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
        _kernel32.OpenProcess.restype = wt.HANDLE
        _kernel32.TerminateProcess.argtypes = [wt.HANDLE, wt.UINT]
        _kernel32.TerminateProcess.restype = wt.BOOL
        _kernel32.CloseHandle.argtypes = [wt.HANDLE]
        _kernel32.CloseHandle.restype = wt.BOOL
        for process in remaining:
            handle = _kernel32.OpenProcess(PROCESS_TERMINATE, False, process.pid)
            if not handle:
                continue
            try:
                _kernel32.TerminateProcess(handle, 0)
            finally:
                _kernel32.CloseHandle(handle)

    deadline = started + timeout_seconds
    while time.monotonic() < deadline:
        if not list_wechat_processes():
            if progress:
                progress("当前微信已退出，准备重新启动...")
            return
        time.sleep(0.25)
    raise RuntimeError(
        "微信未能自动退出。请从微信菜单选择“退出”后再次点击连接。"
    )


def bring_wechat_to_front(
    pid: int,
    *,
    timeout_seconds: float = 20.0,
    progress: Callable[[str], None] | None = None,
) -> bool:
    """Show the first visible titled top-level window created by WeChat."""
    ensure_windows()
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    enum_callback = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)
    user32.EnumWindows.argtypes = [enum_callback, wt.LPARAM]
    user32.EnumWindows.restype = wt.BOOL
    user32.GetWindowThreadProcessId.argtypes = [wt.HWND, ctypes.POINTER(wt.DWORD)]
    user32.GetWindowThreadProcessId.restype = wt.DWORD
    user32.IsWindowVisible.argtypes = [wt.HWND]
    user32.IsWindowVisible.restype = wt.BOOL
    user32.GetWindowTextLengthW.argtypes = [wt.HWND]
    user32.GetWindowTextLengthW.restype = ctypes.c_int
    user32.ShowWindow.argtypes = [wt.HWND, ctypes.c_int]
    user32.ShowWindow.restype = wt.BOOL
    user32.BringWindowToTop.argtypes = [wt.HWND]
    user32.BringWindowToTop.restype = wt.BOOL
    user32.SetForegroundWindow.argtypes = [wt.HWND]
    user32.SetForegroundWindow.restype = wt.BOOL
    try:
        user32.AllowSetForegroundWindow.argtypes = [wt.DWORD]
        user32.AllowSetForegroundWindow.restype = wt.BOOL
        user32.AllowSetForegroundWindow(pid)
    except AttributeError:
        pass

    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        windows: list[int] = []

        @enum_callback
        def collect_window(hwnd: int, _lparam: int) -> bool:
            window_pid = wt.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(window_pid))
            if (
                int(window_pid.value) == pid
                and user32.IsWindowVisible(hwnd)
                and user32.GetWindowTextLengthW(hwnd) > 0
            ):
                windows.append(hwnd)
            return True

        user32.EnumWindows(collect_window, 0)
        if windows:
            hwnd = windows[0]
            user32.ShowWindow(hwnd, 9)  # SW_RESTORE
            user32.BringWindowToTop(hwnd)
            user32.SetForegroundWindow(hwnd)
            if progress:
                progress("微信窗口已显示，请在微信中确认登录")
            return True
        time.sleep(0.1)
    return False


class ProcessMemory:
    """Small, read-only wrapper around a process handle."""

    def __init__(self, pid: int):
        ensure_windows()
        assert _kernel32 is not None
        _kernel32.OpenProcess.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
        _kernel32.OpenProcess.restype = wt.HANDLE
        self.pid = pid
        self.handle = _kernel32.OpenProcess(
            PROCESS_VM_READ | PROCESS_QUERY_INFORMATION, False, pid
        )
        if not self.handle:
            error = ctypes.get_last_error()
            if error == 5:
                raise PermissionError(
                    "无法只读打开微信进程。请退出工具后，以管理员身份重新运行。"
                )
            raise OSError(error, f"无法打开微信进程 PID={pid}")

    def close(self) -> None:
        if self.handle:
            assert _kernel32 is not None
            _kernel32.CloseHandle(self.handle)
            self.handle = None

    def __enter__(self) -> ProcessMemory:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def regions(self, max_region_size: int = 512 * 1024 * 1024) -> Iterator[tuple[int, int]]:
        assert _kernel32 is not None
        _kernel32.VirtualQueryEx.argtypes = [
            wt.HANDLE,
            ctypes.c_void_p,
            ctypes.POINTER(MemoryBasicInformation),
            ctypes.c_size_t,
        ]
        _kernel32.VirtualQueryEx.restype = ctypes.c_size_t
        address = 0
        while address < MAX_USER_ADDRESS:
            info = MemoryBasicInformation()
            result = _kernel32.VirtualQueryEx(
                self.handle,
                ctypes.c_void_p(address),
                ctypes.byref(info),
                ctypes.sizeof(info),
            )
            if result == 0:
                break
            base = int(info.BaseAddress or 0)
            size = int(info.RegionSize)
            protect = int(info.Protect)
            page_type = protect & 0xFF
            if (
                info.State == MEM_COMMIT
                and page_type in READABLE_PAGE_TYPES
                and not protect & PAGE_GUARD
                and page_type != PAGE_NOACCESS
                and 0 < size <= max_region_size
            ):
                yield base, size
            next_address = base + size
            if next_address <= address:
                break
            address = next_address

    def read(self, address: int, size: int) -> bytes:
        assert _kernel32 is not None
        _kernel32.ReadProcessMemory.argtypes = [
            wt.HANDLE,
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_size_t,
            ctypes.POINTER(ctypes.c_size_t),
        ]
        _kernel32.ReadProcessMemory.restype = wt.BOOL
        buffer = ctypes.create_string_buffer(size)
        read_size = ctypes.c_size_t()
        ok = _kernel32.ReadProcessMemory(
            self.handle,
            ctypes.c_void_p(address),
            buffer,
            size,
            ctypes.byref(read_size),
        )
        if not ok or read_size.value == 0:
            return b""
        return buffer.raw[: read_size.value]

    def chunks(
        self,
        *,
        chunk_size: int = 8 * 1024 * 1024,
        overlap: int = 4096,
        progress: Callable[[int, int], None] | None = None,
    ) -> Iterator[tuple[int, bytes]]:
        regions = list(self.regions())
        total = sum(size for _, size in regions)
        processed = 0
        for base, size in regions:
            offset = 0
            previous = b""
            while offset < size:
                request_size = min(chunk_size, size - offset)
                current = self.read(base + offset, request_size)
                if current:
                    combined = previous + current
                    yield base + offset - len(previous), combined
                    previous = combined[-overlap:]
                else:
                    previous = b""
                offset += request_size
                processed += request_size
                if progress:
                    progress(min(processed, total), total)


_ASCII_PATH_RE = re.compile(
    rb"([A-Za-z]:\\[^\x00\r\n]{1,1024}?\\db_storage)(?:\\|\x00)", re.IGNORECASE
)
_WIDE_MARKER = "db_storage".encode("utf-16le")


def _paths_from_memory_chunk(chunk: bytes) -> set[Path]:
    found: set[Path] = set()
    for match in _ASCII_PATH_RE.finditer(chunk):
        try:
            value = match.group(1).decode("utf-8", errors="ignore")
            found.add(Path(value))
        except (OSError, ValueError):
            continue

    start = 0
    while True:
        marker_index = chunk.find(_WIDE_MARKER, start)
        if marker_index < 0:
            break
        window_start = max(0, marker_index - 4096)
        window_end = min(len(chunk), marker_index + len(_WIDE_MARKER) + 32)
        for aligned_start in (window_start, window_start + 1):
            raw = chunk[aligned_start:window_end]
            if len(raw) % 2:
                raw = raw[:-1]
            decoded = raw.decode("utf-16le", errors="ignore")
            for match in re.finditer(
                r"([A-Za-z]:\\[^\x00\r\n]{1,1024}?\\db_storage)", decoded, re.IGNORECASE
            ):
                try:
                    found.add(Path(match.group(1)))
                except (OSError, ValueError):
                    pass
        start = marker_index + len(_WIDE_MARKER)
    return found


def discover_db_paths_from_process(
    pid: int,
    progress: Callable[[int, int], None] | None = None,
) -> list[Path]:
    candidates: set[Path] = set()
    with ProcessMemory(pid) as memory:
        for _, chunk in memory.chunks(progress=progress):
            for candidate in _paths_from_memory_chunk(chunk):
                if candidate.name.lower() == "db_storage" and candidate.is_dir():
                    candidates.add(candidate.resolve())
    return sorted(candidates, key=lambda path: str(path).lower())


def _account_from_path(path: Path, source: str) -> AccountLocation | None:
    resolved = path.expanduser().resolve()
    if resolved.name.lower() == "db_storage" and resolved.is_dir():
        account = resolved.parent
    elif (resolved / "db_storage").is_dir():
        account = resolved
    else:
        return None
    return AccountLocation(account_dir=account, wxid=account.name, source=source)


def account_from_path(path: Path, source: str = "用户选择") -> AccountLocation:
    account = _account_from_path(path, source)
    if account is None:
        raise ValueError("所选目录不是微信 4.x 账号目录（缺少 db_storage）")
    return account


def discover_accounts(
    *,
    include_process_memory: bool = True,
    progress: Callable[[str], None] | None = None,
) -> list[AccountLocation]:
    ensure_windows()
    home = Path.home()
    roots: list[tuple[Path, str]] = [
        (home / "Documents" / "xwechat_files", "常用目录"),
        (home / "xwechat_files", "常用目录"),
        (home / "Desktop" / "xwechat_files", "常用目录"),
    ]
    try:
        executable = find_weixin_executable()
    except (FileNotFoundError, OSError):
        pass
    else:
        roots.extend(
            (
                (executable.parent / "xwechat_files", "微信安装位置"),
                (executable.parent.parent / "xwechat_files", "微信安装位置"),
            )
        )
    found: dict[str, AccountLocation] = {}

    def add(account: AccountLocation | None) -> None:
        if account is None:
            return
        found[str(account.account_dir).lower()] = account

    scanned_roots: set[str] = set()
    for root, source in roots:
        root_key = str(root).lower()
        if root_key in scanned_roots:
            continue
        scanned_roots.add(root_key)
        if not root.is_dir():
            continue
        direct = _account_from_path(root, source)
        add(direct)
        for child in root.iterdir():
            if child.is_dir():
                add(_account_from_path(child, source))

    if found and progress:
        progress("已自动识别微信账号，无需选择目录")

    if include_process_memory and not found:
        processes = list_wechat_processes()
        if processes:
            if progress:
                progress("正在从微信进程中只读识别数据目录...")
            for db_path in discover_db_paths_from_process(processes[0].pid):
                add(_account_from_path(db_path, "微信进程"))

    return sorted(found.values(), key=lambda item: str(item.account_dir).lower())


def select_current_account(
    accounts: Iterable[AccountLocation],
) -> AccountLocation | None:
    values = list(accounts)
    if not values:
        return None

    def rank(account: AccountLocation) -> tuple[int, int, str]:
        newest = 0
        for candidate in (
            account.db_dir / "session" / "session.db",
            account.db_dir / "contact" / "contact.db",
        ):
            try:
                newest = max(newest, candidate.stat().st_mtime_ns)
            except OSError:
                pass
        from_running_process = 1 if account.source == "微信进程" else 0
        return from_running_process, newest, str(account.account_dir).lower()

    return max(values, key=rank)


def read_wechat_version() -> str:
    """Best-effort version lookup from the main process executable."""
    ensure_windows()
    script = (
        "$p=Get-Process Weixin -ErrorAction SilentlyContinue | "
        "Where-Object {$_.Path} | Select-Object -First 1 -ExpandProperty Path; "
        "if($p){(Get-Item -LiteralPath $p).VersionInfo.FileVersion}"
    )
    completed = subprocess.run(
        ["powershell", "-NoProfile", "-Command", script],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    return completed.stdout.strip()


def find_weixin_executable() -> Path:
    """Locate Weixin.exe from a running process or the current-user registry."""
    ensure_windows()
    script = (
        "$p=Get-Process Weixin -ErrorAction SilentlyContinue | "
        "Where-Object {$_.Path} | Select-Object -First 1 -ExpandProperty Path; "
        "if($p){$p; exit}; "
        "$r=(Get-ItemProperty -Path 'HKCU:\\Software\\Tencent\\Weixin' "
        "-Name InstallPath -ErrorAction SilentlyContinue).InstallPath; "
        "if($r){Join-Path $r 'Weixin.exe'}"
    )
    completed = subprocess.run(
        ["powershell", "-NoProfile", "-Command", script],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    path = Path(completed.stdout.strip())
    if not path.is_file():
        raise FileNotFoundError("没有找到 Weixin.exe，请重新安装或启动一次微信。")
    return path
