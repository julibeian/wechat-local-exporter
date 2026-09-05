from __future__ import annotations

import hashlib
import io
import json
import threading
from urllib.error import HTTPError, URLError

import pytest

from wechat_exporter.config import LocalConfig
from wechat_exporter.errors import UserFacingError
from wechat_exporter.update import (
    CHECK_INTERVAL,
    GitHubSource,
    Release,
    UpdateManager,
    _https_url,
    checksum_for,
    version_tuple,
)


def release_json(
    version="1.4.0",
    installer_payload=b"new executable",
    installed_payload=b"installed executable",
):
    prefix = (
        "https://github.com/julibeian/wechat-local-exporter/"
        f"releases/download/v{version}/"
    )
    name = f"WeChat-TXT-PDF-Exporter-Installer-v{version}.exe"
    url = prefix + name
    target_hash = hashlib.sha256(installed_payload).hexdigest()
    entry = {
        "tag_name": f"v{version}",
        "draft": False,
        "prerelease": False,
        "published_at": "2026-09-01T00:00:00Z",
        "body": (
            "新增测试功能\n"
            f"<!-- wechat-exporter-target-sha256:{target_hash} -->"
        ),
        "assets": [
            {
                "name": name,
                "browser_download_url": url,
                "size": len(installer_payload),
                "state": "uploaded",
                "digest": f"sha256:{hashlib.sha256(installer_payload).hexdigest()}",
            }
        ],
    }
    return entry, {url: installer_payload}


def legacy_release_json(version="1.3.0"):
    prefix = (
        "https://github.com/julibeian/wechat-txt-pdf-exporter/"
        f"releases/download/v{version}/"
    )
    installer_name = f"WeChat-TXT-PDF-Exporter-Installer-v{version}.exe"
    portable_name = f"WeChat-TXT-PDF-Exporter-v{version}.exe"
    checksum_name = f"SHA256SUMS-v{version}.txt"
    installer_payload = b"legacy installer"
    portable_payload = b"legacy installed executable"
    manifest = (
        f"{hashlib.sha256(installer_payload).hexdigest()}  {installer_name}\n"
        f"{hashlib.sha256(portable_payload).hexdigest()}  {portable_name}\n"
    ).encode()
    blobs = {
        prefix + installer_name: installer_payload,
        prefix + portable_name: portable_payload,
        prefix + checksum_name: manifest,
    }
    entry = {
        "tag_name": f"v{version}",
        "draft": False,
        "prerelease": False,
        "published_at": "2026-08-31T00:00:00Z",
        "body": "旧版更新",
        "assets": [
            {
                "name": name,
                "browser_download_url": prefix + name,
                "size": len(blobs[prefix + name]),
                "state": "uploaded",
                "digest": None,
            }
            for name in (installer_name, portable_name, checksum_name)
        ],
    }
    return entry, blobs, installer_payload, portable_payload


class Transport:
    def __init__(self, entries=None, error=None):
        self.calls = []
        self.error = error
        entry, self.blobs = release_json()
        self.entries = [entry] if entries is None else entries

    def __call__(self, url, *, timeout):
        self.calls.append((url, timeout))
        if self.error:
            raise self.error
        payload = json.dumps(self.entries).encode() if "api.github.com" in url else self.blobs[url]
        return io.BytesIO(payload)


@pytest.mark.parametrize(
    "latest,expected",
    [
        ("1.4.0", "latest"),
        ("1.5.0", "latest"),
        ("1.2.0", "latest"),
        ("1.10.0", "available"),
        ("2.0.0", "available"),
    ],
)
def test_versions(tmp_path, latest, expected):
    entry, _ = release_json(latest)
    transport = Transport([entry])
    manager = UpdateManager(
        LocalConfig(tmp_path / "settings.json"),
        (GitHubSource(opener=transport),),
        now=lambda: 100_000,
    )
    result = manager.check()
    assert result.status == expected
    assert result.latest_version == latest
    assert "已是最新版本" in result.text if expected == "latest" else latest in result.text


