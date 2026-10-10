"""Original WeChat face artwork, shared by the desktop preview and HTML export.

Bracket labels remain the source text. Only labels with a verified local PNG
are drawn as images; Unicode emoji and unknown labels keep their own identity.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from types import MappingProxyType


NATIVE_FACE_ROOT = Path(__file__).with_name("html_assets") / "faces"
NATIVE_FACE_FILES = MappingProxyType(
    json.loads((NATIVE_FACE_ROOT / "map.json").read_text(encoding="utf-8"))
)
_PATTERN = re.compile("|".join(re.escape(label) for label in sorted(NATIVE_FACE_FILES, key=len, reverse=True)))


def native_face_runs(text: str) -> tuple[tuple[str, bool], ...]:
    runs: list[tuple[str, bool]] = []
    cursor = 0
    for match in _PATTERN.finditer(text):
        if match.start() > cursor:
            runs.append((text[cursor:match.start()], False))
        runs.append((match.group(), True))
        cursor = match.end()
    if cursor < len(text):
        runs.append((text[cursor:], False))
    return tuple(runs)


def native_face_path(label: str) -> Path | None:
    filename = NATIVE_FACE_FILES.get(label)
    return NATIVE_FACE_ROOT / filename if filename else None
