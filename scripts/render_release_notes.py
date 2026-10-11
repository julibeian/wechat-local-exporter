"""Render the installer and optional CLI downloads for one exact release tag."""

from __future__ import annotations

import argparse
import re
from pathlib import Path


RELEASES_URL = "https://github.com/julibeian/wechat-local-exporter/releases"

RELEASE_DISCLOSURE = """## 使用前

- 这是个人项目，界面和导出兼容仍可能有问题。请先用少量记录试用，确认结果后再批量导出。
- 聊天内容在本机处理，不上传，也不修改微信。本机没有的记录无法补齐；朋友圈媒体和主动开启的媒体补全可能联网。
- 安装包没有商业代码签名，Windows 可能提示“未知发布者”。v1.3 及更早版本请手动安装一次 v1.6。
"""


def refresh_release_verification(body: str, installed_exe_sha256: str) -> str:
    """Refresh machine verification without rewriting the author's release text."""
    if not re.fullmatch(r"[a-fA-F0-9]{64}", installed_exe_sha256, flags=re.ASCII):
        raise ValueError("Expected the SHA-256 of the installed executable")
    marker = f"<!-- wechat-exporter-target-sha256:{installed_exe_sha256.lower()} -->"
    pattern = r"<!--\s*wechat-exporter-target-sha256:[a-fA-F0-9]{64}\s*-->"
    matches = list(re.finditer(pattern, body))
    if len(matches) > 1 or ("wechat-exporter-target-sha256" in body and not matches):
        raise ValueError("Existing release verification is malformed")
    if matches:
        return re.sub(pattern, marker, body)
    newline = "\r\n" if "\r\n" in body else "\n"
    return body + newline * (1 if body.endswith(newline) else 2) + marker + newline


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
    cli = next((name for name in (
        f"wechatexporterCLI_{tag}.zip",
        f"命令行_wechatexporterCLI_{tag}.zip",
        f"命令行-WeChat-Local-Exporter-CLI-{tag}.zip",
        f"WeChat-Local-Exporter-CLI-{tag}.zip",
        f"WeChat-Local-Exporter-CLI-Windows-x64-{tag}.zip",
    ) if name in available), "")
    return "\n".join([
        "## 下载",
        "",
        f"**[下载 {tag} 一键安装包]({download_url})**",
        "",
        "Windows 10/11 x64，无需安装 Python。",
        *(["", "> v1.3 及更早版本升级时，请手动安装一次 v1.6.1。"] if tag == "v1.6.1" else []),
        *([
            "",
            f"**[下载 {tag} CLI 免安装包（Agent / 批量导出）]({RELEASES_URL}/download/{tag}/{cli})**",
            "",
            "解压后在终端运行 `wechatexporter.exe`，支持 JSON 任务批量导出；首次连接仍需完成微信登录。",
            "",
            "[CLI 命令与任务文件说明](https://github.com/julibeian/wechat-local-exporter/blob/main/docs/agent-cli.md)",
        ] if cli in available else []),
        *([
            "",
            "**本次重点：离线 HTML 阅读 · 全文搜索与筛选 · 图文表情还原 · 打印 / 另存 PDF。**",
            "",
            "**HTML 归档请保留整个导出文件夹**，网页、消息数据和媒体需要一起移动或备份。",
            "",
            "**v1.5 / v1.3 升级提示：** 仓库更名后，旧版软件内可能提示有更新但无法下载安装包。请从上方链接**手动下载并覆盖安装 v1.6 一次**，后续更新使用 1.6 的软件内更新入口。",
        ] if tag == "v1.6.0" else []),
        "",
        "## 更新内容",
        "",
        sections[0],
        "",
        *([RELEASE_DISCLOSURE.rstrip(), ""] if tag != "v1.6.1" else []),
        f"<!-- wechat-exporter-target-sha256:{installed_exe_sha256.lower()} -->",
        "",
    ])


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tag", required=True, help="Release tag, supplied by GITHUB_REF_NAME")
    parser.add_argument("--notes", type=Path, default=Path("RELEASE_NOTES.md"))
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--existing-body", type=Path, help="Preserve an edited release and refresh only its hidden checksum")
    parser.add_argument("--asset", action="append", help="Existing asset name; repeat for each asset")
    parser.add_argument(
        "--installed-exe-sha256",
        required=True,
        help="SHA-256 of the executable embedded in the installer",
    )
    args = parser.parse_args()
    try:
        if args.existing_body:
            with args.existing_body.open(encoding="utf-8-sig", newline="") as stream:
                body = refresh_release_verification(stream.read(), args.installed_exe_sha256)
        else:
            body = render_release_notes(
                args.notes.read_text(encoding="utf-8-sig"),
                args.tag,
                args.asset,
                installed_exe_sha256=args.installed_exe_sha256,
            )
    except (OSError, ValueError) as exc:
        parser.error(str(exc))
    with args.output.open("w", encoding="utf-8", newline="") as stream:
        stream.write(body)


if __name__ == "__main__":
    main()
