from __future__ import annotations

import re
from dataclasses import dataclass
from types import MappingProxyType
from typing import Final, Mapping


# Plain-text exports use Unicode equivalents of named WeChat faces. These
# preserve the meaning, not Tencent's proprietary artwork. Only exact labels
# are replaced; unknown brackets and media placeholders remain untouched.
NATIVE_FACE_MAP: Final[Mapping[str, str]] = MappingProxyType(
    {
        "[爱心]": "❤️",
        "[微笑]": "🙂", "[撇嘴]": "😒", "[色]": "😍", "[发呆]": "😳",
        "[得意]": "😎", "[流泪]": "😢", "[害羞]": "☺️", "[闭嘴]": "🤐",
        "[睡]": "😴", "[大哭]": "😭", "[尴尬]": "😅", "[发怒]": "😡",
        "[调皮]": "😛", "[呲牙]": "😁", "[惊讶]": "😮", "[难过]": "🙁",
        "[囧]": "😖", "[抓狂]": "😫", "[吐]": "🤮", "[偷笑]": "🤭",
        "[愉快]": "😊", "[白眼]": "🙄", "[傲慢]": "😏", "[困]": "🥱",
        "[惊恐]": "😱", "[憨笑]": "😄", "[悠闲]": "😌", "[咒骂]": "🤬",
        "[疑问]": "❓", "[嘘]": "🤫", "[晕]": "😵", "[衰]": "😞",
        "[敲打]": "🤜", "[再见]": "👋", "[擦汗]": "😓", "[抠鼻]": "👃",
        "[鼓掌]": "👏", "[坏笑]": "😏", "[左哼哼]": "😤", "[右哼哼]": "😤",
        "[鄙视]": "👎", "[委屈]": "🥺", "[快哭了]": "🥹", "[阴险]": "😈",
        "[亲亲]": "😘", "[可怜]": "🥺", "[笑脸]": "😄", "[生病]": "😷",
        "[破涕为笑]": "😂", "[吐舌]": "😛", "[脸红]": "😳", "[恐惧]": "😨",
        "[失望]": "😞", "[无语]": "😑", "[嘿哈]": "🙌", "[捂脸]": "🤦",
        "[奸笑]": "😏", "[机智]": "💡", "[皱眉]": "😟", "[耶]": "✌️",
        "[吃瓜]": "🍉", "[加油]": "💪", "[汗]": "😓", "[天啊]": "😱",
        "[Emm]": "🤔", "[社会社会]": "🙏", "[旺柴]": "🐶", "[好的]": "👌",
        "[打脸]": "🤦", "[哇]": "🤩", "[翻白眼]": "🙄", "[666]": "🤙",
        "[让我看看]": "🧐", "[叹气]": "😮‍💨", "[苦涩]": "🥲", "[裂开]": "🫠",
        "[嘴唇]": "💋", "[心碎]": "💔", "[拥抱]": "🤗", "[强]": "👍",
        "[弱]": "👎", "[握手]": "🤝", "[胜利]": "✌️", "[抱拳]": "🙏",
        "[勾引]": "🫴", "[拳头]": "✊", "[OK]": "👌", "[合十]": "🙏",
        "[啤酒]": "🍺", "[咖啡]": "☕", "[蛋糕]": "🎂", "[玫瑰]": "🌹",
        "[凋谢]": "🥀", "[菜刀]": "🔪", "[炸弹]": "💣", "[便便]": "💩",
        "[月亮]": "🌙", "[太阳]": "☀️", "[庆祝]": "🎉", "[礼物]": "🎁",
        "[烟花]": "🎆", "[爆竹]": "🧨",
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
