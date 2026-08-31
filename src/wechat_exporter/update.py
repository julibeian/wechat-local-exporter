"""Optional update transport/policy. No chat data or credentials enter this module."""
from __future__ import annotations

import hashlib
import hmac
import json
import re
import shutil
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol
from urllib.parse import urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener

from . import PROJECT_URL, __version__
from .config import LocalConfig, app_data_dir
from .errors import UserFacingError


CHECK_INTERVAL = 24 * 60 * 60
MAX_METADATA = 2 * 1024 * 1024
MAX_DOWNLOAD = 2 * 1024**3
_VERSION = re.compile(r"v?(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)\Z")
_SHA256 = re.compile(r"[a-fA-F0-9]{64}\Z")
_HOSTS = {"api.github.com", "github.com", "release-assets.githubusercontent.com",
          "objects.githubusercontent.com"}


def version_tuple(value: str) -> tuple[int, int, int]:
    match = _VERSION.fullmatch(value)
    if match is None:
        raise ValueError("版本号格式无效")
    return tuple(int(v) for v in match.groups())


def normalize_version(value: str) -> str:
    return ".".join(map(str, version_tuple(value)))


def _https_url(url: str) -> str:
    parsed = urlparse(url)
    if (parsed.scheme != "https" or parsed.hostname not in _HOSTS or parsed.username
            or parsed.password or parsed.port not in (None, 443)):
        raise UserFacingError("更新源地址未通过安全检查。")
    return url


class _SafeRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        _https_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def open_url(url: str, *, timeout: float = 8):
    request = Request(_https_url(url), headers={
        "User-Agent": f"WeChatChatExporter/{__version__}",
        "Accept": "application/vnd.github+json" if "api.github.com/" in url else "application/octet-stream",
    })
    return build_opener(_SafeRedirect()).open(request, timeout=timeout)


def _read_bounded(url: str, opener, limit: int = MAX_METADATA) -> bytes:
    with opener(url, timeout=8) as response:
        data = response.read(limit + 1)
    if len(data) > limit:
        raise ValueError("更新信息过大")
    return data


@dataclass(frozen=True)
class Asset:
    name: str
    url: str
    size: int


@dataclass(frozen=True)
class Release:
    version: str
    published_at: str
    notes: str
    assets: tuple[Asset, ...] = ()

    def asset(self, kind: str) -> Asset | None:
        if kind not in {"installer", "portable", "checksum"}:
            raise ValueError("未知更新类型")
        name = (f"SHA256SUMS-v{self.version}.txt" if kind == "checksum" else
                f"WeChat-TXT-PDF-Exporter-{'Installer-' if kind == 'installer' else ''}v{self.version}.exe")
        matches = [a for a in self.assets if a.name == name]
        return matches[0] if len(matches) == 1 else None


class UpdateSource(Protocol):
    def releases(self) -> tuple[Release, ...]: ...
    def download(self, release: Release, kind: str, *, progress=None,
                 cancelled: threading.Event | None = None) -> "DownloadedUpdate": ...


@dataclass(frozen=True)
class DownloadedUpdate:
    path: Path
    sha256: str
    version: str
    kind: str


