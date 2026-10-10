import io
import json
import struct

import pytest
from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from PIL import Image

from wechat_exporter.image_probe import LocalImageSizeProbe, hevc_picture_dimensions
from wechat_exporter.models import AccountLocation


def _ue(value):
    word = bin(value + 1)[2:]
    return "0" * (len(word) - 1) + word


def _sps(width=96, height=80, *, crop=False, bit_depth=8, sub_layers=0):
    # A small HEVC SPS metadata fixture, with standard PTL/chroma/crop fields.
    bits = "0000" + format(sub_layers, "03b") + "1" + "0" * 96
    if sub_layers:
        bits += "11" + "00" * (sub_layers - 1) + "00" * (8 - sub_layers)
        bits += "0" * 96
    bits += _ue(0) + _ue(1) + _ue(width) + _ue(height) + ("1" if crop else "0")
    if crop:
        bits += _ue(1) * 4
    bits += _ue(bit_depth - 8) * 2 + "1"
    bits += "0" * (-len(bits) % 8)
    payload = int(bits, 2).to_bytes(len(bits)//8, "big")
    # RBSP emulation prevention, as it appears in an Annex-B stream.
    escaped = bytearray()
    zeros = 0
    for byte in payload:
        if zeros >= 2 and byte <= 3:
            escaped.append(3)
            zeros = 0
        escaped.append(byte)
        zeros = zeros + 1 if byte == 0 else 0
    return b"wxgf" + b"\x00\x00\x00\x01\x42\x01" + bytes(escaped)


@pytest.mark.parametrize("data,expected", [
    (_sps(), (96, 80, 8)),
    (_sps(100, 84, crop=True), (96, 80, 8)),
    (_sps(3840, 2160, bit_depth=10, sub_layers=2), (3840, 2160, 10)),
    (b"wxgf\0\0\0\1\x42\1\0", None),
    (_sps(999999, 999999), None),
])
def test_header_metadata_handles_crop_emulation_prevention_and_truncation(data, expected):
    assert hevc_picture_dimensions(data) == expected


@pytest.mark.parametrize("version", ["plain", "v1", "v2"])
def test_hevc_size_counts_actual_converter_output_and_reuses_validated_cache(tmp_path, version, monkeypatch):
    # The raw-pixel bound was 32 MB; the real PNG is much smaller.
    raw = _sps(3840, 2160) + bytes(200)
    keys = (b"0123456789abcdef", 123)
    if version == "v1":
        data = b"\x07\x08\x05\x56\x02\x05" + bytes(16) + bytes(v ^ keys[1] for v in raw)
    elif version == "v2":
        aes_size = 32
        padder = padding.PKCS7(128).padder()
        padded = padder.update(raw[:aes_size]) + padder.finalize()
        encryptor = Cipher(algorithms.AES(keys[0]), modes.ECB()).encryptor()
        data = (b"\x07\x08\x56\x32\x08\x07" + struct.pack("<II", aes_size, 0) + b"\x01"
                + encryptor.update(padded) + encryptor.finalize() + raw[aes_size:])
    else:
        data = raw
    path = tmp_path / "picture.dat"
    path.write_bytes(data)
    account = AccountLocation(tmp_path, "self", "test")
    probe = LocalImageSizeProbe(account, "self", cache_root=tmp_path / "cache")
    probe._load_keys = lambda: keys
    png = io.BytesIO()
    Image.new("RGB", (3840, 2160), "red").save(png, format="PNG")
    converted = []
    def convert(payload):
        assert payload == raw
        converted.append(1)
        return png.getvalue()
    monkeypatch.setattr(probe._resolver, "_wxgf_to_png", convert)
    lower, upper = probe.estimate(path, len(data))
    assert lower == upper == len(png.getvalue()) < 100_000
    probe.flush()
    cached = LocalImageSizeProbe(account, "self", cache_root=tmp_path / "cache")
    monkeypatch.setattr(cached._resolver, "_wxgf_to_png", lambda *a: pytest.fail("cached image decoded again"))
    assert cached.measure(path).byte_count == lower
    assert converted == [1]
    # Cache stores only measurements and hashes, not image keys or message data.
    payload = json.loads(cached._path.read_text())
    assert keys[0].hex() not in cached._path.read_text()
    assert len(payload["images"]) == 1


def test_absent_key_has_explicit_uncertainty_and_cannot_acquire_live_keys(tmp_path, monkeypatch):
    import wechat_exporter.media as media
    monkeypatch.setattr(media, "discover_account_image_keys", lambda *a, **kw: pytest.fail("preview acquires keys"))
    path = tmp_path / "unknown.dat"
    path.write_bytes(b"\x07\x08\x56\x32\x08\x07" + bytes(100))
    probe = LocalImageSizeProbe(AccountLocation(tmp_path, "self", "test"), "self")
    probe._load_keys = lambda: None
    lower, upper = probe.estimate(path, path.stat().st_size)
    assert lower == upper == 0
    assert probe.measure(path) is None


def test_changed_source_and_corrupt_cache_are_remeasured_and_outside_paths_rejected(tmp_path):
    account = AccountLocation(tmp_path / "account", "self", "test")
    account.account_dir.mkdir()
    source = account.account_dir / "image.dat"
    image = io.BytesIO()
    Image.new("RGB", (30, 40), "red").save(image, format="PNG")
    source.write_bytes(image.getvalue())
    probe = LocalImageSizeProbe(account, "self", cache_root=tmp_path / "cache")
    first = probe.measure(source)
    probe.flush()
    image = io.BytesIO()
    Image.new("RGB", (90, 100), "blue").save(image, format="PNG")
    source.write_bytes(image.getvalue())
    fresh = LocalImageSizeProbe(account, "self", cache_root=tmp_path / "cache")
    second = fresh.measure(source)
    assert second.byte_count == len(image.getvalue()) and second.digest != first.digest
    fresh._path.write_text('{broken')
    repaired = LocalImageSizeProbe(account, "self", cache_root=tmp_path / "cache")
    assert repaired.measure(source) == second
    outside = tmp_path / "outside.png"
    outside.write_bytes(image.getvalue())
    assert repaired.measure(outside) is None
