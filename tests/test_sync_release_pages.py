from __future__ import annotations

import runpy
from pathlib import Path

import pytest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "sync_release_pages.py"
module = runpy.run_path(str(SCRIPT))
apply_sync_plan = module["apply_sync_plan"]
build_sync_plan = module["build_sync_plan"]
parse_release_sections = module["parse_release_sections"]

REPOSITORY = "julibeian/wechat-local-exporter"
TARGET_HASH = "a" * 64


def asset(tag: str, name: str, asset_id: int) -> dict:
    return {
        "id": asset_id,
        "name": name,
        "size": 123,
        "digest": f"sha256:{'b' * 64}",
        "browser_download_url": (
            f"https://github.com/{REPOSITORY}/releases/download/{tag}/{name}"
        ),
    }


def release(tag: str, release_id: int, *, latest: bool = False) -> dict:
    names = [f"WeChat-TXT-PDF-Exporter-Installer-{tag}.exe"]
    if tag in {"v1.2.0", "v1.3.0"}:
        names.extend(
            [f"WeChat-TXT-PDF-Exporter-{tag}.exe", f"SHA256SUMS-{tag}.txt"]
        )
    body = (
        f"Old copy\n\n<!-- wechat-exporter-target-sha256:{TARGET_HASH} -->\n"
        if latest
        else "Old copy\n"
    )
    return {
        "id": release_id,
        "tag_name": tag,
        "name": tag,
        "body": body,
        "draft": False,
        "prerelease": False,
        "published_at": "2026-09-05T00:00:00Z",
        "assets": [asset(tag, name, release_id * 10 + index) for index, name in enumerate(names)],
    }


def notes() -> str:
    return """# v1.5.0

发布日期：2026-09-05。

Latest notes.

# v1.3.0

Older notes.

# v1.2.0

More notes.

# v1.1.0

No public release for this section.

# v1.0.0

First notes.
"""


def releases() -> list[dict]:
    return [
        release("v1.5.0", 15, latest=True),
        release("v1.3.0", 13),
        release("v1.2.0", 12),
        release("v1.0.0", 10),
    ]


def test_plan_updates_only_existing_public_releases_and_builds_navigation():
    items = releases()
    plan = build_sync_plan(
        REPOSITORY,
        items,
        items[0],
        parse_release_sections(notes()),
    )

    assert [change.tag for change in plan] == ["v1.5.0", "v1.3.0", "v1.2.0", "v1.0.0"]
    assert all(change.name == f"微信聊天本地导出工具 {change.tag}" for change in plan)
    assert all("/releases/latest)" in change.body for change in plan)
    assert all("[查看全部历史版本]" in change.body for change in plan)
    assert all("欢迎点个 [Star]" in change.body for change in plan)
    assert "No public release" not in "\n".join(change.body for change in plan)
    assert "发布日期" not in plan[0].body
    assert plan[0].body.count(TARGET_HASH) == 1
    assert "这是历史版本" not in plan[0].body
    assert all("这是历史版本" in change.body for change in plan[1:])
    assert "[便携版]" in plan[1].body and "[SHA-256]" in plan[1].body
    assert "[便携版]" not in plan[-1].body and "[SHA-256]" not in plan[-1].body


def test_apply_uses_name_and_body_only_and_verifies_protected_fields():
    items = releases()
    plan = build_sync_plan(REPOSITORY, items, items[0], parse_release_sections(notes()))
    calls = []

    def request(args, payload):
        calls.append((args, payload))
        endpoint = next(arg for arg in args if arg.startswith("repos/"))
        original = next(
            item for item in items if item["id"] == int(endpoint.rsplit("/", 1)[1])
        )
        return {**original, **payload}

    apply_sync_plan(REPOSITORY, plan, request=request)

    assert len(calls) == 4
    assert all(set(payload) == {"name", "body"} for _, payload in calls)
    assert all("release create" not in " ".join(args) for args, _ in calls)
    assert all("--latest" not in args for args, _ in calls)


@pytest.mark.parametrize(
    "bad_body",
    [
        "No marker",
        "<!-- wechat-exporter-target-sha256:not-a-hash -->",
        (
            f"<!-- wechat-exporter-target-sha256:{TARGET_HASH} -->\n"
            f"<!-- wechat-exporter-target-sha256:{TARGET_HASH} -->"
        ),
    ],
)
def test_latest_hash_marker_must_be_unique_and_valid(bad_body):
    items = releases()
    items[0]["body"] = bad_body
    with pytest.raises(ValueError, match="SHA-256"):
        build_sync_plan(REPOSITORY, items, items[0], parse_release_sections(notes()))


def test_validation_finishes_before_any_patch():
    items = releases()
    items[-1]["assets"] = []
    with pytest.raises(ValueError, match="Installer asset is missing"):
        build_sync_plan(REPOSITORY, items, items[0], parse_release_sections(notes()))


def test_workflow_cannot_create_delete_or_relabel_releases():
    workflow = (SCRIPT.parents[1] / ".github" / "workflows" / "sync-release-pages.yml").read_text(
        encoding="utf-8"
    )
    assert "contents: write" in workflow
    assert "cancel-in-progress: true" in workflow
    assert "--apply" in workflow
    assert "release create" not in workflow
    assert "release upload" not in workflow
    assert "release delete" not in workflow
    assert "--latest" not in workflow
