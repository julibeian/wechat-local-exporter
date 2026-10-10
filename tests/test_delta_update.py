import hashlib
import io
import json
import random
import runpy
import threading
import zipfile
from pathlib import Path

import pytest

from wechat_exporter.delta_update import DeltaError, apply_delta, build_delta, file_sha256
from wechat_exporter.errors import UserFacingError
from wechat_exporter.update import Asset, GitHubSource, Release


@pytest.fixture
def binaries(tmp_path):
    data = random.Random(73).randbytes(768 * 1024)
    source, target = tmp_path / "old.exe", tmp_path / "new.exe"
    source.write_bytes(data)
    target.write_bytes(data[:100000] + b"new resources" * 30 + data[100000:420000] + b"changed" + data[420007:])
    patch = tmp_path / "delta.zip"
    manifest = build_delta(source, target, patch, source_version="1.6.0", target_version="1.7.0")
    return source, target, patch, manifest


def test_delta_reuses_existing_bytes_and_reconstructs_exact_executable(tmp_path, binaries):
    source, target, patch, manifest = binaries
    result = apply_delta(source, patch, tmp_path / "result.exe", target_sha256=file_sha256(target),
                         source_version="1.6.0", target_version="1.7.0")
    assert result.read_bytes() == target.read_bytes()
    assert patch.stat().st_size < target.stat().st_size / 3
    assert any(operation[0] == "copy" for operation in manifest["operations"])


def test_prepared_release_assets_work_with_installed_updater(tmp_path, binaries):
    source_path, target_path, _, _ = binaries
    prepare = runpy.run_path(str(Path(__file__).parents[1] / "scripts/build_update_assets.py"))["prepare_assets"]
    files = prepare(target_path, "1.7.0", tmp_path / "assets", base_executable=source_path, base_version="1.6.0")
    payloads = {path.name: path.read_bytes() for path in files}
    assets = tuple(Asset(path.name, path.name, path.stat().st_size, file_sha256(path)) for path in files)
    release = Release("1.7.0", "2026-10-10", "", assets, file_sha256(target_path))
    requested = []
    def opener(url, **kwargs):
        requested.append(url)
        return io.BytesIO(payloads[url])
    updater = GitHubSource(opener=opener, download_root=tmp_path / "downloads",
                           current_executable=source_path, current_version="1.6.0")
    result = updater.download(release, "installer")
    assert result.kind == "executable"
    assert result.path.read_bytes() == target_path.read_bytes()
    assert len(requested) == 1 and requested[0].endswith(".zip")


def test_wrong_base_is_rejected_without_modifying_program(tmp_path, binaries):
    source, target, patch, _ = binaries
    source.write_bytes(b"other build" + source.read_bytes()[11:])
    original = source.read_bytes()
    with pytest.raises(DeltaError, match="基础版本"):
        apply_delta(source, patch, tmp_path / "result.exe", target_sha256=file_sha256(target))
    assert source.read_bytes() == original
    assert not (tmp_path / "result.exe").exists()
    assert not (tmp_path / "result.exe.part").exists()


@pytest.mark.parametrize("damage", ["range", "size", "target", "traversal", "kind"])
def test_invalid_delta_operations_fail_before_any_replacement(tmp_path, binaries, damage):
    source, target, patch, manifest = binaries
    with zipfile.ZipFile(patch) as archive:
        literals = archive.read("data.bin")
    if damage == "range":
        manifest["operations"][0] = ["copy", source.stat().st_size + 1, 10]
    elif damage == "size":
        manifest["target_size"] += 1
    elif damage == "target":
        manifest["target_sha256"] = "0" * 64
    elif damage == "kind":
        manifest["operations"][0][0] = []
    broken = tmp_path / "broken.zip"
    with zipfile.ZipFile(broken, "w") as archive:
        archive.writestr("patch.json", json.dumps(manifest))
        archive.writestr("data.bin", literals)
        if damage == "traversal":
            archive.writestr("../outside.exe", b"no")
    with pytest.raises(DeltaError):
        apply_delta(source, broken, tmp_path / "result.exe", target_sha256=file_sha256(target))
    assert not (tmp_path / "result.exe").exists()
    assert not (tmp_path / "outside.exe").exists()


def test_final_hash_failure_removes_partial_output(tmp_path, binaries):
    source, _target, patch, manifest = binaries
    with zipfile.ZipFile(patch) as archive:
        literals = archive.read("data.bin")
    manifest["target_sha256"] = "0" * 64
    with zipfile.ZipFile(patch, "w") as archive:
        archive.writestr("patch.json", json.dumps(manifest))
        archive.writestr("data.bin", literals)
    with pytest.raises(DeltaError, match="结果 SHA256"):
        apply_delta(source, patch, tmp_path / "result.exe", target_sha256="0" * 64)
    assert not (tmp_path / "result.exe.part").exists()


def test_updater_downloads_only_delta_and_falls_back_to_full_on_failure(tmp_path, binaries):
    source_path, target, patch, manifest = binaries
    prefix = "https://github.com/julibeian/wechat-local-exporter/releases/download/v1.7.0/"
    delta_name = f"WeChat-Local-Exporter-Delta-v1.6.0-to-v1.7.0-{manifest['source_sha256'][:12]}.zip"
    full_name = "WeChat-Local-Exporter-Update-v1.7.0.exe"
    payloads = {prefix + delta_name: patch.read_bytes(), prefix + full_name: target.read_bytes()}
    assets = tuple(Asset(name, prefix + name, len(payloads[prefix + name]), hashlib.sha256(payloads[prefix + name]).hexdigest()) for name in (delta_name, full_name))
    release = Release("1.7.0", "", "", assets, manifest["target_sha256"])
    calls = []
    def opener(url, **_kwargs):
        calls.append(url)
        return io.BytesIO(payloads[url])
    source = GitHubSource(opener=opener, download_root=tmp_path / "downloads",
                          current_executable=source_path, current_version="1.6.0")
    downloaded = source.download(release, "installer")
    assert downloaded.kind == "executable" and downloaded.version == "1.7.0"
    assert downloaded.path.read_bytes() == target.read_bytes()
    assert calls == [prefix + delta_name]

    calls.clear()
    payloads[prefix + delta_name] = b"corrupt patch"
    downloaded = source.download(release, "installer")
    assert downloaded.path.read_bytes() == target.read_bytes()
    assert calls == [prefix + delta_name, prefix + full_name]

    calls.clear()
    source_path.write_bytes(b"different build")
    downloaded = source.download(release, "installer")
    assert calls == [prefix + full_name]


def test_cancelling_delta_does_not_start_a_full_download(tmp_path, binaries):
    source_path, _target, patch, manifest = binaries
    name = f"WeChat-Local-Exporter-Delta-v1.6.0-to-v1.7.0-{manifest['source_sha256'][:12]}.zip"
    asset = Asset(name, "https://github.com/julibeian/wechat-local-exporter/releases/download/v1.7.0/" + name,
                  patch.stat().st_size, file_sha256(patch))
    calls = []
    def opener(url, **_kwargs):
        calls.append(url)
        return io.BytesIO(patch.read_bytes())
    cancelled = threading.Event()
    cancelled.set()
    source = GitHubSource(opener=opener, download_root=tmp_path / "downloads", current_executable=source_path,
                          current_version="1.6.0")
    with pytest.raises(UserFacingError, match="取消"):
        source.download(Release("1.7.0", "", "", (asset,), manifest["target_sha256"]), "installer", cancelled=cancelled)
    assert calls == [asset.url]
    assert not list((tmp_path / "downloads").glob("update-*"))
