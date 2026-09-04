"""Per-account persistent database cache for fast subsequent starts on Windows."""
from __future__ import annotations

import ctypes
import hashlib
import json
import os
import tempfile
from collections.abc import Callable, Iterable
from ctypes import wintypes
from pathlib import Path

from .config import app_data_dir
from .crypto import (
    DatabaseKeys,
    DatabaseTarget,
    apply_wal,
    copy_consistent_database_snapshot,
    database_source_signature,
    decrypt_database,
    validate_sqlite,
    verify_key,
)
from .models import AccountLocation


_CACHE_VERSION = 1
_CACHE_FOLDER = "database-cache"
_KEY_ENTROPY_PREFIX = b"wechat-exporter-database-cache-v1\0"
_CRYPTPROTECT_UI_FORBIDDEN = 0x1


class _DataBlob(ctypes.Structure):
    _fields_ = [
        ("cbData", wintypes.DWORD),
        ("pbData", ctypes.POINTER(ctypes.c_ubyte)),
    ]


def _blob(data: bytes) -> tuple[_DataBlob, ctypes.Array]:
    buffer = ctypes.create_string_buffer(data or b"\0")
    value = _DataBlob(
        len(data),
        ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)),
    )
    return value, buffer


def protect_for_current_user(data: bytes, *, entropy: bytes) -> bytes:
    """Protect bytes with Windows DPAPI for the current Windows user."""

    if os.name != "nt":
        raise OSError("持久密钥缓存仅支持 Windows DPAPI。")
    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    crypt32.CryptProtectData.argtypes = [
        ctypes.POINTER(_DataBlob),
        wintypes.LPCWSTR,
        ctypes.POINTER(_DataBlob),
        ctypes.c_void_p,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(_DataBlob),
    ]
    crypt32.CryptProtectData.restype = wintypes.BOOL
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p
    source, source_buffer = _blob(data)
    entropy_blob, entropy_buffer = _blob(entropy)
    destination = _DataBlob()
    _ = source_buffer, entropy_buffer
    if not crypt32.CryptProtectData(
        ctypes.byref(source),
        "微信聊天本地导出工具账号缓存",
        ctypes.byref(entropy_blob),
        None,
        None,
        _CRYPTPROTECT_UI_FORBIDDEN,
        ctypes.byref(destination),
    ):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        return ctypes.string_at(destination.pbData, destination.cbData)
    finally:
        kernel32.LocalFree(destination.pbData)


def unprotect_for_current_user(data: bytes, *, entropy: bytes) -> bytes:
    """Unprotect current-user DPAPI bytes without displaying Windows UI."""

    if os.name != "nt":
        raise OSError("持久密钥缓存仅支持 Windows DPAPI。")
    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    crypt32.CryptUnprotectData.argtypes = [
        ctypes.POINTER(_DataBlob),
        ctypes.c_void_p,
        ctypes.POINTER(_DataBlob),
        ctypes.c_void_p,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(_DataBlob),
    ]
    crypt32.CryptUnprotectData.restype = wintypes.BOOL
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p
    source, source_buffer = _blob(data)
    entropy_blob, entropy_buffer = _blob(entropy)
    destination = _DataBlob()
    _ = source_buffer, entropy_buffer
    if not crypt32.CryptUnprotectData(
        ctypes.byref(source),
        None,
        ctypes.byref(entropy_blob),
        None,
        None,
        _CRYPTPROTECT_UI_FORBIDDEN,
        ctypes.byref(destination),
    ):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        return ctypes.string_at(destination.pbData, destination.cbData)
    finally:
        kernel32.LocalFree(destination.pbData)


class AccountDatabaseCache:
    """Securely retain validated keys and reusable read-only DB snapshots."""

    def __init__(
        self,
        account: AccountLocation,
        *,
        root: Path | None = None,
        protect: Callable[..., bytes] = protect_for_current_user,
        unprotect: Callable[..., bytes] = unprotect_for_current_user,
    ):
        self.account = account
        self.identity = self._identity(account)
        digest = hashlib.sha256(self.identity.encode("utf-8")).hexdigest()[:32]
        self.root = (root or app_data_dir() / _CACHE_FOLDER) / digest
        self.keys_path = self.root / "keys.dpapi"
        self.workspace_root = self.root / "workspace"
        self._protect = protect
        self._unprotect = unprotect
        self._entropy = _KEY_ENTROPY_PREFIX + digest.encode("ascii")

    @staticmethod
    def _identity(account: AccountLocation) -> str:
        return f"{account.wxid.casefold()}\n{str(account.db_dir.resolve()).casefold()}"

    def load_keys(self, targets: Iterable[DatabaseTarget]) -> DatabaseKeys | None:
        target_list = list(targets)
        if not target_list:
            return None
        try:
            if self.keys_path.stat().st_size > 1024 * 1024:
                return None
            decrypted = self._unprotect(
                self.keys_path.read_bytes(),
                entropy=self._entropy,
            )
            payload = json.loads(decrypted.decode("utf-8"))
            if (
                payload.get("version") != _CACHE_VERSION
                or payload.get("identity") != self.identity
                or not isinstance(payload.get("keys"), dict)
            ):
                return None
            values: dict[str, bytes] = {}
            entries = payload["keys"]
            for target in target_list:
                entry = entries.get(target.relative_path)
                if not isinstance(entry, dict) or entry.get("salt") != target.salt.hex():
                    return None
                key = bytes.fromhex(str(entry.get("key", "")))
                if not verify_key(key, target.first_page):
                    return None
                values[target.relative_path] = key
            return DatabaseKeys(values)
        except (OSError, ValueError, TypeError, UnicodeError, AttributeError):
            return None

    def save_keys(
        self,
        keys: DatabaseKeys,
        targets: Iterable[DatabaseTarget],
    ) -> None:
        entries: dict[str, dict[str, str]] = {}
        for target in targets:
            if target.relative_path not in keys:
                continue
            entries[target.relative_path] = {
                "salt": target.salt.hex(),
                "key": keys[target.relative_path].hex(),
            }
        if not entries:
            return
        payload = json.dumps(
            {
                "version": _CACHE_VERSION,
                "identity": self.identity,
                "keys": entries,
            },
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        protected = self._protect(payload, entropy=self._entropy)
        self.root.mkdir(parents=True, exist_ok=True)
        temporary: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                dir=self.root,
                prefix="keys-",
                suffix=".tmp",
                delete=False,
            ) as stream:
                temporary = Path(stream.name)
                stream.write(protected)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.keys_path)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    def workspace(self, keys: DatabaseKeys) -> PersistentDecryptedWorkspace:
        return PersistentDecryptedWorkspace(
            self.account.db_dir,
            keys,
            self.workspace_root,
            identity=self.identity,
        )


