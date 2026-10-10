from __future__ import annotations

import argparse
import sys



def main(argv: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if not arguments or arguments[0] == "gui":
        parser = argparse.ArgumentParser(description="启动微信聊天本地导出工具")
        parser.add_argument("command", nargs="?", choices=("gui",))
        parser.parse_args(arguments)
        from .gui import main as gui_main

        gui_main()
        return 0
    from .agent_cli import main as agent_main

    return agent_main(arguments)


if __name__ == "__main__":
    raise SystemExit(main())
