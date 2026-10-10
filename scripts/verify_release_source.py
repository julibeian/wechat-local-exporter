"""Audit a release Git tree or source ZIP before publication."""
from __future__ import annotations

import argparse
import io
import json
from pathlib import Path, PurePosixPath
import re
import subprocess
import zipfile


PUBLIC_MARKDOWN = frozenset({
    "README.md", "RELEASE_NOTES.md", "THIRD_PARTY_NOTICES.md", "docs/agent-cli.md",
})
PUBLIC_ROOT_FILES = PUBLIC_MARKDOWN | {"LICENSE", "pyproject.toml", ".gitattributes", ".gitignore"}
PUBLIC_DIRECTORIES = frozenset({"src", "tests", "scripts", "tools", "assets", "packaging", ".github"})
PRIVATE_SUFFIXES = frozenset({
    ".db", ".sqlite", ".sqlite3", ".log", ".jsonl", ".csv", ".xlsx", ".pdf", ".docx",
    ".exe", ".zip", ".pem", ".pfx", ".key",
})
SENSITIVE_TEXT = (
    re.compile(r"(?i)[a-z]:[\\/]+Users[\\/]+(?!Public\b|Default\b|sample\b|example\b|test\b|甲\b)[^\\/\s\"']+"),
    re.compile(r"(?i)/(?:home|Users)/(?!(?:sample|example|test)/)[a-z0-9_.-]+/"),
    re.compile(r"\bwxid_[a-z0-9]{12,}(?:_[a-z0-9]{4,})?\b"),
    re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|sk-[A-Za-z0-9_-]{20,})\b"),
)


def audit_names(names: list[str]) -> None:
    for name in names:
        path = PurePosixPath(name)
        if path.is_absolute() or ".." in path.parts or "\\" in name:
            raise ValueError(f"Unsafe source path: {name}")
        if path.suffix.lower() == ".md" and name not in PUBLIC_MARKDOWN:
            raise ValueError(f"Internal document in source: {name}")
        if path.suffix.lower() in PRIVATE_SUFFIXES:
            raise ValueError(f"Private data or build artifact in source: {name}")
        if name in PUBLIC_ROOT_FILES or name == "docs/agent-cli-example.json":
            continue
        if not path.parts or path.parts[0] not in PUBLIC_DIRECTORIES:
            raise ValueError(f"Unreviewed source path: {name}")
        if any(part in {"__pycache__", ".env", ".git", ".codex", "node_modules"} for part in path.parts):
            raise ValueError(f"Local state in source: {name}")


def audit_files(files: dict[str, bytes]) -> dict:
    audit_names(list(files))
    for name, data in files.items():
        if PurePosixPath(name).suffix.lower() in {".png", ".ico"}:
            if b"\x00\x00\x00\x00IEND" not in data and name.endswith(".png"):
                raise ValueError(f"Unexpected image resource: {name}")
            continue
        try:
            content = data.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise ValueError(f"Unreviewed binary in source: {name}") from exc
        for number, line in enumerate(content.splitlines(), 1):
            if any(pattern.search(line) for pattern in SENSITIVE_TEXT):
                raise ValueError(f"Private identifier or credential at {name}:{number}")
    return {"status": "ok", "files": len(files), "markdown": sorted(set(files) & PUBLIC_MARKDOWN)}


def audit_zip(data: bytes) -> dict:
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        names = [item.filename for item in archive.infolist() if not item.is_dir()]
        if len(names) != len(set(names)):
            raise ValueError("Duplicate source archive entries")
        prefix = ""
        if names and all("/" in name for name in names):
            first = names[0].split("/", 1)[0]
            if first not in PUBLIC_DIRECTORIES and all(name.startswith(first + "/") for name in names):
                prefix = first + "/"
        return audit_files({name.removeprefix(prefix): archive.read(name) for name in names})


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--ref", help="Git commit or tag to audit")
    group.add_argument("--zip", type=Path, help="Existing source ZIP to audit")
    args = parser.parse_args()
    try:
        if args.ref:
            names = subprocess.check_output(["git", "ls-tree", "-rz", "--name-only", args.ref]).decode().split("\0")
            audit_names([name for name in names if name])
            data = subprocess.check_output(["git", "archive", "--format=zip", args.ref])
        else:
            data = args.zip.read_bytes()
        result = audit_zip(data)
    except (OSError, ValueError, zipfile.BadZipFile, subprocess.CalledProcessError) as exc:
        parser.exit(1, f"Source audit failed: {exc}\n")
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
