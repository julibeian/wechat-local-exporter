from __future__ import annotations

import zstandard

from wechat_exporter.content import decode_database_content, parse_message_text


def test_plain_text_that_looks_encoded_is_preserved() -> None:
    hexadecimal = "0123456789abcdef0123456789abcdef"
    base64_like = "abcdefghijklmnopqrstuvwx"
    assert decode_database_content(hexadecimal) == hexadecimal
    assert decode_database_content(base64_like) == base64_like


def test_zstd_blob_is_decoded() -> None:
    payload = "微信压缩消息内容".encode("utf-8")
    compressed = zstandard.ZstdCompressor().compress(payload)
    assert decode_database_content(b"fallback", compressed) == payload.decode("utf-8")


def test_wechat_official_voice_transcript_is_extracted() -> None:
    content = (
        '<msg><voicemsg voicelength="1234"/>'
        '<voicetrans transtext="今天下午三点&amp;四点" istransend="1"/></msg>'
    )
    assert parse_message_text(34, content) == (
        "[微信语音转文字] 今天下午三点&四点"
    )
    assert parse_message_text(34, "<msg><voicemsg/></msg>") == "[语音]"
