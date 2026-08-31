from __future__ import annotations

import sys
from pathlib import Path


def _run() -> int:
    from wechat_exporter.integrity import require_signature_integrity

    require_signature_integrity()
    if len(sys.argv) == 3 and sys.argv[1] == "--apply-update":
        from wechat_exporter.updater import run_helper

        return run_helper(Path(sys.argv[2]))
    if len(sys.argv) >= 2 and sys.argv[1] in {
        "--self-test",
        "--self-test-offline",
    }:
        if len(sys.argv) != 3:
            return 2
        from wechat_exporter.selftest import run_packaged_self_test

        run_packaged_self_test(
            Path(sys.argv[2]),
            check_environment=sys.argv[1] == "--self-test",
        )
        return 0

    from wechat_exporter.gui import main

    main()
    return 0


if __name__ == "__main__":
    raise SystemExit(_run())
