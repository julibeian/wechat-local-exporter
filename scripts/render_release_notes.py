"""Render the one-installer GitHub release page for one exact tag."""

from __future__ import annotations

import argparse
import re
from pathlib import Path


PROJECT_URL = "https://github.com/julibeian/wechat-txt-pdf-exporter"
RELEASES_URL = f"{PROJECT_URL}/releases"

RELEASE_DISCLOSURE = """## 使用前

这是个人项目，请先用少量记录试用。聊天内容在本机处理，不上传，也不修改微信；朋友圈媒体和主动开启的媒体补全可能联网。安装包没有商业代码签名，Windows 可能提示“未知发布者”，请只从本项目下载。
"""


def render_release_notes(
    notes: str,
    tag: str,
    asset_names: list[str] | None = None,
    *,
    installed_exe_sha256: str,
) -> str:
    if not re.fullmatch(r"v\d+\.\d+\.\d+", tag, flags=re.ASCII):
        raise ValueError(f"Expected a release tag such as v1.4.0, got {tag!r}")

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
    available = set(asset_names if asset_names is not None else [installer])
    if installer not in available:
        raise ValueError(f"Recommended installer is missing: {installer}")
    if not re.fullmatch(r"[a-fA-F0-9]{64}", installed_exe_sha256, flags=re.ASCII):
        raise ValueError("Expected the SHA-256 of the installed executable")
    download_url = f"{RELEASES_URL}/download/{tag}/{installer}"
    return "\n".join([
        f"[下载最新版本]({RELEASES_URL}/latest) · [查看全部历史版本]({RELEASES_URL})",
        "",
        "## 下载",
        "",
        f"**[下载 {tag} 一键安装包]({download_url})**",
        "",
        "Windows 10/11 x64，无需安装 Python。",
        "",
        "## 更新内容",
        "",
        sections[0],
        "",
        RELEASE_DISCLOSURE.rstrip(),
        "",
        f"觉得好用，欢迎点个 [Star]({PROJECT_URL})。",
        "",
        f"<!-- wechat-exporter-target-sha256:{installed_exe_sha256.lower()} -->",
        "",
    ])


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tag", required=True, help="Release tag, supplied by GITHUB_REF_NAME")
    parser.add_argument("--notes", type=Path, default=Path("RELEASE_NOTES.md"))
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--asset", action="append", help="Existing asset name; repeat for each asset")
    parser.add_argument(
        "--installed-exe-sha256",
        required=True,
        help="SHA-256 of the executable embedded in the installer",
    )
    args = parser.parse_args()
    try:
        body = render_release_notes(
            args.notes.read_text(encoding="utf-8-sig"),
            args.tag,
            args.asset,
            installed_exe_sha256=args.installed_exe_sha256,
        )
    except (OSError, ValueError) as exc:
        parser.error(str(exc))
    args.output.write_text(body, encoding="utf-8", newline="\n")


if __name__ == "__main__":
    main()
