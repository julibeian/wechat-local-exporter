"""Independent Windows update worker. The running executable never replaces itself."""
from __future__ import annotations

import ctypes
import hashlib
import hmac
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from .errors import UserFacingError
from .update import DownloadedUpdate, normalize_version


STABLE_EXE = "WeChat-TXT-PDF-Exporter.exe"


def installation_kind(executable: Path | None = None) -> str:
    if executable is None:
        if not getattr(sys, "frozen", False):
            return "source"
        executable = Path(sys.executable)
    return "installer" if executable.name == STABLE_EXE and (executable.parent / "unins000.exe").is_file() else "portable"


def verified(path: Path, expected: str) -> bool:
    if len(expected) != 64:
        return False
    with path.open("rb") as stream:
        actual = hashlib.file_digest(stream, "sha256").hexdigest()
    return hmac.compare_digest(actual, expected.lower())


def _launch(executable: Path, *args: str):
    environment = os.environ.copy()
    # New onefile processes must unpack independently of the exiting parent.
    environment["PYINSTALLER_RESET_ENVIRONMENT"] = "1"
    # An external installer must not inherit PyInstaller's DLL search directory.
    if os.name == "nt":
        ctypes.windll.kernel32.SetDllDirectoryW(None)
    try:
        return subprocess.Popen([str(executable), *args], cwd=executable.parent,
                                env=environment, close_fds=True,
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    finally:
        if os.name == "nt" and getattr(sys, "frozen", False):
            ctypes.windll.kernel32.SetDllDirectoryW(str(sys._MEIPASS))


@dataclass(frozen=True)
class UpdatePlan:
    payload: str
    sha256: str
    target: str
    kind: str
    version: str
    parent_pid: int


def stage_update(download: DownloadedUpdate) -> Path:
    if os.name != "nt" or not getattr(sys, "frozen", False):
        raise UserFacingError("源码运行模式不替换 Python；请使用 Windows 安装版或便携版更新。")
    current = Path(sys.executable).resolve()
    if download.kind != installation_kind(current) or not verified(download.path, download.sha256):
        raise UserFacingError("更新文件未通过安装前检查，当前版本保持不变。")
    # Every retry gets its own ready/abort files; a slow previous helper must
    # never mistake a later attempt's consent for its own.
    directory = Path(tempfile.mkdtemp(prefix="install-", dir=download.path.parent))
    payload = directory / download.path.name
    shutil.copy2(download.path, payload)
    helper = directory / "update-runner.exe"
    plan_path = directory / "plan.json"
    plan = UpdatePlan(str(payload.resolve()), download.sha256, str(current),
                      download.kind, download.version, os.getpid())
    shutil.copy2(current, helper)
    plan_path.write_text(json.dumps(asdict(plan)), encoding="utf-8")
    _launch(helper, "--apply-update", str(plan_path))
    return plan_path


def apply_update(plan: UpdatePlan, *, launch=_launch) -> None:
    """Called only after the old process exits. Kept separate for failure tests."""
    payload, target = Path(plan.payload), Path(plan.target)
    if (plan.kind not in {"installer", "portable"} or not verified(payload, plan.sha256)
            or target.suffix.lower() != ".exe" or not target.is_file()
            or payload.resolve() == target.resolve()):
        raise UserFacingError("安装前校验失败，原程序未被替换。")
    backup = payload.parent / "previous.exe"
    shutil.copy2(target, backup)
    changed = False
    try:
        if plan.kind == "installer":
            if installation_kind(target) != "installer":
                raise UserFacingError("未能确认安装目录，已停止自动更新。")
            process = launch(payload, "/VERYSILENT", "/SUPPRESSMSGBOXES", "/SP-", "/NORESTART",
                             "/NORESTARTAPPLICATIONS", "/NOCLOSEAPPLICATIONS",
                             "/RESTARTEXITCODE=3010", f"/DIR={target.parent}")
            changed = True
            if process.wait() != 0:
                raise UserFacingError("安装程序未成功完成，已尝试恢复原程序。")
        else:
            # Copy beside target first: atomic replacement also works across volumes.
            staging = target.with_name(target.name + ".update-new")
            if staging.exists():
                raise UserFacingError("发现上次未完成的更新文件，请检查后重试。")
            try:
                shutil.copy2(payload, staging)
                if not verified(staging, plan.sha256):
                    raise UserFacingError("替换前校验失败，原程序保持不变。")
                os.replace(staging, target)
                changed = True
            finally:
                staging.unlink(missing_ok=True)
        launch(target)
    except Exception:
        if changed:
            shutil.copy2(backup, target)
        try:
            launch(target)
        except Exception:
            pass
        raise UserFacingError("更新未完成，已尝试恢复并启动原版本；导出文件不受影响。") from None
    # Keep the rollback copy until the next app startup cleans this update folder.


def _wait_for_parent(plan: UpdatePlan, directory: Path) -> bool:
    from ctypes import wintypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel.WaitForSingleObject.restype = wintypes.DWORD
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel.OpenProcess(0x00100000, False, plan.parent_pid)  # SYNCHRONIZE
    if not handle:
        return False  # No ready signal: the main app must stay open.
    try:
        if (directory / "abort").exists():
            return False
        (directory / "ready").touch()
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            if (directory / "abort").exists():
                return False
            if kernel.WaitForSingleObject(handle, 250) == 0:
                return True
        return False
    finally:
        kernel.CloseHandle(handle)


def _wait_for_file_release(target: Path) -> None:
    # PyInstaller's outer bootloader can briefly outlive the Python process.
    from ctypes import wintypes
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                  ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    kernel.CreateFileW.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    for _ in range(100):
        handle = kernel.CreateFileW(str(target), 0x80000000, 0, None, 3, 0, None)
        if handle not in (None, ctypes.c_void_p(-1).value):
            kernel.CloseHandle(handle)
            return
        time.sleep(0.2)
    raise UserFacingError("程序文件仍被占用，未执行更新。请关闭其他导出工具窗口后重试。")


def run_helper(plan_path: Path) -> int:
    directory = plan_path.resolve().parent
    try:
        if os.name != "nt" or plan_path.stat().st_size > 16 * 1024:
            return 2
        plan = UpdatePlan(**json.loads(plan_path.read_text(encoding="utf-8")))
        normalize_version(plan.version)
        if (plan.kind not in {"installer", "portable"} or type(plan.parent_pid) is not int
                or plan.parent_pid <= 0 or Path(plan.payload).resolve().parent != directory
                or Path(plan.target).resolve().parent == directory
                or not verified(Path(plan.payload), plan.sha256)):
            return 2
        if not _wait_for_parent(plan, directory):
            return 3
        _wait_for_file_release(Path(plan.target))
        apply_update(plan)
        (directory / "success").touch()
        return 0
    except Exception:
        # Never propagate an implementation stack or arbitrary exception string.
        if os.name == "nt":
            ctypes.windll.user32.MessageBoxW(None,
                "更新未完成。请重新打开导出工具；原程序备份保留在更新目录的 previous.exe。导出文件不受影响。",
                "版本更新", 0x10)
        return 1
    finally:
        try:
            (directory / "finished").touch()
        except OSError:
            pass


def cleanup_finished_updates(root: Path) -> None:
    """Delete only our completed transaction directories, never arbitrary paths."""
    root = root.resolve()
    if not root.is_dir():
        return
    for directory in root.glob("update-*"):
        if directory.is_symlink() or directory.resolve().parent != root:
            continue
        # Keep failed updates/backups for recovery; only explicit successful cleanup later.
        if (directory / "success").is_file() or any(directory.glob("install-*/success")):
            shutil.rmtree(directory, ignore_errors=True)
