"""Connection policy, independent of Tk. Hints never establish account identity."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .config import LocalConfig
from .crypto import collect_required_databases
from .errors import UserFacingError
from .key_capture import capture_keys_during_wechat_start, prepare_key_capture
from .models import AccountLocation
from .service import ExporterService, RestartRequired
from .windows import (account_from_path, discover_db_paths_from_process,
                      find_weixin_executable, list_wechat_processes, request_wechat_exit)


@dataclass(frozen=True)
class RunningAccount:
    pid: int
    account: AccountLocation


def resolve_running_account(pid: int | None = None) -> RunningAccount | None:
    """Accept a unique valid DB path in the actual process, never mtime ranking.

    Multiple logins or stale paths in memory are ambiguous; require the user to
    finish login/close the extra account rather than pairing unrelated keys/DBs.
    """
    processes = list_wechat_processes()
    if pid is not None:
        processes = [p for p in processes if p.pid == pid]
    found: dict[Path, RunningAccount] = {}
    for process in processes:
        try:
            paths = discover_db_paths_from_process(process.pid)
        except (OSError, PermissionError):
            continue
        for path in paths:
            account = account_from_path(path, "微信进程")
            if all((account.db_dir / folder / f"{folder}.db").is_file()
                   for folder in ("contact", "session")):
                found.setdefault(account.db_dir.resolve(), RunningAccount(process.pid, account))
    return next(iter(found.values())) if len(found) == 1 else None


class ConnectionManager:
    def __init__(self, config: LocalConfig):
        self.config = config

    def executable(self) -> Path:
        # A running client/registry wins over a hint left by an older install.
        try:
            executable = find_weixin_executable()
        except FileNotFoundError:
            cached = Path(self.config.get("weixin_executable", "."))
            if cached.name.lower() != "weixin.exe" or not cached.is_file():
                raise
            executable = cached
        self.config.set(weixin_executable=str(executable.resolve()))
        return executable

    def connect(self, *, allow_restart: bool = False, progress=None) -> ExporterService:
        service = None
        try:
            if not allow_restart:
                if progress:
                    progress("正在确认当前微信进程使用的账号...", 0.05)
                if not list_wechat_processes():
                    raise RestartRequired("微信尚未运行，需要启动微信后连接。")
                running = resolve_running_account()
                if running is None:
                    raise UserFacingError("尚未确认当前登录账号。请完成微信登录；若同时登录了多个账号，请只保留需要导出的账号后重试。")
                service = ExporterService(running.account, process_id=running.pid)
                service.connect_without_restart(progress=progress)
            else:
                executable = self.executable()
                # Verify the hook and its cache before disturbing WeChat.
                preparation = prepare_key_capture(executable, config=self.config)
                if list_wechat_processes():
                    request_wechat_exit(progress=lambda text: progress(text, 0.1) if progress else None)
                resolved: list[RunningAccount] = []

                def targets_after_login(pid: int):
                    current = resolve_running_account(pid)
                    if current is None:
                        return []
                    resolved[:] = [current]
                    return collect_required_databases(current.account.db_dir)

                keys = capture_keys_during_wechat_start(
                    executable, [], preparation=preparation, target_resolver=targets_after_login,
                    progress=lambda text: progress(text, 0.25) if progress else None,
                )
                if not resolved:
                    raise UserFacingError("尚未确认当前登录账号，请完成登录后重新连接。")
                running = resolved[0]
                self._verify_current(running)
                # Only now create the account-bound service. Never reuse A after login B.
                service = ExporterService(running.account, process_id=running.pid)
                service._prepare(keys, progress=progress, calibrations=None)
            self._verify_current(running)
            self.config.set(last_account_wxid=running.account.wxid,
                            last_db_path=str(running.account.db_dir.resolve()))
            return service
        except BaseException:
            if service is not None:
                service.close()
            raise

    @staticmethod
    def _verify_current(running: RunningAccount) -> None:
        if resolve_running_account(running.pid) != running:
            raise UserFacingError("连接期间微信账号发生变化或已退出，请重新连接。")
