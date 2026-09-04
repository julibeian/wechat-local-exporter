from __future__ import annotations

import argparse

from .gui import main as gui_main
from .integrity import require_signature_integrity
from .windows import discover_accounts, list_wechat_processes, read_wechat_version


def main() -> None:
    require_signature_integrity()
    parser = argparse.ArgumentParser(description="微信聊天本地导出工具")
    subparsers = parser.add_subparsers(dest="command")
    subparsers.add_parser("gui", help="启动图形界面")
    subparsers.add_parser("diagnose", help="只读检测微信版本、进程和数据目录")
    args = parser.parse_args()
    if args.command in (None, "gui"):
        gui_main()
        return
    print(f"微信版本: {read_wechat_version() or '未运行'}")
    processes = list_wechat_processes()
    print(f"微信进程: {len(processes)} 个")
    accounts = discover_accounts()
    if not accounts:
        print("数据目录: 未自动发现")
    for account in accounts:
        print(f"数据目录: {account.account_dir} ({account.source})")


if __name__ == "__main__":
    main()
