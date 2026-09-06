from __future__ import annotations

import re
from dataclasses import dataclass
from types import MappingProxyType
from typing import Final, Mapping


# Only add labels whose WeChat meaning and Unicode presentation have been
# explicitly verified.  Generic bracketed text is user content too, so this
# table must never be expanded with fuzzy matching or guessed aliases.
NATIVE_FACE_MAP: Final[Mapping[str, str]] = MappingProxyType(
    {
        "[爱心]": "❤️",
    }
)

_NATIVE_FACE_PATTERN: Final[re.Pattern[str]] = re.compile(
    "|".join(re.escape(label) for label in NATIVE_FACE_MAP)
)


@dataclass(frozen=True, slots=True)
class FaceReplacement:
    """One verified native-face replacement in the original source text."""

    source: str
    display: str
    start: int
    end: int


@dataclass(frozen=True, slots=True)
class DisplayText:
    """User-facing text plus enough information to retain its source form."""

    text: str
    source_text: str
    replacements: tuple[FaceReplacement, ...] = ()

    @property
    def changed(self) -> bool:
        return bool(self.replacements)

    @property
    def raw_text(self) -> str | None:
        """Return source text only when normalization changed the display."""

        return self.source_text if self.changed else None


def normalize_wechat_faces(source_text: str) -> DisplayText:
    """Replace verified WeChat native-face labels with visible Unicode.

    Unknown bracketed labels and media placeholders deliberately remain
    untouched. Existing Unicode emoji also pass through unchanged.
    Replacement offsets refer to the original ``source_text``.
    """

    matches = tuple(_NATIVE_FACE_PATTERN.finditer(source_text))
    if not matches:
        return DisplayText(text=source_text, source_text=source_text)

    parts: list[str] = []
    replacements: list[FaceReplacement] = []
    cursor = 0
    for match in matches:
        label = match.group(0)
        display = NATIVE_FACE_MAP[label]
        parts.append(source_text[cursor : match.start()])
        parts.append(display)
        replacements.append(
            FaceReplacement(
                source=label,
                display=display,
                start=match.start(),
                end=match.end(),
            )
        )
        cursor = match.end()
    parts.append(source_text[cursor:])
    return DisplayText(
        text="".join(parts),
        source_text=source_text,
        replacements=tuple(replacements),
    )
