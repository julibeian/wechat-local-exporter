"""Run the frozen updater against synthetic EXEs in a disposable directory.

No real WeChat process, installed exporter, registry, or account data is touched.
Usage: python scripts/verify_packaged_update.py dist/<portable.exe>
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time


def main() -> None:
    package = Path(sys.argv[1]).resolve()
    temporary_root = Path("tmp").resolve()
    temporary_root.mkdir(parents=True, exist_ok=True)
    root = Path(tempfile.mkdtemp(prefix="packaged-update-", dir=temporary_root))
    compiler = Path(os.environ["WINDIR"]) / "Microsoft.NET/Framework64/v4.0.30319/csc.exe"
    if not compiler.is_file():
        raise RuntimeError("C# compiler is required for synthetic EXE smoke test")
    app = root / "app with spaces"
    transaction = root / "update-transaction"
    app.mkdir()
    transaction.mkdir()
    target, payload = app / "WeChat-TXT-PDF-Exporter.exe", transaction / "payload.exe"
    for label, executable in (("old", target), ("new", payload)):
        code = root / f"{label}.cs"
        code.write_text('using System; using System.IO; class Program { static void Main() { '
                        f'File.WriteAllText(Path.Combine(AppDomain.CurrentDomain.BaseDirectory, "started.txt"), "{label}");'
                        ' } }', encoding="utf-8")
        subprocess.run([str(compiler), "/nologo", "/target:winexe", f"/out:{executable}", str(code)], check=True)
    helper = transaction / "update-runner.exe"
    shutil.copy2(package, helper)
    env = os.environ.copy()
    env["PYINSTALLER_RESET_ENVIRONMENT"] = "1"
    env["LOCALAPPDATA"] = str(root / "local")
    hidden = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    parent = subprocess.Popen([sys.executable, "-c", "input()"], stdin=subprocess.PIPE,
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=hidden)
    payload_sha256 = hashlib.sha256(payload.read_bytes()).hexdigest()
    plan = dict(
        payload=str(payload),
        sha256=payload_sha256,
        target_sha256=payload_sha256,
        target=str(target),
        kind="portable",
        version="1.4.0",
        parent_pid=parent.pid,
    )
    plan_path = transaction / "plan.json"
    plan_path.write_text(json.dumps(plan), encoding="utf-8")
    original = target.read_bytes()
    worker = subprocess.Popen([str(helper), "--apply-update", str(plan_path)], env=env, creationflags=hidden)
    try:
        deadline = time.monotonic() + 45
        while not (transaction / "ready").exists():
            if worker.poll() is not None or time.monotonic() > deadline:
                raise RuntimeError("Frozen helper failed to signal ready")
            time.sleep(0.1)
        assert target.read_bytes() == original, "Updated before parent exit"
        parent.communicate(b"\n", timeout=5)
        if worker.wait(timeout=45) != 0:
            raise RuntimeError("Frozen helper did not finish successfully")
        deadline = time.monotonic() + 10
        while not (app / "started.txt").exists() and time.monotonic() < deadline:
            time.sleep(0.1)
        assert target.read_bytes() == payload.read_bytes()
        assert (app / "started.txt").read_text() == "new"
        assert (transaction / "previous.exe").read_bytes() == original
        (root / "result.json").write_text(json.dumps({"status": "passed", "parent_exit_gate": True,
            "portable_replacement": True, "restart": True, "backup": True}), encoding="utf-8")
        print(root / "result.json")
    finally:
        if parent.poll() is None:
            parent.terminate()
        if worker.poll() is None:
            (transaction / "abort").touch()
            subprocess.run(["taskkill", "/PID", str(worker.pid), "/T", "/F"], capture_output=True, creationflags=hidden)


if __name__ == "__main__":
    main()
