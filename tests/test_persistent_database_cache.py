from __future__ import annotations

import os
import shutil
import sqlite3
from pathlib import Path

import pytest

from wechat_exporter import database_cache
from wechat_exporter.crypto import DatabaseKeys, DatabaseTarget, database_source_signature
from wechat_exporter.database_cache import (
    AccountDatabaseCache,
    PersistentDecryptedWorkspace,
    protect_for_current_user,
    unprotect_for_current_user,
)
from wechat_exporter.models import AccountLocation


def _target(path: Path) -> DatabaseTarget:
    page = b"s" * 16 + b"x" * (4096 - 16)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(page)
    return DatabaseTarget("session\\session.db", path, len(page), page[:16], page)


@pytest.mark.skipif(os.name != "nt", reason="Windows DPAPI")
def test_dpapi_cache_is_current_user_bound_and_not_plaintext() -> None:
    plaintext = b"unique-database-key-material"
    protected = protect_for_current_user(plaintext, entropy=b"cache-test-entropy")

    assert plaintext not in protected
    assert unprotect_for_current_user(protected, entropy=b"cache-test-entropy") == plaintext
    with pytest.raises(OSError):
        unprotect_for_current_user(protected, entropy=b"different-entropy")


def test_account_cache_roundtrips_only_valid_complete_key_sets(tmp_path, monkeypatch) -> None:
    account = AccountLocation(tmp_path / "wxid_a", "wxid_a", "test")
    target = _target(account.db_dir / "session" / "session.db")
    prefix = b"test-protected:"
    cache = AccountDatabaseCache(
        account,
        root=tmp_path / "cache",
        protect=lambda data, **_kwargs: prefix + data,
        unprotect=lambda data, **_kwargs: data.removeprefix(prefix),
    )
    monkeypatch.setattr(database_cache, "verify_key", lambda key, _page: key == b"k" * 32)

    cache.save_keys(DatabaseKeys({target.relative_path: b"k" * 32}), (target,))
    loaded = cache.load_keys((target,))

    assert loaded is not None
    assert loaded[target.relative_path] == b"k" * 32
    changed = DatabaseTarget(
        target.relative_path,
        target.path,
        target.size,
        b"different-salt!!",
        target.first_page,
    )
    assert cache.load_keys((changed,)) is None


def test_persistent_workspace_reuses_unchanged_db_and_refreshes_only_changes(
    tmp_path,
    monkeypatch,
) -> None:
    db_dir = tmp_path / "account" / "db_storage"
    source = db_dir / "session" / "session.db"
    target = _target(source)
    template = tmp_path / "template.db"
    connection = sqlite3.connect(template)
    connection.execute("CREATE TABLE SessionTable(username TEXT)")
    connection.commit()
    connection.close()
    decrypt_calls: list[Path] = []

    def fake_snapshot(input_path: Path, destination: Path):
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(input_path, destination)
        return Path(str(destination) + "-wal"), database_source_signature(input_path)

    def fake_decrypt(_source: Path, destination: Path, _key: bytes):
        decrypt_calls.append(destination)
        shutil.copyfile(template, destination)

    monkeypatch.setattr(database_cache, "copy_consistent_database_snapshot", fake_snapshot)
    monkeypatch.setattr(database_cache, "decrypt_database", fake_decrypt)
    root = tmp_path / "persistent"

    first = PersistentDecryptedWorkspace(
        db_dir,
        DatabaseKeys({target.relative_path: b"k" * 32}),
        root,
        identity="account-a",
    )
    first.prepare()
    assert first.refreshed_count == 1 and first.reused_count == 0
    assert len(decrypt_calls) == 1
    first.close()

    second = PersistentDecryptedWorkspace(
        db_dir,
        DatabaseKeys({target.relative_path: b"k" * 32}),
        root,
        identity="account-a",
    )
    second.prepare()
    assert second.reused_count == 1 and second.refreshed_count == 0
    assert len(decrypt_calls) == 1

    source.write_bytes(source.read_bytes() + b"changed")
    third = PersistentDecryptedWorkspace(
        db_dir,
        DatabaseKeys({target.relative_path: b"k" * 32}),
        root,
        identity="account-a",
    )
    third.prepare()
    assert third.refreshed_count == 1 and third.reused_count == 0
    assert len(decrypt_calls) == 2