@pytest.mark.parametrize(
    "error",
    [
        TimeoutError(),
        URLError("network unreachable"),
        HTTPError("https://api.github.com", 500, "failure", {}, None),
    ],
)
def test_network_failures_are_quiet_and_throttled_across_restarts(tmp_path, error):
    transport = Transport(error=error)
    path = tmp_path / "settings.json"
    first = UpdateManager(
        LocalConfig(path), (GitHubSource(opener=transport),), now=lambda: 100_000
    )
    assert first.check().text == "暂时无法检查更新"
    second = UpdateManager(
        LocalConfig(path), (GitHubSource(opener=transport),), now=lambda: 100_010
    )
    assert second.check().status == "unavailable"
    assert len(transport.calls) == 1
    second.check(automatic=False)
    assert len(transport.calls) == 2


@pytest.mark.parametrize(
    "raw",
    [
        {"message": "rate limit"},
        [],
        [None],
        [{"tag_name": "v1.4.0", "body": 123}],
    ],
)
def test_malformed_api_does_not_escape(tmp_path, raw):
    manager = UpdateManager(
        LocalConfig(tmp_path / "settings.json"),
        (GitHubSource(opener=Transport(raw)),),
    )
    assert manager.check().status == "unavailable"


def test_non_json_api(tmp_path):
    source = GitHubSource(opener=lambda *a, **kw: io.BytesIO(b"<html>offline</html>"))
    assert UpdateManager(LocalConfig(tmp_path / "s.json"), (source,)).check().status == "unavailable"


def test_banner_dismissal_persists_until_a_different_release(tmp_path):
    path = tmp_path / "s.json"
    entry, _ = release_json("1.6.0")
    source = GitHubSource(opener=Transport([entry]))
    manager = UpdateManager(LocalConfig(path), (source,), now=lambda: 100_000)
    manager.check()
    assert manager.should_show_banner()
    manager.dismiss()
    assert not manager.should_show_banner()
    restarted = UpdateManager(LocalConfig(path), (source,), now=lambda: 100_001)
    assert restarted.check().status == "available"
    assert not restarted.should_show_banner()
    entry, _ = release_json("1.7.0")
    restarted.sources = (GitHubSource(opener=Transport([entry])),)
    restarted.check(automatic=False)
    assert restarted.should_show_banner()


def test_shared_instances_and_24_hour_boundary(tmp_path):
    path = tmp_path / "s.json"
    transport = Transport()
    first = UpdateManager(
        LocalConfig(path), (GitHubSource(opener=transport),), now=lambda: 100_000
    )
    second = UpdateManager(LocalConfig(path), first.sources, now=lambda: 100_000)
    first.check()
    assert second.check().status == "latest"
    assert len(transport.calls) == 1
    third = UpdateManager(
        LocalConfig(path), first.sources, now=lambda: 100_000 + CHECK_INTERVAL
    )
    third.check()
    assert len(transport.calls) == 2


def test_source_fallback_and_prerelease_filter(tmp_path):
    beta, _ = release_json("9.0.0")
    beta["prerelease"] = True
    stable, _ = release_json("1.4.0")
    source = GitHubSource(opener=Transport([beta, stable]))
    manager = UpdateManager(
        LocalConfig(tmp_path / "s.json"),
        (GitHubSource(opener=Transport(error=TimeoutError())), source),
    )
    assert manager.check().latest_version == "1.4.0"
    assert manager.source is source


@pytest.mark.parametrize("bad", [False, True])
def test_one_installer_download_and_digest_failure_preserve_old_binary(tmp_path, bad):
    transport = Transport()
    source = GitHubSource(opener=transport, download_root=tmp_path / "updates")
    release = source.releases()[0]
    old = tmp_path / "old.exe"
    old.write_bytes(b"original")
    if bad:
        transport.blobs[release.asset("installer").url] = b"bad executable"
        with pytest.raises(UserFacingError, match="SHA256"):
            source.download(release, "installer")
        assert not list((tmp_path / "updates").iterdir())
    else:
        progress = []
        result = source.download(
            release, "installer", progress=lambda *values: progress.append(values)
        )
        assert result.path.read_bytes() == b"new executable"
        assert result.sha256 == hashlib.sha256(b"new executable").hexdigest()
        assert result.target_sha256 == hashlib.sha256(b"installed executable").hexdigest()
        assert progress[-1] == (14, 14)
    assert old.read_bytes() == b"original"