class PersistentDecryptedWorkspace:
    """Reusable per-account SQLite snapshots with incremental refresh."""

    def __init__(
        self,
        db_dir: Path,
        keys: DatabaseKeys,
        root: Path,
        *,
        identity: str,
    ):
        self.db_dir = db_dir.resolve()
        self.keys = keys
        self.root = root
        self.decrypted_dir = root / "decrypted"
        self.manifest_path = root / "manifest.json"
        self.identity = identity
        self.reused_count = 0
        self.refreshed_count = 0

    def close(self) -> None:
        self.keys = DatabaseKeys({})

    def decrypted_path(self, relative_path: str | Path) -> Path:
        normalized = Path(str(relative_path).replace("\\", os.sep).lstrip("/\\"))
        destination = (self.decrypted_dir / normalized).resolve()
        if not destination.is_relative_to(self.decrypted_dir.resolve()):
            raise ValueError("数据库缓存路径越界。")
        return destination

    def prepare(
        self,
        relative_paths: Iterable[str] | None = None,
        *,
        progress: Callable[[str, float], None] | None = None,
    ) -> None:
        selected = list(relative_paths) if relative_paths is not None else list(self.keys.paths())
        manifest = self._read_manifest()
        entries = manifest.setdefault("entries", {})
        assert isinstance(entries, dict)
        total = max(1, len(selected))
        self.root.mkdir(parents=True, exist_ok=True)
        self.decrypted_dir.mkdir(parents=True, exist_ok=True)
        for index, relative in enumerate(selected):
            normalized = str(relative).replace("/", "\\").lstrip("\\")
            if normalized not in self.keys:
                continue
            source = self.db_dir / Path(normalized.replace("\\", os.sep))
            destination = self.decrypted_path(normalized)
            current_signature = database_source_signature(source)
            cached_entry = entries.get(normalized)
            if (
                isinstance(cached_entry, dict)
                and cached_entry.get("signature") == current_signature
                and destination.is_file()
            ):
                try:
                    validate_sqlite(destination)
                    self.reused_count += 1
                    if progress:
                        progress(f"已复用缓存 {normalized}", (index + 1) / total)
                    continue
                except (OSError, ValueError):
                    pass

            destination.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.TemporaryDirectory(prefix="refresh-", dir=self.root) as temporary:
                temporary_root = Path(temporary)
                encrypted_copy = temporary_root / "encrypted.db"
                wal_copy, snapshot_signature = copy_consistent_database_snapshot(
                    source,
                    encrypted_copy,
                )
                decrypted_copy = temporary_root / "decrypted.db"
                decrypt_database(
                    encrypted_copy,
                    decrypted_copy,
                    self.keys[normalized],
                )
                if wal_copy.is_file():
                    apply_wal(wal_copy, decrypted_copy, self.keys[normalized])
                validate_sqlite(decrypted_copy)
                os.replace(decrypted_copy, destination)
            entries[normalized] = {"signature": snapshot_signature}
            self.refreshed_count += 1
            if progress:
                progress(f"已刷新缓存 {normalized}", (index + 1) / total)
        self._write_manifest(manifest)

    def _read_manifest(self) -> dict[str, object]:
        try:
            if self.manifest_path.stat().st_size > 4 * 1024 * 1024:
                raise ValueError("缓存清单过大")
            payload = json.loads(self.manifest_path.read_text(encoding="utf-8"))
            if (
                payload.get("version") != _CACHE_VERSION
                or payload.get("identity") != self.identity
                or not isinstance(payload.get("entries"), dict)
            ):
                raise ValueError("缓存清单不匹配")
            return payload
        except (OSError, ValueError, TypeError, AttributeError):
            return {
                "version": _CACHE_VERSION,
                "identity": self.identity,
                "entries": {},
            }

    def _write_manifest(self, manifest: dict[str, object]) -> None:
        temporary: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=self.root,
                prefix="manifest-",
                suffix=".tmp",
                delete=False,
            ) as stream:
                temporary = Path(stream.name)
                json.dump(manifest, stream, ensure_ascii=False, separators=(",", ":"))
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.manifest_path)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
