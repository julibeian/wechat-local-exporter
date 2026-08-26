"""Developer helper: find Weixin.dll code references to the MMV1 marker.

This is a static, read-only PE scan and has no third-party dependencies.
"""

from __future__ import annotations

import argparse
import bisect
import re
import struct
from dataclasses import dataclass
from pathlib import Path


RIP_RELATIVE_LEA = re.compile(rb"[HL]\x8d[\x05\x0d\x15\x1d\x25\x2d\x35\x3d].{4}", re.DOTALL)


@dataclass(frozen=True)
class Section:
    name: str
    virtual_address: int
    virtual_size: int
    raw_offset: int
    raw_size: int


def read_sections(data: bytes) -> list[Section]:
    pe_offset = struct.unpack_from("<I", data, 0x3C)[0]
    if data[pe_offset : pe_offset + 4] != b"PE\0\0":
        raise ValueError("not a PE file")
    coff = pe_offset + 4
    section_count = struct.unpack_from("<H", data, coff + 2)[0]
    optional_size = struct.unpack_from("<H", data, coff + 16)[0]
    section_table = coff + 20 + optional_size
    sections = []
    for index in range(section_count):
        offset = section_table + index * 40
        name = data[offset : offset + 8].rstrip(b"\0").decode("ascii", errors="replace")
        virtual_size, virtual_address, raw_size, raw_offset = struct.unpack_from(
            "<IIII", data, offset + 8
        )
        sections.append(Section(name, virtual_address, virtual_size, raw_offset, raw_size))
    return sections


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("dll", type=Path)
    args = parser.parse_args()
    data = args.dll.read_bytes()
    sections = read_sections(data)
    text = next(section for section in sections if section.name == ".text")
    pdata = next(section for section in sections if section.name == ".pdata")

    targets: set[int] = set()
    for section in sections:
        raw = data[section.raw_offset : section.raw_offset + section.raw_size]
        cursor = 0
        while True:
            cursor = raw.find(b"MMV1", cursor)
            if cursor < 0:
                break
            targets.add(section.virtual_address + cursor)
            cursor += 4

    function_ranges = []
    pdata_raw = data[pdata.raw_offset : pdata.raw_offset + pdata.raw_size]
    for offset in range(0, len(pdata_raw) - 11, 12):
        begin, end, _ = struct.unpack_from("<III", pdata_raw, offset)
        if begin < end:
            function_ranges.append((begin, end))
    function_ranges.sort()
    starts = [begin for begin, _ in function_ranges]

    text_raw = data[text.raw_offset : text.raw_offset + text.raw_size]
    print("MMV1 RVAs:", ", ".join(hex(value) for value in sorted(targets)), flush=True)
    for match in RIP_RELATIVE_LEA.finditer(text_raw):
        instruction_rva = text.virtual_address + match.start()
        displacement = struct.unpack("<i", match.group(0)[3:7])[0]
        target_rva = instruction_rva + 7 + displacement
        if target_rva not in targets:
            continue
        range_index = bisect.bisect_right(starts, instruction_rva) - 1
        function_start = None
        if range_index >= 0:
            begin, end = function_ranges[range_index]
            if begin <= instruction_rva < end:
                function_start = begin
        print(
            f"xref={instruction_rva:#x} function={function_start and hex(function_start)} "
            f"bytes={match.group(0).hex()}",
            flush=True,
        )


if __name__ == "__main__":
    main()
