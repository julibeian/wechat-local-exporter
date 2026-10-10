"""Extract unchanged face PNGs and their labels from a local Weixin.dll.

This reads the embedded Qt resource tables, without loading or modifying the
DLL. The output retains Tencent's artwork and must keep its NOTICE.txt.
"""
from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import re
import struct
from xml.etree import ElementTree
import zlib


def extract(dll: Path, destination: Path) -> dict[str, str]:
    data = dll.read_bytes()
    names: dict[int, str] = {}
    for match in re.finditer(re.escape("emoji".encode("utf-16-be")), data):
        base = match.start() - 6
        position = base
        candidate: dict[int, str] = {}
        while position + 6 < len(data):
            length = struct.unpack_from(">H", data, position)[0]
            if not 0 < length <= 100:
                break
            name = data[position + 6:position + 6 + length * 2].decode("utf-16-be", errors="replace")
            if any(not 32 <= ord(character) <= 127 for character in name):
                break
            candidate[position - base] = name
            position += 6 + length * 2
        if "emojipanel-config.xml" in candidate.values():
            names = candidate
            break
    if not names:
        raise RuntimeError("Native emoji resource table was not found")
    root = data.index(bytes.fromhex("0000000000020000000100000001"), position, position + 64)
    nodes: list[tuple[str, int, int]] = []
    for index in range(len(names) + 1):
        node = root + 22 * index
        name_offset, flags = struct.unpack_from(">IH", data, node)
        if not flags & 2:
            nodes.append((names[name_offset], flags, struct.unpack_from(">I", data, node + 10)[0]))
    png_starts = [match.start() - 4 for match in re.finditer(
        re.escape(b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR"), data)]
    votes = Counter(start - offset for name, flags, offset in nodes
                    if name.endswith(".png") and not flags for start in png_starts)
    data_base, matched = votes.most_common(1)[0]
    if matched < 100:
        raise RuntimeError("Native emoji data offsets could not be verified")
    assets: dict[str, bytes] = {}
    for name, flags, offset in nodes:
        if Path(name).name != name:
            raise RuntimeError("Unexpected resource filename")
        position = data_base + offset
        size = struct.unpack_from(">I", data, position)[0]
        payload = data[position + 4:position + 4 + size]
        if flags & 1:
            payload = zlib.decompress(payload[4:])
        if name.endswith(".png") and not payload.startswith(b"\x89PNG\r\n\x1a\n"):
            raise RuntimeError(f"Invalid PNG resource: {name}")
        assets[name] = payload
    first = data.index("[微笑]\x00".encode())
    last = data.index("[右太极]\x00".encode(), first) + len("[右太极]".encode())
    labels = [item.decode() for item in re.findall(rb"\[[^\]\x00]+\]", data[first:last])]
    insertion = labels.index("[爱你]") + 1
    labels[insertion:insertion] = ["[NO]", "[OK]"]
    if len(labels) != 105:
        raise RuntimeError("Unexpected classic face label table")
    mapping = {label: f"expression_{index}.png" for index, label in enumerate(labels, 1)}
    for entry in ElementTree.fromstring(assets["newemoji-config.xml"]):
        filename = entry.findtext("fileName")
        for child in entry:
            if child.tag == "key" or child.tag.endswith("-value"):
                if child.text:
                    mapping[child.text] = str(filename)
    for label in ("[花]", "【花】", "【玫瑰】"):
        mapping[label] = mapping["[玫瑰]"]
    if any(filename not in assets for filename in mapping.values()):
        raise RuntimeError("A face label refers to an absent original resource")
    destination.mkdir(parents=True, exist_ok=True)
    for filename in sorted(set(mapping.values())):
        (destination / filename).write_bytes(assets[filename])
    (destination / "map.json").write_text(json.dumps(mapping, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    provenance = {"source": "local Weixin.dll embedded emoji resource",
                  "source_sha256": hashlib.sha256(data).hexdigest(),
                  "images": {filename: hashlib.sha256(assets[filename]).hexdigest()
                             for filename in sorted(set(mapping.values()))}}
    (destination / "provenance.json").write_text(json.dumps(provenance, indent=2) + "\n", encoding="utf-8")
    return mapping


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("dll", type=Path)
    parser.add_argument("destination", type=Path)
    args = parser.parse_args()
    result = extract(args.dll, args.destination)
    print(f"Verified {len(result)} labels and {len(set(result.values()))} original PNGs")
