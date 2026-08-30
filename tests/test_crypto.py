from __future__ import annotations

import hashlib
import hmac
import os
import shutil
import struct
from pathlib import Path

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from wechat_exporter.crypto import (
    DatabaseTarget,
    PAGE_SIZE,
    RESERVE_SIZE,
    SALT_SIZE,
    SQLITE_HEADER,
    apply_wal,
    decrypt_page,
    derive_encryption_key,
    derive_mac_key,
    keys_from_master_password,
    verify_key,
    _copy_consistent_database_snapshot,
)


def _encrypt_page(key: bytes, plaintext: bytes, page_number: int, salt: bytes) -> bytes:
    iv = os.urandom(16)
    if page_number == 1:
        encrypted_plaintext = plaintext[SALT_SIZE : PAGE_SIZE - RESERVE_SIZE]
        prefix = salt
    else:
        encrypted_plaintext = plaintext[: PAGE_SIZE - RESERVE_SIZE]
        prefix = b""
    encryptor = Cipher(algorithms.AES(key), modes.CBC(iv)).encryptor()
    encrypted = encryptor.update(encrypted_plaintext) + encryptor.finalize()
    page_without_hmac = prefix + encrypted + iv
    mac_key = derive_mac_key(key, salt)
    authenticated = page_without_hmac[SALT_SIZE if page_number == 1 else 0 :]
    digest = hmac.new(mac_key, authenticated, hashlib.sha512)
    digest.update(struct.pack("<I", page_number))
    return page_without_hmac + digest.digest()


def test_master_password_and_page_roundtrip() -> None:
    password = bytes(range(32))
    salt = bytes(range(16))
    key = derive_encryption_key(password, salt)
    assert len(key) == 32
    plaintext = SQLITE_HEADER + os.urandom(4000) + b"\x00" * 80
    encrypted = _encrypt_page(key, plaintext, 1, salt)
    assert len(encrypted) == PAGE_SIZE
    assert verify_key(key, encrypted)
    assert not verify_key(b"x" * 32, encrypted)
    assert decrypt_page(key, encrypted, 1) == plaintext


def test_bad_master_password_is_rejected_after_one_database(tmp_path, monkeypatch) -> None:
    targets = [
        DatabaseTarget(
            relative_path=relative_path,
            path=tmp_path / f"database-{index}.db",
            size=PAGE_SIZE,
            salt=bytes([index]) * SALT_SIZE,
            first_page=bytes(PAGE_SIZE),
        )
        for index, relative_path in enumerate(
            ("contact\\contact.db", "message\\message_0.db", "session\\session.db"),
            start=1,
        )
    ]
    derived_salts = []

    def fake_derive(_password, salt):
        derived_salts.append(salt)
        return b"x" * 32

    monkeypatch.setattr("wechat_exporter.crypto.derive_encryption_key", fake_derive)
    monkeypatch.setattr("wechat_exporter.crypto.verify_key", lambda _key, _page: False)

    keys = keys_from_master_password(b"p" * 32, targets)

    assert keys.paths() == ()
    assert derived_salts == [bytes([3]) * SALT_SIZE]


def test_apply_wal_uses_last_commit(tmp_path) -> None:
    salt = os.urandom(16)
    key = os.urandom(32)
    page1 = SQLITE_HEADER + os.urandom(4000) + b"\x00" * 80
    old_page2 = os.urandom(PAGE_SIZE - 80) + b"\x00" * 80
    new_page2 = os.urandom(PAGE_SIZE - 80) + b"\x00" * 80
    db_path = tmp_path / "decrypted.db"
    db_path.write_bytes(page1 + old_page2)

    encrypted_page2 = _encrypt_page(key, new_page2, 2, salt)
    wal_salt = os.urandom(8)
    wal_header = struct.pack(">IIII", 0x377F0682, 3007000, PAGE_SIZE, 0) + wal_salt + b"\x00" * 8
    frame_header = struct.pack(">II", 2, 2) + wal_salt + b"\x00" * 8
    wal_path = tmp_path / "decrypted.db-wal"
    wal_path.write_bytes(wal_header + frame_header + encrypted_page2)

    assert apply_wal(wal_path, db_path, key) == 1
    assert db_path.read_bytes()[PAGE_SIZE:] == new_page2


def test_snapshot_retries_when_wal_changes(tmp_path, monkeypatch) -> None:
    source = tmp_path / "source.db"
    source.write_bytes(b"database-v1")
    wal_source = Path(str(source) + "-wal")
    wal_source.write_bytes(b"wal-v1")
    destination = tmp_path / "copy" / "source.db"
    destination.parent.mkdir()

    real_copyfile = shutil.copyfile
    changed = False

    def racing_copy(src, dst, *args, **kwargs):
        nonlocal changed
        result = real_copyfile(src, dst, *args, **kwargs)
        if Path(src) == wal_source and not changed:
            wal_source.write_bytes(b"wal-v2-with-a-different-size")
            changed = True
        return result

    monkeypatch.setattr("wechat_exporter.crypto.shutil.copyfile", racing_copy)
    wal_destination = _copy_consistent_database_snapshot(source, destination)

    assert destination.read_bytes() == source.read_bytes()
    assert wal_destination.read_bytes() == wal_source.read_bytes()


def test_collect_required_databases_includes_moments_but_skips_voice_media(tmp_path) -> None:
    from wechat_exporter.crypto import collect_required_databases

    db_dir = tmp_path / "db_storage"
    paths = (
        db_dir / "contact" / "contact.db",
        db_dir / "session" / "session.db",
        db_dir / "message" / "message_0.db",
        db_dir / "message" / "media_0.db",
        db_dir / "sns" / "sns.db",
    )
    for path in paths:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(bytes(PAGE_SIZE))

    assert {target.relative_path for target in collect_required_databases(db_dir)} == {
        "contact\\contact.db",
        "session\\session.db",
        "message\\message_0.db",
        "sns\\sns.db",
    }
