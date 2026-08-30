from __future__ import annotations

import hashlib
import hmac
import os
import re
import shutil
import sqlite3
import struct
import tempfile
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from .windows import ProcessMemory, list_wechat_processes


PAGE_SIZE = 4096
SALT_SIZE = 16
KEY_SIZE = 32
IV_SIZE = 16
HMAC_SIZE = 64
RESERVE_SIZE = IV_SIZE + HMAC_SIZE
SQLITE_HEADER = b"SQLite format 3\x00"
WAL_HEADER_SIZE = 32
WAL_FRAME_HEADER_SIZE = 24

_RAW_KEY_RE = re.compile(rb"x'([0-9a-fA-F]{64,192})'")


@dataclass(frozen=True, slots=True)
class DatabaseTarget:
    relative_path: str
    path: Path
    size: int
    salt: bytes
    first_page: bytes


class DatabaseKeys:
    """In-memory key container whose representation never reveals keys."""

    def __init__(self, values: dict[str, bytes]):
        self._values = dict(values)

    def __contains__(self, relative_path: str) -> bool:
        return _normalize_relative(relative_path) in self._values

    def __getitem__(self, relative_path: str) -> bytes:
        return self._values[_normalize_relative(relative_path)]

    def __len__(self) -> int:
        return len(self._values)

    def paths(self) -> tuple[str, ...]:
        return tuple(sorted(self._values))

    def __repr__(self) -> str:
        return f"DatabaseKeys(count={len(self._values)}, values=<redacted>)"


def _normalize_relative(value: str | Path) -> str:
    return str(value).replace("/", "\\").lstrip("\\")


def collect_required_databases(db_dir: Path) -> list[DatabaseTarget]:
    db_dir = db_dir.resolve()
    candidates = [
        db_dir / "contact" / "contact.db",
        db_dir / "session" / "session.db",
        db_dir / "sns" / "sns.db",
    ]
    message_dir = db_dir / "message"
    if message_dir.is_dir():
        candidates.extend(sorted(message_dir.glob("message_[0-9]*.db")))

    targets: list[DatabaseTarget] = []
    for path in candidates:
        if not path.is_file():
            continue
        size = path.stat().st_size
        if size < PAGE_SIZE:
            continue
        with path.open("rb") as stream:
            first_page = stream.read(PAGE_SIZE)
        if len(first_page) != PAGE_SIZE:
            continue
        relative = _normalize_relative(path.relative_to(db_dir))
        targets.append(
            DatabaseTarget(
                relative_path=relative,
                path=path,
                size=size,
                salt=first_page[:SALT_SIZE],
                first_page=first_page,
            )
        )
    return targets


def derive_mac_key(encryption_key: bytes, salt: bytes) -> bytes:
    mac_salt = bytes(value ^ 0x3A for value in salt)
    return hashlib.pbkdf2_hmac("sha512", encryption_key, mac_salt, 2, dklen=KEY_SIZE)


def derive_encryption_key(master_password: bytes, salt: bytes) -> bytes:
    if len(master_password) != KEY_SIZE:
        raise ValueError("微信数据库主密钥必须是 32 字节")
    return hashlib.pbkdf2_hmac(
        "sha512", master_password, salt, 256_000, dklen=KEY_SIZE
    )


def keys_from_master_password(
    master_password: bytes, targets: Iterable[DatabaseTarget]
) -> DatabaseKeys:
    target_list = list(targets)
    if not target_list:
        return DatabaseKeys({})

    # A bad candidate should pay the 256,000-round PBKDF2 cost only once.
    # All account databases share the master password, although every file has
    # its own salt and derived key.
    probe = min(target_list, key=_master_password_probe_priority)
    probe_key = derive_encryption_key(master_password, probe.salt)
    if not verify_key(probe_key, probe.first_page):
        return DatabaseKeys({})

    values: dict[str, bytes] = {probe.relative_path: probe_key}
    for target in target_list:
        if target is probe:
            continue
        key = derive_encryption_key(master_password, target.salt)
        if verify_key(key, target.first_page):
            values[target.relative_path] = key
    return DatabaseKeys(values)


def _master_password_probe_priority(target: DatabaseTarget) -> tuple[int, int]:
    normalized = _normalize_relative(target.relative_path).lower()
    if normalized == "session\\session.db":
        return (0, target.size)
    if normalized == "contact\\contact.db":
        return (1, target.size)
    return (2, target.size)


