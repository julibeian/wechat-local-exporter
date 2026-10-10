"""Verified binary delta files using only the Python standard library."""
from __future__ import annotations

import hashlib
import json
import random
import tempfile
import zipfile
from pathlib import Path


MAX_FILE = 2 * 1024**3
MAX_MANIFEST = 2 * 1024**2
_MASK64 = (1 << 64) - 1
_GEAR = tuple(random.Random(0x574543484154 + index).getrandbits(64) for index in range(256))


class DeltaError(ValueError):
    pass


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024**2), b""):
            digest.update(block)
    return digest.hexdigest()


def _chunks(path: Path):
    """Content boundaries survive insertions before otherwise unchanged data."""
    carry = b""
    offset = 0
    rolling = 0
    with path.open("rb") as stream:
        while incoming := stream.read(4 * 1024**2):
            buffer = carry + incoming
            start = len(carry)
            chunk_start = 0
            for position in range(start, len(buffer)):
                rolling = ((rolling << 1) + _GEAR[buffer[position]]) & _MASK64
                length = position + 1 - chunk_start
                if length >= 8 * 1024 and (not rolling & 0x7FFF or length >= 128 * 1024):
                    data = buffer[chunk_start:position + 1]
                    yield offset, data
                    offset += len(data)
                    chunk_start = position + 1
                    rolling = 0
            carry = buffer[chunk_start:]
        if carry:
            yield offset, carry


def build_delta(source: Path, target: Path, destination: Path, *, source_version: str, target_version: str) -> dict:
    source, target, destination = map(Path, (source, target, destination))
    if any(path.stat().st_size <= 0 or path.stat().st_size > MAX_FILE for path in (source, target)):
        raise DeltaError("差异更新文件大小无效。")
    if destination.resolve() in {source.resolve(), target.resolve()}:
        raise DeltaError("差异包不能覆盖输入文件。")
    index = {}
    for offset, data in _chunks(source):
        index.setdefault(hashlib.sha256(data).digest(), (offset, len(data)))
    destination.parent.mkdir(parents=True, exist_ok=True)
    operations = []
    literal_size = 0
    with tempfile.TemporaryDirectory(prefix="delta-build-", dir=destination.parent) as scratch:
        literals = Path(scratch) / "data.bin"
        with literals.open("wb") as stream:
            for _offset, data in _chunks(target):
                match = index.get(hashlib.sha256(data).digest())
                if match:
                    operation = ["copy", *match]
                else:
                    operation = ["data", literal_size, len(data)]
                    stream.write(data)
                    literal_size += len(data)
                if operations and operations[-1][0] == operation[0] and operations[-1][1] + operations[-1][2] == operation[1]:
                    operations[-1][2] += operation[2]
                else:
                    operations.append(operation)
        manifest = {
            "format": "wechat-executable-delta-v1",
            "source_version": source_version, "target_version": target_version,
            "source_sha256": file_sha256(source), "target_sha256": file_sha256(target),
            "source_size": source.stat().st_size, "target_size": target.stat().st_size,
            "operations": operations,
        }
        with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
            archive.writestr("patch.json", json.dumps(manifest, separators=(",", ":")).encode())
            archive.write(literals, "data.bin")
    return manifest | {"patch_size": destination.stat().st_size}


def _copy_bytes(source, target, count, cancelled):
    while count:
        if cancelled is not None and cancelled.is_set():
            raise DeltaError("已取消差异更新。")
        block = source.read(min(count, 1024**2))
        if not block:
            raise DeltaError("差异更新数据不完整。")
        target.write(block)
        count -= len(block)


def apply_delta(source: Path, patch: Path, destination: Path, *, target_sha256: str,
                source_version: str | None = None, target_version: str | None = None, cancelled=None) -> Path:
    source, patch, destination = map(Path, (source, patch, destination))
    if destination.resolve() in {source.resolve(), patch.resolve()} or destination.exists():
        raise DeltaError("差异更新输出位置无效。")
    partial = destination.with_name(destination.name + ".part")
    if partial.exists():
        raise DeltaError("差异更新输出位置已被使用。")
    try:
        with zipfile.ZipFile(patch) as archive:
            if sorted(archive.namelist()) != ["data.bin", "patch.json"]:
                raise DeltaError("差异包文件列表无效。")
            if archive.getinfo("patch.json").file_size > MAX_MANIFEST:
                raise DeltaError("差异包说明过大。")
            manifest = json.loads(archive.read("patch.json"))
            if not isinstance(manifest, dict) or manifest.get("format") != "wechat-executable-delta-v1":
                raise DeltaError("差异包格式无效。")
            for field in ("source_size", "target_size"):
                if type(manifest.get(field)) is not int or not 0 < manifest[field] <= MAX_FILE:
                    raise DeltaError("差异包文件大小无效。")
            if manifest.get("target_sha256") != target_sha256:
                raise DeltaError("差异包目标校验值不匹配。")
            if ((source_version is not None and manifest.get("source_version") != source_version)
                    or (target_version is not None and manifest.get("target_version") != target_version)):
                raise DeltaError("差异包版本不匹配。")
            if source.stat().st_size != manifest["source_size"] or file_sha256(source) != manifest.get("source_sha256"):
                raise DeltaError("当前程序与差异包基础版本不匹配。")
            literal_size = archive.getinfo("data.bin").file_size
            if literal_size > manifest["target_size"]:
                raise DeltaError("差异包数据大小无效。")
            operations = manifest.get("operations")
            if not isinstance(operations, list) or not operations or len(operations) > 65536:
                raise DeltaError("差异包操作列表无效。")
            total = literal_offset = 0
            for operation in operations:
                if (not isinstance(operation, list) or len(operation) != 3
                        or not isinstance(operation[0], str) or operation[0] not in {"copy", "data"}
                        or any(type(value) is not int for value in operation[1:])):
                    raise DeltaError("差异包操作无效。")
                kind, offset, count = operation
                limit = manifest["source_size"] if kind == "copy" else literal_size
                if offset < 0 or count <= 0 or offset + count > limit:
                    raise DeltaError("差异包操作超出文件范围。")
                if kind == "data":
                    if offset != literal_offset:
                        raise DeltaError("差异包数据顺序无效。")
                    literal_offset += count
                total += count
                if total > manifest["target_size"]:
                    raise DeltaError("差异包输出超出文件大小。")
            if total != manifest["target_size"] or literal_offset != literal_size:
                raise DeltaError("差异包输出大小不匹配。")
            destination.parent.mkdir(parents=True, exist_ok=True)
            with source.open("rb") as original, archive.open("data.bin") as literals, partial.open("xb") as output:
                for kind, offset, count in operations:
                    if kind == "copy":
                        original.seek(offset)
                        _copy_bytes(original, output, count, cancelled)
                    else:
                        _copy_bytes(literals, output, count, cancelled)
        if file_sha256(partial) != target_sha256:
            raise DeltaError("差异更新结果 SHA256 校验失败。")
        partial.replace(destination)
        return destination
    except (OSError, ValueError, zipfile.BadZipFile, RuntimeError) as error:
        partial.unlink(missing_ok=True)
        if isinstance(error, DeltaError):
            raise
        raise DeltaError("差异包无法应用，需使用完整更新。") from error
