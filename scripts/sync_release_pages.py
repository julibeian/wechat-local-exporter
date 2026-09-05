"""Synchronize the visible text of existing GitHub Releases without touching assets."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
from pathlib import Path
import re
import subprocess
from typing import Callable, Iterable


PRODUCT_NAME = "微信聊天本地导出工具"
REPOSITORY_RE = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+\Z", flags=re.ASCII)
TAG_RE = re.compile(r"v(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)\Z", flags=re.ASCII)
TARGET_HASH_RE = re.compile(
    r"<!--\s*wechat-exporter-target-sha256:([a-fA-F0-9]{64})\s*-->",
    flags=re.ASCII,
)
TARGET_HASH_TOKEN = "wechat-exporter-target-sha256"
USE_NOTICE = """## 使用前

这是个人项目，请先用少量记录试用。聊天内容在本机处理，不上传，也不修改微信；朋友圈媒体和主动开启的媒体补全可能联网。安装包没有商业代码签名，Windows 可能提示“未知发布者”，请只从本项目下载。"""


@dataclass(frozen=True)
class ReleaseChange:
    release_id: int
    tag: str
    name: str
    body: str
    original: dict


def version_tuple(tag: str) -> tuple[int, int, int]:
    match = TAG_RE.fullmatch(tag)
    if match is None:
        raise ValueError(f"Invalid release tag: {tag!r}")
    return tuple(int(part) for part in match.groups())


def parse_release_sections(notes: str) -> dict[str, str]:
    normalized = notes.replace("\r\n", "\n").rstrip() + "\n"
    headings = list(re.finditer(r"^# (v[^\n]+)\n", normalized, flags=re.MULTILINE))
    sections: dict[str, str] = {}
    for index, heading in enumerate(headings):
        tag = heading.group(1)
        version_tuple(tag)
        end = headings[index + 1].start() if index + 1 < len(headings) else len(normalized)
        section = normalized[heading.end() : end].strip()
        if tag in sections or not section:
            raise ValueError(f"Expected one non-empty release-notes section for {tag}")
        sections[tag] = section
    if not sections:
        raise ValueError("No release-notes sections found")
    return sections


def _without_date(section: str) -> str:
    lines = section.splitlines()
    while lines and not lines[0].strip():
        lines.pop(0)
    if lines and re.fullmatch(r"发布日期：\d{4}-\d{2}-\d{2}。", lines[0].strip()):
        lines.pop(0)
        while lines and not lines[0].strip():
            lines.pop(0)
    return "\n".join(lines).strip()


def extract_target_hash(body: str, tag: str) -> str | None:
    matches = TARGET_HASH_RE.findall(body)
    if len(matches) > 1 or (TARGET_HASH_TOKEN in body and len(matches) != 1):
        raise ValueError(f"Invalid or repeated target SHA-256 marker in {tag}")
    if version_tuple(tag) >= (1, 5, 0) and len(matches) != 1:
        raise ValueError(f"Missing target SHA-256 marker in {tag}")
    return matches[0].lower() if matches else None


def _asset_map(release: dict, repository: str, tag: str) -> dict[str, str]:
    assets = release.get("assets")
    if not isinstance(assets, list):
        raise ValueError(f"Invalid asset list in {tag}")
    mapped: dict[str, str] = {}
    prefix = f"https://github.com/{repository}/releases/download/{tag}/"
    for asset in assets:
        if not isinstance(asset, dict):
            raise ValueError(f"Invalid asset entry in {tag}")
        name = asset.get("name")
        url = asset.get("browser_download_url")
        if not isinstance(name, str) or not isinstance(url, str) or url != prefix + name:
            raise ValueError(f"Invalid download URL in {tag}")
        if name in mapped:
            raise ValueError(f"Duplicate asset {name!r} in {tag}")
        mapped[name] = url
    return mapped


def build_release_body(
    repository: str,
    release: dict,
    section: str,
    *,
    latest_tag: str,
) -> str:
    tag = release.get("tag_name")
    if not isinstance(tag, str):
        raise ValueError("Release tag is missing")
    version_tuple(tag)
    project_url = f"https://github.com/{repository}"
    releases_url = f"{project_url}/releases"
    assets = _asset_map(release, repository, tag)
    installer_name = f"WeChat-TXT-PDF-Exporter-Installer-{tag}.exe"
    installer_url = assets.get(installer_name)
    if installer_url is None:
        raise ValueError(f"Installer asset is missing in {tag}")

    optional = []
    portable_name = f"WeChat-TXT-PDF-Exporter-{tag}.exe"
    checksum_name = f"SHA256SUMS-{tag}.txt"
    known_names = {installer_name, portable_name, checksum_name}
    unknown_names = sorted(set(assets) - known_names)
    if unknown_names:
        raise ValueError(f"Unexpected assets in {tag}: {', '.join(unknown_names)}")
    if portable_name in assets:
        optional.append(f"[便携版]({assets[portable_name]})")
    if checksum_name in assets:
        optional.append(f"[SHA-256]({assets[checksum_name]})")

    target_hash = extract_target_hash(str(release.get("body") or ""), tag)
    cleaned_section = _without_date(section)
    if not cleaned_section:
        raise ValueError(f"Release-notes section is empty after cleanup for {tag}")

    parts = [
        f"[下载最新版本]({releases_url}/latest) · [查看全部历史版本]({releases_url})",
    ]
    if tag != latest_tag:
        parts.extend(["", "这是历史版本，日常使用请优先下载最新版。"])
    parts.extend(
        [
            "",
            "## 下载",
            "",
            f"**[Windows 一键安装包]({installer_url})**",
        ]
    )
    if optional:
        parts.extend(["", " · ".join(optional)])
    parts.extend(
        [
            "",
            "Windows 10/11 x64，无需安装 Python。",
            "",
            "## 更新内容",
            "",
            cleaned_section,
            "",
            USE_NOTICE,
            "",
            f"觉得好用，欢迎点个 [Star]({project_url})。",
        ]
    )
    if target_hash:
        parts.extend(["", f"<!-- wechat-exporter-target-sha256:{target_hash} -->"])
    return "\n".join(parts).rstrip() + "\n"


def build_sync_plan(
    repository: str,
    releases: Iterable[dict],
    latest: dict,
    sections: dict[str, str],
) -> list[ReleaseChange]:
    if REPOSITORY_RE.fullmatch(repository) is None:
        raise ValueError(f"Invalid repository: {repository!r}")
    latest_tag = latest.get("tag_name") if isinstance(latest, dict) else None
    if not isinstance(latest_tag, str):
        raise ValueError("Latest release tag is missing")
    version_tuple(latest_tag)

    formal: list[dict] = []
    seen_ids: set[int] = set()
    seen_tags: set[str] = set()
    for release in releases:
        if not isinstance(release, dict):
            raise ValueError("Invalid release entry")
        if release.get("draft") or release.get("prerelease"):
            continue
        release_id = release.get("id")
        tag = release.get("tag_name")
        if type(release_id) is not int or not isinstance(tag, str):
            raise ValueError("Release id or tag is missing")
        version_tuple(tag)
        if release_id in seen_ids or tag in seen_tags:
            raise ValueError(f"Duplicate release id or tag: {tag}")
        seen_ids.add(release_id)
        seen_tags.add(tag)
        formal.append(release)
    if latest_tag not in seen_tags:
        raise ValueError("Latest release is not in the formal release list")

    missing_sections = sorted(seen_tags - set(sections), key=version_tuple, reverse=True)
    if missing_sections:
        raise ValueError(f"Missing release notes for: {', '.join(missing_sections)}")

    plan = []
    for release in sorted(formal, key=lambda item: version_tuple(item["tag_name"]), reverse=True):
        tag = release["tag_name"]
        name = f"{PRODUCT_NAME} {tag}"
        body = build_release_body(
            repository,
            release,
            sections[tag],
            latest_tag=latest_tag,
        )
        if release.get("name") != name or (release.get("body") or "").replace("\r\n", "\n") != body:
            plan.append(ReleaseChange(release["id"], tag, name, body, release))
    return plan


def _gh_json(args: list[str], *, input_json: dict | None = None) -> object:
    completed = subprocess.run(
        ["gh", *args],
        input=None if input_json is None else json.dumps(input_json, ensure_ascii=False),
        text=True,
        encoding="utf-8",
        capture_output=True,
        check=True,
    )
    return json.loads(completed.stdout)


def apply_sync_plan(
    repository: str,
    plan: Iterable[ReleaseChange],
    *,
    request: Callable[[list[str], dict | None], object] | None = None,
) -> None:
    caller = request or (lambda args, payload: _gh_json(args, input_json=payload))
    for change in plan:
        payload = {"name": change.name, "body": change.body}
        result = caller(
            [
                "api",
                "--method",
                "PATCH",
                f"repos/{repository}/releases/{change.release_id}",
                "--input",
                "-",
            ],
            payload,
        )
        if not isinstance(result, dict) or result.get("id") != change.release_id:
            raise RuntimeError(f"GitHub did not confirm the update for {change.tag}")
        for field in ("tag_name", "draft", "prerelease", "published_at"):
            if result.get(field) != change.original.get(field):
                raise RuntimeError(f"GitHub changed protected release field {field} for {change.tag}")
        before_assets = sorted(
            (asset.get("id"), asset.get("name"), asset.get("size"), asset.get("digest"))
            for asset in change.original.get("assets", [])
        )
        after_assets = sorted(
            (asset.get("id"), asset.get("name"), asset.get("size"), asset.get("digest"))
            for asset in result.get("assets", [])
        )
        if before_assets != after_assets:
            raise RuntimeError(f"GitHub changed release assets for {change.tag}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", required=True, help="GitHub repository in owner/name form")
    parser.add_argument("--notes", type=Path, default=Path("RELEASE_NOTES.md"))
    parser.add_argument("--apply", action="store_true", help="Apply the fully validated plan")
    args = parser.parse_args()

    try:
        sections = parse_release_sections(args.notes.read_text(encoding="utf-8-sig"))
        releases = _gh_json(["api", f"repos/{args.repository}/releases?per_page=100"])
        latest = _gh_json(["api", f"repos/{args.repository}/releases/latest"])
        if not isinstance(releases, list) or not isinstance(latest, dict):
            raise ValueError("GitHub returned invalid release metadata")
        plan = build_sync_plan(args.repository, releases, latest, sections)
        if args.apply:
            apply_sync_plan(args.repository, plan)
    except (OSError, ValueError, RuntimeError, subprocess.CalledProcessError) as exc:
        parser.error(str(exc))

    action = "updated" if args.apply else "would update"
    print(f"{action} {len(plan)} release page(s)")
    for change in plan:
        print(f"- {change.tag}: {change.name}")


if __name__ == "__main__":
    main()