def verify_key(encryption_key: bytes, first_page: bytes) -> bool:
    if len(encryption_key) != KEY_SIZE or len(first_page) != PAGE_SIZE:
        return False
    salt = first_page[:SALT_SIZE]
    mac_key = derive_mac_key(encryption_key, salt)
    authenticated = first_page[SALT_SIZE : PAGE_SIZE - HMAC_SIZE]
    expected = first_page[PAGE_SIZE - HMAC_SIZE :]
    digest = hmac.new(mac_key, authenticated, hashlib.sha512)
    digest.update(struct.pack("<I", 1))
    return hmac.compare_digest(digest.digest(), expected)


def extract_database_keys(
    targets: Iterable[DatabaseTarget],
    *,
    progress: Callable[[str, float], None] | None = None,
) -> DatabaseKeys:
    """Find WCDB raw keys in the current user's running Weixin.exe process.

    Keys are validated with the encrypted database's first-page HMAC and are
    returned in memory only.
    """
    target_list = list(targets)
    if not target_list:
        raise FileNotFoundError("没有找到 contact/session/message/sns 数据库")
    by_salt: dict[bytes, list[DatabaseTarget]] = {}
    for target in target_list:
        by_salt.setdefault(target.salt, []).append(target)

    processes = list_wechat_processes()
    if not processes:
        raise RuntimeError("微信没有运行。请先登录并保持微信窗口开启。")

    found_by_salt: dict[bytes, bytes] = {}
    fallback_keys: set[bytes] = set()
    for process_index, process in enumerate(processes):
        if len(found_by_salt) == len(by_salt):
            break
        if progress:
            progress(f"正在只读扫描微信进程 PID {process.pid}", process_index / len(processes))
        try:
            with ProcessMemory(process.pid) as memory:
                regions = list(memory.regions())
                total = max(1, sum(size for _, size in regions))
                processed = 0
                for base, size in regions:
                    offset = 0
                    previous = b""
                    while offset < size:
                        request_size = min(8 * 1024 * 1024, size - offset)
                        current = memory.read(base + offset, request_size)
                        combined = previous + current if current else b""
                        if combined:
                            # WeChat builds have used narrow SQLCipher literals,
                            # UTF-16 literals, and adjacent binary key/salt
                            # buffers. Every candidate still has to pass the
                            # database page HMAC before it is accepted.
                            for salt, targets_for_salt in by_salt.items():
                                if salt in found_by_salt:
                                    continue
                                candidate = _find_adjacent_key_candidate(
                                    combined, salt, targets_for_salt[0].first_page
                                )
                                if candidate is not None:
                                    found_by_salt[salt] = candidate
                            for match in _RAW_KEY_RE.finditer(combined):
                                raw_hex = match.group(1)
                                if len(raw_hex) < 64 or len(raw_hex) % 2:
                                    continue
                                try:
                                    candidate = bytes.fromhex(raw_hex[:64].decode("ascii"))
                                except (UnicodeDecodeError, ValueError):
                                    continue
                                if len(raw_hex) == 64:
                                    fallback_keys.add(candidate)
                                    continue
                                try:
                                    salt = bytes.fromhex(raw_hex[-32:].decode("ascii"))
                                except (UnicodeDecodeError, ValueError):
                                    continue
                                targets_for_salt = by_salt.get(salt)
                                if not targets_for_salt or salt in found_by_salt:
                                    continue
                                if verify_key(candidate, targets_for_salt[0].first_page):
                                    found_by_salt[salt] = candidate
                            previous = combined[-512:]
                        else:
                            previous = b""
                        offset += request_size
                        processed += request_size
                        if progress and processed % (64 * 1024 * 1024) < request_size:
                            within = min(1.0, processed / total)
                            overall = (process_index + within) / len(processes)
                            progress(
                                f"已验证 {len(found_by_salt)}/{len(by_salt)} 组数据库密钥",
                                overall,
                            )
                    if len(found_by_salt) == len(by_salt):
                        break
        except PermissionError:
            raise
        except OSError:
            continue

    if len(found_by_salt) < len(by_salt) and fallback_keys:
        for salt, salt_targets in by_salt.items():
            if salt in found_by_salt:
                continue
            for candidate in fallback_keys:
                if verify_key(candidate, salt_targets[0].first_page):
                    found_by_salt[salt] = candidate
                    break

    values: dict[str, bytes] = {}
    for target in target_list:
        key = found_by_salt.get(target.salt)
        if key is not None:
            values[target.relative_path] = key

    required_core = {"contact\\contact.db", "session\\session.db"}
    missing_core = sorted(path for path in required_core if path not in values)
    if missing_core:
        raise RuntimeError(
            "未能验证核心数据库密钥：" + "、".join(missing_core) + "。请在微信中打开任一聊天后重试。"
        )
    if not any(path.startswith("message\\message_") for path in values):
        raise RuntimeError("未能验证消息数据库密钥。请在微信中打开一个聊天窗口后重试。")
    if progress:
        progress(f"已在内存中验证 {len(values)} 个数据库密钥（不会落盘）", 1.0)
    return DatabaseKeys(values)