class GitHubSource:
    def __init__(self, *, opener=open_url, download_root: Path | None = None):
        self.opener = opener
        self.download_root = download_root if download_root is not None else app_data_dir() / "updates"

    def releases(self) -> tuple[Release, ...]:
        url = "https://api.github.com/repos/julibeian/wechat-txt-pdf-exporter/releases?per_page=30"
        raw = json.loads(_read_bounded(url, self.opener))
        if not isinstance(raw, list):
            raise ValueError("更新服务未返回版本列表")
        result = []
        for entry in raw:
            if not isinstance(entry, dict):
                raise ValueError("版本信息格式无效")
            if entry.get("draft") or entry.get("prerelease"):
                continue
            tag = entry.get("tag_name", "")
            if not isinstance(tag, str) or not _VERSION.fullmatch(tag):
                continue
            version = normalize_version(tag)
            notes, published = entry.get("body") or "", entry.get("published_at") or ""
            if not isinstance(notes, str) or not isinstance(published, str):
                raise ValueError("版本说明格式无效")
            assets = []
            for a in entry.get("assets", []):
                if not isinstance(a, dict):
                    raise ValueError("更新文件信息无效")
                name, download_url, size = a.get("name"), a.get("browser_download_url"), a.get("size")
                if not isinstance(name, str) or not isinstance(download_url, str) or type(size) is not int:
                    raise ValueError("更新文件信息无效")
                # Only release files in this repository; remote names never become arbitrary paths.
                if (download_url != f"{PROJECT_URL}/releases/download/{tag}/{name}"
                        or Path(name).name != name or "/" in name or "\\" in name
                        or not 0 < size <= MAX_DOWNLOAD):
                    continue
                _https_url(download_url)
                assets.append(Asset(name, download_url, size))
            result.append(Release(version, published[:10], notes[:100_000], tuple(assets)))
        if not result:
            raise ValueError("未取得正式版本信息")
        return tuple(sorted(result, key=lambda r: version_tuple(r.version), reverse=True))

    def download(self, release: Release, kind: str, *, progress=None,
                 cancelled: threading.Event | None = None) -> DownloadedUpdate:
        asset, checksum = release.asset(kind), release.asset("checksum")
        if asset is None or checksum is None:
            raise UserFacingError("该版本缺少安装文件或 SHA256 清单，暂不能安全更新。")
        directory = None
        try:
            manifest = _read_bounded(checksum.url, self.opener, 64 * 1024).decode("utf-8-sig")
            expected = checksum_for(manifest, asset.name)
            self.download_root.mkdir(parents=True, exist_ok=True)
            directory = Path(tempfile.mkdtemp(prefix="update-", dir=self.download_root))
            partial = directory / (asset.name + ".part")
            destination = directory / asset.name
            digest = hashlib.sha256()
            received = 0
            deadline = time.monotonic() + 15 * 60
            with self.opener(asset.url, timeout=20) as response, partial.open("xb") as stream:
                while True:
                    if cancelled is not None and cancelled.is_set():
                        raise UserFacingError("已取消下载，当前版本不受影响。")
                    if time.monotonic() > deadline:
                        raise TimeoutError()
                    block = response.read(1024 * 1024)
                    if not block:
                        break
                    received += len(block)
                    if received > min(asset.size, MAX_DOWNLOAD):
                        raise ValueError("更新文件大小异常")
                    stream.write(block)
                    digest.update(block)
                    if progress:
                        progress(received, asset.size)
            if received != asset.size or not hmac.compare_digest(digest.hexdigest(), expected):
                raise UserFacingError("SHA256 校验失败，已丢弃下载文件。当前版本不受影响。")
            partial.replace(destination)
            return DownloadedUpdate(destination, expected, release.version, kind)
        except Exception as error:
            if directory is not None:
                shutil.rmtree(directory, ignore_errors=True)
            if isinstance(error, UserFacingError):
                raise
            raise UserFacingError("更新下载未完成，请检查网络或可用磁盘空间后重试。当前版本不受影响。") from None


def checksum_for(manifest: str, filename: str) -> str:
    matches = []
    for line in manifest.splitlines():
        parts = line.strip().split(maxsplit=1)
        if len(parts) == 2 and parts[1].lstrip("*") == filename:
            if not _SHA256.fullmatch(parts[0]):
                raise UserFacingError("更新 SHA256 清单格式无效。")
            matches.append(parts[0].lower())
    if len(matches) != 1:
        raise UserFacingError("更新 SHA256 清单缺失或重复，已停止更新。")
    return matches[0]


@dataclass(frozen=True)
class CheckResult:
    status: str  # latest / available / unavailable
    latest_version: str = ""
    releases: tuple[Release, ...] = ()

    @property
    def text(self) -> str:
        if self.status == "available":
            return f"● 新版本 v{self.latest_version}"
        if self.status == "latest":
            return "✓ 已是最新版本"
        return "暂时无法检查更新"


class UpdateManager:
    def __init__(self, config: LocalConfig, sources: tuple[UpdateSource, ...] | None = None,
                 *, current_version: str = __version__, now=time.time):
        self.config, self.current_version, self.now = config, current_version, now
        self.sources = sources if sources is not None else (GitHubSource(),)
        self.result = self.cached_result()
        self.source: UpdateSource | None = None
        self._lock = threading.Lock()

    def cached_result(self) -> CheckResult:
        latest = self.config.get("latest_version", "")
        status = self.config.get("update_status", "unavailable")
        try:
            status = ("available" if version_tuple(latest) > version_tuple(self.current_version) else "latest") if status != "unavailable" else status
        except ValueError:
            return CheckResult("unavailable")
        return CheckResult(status, latest)

    def check(self, *, automatic: bool = True) -> CheckResult:
        with self._lock:
            now = self.now()
            last = self.config.get("last_update_check", 0)
            if automatic and last and now - last < CHECK_INTERVAL:
                return self.result
            # Record attempts, including timeouts; do not retry every startup offline.
            if automatic:
                if not self.config.reserve_auto_check(now, CHECK_INTERVAL):
                    self.result = self.cached_result()
                    return self.result
            else:
                self.config.set(last_update_check=float(now))
            for source in self.sources:
                try:
                    releases = source.releases()
                    latest = max(releases, key=lambda r: version_tuple(r.version)).version
                    status = "available" if version_tuple(latest) > version_tuple(self.current_version) else "latest"
                    self.source = source
                    self.result = CheckResult(status, latest, releases)
                    self.config.set(latest_version=latest, update_status=status)
                    return self.result
                except Exception:
                    continue
            self.config.set(update_status="unavailable")
            self.result = CheckResult("unavailable", releases=self.result.releases)
            return self.result

    def should_show_banner(self) -> bool:
        return (self.result.status == "available" and
                self.config.get("dismissed_version", "") != self.result.latest_version)

    def dismiss(self) -> None:
        if self.result.status == "available":
            self.config.set(dismissed_version=self.result.latest_version)
