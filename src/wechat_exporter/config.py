"""Best-effort, allow-listed local hints. Never store keys or decrypted data."""
from __future__ import annotations

import json
import math
import os
import tempfile
import threading
from pathlib import Path


def app_data_dir() -> Path:
    return Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "WeChatChatExporter"


_FIELDS = {
    "last_account_wxid": str, "last_db_path": str,
    "weixin_executable": str, "weixin_version": str,
    "dll_path": str, "dll_size": int, "dll_mtime_ns": int, "codec_rva": int,
    "last_update_check": float, "latest_version": str,
    "update_status": str, "dismissed_version": str,
}
_LOCK = threading.RLock()


def _valid(name: str, value: object) -> bool:
    expected = _FIELDS.get(name)
    if expected is str:
        return isinstance(value, str) and len(value) <= 4096
    if expected is int:
        return type(value) is int and 0 <= value < 2**63
    if expected is float:
        return type(value) in (int, float) and math.isfinite(value) and value >= 0
    return False


class LocalConfig:
    def __init__(self, path: Path | None = None):
        self.path = path if path is not None else app_data_dir() / "settings.json"
        self._values = self._read()

    def _read(self) -> dict:
        try:
            if self.path.stat().st_size > 64 * 1024:
                return {}
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            return {k: v for k, v in raw.items() if _valid(k, v)}
        except (OSError, ValueError, AttributeError):
            return {}

    def get(self, name: str, default=None):
        with _LOCK:
            return self._values.get(name, default)

    def set(self, **values) -> bool:
        if not all(_valid(k, v) for k, v in values.items()):
            raise ValueError("配置只接受已定义的非敏感字段")
        with _LOCK:
            self._values.update(values)
            temporary = None
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=self.path.parent,
                                                 prefix="settings-", suffix=".tmp", delete=False) as stream:
                    temporary = Path(stream.name)
                    json.dump(self._values, stream, ensure_ascii=False, indent=2)
                os.replace(temporary, self.path)
                return True
            except OSError:
                # Cache failures must not prevent offline connection/export.
                return False
            finally:
                if temporary is not None:
                    try:
                        temporary.unlink(missing_ok=True)
                    except OSError:
                        pass

    def reserve_auto_check(self, now: float, interval: int) -> bool:
        """An OS lock serializes attempts from multiple installed/portable instances."""
        with _LOCK:
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                with self.path.with_suffix(".lock").open("a+b") as lock:
                    lock.seek(0)
                    if not lock.read(1):
                        lock.write(b"0")
                        lock.flush()
                    lock.seek(0)
                    if os.name == "nt":
                        import msvcrt
                        msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
                    else:
                        import fcntl
                        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    try:
                        self._values.update(self._read())
                        last = self.get("last_update_check", 0)
                        if last and now - last < interval:
                            return False
                        return self.set(last_update_check=float(now))
                    finally:
                        if os.name == "nt":
                            lock.seek(0)
                            msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
                        else:
                            fcntl.flock(lock, fcntl.LOCK_UN)
            except OSError:
                return False