def _find_adjacent_key_candidate(
    data: bytes, salt: bytes, first_page: bytes
) -> bytes | None:
    encodings = (
        (salt, "binary"),
        (salt.hex().encode("ascii"), "ascii"),
        (salt.hex().encode("utf-16le"), "wide"),
    )
    for needle, encoding in encodings:
        start = 0
        while True:
            index = data.find(needle, start)
            if index < 0:
                break
            candidates: list[bytes] = []
            if encoding == "binary":
                if index >= KEY_SIZE:
                    candidates.append(data[index - KEY_SIZE : index])
                after = index + len(needle)
                if after + KEY_SIZE <= len(data):
                    candidates.append(data[after : after + KEY_SIZE])
            elif encoding == "ascii":
                if index >= KEY_SIZE * 2:
                    raw = data[index - KEY_SIZE * 2 : index]
                    try:
                        candidates.append(bytes.fromhex(raw.decode("ascii")))
                    except (UnicodeDecodeError, ValueError):
                        pass
            else:
                if index >= KEY_SIZE * 4:
                    raw = data[index - KEY_SIZE * 4 : index]
                    try:
                        candidates.append(bytes.fromhex(raw.decode("utf-16le")))
                    except (UnicodeDecodeError, ValueError):
                        pass
            for candidate in candidates:
                if verify_key(candidate, first_page):
                    return candidate
            start = index + max(1, len(needle))
    return None


def _aes_cbc_decrypt(key: bytes, iv: bytes, encrypted: bytes) -> bytes:
    decryptor = Cipher(algorithms.AES(key), modes.CBC(iv)).decryptor()
    return decryptor.update(encrypted) + decryptor.finalize()


def decrypt_page(encryption_key: bytes, encrypted_page: bytes, page_number: int) -> bytes:
    if len(encrypted_page) != PAGE_SIZE:
        raise ValueError("数据库页长度不是 4096 字节")
    iv = encrypted_page[PAGE_SIZE - RESERVE_SIZE : PAGE_SIZE - HMAC_SIZE]
    if page_number == 1:
        encrypted = encrypted_page[SALT_SIZE : PAGE_SIZE - RESERVE_SIZE]
        plaintext = _aes_cbc_decrypt(encryption_key, iv, encrypted)
        return SQLITE_HEADER + plaintext + (b"\x00" * RESERVE_SIZE)
    encrypted = encrypted_page[: PAGE_SIZE - RESERVE_SIZE]
    plaintext = _aes_cbc_decrypt(encryption_key, iv, encrypted)
    return plaintext + (b"\x00" * RESERVE_SIZE)


def decrypt_database(source: Path, destination: Path, encryption_key: bytes) -> None:
    size = source.stat().st_size
    if size < PAGE_SIZE or size % PAGE_SIZE:
        raise ValueError(f"数据库大小异常：{source}")
    with source.open("rb") as input_stream:
        first_page = input_stream.read(PAGE_SIZE)
    if not verify_key(encryption_key, first_page):
        raise ValueError(f"数据库密钥校验失败：{source.name}")

    destination.parent.mkdir(parents=True, exist_ok=True)
    with source.open("rb") as input_stream, destination.open("wb") as output_stream:
        page_number = 1
        while encrypted_page := input_stream.read(PAGE_SIZE):
            if len(encrypted_page) != PAGE_SIZE:
                raise ValueError(f"数据库末页不完整：{source.name}")
            output_stream.write(decrypt_page(encryption_key, encrypted_page, page_number))
            page_number += 1


