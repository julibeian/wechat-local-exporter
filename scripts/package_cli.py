"""Package the verified console executable and Agent instructions."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import zipfile


ROOT = Path(__file__).resolve().parents[1]


def package_cli(executable: Path, version: str, output_dir: Path) -> Path:
    if not re.fullmatch(r"\d+\.\d+\.\d+", version, flags=re.ASCII):
        raise ValueError("Invalid version")
    if not executable.is_file():
        raise FileNotFoundError(executable)
    output_dir.mkdir(parents=True, exist_ok=True)
    destination = output_dir / f"WeChat-Local-Exporter-CLI-Windows-x64-v{version}.zip"
    sources = {
        "wechat-exporter.exe": executable,
        "README.md": ROOT / "docs" / "agent-cli.md",
        "example-job.json": ROOT / "docs" / "agent-cli-example.json",
        "LICENSE": ROOT / "LICENSE",
        "THIRD_PARTY_NOTICES.md": ROOT / "THIRD_PARTY_NOTICES.md",
    }
    checksums = []
    with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        for name, path in sources.items():
            archive.write(path, name)
            with path.open("rb") as stream:
                digest = hashlib.file_digest(stream, "sha256").hexdigest()
            checksums.append(f"{digest}  {name}")
        archive.writestr("SHA256SUMS.txt", "\n".join(checksums) + "\n")
    with destination.open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
    destination.with_suffix(".zip.sha256").write_text(f"{digest}  {destination.name}\n", encoding="utf-8")
    return destination


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--executable", type=Path, required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    destination = package_cli(args.executable, args.version, args.output_dir)
    print(json.dumps({"zip": str(destination.resolve())}, ensure_ascii=False))


if __name__ == "__main__":
    main()
