"""Verify a real console EXE with redirected UTF-8 output and owned test data."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import tempfile


def verify_cli(executable: Path) -> None:
    executable = executable.resolve()
    with tempfile.TemporaryDirectory(prefix="wechat-cli-验证 ") as directory:
        root = Path(directory)
        environment = os.environ.copy()
        environment["LOCALAPPDATA"] = str(root / "isolated-settings")

        def run(*args, expected=0):
            result = subprocess.run([str(executable), *map(str, args)],
                capture_output=True, encoding="utf-8", check=False,
                env=environment, cwd=root, timeout=180,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            if result.returncode != expected:
                raise RuntimeError(f"CLI command {args[0]} exited {result.returncode}: {result.stderr}")
            return result.stdout

        if "conversations" not in run("--help"):
            raise RuntimeError("CLI help is missing export commands")
        version = json.loads(run("version"))
        if not version["success"] or not version["headless"]:
            raise RuntimeError("CLI version response is invalid")
        schema = json.loads(run("schema"))
        if "batch" not in schema["commands"]:
            raise RuntimeError("CLI batch schema is missing")
        invalid = json.loads(run("export", "--all", "--format", "invalid", "--output", root / "unused", expected=2))
        if invalid["success"] or invalid["error"]["code"] != "INVALID_ARGUMENT":
            raise RuntimeError("CLI did not reject invalid arguments with JSON")
        job = root / "invalid-job.json"
        job.write_text('{"version":1,"jobs":[{"task":"export","all":true,"format":"json","output":"unused"},{"task":"invalid"}]}', encoding="utf-8")
        rejected = json.loads(run("batch", "--job", job, "--quiet", expected=2))
        if rejected["success"] or (root / "unused").exists():
            raise RuntimeError("CLI wrote output for an invalid batch")
        receipt = json.loads(run("self-test", "--output", root / "合成数据 输出"))
        checks = receipt["checks"]
        if not receipt["success"] or checks["conversation_preview_faces"] != "skipped_headless":
            raise RuntimeError("CLI self-test attempted GUI work")
        for name in ("html_archive", "jsonl_package", "moments_archive", "image_key_cache", "emoticon_key_cache"):
            if checks[name] != "ok":
                raise RuntimeError(f"CLI export check failed: {name}")
        if not checks["json_count"] == checks["jsonl_count"] == checks["txt_count"] == checks["pdf_count"] == checks["html_count"] == 4:
            raise RuntimeError("CLI self-test message counts do not match")
        print(json.dumps({"status": "ok", "version": version["version"], "headless": True, "formats_verified": ["json", "txt", "pdf", "html", "jsonl_package", "moments"]}))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("executable", type=Path)
    verify_cli(parser.parse_args().executable)
