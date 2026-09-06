from __future__ import annotations

from wechat_exporter.faces import NATIVE_FACE_MAP, normalize_wechat_faces


def test_verified_native_face_is_replaced_and_source_is_traceable() -> None:
    result = normalize_wechat_faces("谢谢[爱心]，再送一个[爱心]")

    assert result.text == "谢谢❤️，再送一个❤️"
    assert result.source_text == "谢谢[爱心]，再送一个[爱心]"
    assert result.raw_text == result.source_text
    assert [(item.source, item.display, item.start, item.end) for item in result.replacements] == [
        ("[爱心]", "❤️", 2, 6),
        ("[爱心]", "❤️", 11, 15),
    ]


def test_existing_unicode_emoji_passes_through_without_raw_duplicate() -> None:
    source = "已经是 ❤️ 和 💐"
    result = normalize_wechat_faces(source)

    assert result.text == source
    assert result.source_text == source
    assert result.raw_text is None
    assert result.replacements == ()


def test_unknown_brackets_and_media_placeholders_are_not_changed() -> None:
    source = "[图片] [文件] [语音] [动画表情] [某个未来表情] 用户写的[说明]"
    result = normalize_wechat_faces(source)

    assert result.text == source
    assert result.raw_text is None


def test_native_face_map_is_a_deliberately_small_immutable_whitelist() -> None:
    assert NATIVE_FACE_MAP == {"[爱心]": "❤️"}