def apply_wal(wal_path: Path, decrypted_db: Path, encryption_key: bytes) -> int:
    """Apply only frames through the last committed transaction in the WAL."""
    if not wal_path.is_file() or wal_path.stat().st_size <= WAL_HEADER_SIZE:
        return 0
    data = wal_path.read_bytes()
    if len(data) <= WAL_HEADER_SIZE:
        return 0
    page_size = struct.unpack(">I", data[8:12])[0]
    if page_size == 1:
        page_size = 65536
    if page_size != PAGE_SIZE:
        raise ValueError(f"不支持的 WAL 页大小：{page_size}")
    wal_salt = data[16:24]
    frame_size = WAL_FRAME_HEADER_SIZE + PAGE_SIZE
    frames: list[tuple[int, int, bytes]] = []
    cursor = WAL_HEADER_SIZE
    while cursor + frame_size <= len(data):
        header = data[cursor : cursor + WAL_FRAME_HEADER_SIZE]
        page_number, commit_size = struct.unpack(">II", header[:8])
        if page_number <= 0 or page_number > 1_000_000:
            break
        if header[8:16] != wal_salt:
            break
        encrypted_page = data[
            cursor + WAL_FRAME_HEADER_SIZE : cursor + WAL_FRAME_HEADER_SIZE + PAGE_SIZE
        ]
        frames.append((page_number, commit_size, encrypted_page))
        cursor += frame_size

    last_commit_index = -1
    committed_size = 0
    for index, (_, commit_size, _) in enumerate(frames):
        if commit_size:
            last_commit_index = index
            committed_size = commit_size
    if last_commit_index < 0:
        return 0

    with decrypted_db.open("r+b") as output:
        for page_number, _, encrypted_page in frames[: last_commit_index + 1]:
            output.seek((page_number - 1) * PAGE_SIZE)
            output.write(decrypt_page(encryption_key, encrypted_page, page_number))
        if committed_size:
            output.truncate(committed_size * PAGE_SIZE)
    return last_commit_index + 1


def validate_sqlite(path: Path) -> None:
    uri = f"file:{path.as_posix()}?mode=ro&immutable=1"
    connection = sqlite3.connect(uri, uri=True)
    try:
        row = connection.execute("SELECT count(*) FROM sqlite_master").fetchone()
        if row is None:
            raise ValueError(f"SQLite 元数据不可读：{path.name}")
    finally:
        connection.close()


class DecryptedWorkspace:
    """Temporary encrypted snapshots plus decrypted, read-only query copies."""

    def __init__(self, db_dir: Path, keys: DatabaseKeys):
        self.db_dir = db_dir.resolve()
        self.keys = keys
        self._temp = tempfile.TemporaryDirectory(prefix="wechat-txt-pdf-")
        self.root = Path(self._temp.name)
        self.encrypted_dir = self.root / "encrypted"
        self.decrypted_dir = self.root / "decrypted"

    def close(self) -> None:
        self._temp.cleanup()

    def __enter__(self) -> DecryptedWorkspace:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def decrypted_path(self, relative_path: str | Path) -> Path:
        normalized = Path(_normalize_relative(relative_path).replace("\\", os.sep))
        return self.decrypted_dir / normalized

    def prepare(
        self,
        relative_paths: Iterable[str] | None = None,
        *,
        progress: Callable[[str, float], None] | None = None,
    ) -> None:
        selected = list(relative_paths) if relative_paths is not None else list(self.keys.paths())
        total = max(1, len(selected))
        for index, relative in enumerate(selected):
            relative = _normalize_relative(relative)
            if relative not in self.keys:
                continue
            source = self.db_dir / Path(relative.replace("\\", os.sep))
            if not source.is_file():
                continue
            encrypted_copy = self.encrypted_dir / Path(relative.replace("\\", os.sep))
            encrypted_copy.parent.mkdir(parents=True, exist_ok=True)
            wal_copy = _copy_consistent_database_snapshot(source, encrypted_copy)

            destination = self.decrypted_path(relative)
            decrypt_database(encrypted_copy, destination, self.keys[relative])
            if wal_copy.is_file():
                apply_wal(wal_copy, destination, self.keys[relative])
            validate_sqlite(destination)
            if progress:
                progress(f"已准备 {relative}", (index + 1) / total)


def _file_signature(path: Path) -> tuple[int, int]:
    stat = path.stat()
    return stat.st_size, stat.st_mtime_ns


def _optional_file_signature(path: Path) -> tuple[int, int] | None:
    try:
        return _file_signature(path)
    except FileNotFoundError:
        return None


def _copy_consistent_database_snapshot(
    source: Path, destination: Path, *, attempts: int = 4
) -> Path:
    """Copy an encrypted DB/WAL pair only when both stay stable during the copy."""
    wal_source = Path(str(source) + "-wal")
    wal_destination = Path(str(destination) + "-wal")
    for _ in range(attempts):
        try:
            before = (_file_signature(source), _optional_file_signature(wal_source))
            shutil.copyfile(source, destination)
            if before[1] is not None:
                shutil.copyfile(wal_source, wal_destination)
            elif wal_destination.exists():
                wal_destination.unlink()
            after = (_file_signature(source), _optional_file_signature(wal_source))
        except FileNotFoundError:
            # A WAL can disappear during checkpointing; retry the whole pair.
            continue
        if before == after:
            return wal_destination
    raise RuntimeError(
        f"微信正在持续写入数据库，无法取得一致快照：{source.name}。请稍候后重试。"
    )
