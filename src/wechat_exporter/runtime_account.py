"""Fast account confirmation using live file ownership and login identity."""
from __future__ import annotations

from collections.abc import Iterable

from .archive import _clean_account_wxid
from .media import _find_weixin_module, _read_cfg_candidates
from .models import AccountLocation
from .windows import ProcessInfo, file_resource_users, process_start_time


def confirm_runtime_account(account: AccountLocation, processes: Iterable[ProcessInfo]) -> int | None:
    """A saved directory is only a hint until both live checks succeed.

    Unknown layouts, permissions, multiple clients and moved data folders
    fall back to full discovery. No process addresses or keys are persisted.
    """
    processes = tuple(processes)
    if not processes:
        return None
    try:
        files = tuple(account.db_dir / folder / f"{folder}.db" for folder in ("contact", "session"))
        if not all(path.is_file() for path in files):
            return None
        owners = file_resource_users(files)
        candidates = {p.pid for p in processes} & {pid for pid, _ in owners}
        if not candidates:
            return None
        clients: list[tuple[int, str]] = []
        for process in processes:
            if _find_weixin_module(process.pid, strict=True) is None:
                continue  # A successfully enumerated helper has no account DLL.
            identities = {wxid for uin, wxid in _read_cfg_candidates(process.pid, full_uin=True)
                          if uin and wxid}
            if len(identities) != 1:
                return None
            clients.append((process.pid, identities.pop()))
        if len(clients) != 1:
            return None
        pid, wxid = clients[0]
        if pid not in candidates or wxid != _clean_account_wxid(account.wxid):
            return None
        if (pid, process_start_time(pid)) not in owners:
            return None
        return pid
    except (OSError, ValueError, RuntimeError):
        return None