def test_installer_download_uses_hidden_installed_executable_hash(tmp_path):
    installer_payload = b"installer payload"
    installed_payload = b"separate installed executable"
    entry, blobs = release_json(
        installer_payload=installer_payload,
        installed_payload=installed_payload,
    )
    transport = Transport([entry])
    transport.blobs = blobs
    release = GitHubSource(opener=transport).releases()[0]
    result = GitHubSource(
        opener=transport, download_root=tmp_path / "updates"
    ).download(release, "installer")
    assert result.sha256 == hashlib.sha256(installer_payload).hexdigest()
    assert result.target_sha256 == hashlib.sha256(installed_payload).hexdigest()


def test_legacy_release_and_repository_url_still_work(tmp_path):
    entry, blobs, installer_payload, portable_payload = legacy_release_json()
    transport = Transport([entry])
    transport.blobs = blobs
    release = GitHubSource(opener=transport).releases()[0]
    assert transport.calls[0][0] == (
        "https://api.github.com/repos/julibeian/wechat-local-exporter/"
        "releases?per_page=30"
    )
    result = GitHubSource(
        opener=transport, download_root=tmp_path / "updates"
    ).download(release, "installer")
    assert result.sha256 == hashlib.sha256(installer_payload).hexdigest()
    assert result.target_sha256 == hashlib.sha256(portable_payload).hexdigest()


def test_missing_target_hash_rejects_single_asset_update(tmp_path):
    entry, blobs = release_json()
    entry["body"] = "没有隐藏主程序摘要"
    transport = Transport([entry])
    transport.blobs = blobs
    release = GitHubSource(opener=transport).releases()[0]
    with pytest.raises(UserFacingError, match="安装后程序"):
        GitHubSource(opener=transport, download_root=tmp_path / "updates").download(
            release, "installer"
        )


def test_download_cancel_and_missing_asset(tmp_path):
    source = GitHubSource(opener=Transport(), download_root=tmp_path / "updates")
    release = source.releases()[0]
    cancelled = threading.Event()
    cancelled.set()
    with pytest.raises(UserFacingError, match="取消"):
        source.download(release, "installer", cancelled=cancelled)
    assert not list((tmp_path / "updates").iterdir())
    with pytest.raises(UserFacingError, match="缺少"):
        source.download(Release("1.4.0", "", ""), "installer")


@pytest.mark.parametrize("manifest", ["", "bad  a.exe", ("a" * 64 + "  a.exe\n") * 2])
def test_checksum_missing_invalid_or_duplicate(manifest):
    with pytest.raises(UserFacingError):
        checksum_for(manifest, "a.exe")


@pytest.mark.parametrize(
    "url",
    [
        "http://github.com/a",
        "https://evil.test/a",
        "https://github.com.evil.test/a",
        "https://user:pass@github.com/a",
    ],
)
def test_unsafe_download_or_redirect_url(url):
    with pytest.raises(UserFacingError):
        _https_url(url)


@pytest.mark.parametrize("mutation", ["digest", "target", "duplicate-target"])
def test_malformed_release_hash_metadata_is_rejected(mutation):
    entry, _ = release_json()
    if mutation == "digest":
        entry["assets"][0]["digest"] = "sha256:not-a-hash"
    elif mutation == "target":
        entry["body"] = "<!-- wechat-exporter-target-sha256:not-a-hash -->"
    else:
        entry["body"] += "\n" + entry["body"].splitlines()[-1]
    with pytest.raises(ValueError):
        GitHubSource(opener=Transport([entry])).releases()


def test_non_uploaded_external_or_traversal_assets_are_ignored():
    entry, _ = release_json()
    entry["assets"][0]["state"] = "new"
    assert GitHubSource(opener=Transport([entry])).releases()[0].asset("installer") is None

    entry, _ = release_json()
    entry["assets"][0]["browser_download_url"] = "https://github.com/attacker/payload.exe"
    assert GitHubSource(opener=Transport([entry])).releases()[0].asset("installer") is None

    entry, _ = release_json()
    entry["assets"][0]["name"] = "../bad.exe"
    assert GitHubSource(opener=Transport([entry])).releases()[0].asset("installer") is None
    assert version_tuple("1.10.0") > version_tuple("1.9.0")
