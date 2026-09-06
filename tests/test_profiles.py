from __future__ import annotations

import io
import sqlite3
from pathlib import Path

from PIL import Image

from wechat_exporter.archive import WeChatArchive
from wechat_exporter.models import AccountLocation
from wechat_exporter.profiles import (
    AvatarReference,
    AvatarResolver,
    ContactProfile,
    load_contact_profiles,
)


class _Workspace:
    def __init__(self, contact_db: Path) -> None:
        self.contact_db = contact_db

    def decrypted_path(self, relative_path: str) -> Path:
        assert relative_path.replace("/", "\\").lower() == "contact\\contact.db"
        return self.contact_db


def _database(path: Path, statements: tuple[str, ...]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    try:
        for statement in statements:
            connection.execute(statement)
        connection.commit()
    finally:
        connection.close()


def _png_bytes(*, size: tuple[int, int] = (12, 8)) -> bytes:
    output = io.BytesIO()
    Image.new("RGB", size, "#2f7d68").save(output, format="PNG")
    return output.getvalue()


def test_profile_display_name_priority_and_stable_fallback() -> None:
    profile = ContactProfile(
        username="wxid_alice",
        group_alias="群名片",
        remark="备注",
        nickname="昵称",
        alias="alias-name",
    )
    assert profile.display_name == "群名片"

    assert ContactProfile(
        username="wxid_alice", remark="备注", nickname="昵称", alias="alias-name"
    ).display_name == "备注"
    assert ContactProfile(
        username="wxid_alice", nickname="昵称", alias="alias-name"
    ).display_name == "昵称"
    assert ContactProfile(username="wxid_alice", alias="alias-name").display_name == "alias-name"
    assert ContactProfile(username="wxid_alice").display_name == "wxid_alice"

    fallback = profile.avatar_fallback
    assert fallback.text == "群"
    assert fallback == profile.avatar_fallback
    assert fallback.background.startswith("hsl(")


def test_load_profiles_accepts_schema_candidates_and_group_aliases(tmp_path: Path) -> None:
    account = AccountLocation(tmp_path / "account", "wxid_self", "test")
    contact_db = tmp_path / "decrypted" / "contact.db"
    _database(
        contact_db,
        (
            "CREATE TABLE Contact(UserName TEXT, LocalType INTEGER, VerifyFlag INTEGER, "
            "DeleteFlag INTEGER, RemarkName TEXT, NickName TEXT, UserAlias TEXT, "
            "AvatarPath TEXT, HeadImgUrl TEXT)",
            "INSERT INTO Contact VALUES "
            "('wxid_alice', 1, 0, 0, '备注 Alice', '昵称 Alice', 'alice', "
            "'avatar/alice.png', 'https://example.invalid/alice.png')",
            "INSERT INTO Contact VALUES "
            "('wxid_noremark', 1, 0, 0, '', '昵称 Bob', 'bob', '', '')",
            "INSERT INTO Contact VALUES "
            "('gh_official', 1, 24, 0, '', '公众号', '', '', '')",
            "CREATE TABLE ChatRoomMember(ChatRoomId TEXT, MemberWxid TEXT, RoomNickname TEXT)",
            "INSERT INTO ChatRoomMember VALUES "
            "('study@chatroom', 'wxid_alice', '群名片 Alice')",
        ),
    )

    store = load_contact_profiles(account, _Workspace(contact_db))

    assert set(store.profiles) == {"wxid_alice", "wxid_noremark", "gh_official"}
    assert store.personal_usernames == frozenset({"wxid_alice", "wxid_noremark"})
    alice = store.profile_for("wxid_alice")
    assert alice.display_name == "备注 Alice"
    assert alice.avatar == AvatarReference(
        local_hint="avatar/alice.png",
        remote_url="https://example.invalid/alice.png",
    )
    assert store.profile_for("wxid_alice", group_id="study@chatroom").display_name == "群名片 Alice"
    assert store.profile_for("wxid_noremark", group_id="study@chatroom").display_name == "昵称 Bob"

    participants = store.participants_for(
        "study@chatroom", ("wxid_noremark", "wxid_alice", "wxid_missing")
    )
    assert [item.username for item in participants] == [
        "wxid_alice",
        "wxid_missing",
        "wxid_noremark",
    ]
    assert [item.display_name for item in participants] == [
        "群名片 Alice",
        "wxid_missing",
        "昵称 Bob",
    ]
    assert store.participants_for("study@chatroom")[0].username == "wxid_alice"


def test_missing_optional_profile_columns_fall_back_without_guessing(tmp_path: Path) -> None:
    account = AccountLocation(tmp_path / "account", "wxid_self", "test")
    contact_db = tmp_path / "contact.db"
    _database(
        contact_db,
        (
            "CREATE TABLE contacts(wxid TEXT)",
            "INSERT INTO contacts VALUES ('wxid_only')",
            # This unrelated table has tempting column names but must not be
            # interpreted as a group-member source.
            "CREATE TABLE Analytics(group_id TEXT, username TEXT, display_name TEXT)",
            "INSERT INTO Analytics VALUES ('study@chatroom', 'wxid_only', '不应采用')",
        ),
    )

    store = load_contact_profiles(account, _Workspace(contact_db))

    assert store.profile_for("wxid_only", group_id="study@chatroom").display_name == "wxid_only"
    assert store.participants_for("study@chatroom") == ()


def test_archive_keeps_legacy_contact_map_and_exposes_profiles(tmp_path: Path) -> None:
    account = AccountLocation(tmp_path / "account", "wxid_self", "test")
    contact_db = tmp_path / "contact.db"
    _database(
        contact_db,
        (
            "CREATE TABLE contact(username TEXT, local_type INTEGER, remark TEXT, nickname TEXT)",
            "INSERT INTO contact VALUES ('wxid_friend', 1, '好友备注', '好友昵称')",
        ),
    )
    archive = WeChatArchive(account, _Workspace(contact_db))  # type: ignore[arg-type]

    archive.load_metadata()

    assert archive.contacts == {"wxid_friend": "好友备注"}
    assert archive.profiles["wxid_friend"].remark == "好友备注"
    assert archive.get_profile("wxid_friend").display_name == "好友备注"
    assert archive.resolve_avatar("wxid_friend").status == "missing"


def test_avatar_resolver_returns_validated_local_bytes(tmp_path: Path) -> None:
    account_dir = tmp_path / "account"
    avatar_path = account_dir / "avatar" / "alice.png"
    avatar_path.parent.mkdir(parents=True)
    avatar_bytes = _png_bytes(size=(17, 11))
    avatar_path.write_bytes(avatar_bytes)
    resolver = AvatarResolver(AccountLocation(account_dir, "wxid_self", "test"))
    profile = ContactProfile(
        username="wxid_alice",
        avatar=AvatarReference(local_hint="avatar/alice.png"),
    )

    result = resolver.resolve(profile)

    assert result.available
    assert result.status == "available"
    assert result.data == avatar_bytes
    assert result.path == avatar_path.resolve()
    assert result.mime_type == "image/png"
    assert result.extension == "png"
    assert (result.width, result.height) == (17, 11)
    assert len(result.sha256) == 64


def test_avatar_resolver_rejects_paths_outside_account_and_invalid_images(tmp_path: Path) -> None:
    account_dir = tmp_path / "account"
    account_dir.mkdir()
    outside = tmp_path / "private.png"
    outside.write_bytes(_png_bytes())
    invalid = account_dir / "invalid.jpg"
    invalid.write_text("not an image", encoding="utf-8")
    resolver = AvatarResolver(AccountLocation(account_dir, "wxid_self", "test"))

    escaped = resolver.resolve(
        ContactProfile(
            username="wxid_alice",
            avatar=AvatarReference(local_hint="../private.png"),
        )
    )
    corrupt = resolver.resolve(
        ContactProfile(
            username="wxid_bob",
            avatar=AvatarReference(local_hint="invalid.jpg"),
        )
    )

    assert escaped.status == "rejected"
    assert "超出" in escaped.reason
    assert not escaped.data
    assert corrupt.status == "rejected"
    assert "图片" in corrupt.reason


def test_avatar_resolver_uses_embedded_data_and_keeps_network_opt_in(tmp_path: Path) -> None:
    account = AccountLocation(tmp_path / "account", "wxid_self", "test")
    avatar_bytes = _png_bytes()
    local = AvatarResolver(account).resolve(
        ContactProfile(
            username="wxid_alice",
            avatar=AvatarReference(embedded_data=avatar_bytes),
        )
    )
    disabled = AvatarResolver(account).resolve(
        ContactProfile(
            username="wxid_bob",
            avatar=AvatarReference(remote_url="https://weixin.qq.com/avatar/bob"),
        )
    )

    calls: list[tuple[str, int]] = []

    def download(url: str, limit: int) -> bytes:
        calls.append((url, limit))
        return avatar_bytes

    completed = AvatarResolver(
        account,
        allow_network=True,
        downloader=download,
    ).resolve(
        ContactProfile(
            username="wxid_bob",
            avatar=AvatarReference(remote_url="https://weixin.qq.com/avatar/bob"),
        )
    )

    assert local.available and local.path is None
    assert disabled.status == "network_disabled"
    assert completed.available
    assert calls == [("https://weixin.qq.com/avatar/bob", 5 * 1024 * 1024)]


def test_avatar_resolver_does_not_call_downloader_for_untrusted_host(tmp_path: Path) -> None:
    account = AccountLocation(tmp_path / "account", "wxid_self", "test")
    called = False

    def download(_url: str, _limit: int) -> bytes:
        nonlocal called
        called = True
        return _png_bytes()

    result = AvatarResolver(account, allow_network=True, downloader=download).resolve(
        ContactProfile(
            username="wxid_alice",
            avatar=AvatarReference(remote_url="https://example.invalid/avatar.png"),
        )
    )

    assert result.status == "rejected"
    assert not called
