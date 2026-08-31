"""Render GitHub download navigation and the selected tag's release notes.

New releases include all three standard build assets by default. When updating
an existing release's description, pass --asset for each actual asset so old
versions never acquire links to files that were not published.
"""

from __future__ import annotations

import argparse
import re
from pathlib import Path


RELEASES_URL = "https://github.com/julibeian/wechat-txt-pdf-exporter/releases"


def render_release_notes(
    notes: str, tag: str, asset_names: list[str] | None = None
) -> str:
    if not re.fullmatch(r"v\d+\.\d+\.\d+", tag, flags=re.ASCII):
        raise ValueError(f"Expected a release tag such as v1.3.0, got {tag!r}")

    notes = notes.replace("\r\n", "\n").rstrip() + "\n"
    headings = list(re.finditer(r"^# (v[^\n]+)\n", notes, flags=re.MULTILINE))
    sections = []
    for index, heading in enumerate(headings):
        if heading.group(1) == tag:
            end = headings[index + 1].start() if index + 1 < len(headings) else len(notes)
            sections.append(notes[heading.end():end].strip())
    if len(sections) != 1 or not sections[0]:
        raise ValueError(f"Expected exactly one non-empty release notes section for {tag}")

    installer = f"WeChat-TXT-PDF-Exporter-Installer-{tag}.exe"
    portable = f"WeChat-TXT-PDF-Exporter-{tag}.exe"
    checksums = f"SHA256SUMS-{tag}.txt"
    available = set(asset_names if asset_names is not None else [installer, portable, checksums])
    if installer not in available:
        raise ValueError(f"Recommended installer is missing: {installer}")

    rows = []
    for kind, audience, filename in [
        ("**一键安装包（推荐）**", "普通 Windows 用户", installer),
        ("便携版", "不希望安装的用户", portable),
        ("SHA256", "校验下载文件", checksums),
    ]:
        file_cell = (
            f"[{filename}]({RELEASES_URL}/download/{tag}/{filename})"
            if filename in available else "本版本未提供"
        )
        rows.append(f"| {kind} | {audience} | {file_cell} |")

    choice = (
        "普通用户推荐安装包；希望免安装运行时选择便携版。"
        if portable in available else "本版本未提供便携版；普通用户请选择安装包。"
    )
    return "\n".join([
        "## 下载",
        "",
        "适用 Windows 10/11 x64，无需安装 Python。",
        "",
        "| 类型 | 适合用户 | 文件 |",
        "| --- | --- | --- |",
        *rows,
        "",
        choice,
        "",
        f"上表为 **{tag}** 的文件，使用前请阅读本页更新说明。",
        "",
        f"[下载最新版本]({RELEASES_URL}/latest) · 需要旧版本？[查看全部历史版本]({RELEASES_URL})",
        "",
        "## 更新内容",
        "",
        sections[0],
        "",
    ])


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tag", required=True, help="Release tag, supplied by GITHUB_REF_NAME")
    parser.add_argument("--notes", type=Path, default=Path("RELEASE_NOTES.md"))
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--asset", action="append", help="Existing asset name; repeat for each asset")
    args = parser.parse_args()
    try:
        body = render_release_notes(args.notes.read_text(encoding="utf-8-sig"), args.tag, args.asset)
    except (OSError, ValueError) as exc:
        parser.error(str(exc))
    args.output.write_text(body, encoding="utf-8", newline="\n")


if __name__ == "__main__":
    main()
