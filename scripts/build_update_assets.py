"""Prepare full executable and verified delta assets for a GitHub release."""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

from wechat_exporter.delta_update import apply_delta, build_delta, file_sha256
from wechat_exporter.update import GitHubSource, version_tuple


def prepare_assets(executable: Path, version: str, output: Path, *, base_executable: Path | None = None,
                   base_version: str | None = None, fetch_previous: bool = False) -> list[Path]:
    version_tuple(version)
    output.mkdir(parents=True, exist_ok=True)
    full = output / f"WeChat-Local-Exporter-Update-v{version}.exe"
    if full.resolve() != executable.resolve():
        shutil.copy2(executable, full)
    assets = [full]
    with tempfile.TemporaryDirectory(prefix="update-assets-", dir=output) as temporary:
        scratch = Path(temporary)
        if base_executable is None and fetch_previous:
            source = GitHubSource(download_root=scratch / "downloads")
            releases = [release for release in source.releases() if version_tuple(release.version) < version_tuple(version)]
            previous = max(releases, key=lambda release: version_tuple(release.version), default=None)
            if previous is not None:
                base_version = previous.version
                if previous.asset("update") is not None:
                    base_executable = source.download(previous, "update").path
                elif os.name == "nt" and os.environ.get("CI", "").lower() in {"true", "1", "yes"}:
                    installer = previous.asset("installer")
                    if installer is not None and installer.sha256:
                        downloaded = source._download_payload(installer, "installer", previous.target_sha256,
                                                              version=previous.version)
                        installation = scratch / "previous-installed"
                        subprocess.run([str(downloaded.path), "/VERYSILENT", "/SUPPRESSMSGBOXES", "/SP-",
                                        "/NORESTART", "/NOICONS", "/NOCLOSEAPPLICATIONS", f"/DIR={installation}"],
                                       check=True, timeout=180, creationflags=subprocess.CREATE_NO_WINDOW)
                        base_executable = installation / "WeChat-TXT-PDF-Exporter.exe"
                        if previous.target_sha256 and file_sha256(base_executable) != previous.target_sha256:
                            raise ValueError("Previous installed executable checksum does not match its release")
        if base_executable is not None:
            if base_version is None:
                raise ValueError("A base executable requires its version")
            version_tuple(base_version)
            source_hash = file_sha256(base_executable)
            patch = output / f"WeChat-Local-Exporter-Delta-v{base_version}-to-v{version}-{source_hash[:12]}.zip"
            manifest = build_delta(base_executable, executable, patch, source_version=base_version, target_version=version)
            apply_delta(base_executable, patch, scratch / "verified.exe", target_sha256=manifest["target_sha256"],
                        source_version=base_version, target_version=version)
            if patch.stat().st_size < full.stat().st_size * .9:
                assets.append(patch)
            else:
                patch.unlink()
    return assets


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--executable", type=Path, required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--base-executable", type=Path)
    parser.add_argument("--base-version")
    parser.add_argument("--fetch-previous", action="store_true")
    args = parser.parse_args()
    assets = prepare_assets(args.executable, args.version, args.output_dir, base_executable=args.base_executable,
                            base_version=args.base_version, fetch_previous=args.fetch_previous)
    print(json.dumps([str(path) for path in assets]))


if __name__ == "__main__":
    main()
