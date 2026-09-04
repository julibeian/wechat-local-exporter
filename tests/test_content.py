from __future__ import annotations

import zstandard

from wechat_exporter.content import (
    app_message_semantic_type,
    decode_database_content,
    extract_message_details,
    parse_message_text,
)


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


def test_explicit_file_message_keeps_filename_in_searchable_text() -> None:
    content = "<msg><appmsg><title>课程资料.docx</title></appmsg></msg>"
    assert parse_message_text(34359738417, content) == "[文件] 课程资料.docx"


def test_quoted_message_includes_the_original_text_and_sender() -> None:
    content = """<msg><appmsg>
      <title>好的，按这个执行</title><type>57</type>
      <refermsg>
        <type>1</type><displayname>张三</displayname>
        <content>原计划周五提交&amp;归档</content>
      </refermsg>
    </appmsg></msg>"""

    assert parse_message_text(244813135921, content) == (
        "[引用消息] 好的，按这个执行\n"
        "引用原文（张三）：原计划周五提交&归档"
    )


def test_combined_forward_chat_history_is_expanded() -> None:
    record = """<recordinfo>
      <title>项目群聊天记录</title>
      <datalist count="3">
        <dataitem datatype="1">
          <sourcename>李四</sourcename><sourcetime>2026-08-29 10:20</sourcetime>
          <datadesc>第一行&amp;第二项</datadesc>
        </dataitem>
        <dataitem datatype="2">
          <sourcename>王五</sourcename><sourcetime>2026-08-29 10:21</sourcetime>
        </dataitem>
        <dataitem datatype="8">
          <sourcename>赵六</sourcename><sourcetime>2026-08-29 10:22</sourcetime>
          <datatitle>方案.pdf</datatitle>
        </dataitem>
      </datalist>
    </recordinfo>"""
    content = (
        "<msg><appmsg><title>项目群聊天记录</title><type>19</type>"
        f"<recorditem><![CDATA[{record}]]></recorditem>"
        "</appmsg></msg>"
    )

    assert parse_message_text(81604378673, content) == (
        "[聊天记录] 项目群聊天记录\n"
        "[2026-08-29 10:20] 李四： 第一行&第二项\n"
        "[2026-08-29 10:21] 王五： [图片]\n"
        "[2026-08-29 10:22] 赵六： [文件] 方案.pdf"
    )


def test_quoted_combined_forward_chat_history_is_expanded() -> None:
    embedded = """<msg><appmsg><title>两条记录</title><type>19</type>
      <recorditem><recordinfo><datalist count="1">
        <dataitem datatype="1"><sourcename>小明</sourcename>
          <datadesc>被转发的原文</datadesc></dataitem>
      </datalist></recordinfo></recorditem>
    </appmsg></msg>"""
    content = (
        "<msg><appmsg><title>请看这里</title><type>57</type><refermsg>"
        "<type>49</type><displayname>同事</displayname>"
        f"<content><![CDATA[{embedded}]]></content>"
        "</refermsg></appmsg></msg>"
    )

    assert parse_message_text(244813135921, content) == (
        "[引用消息] 请看这里\n"
        "引用原文（同事）：[聊天记录] 两条记录\n"
        "小明： 被转发的原文"
    )


def test_structured_details_cover_location_link_and_quote_without_raw_dump() -> None:
    location = extract_message_details(
        48,
        '<msg><location x="31.2304" y="121.4737" poiname="人民广场" label="上海市黄浦区"/></msg>',
    )
    assert location == {
        "name": "人民广场",
        "address": "上海市黄浦区",
        "latitude": 31.2304,
        "longitude": 121.4737,
    }

    link_xml = """<msg><appmsg><type>5</type><title>课程页</title>
      <des>课程说明</des><url>https://example.test/course</url>
      <sourcedisplayname>教务系统</sourcedisplayname>
      <thumburl>https://example.test/cover.jpg</thumburl></appmsg></msg>"""
    link = extract_message_details(49, link_xml)
    assert link["title"] == "课程页"
    assert link["source"] == "教务系统"
    assert link["cover_urls"] == ["https://example.test/cover.jpg"]
    assert app_message_semantic_type(49, link_xml) == "link"

    quote_xml = """<msg><appmsg><title>收到</title><type>57</type><refermsg>
      <type>1</type><svrid>12345</svrid><displayname>张三</displayname>
      <content>明天见</content></refermsg></appmsg></msg>"""
    quote = extract_message_details(244813135921, quote_xml)
    assert quote["quoted_message_id"] == "12345"
    assert quote["quoted_sender"] == "张三"
    assert quote["quoted_text"] == "明天见"


def test_unknown_message_keeps_readable_text() -> None:
    assert parse_message_text(987654321, "仍然能读的内容") == (
        "[消息类型 987654321] 仍然能读的内容"
    )


def test_payment_and_call_keep_only_visible_status_text() -> None:
    transfer = "<msg><appmsg><title>已收款</title><des>转账已被接收</des></appmsg></msg>"
    assert parse_message_text(8589934592049, transfer) == "[转账] 已收款"
    transfer_details = extract_message_details(8589934592049, transfer)
    assert transfer_details == {
        "visible_text": "[转账] 已收款",
        "status_text": "已收款",
    }

    call = "<msg><voipmsg><title>通话已结束</title><duration>42</duration></voipmsg></msg>"
    call_details = extract_message_details(50, call)
    assert call_details["visible_text"] == "[通话] 通话已结束"
    assert call_details["duration_seconds"] == 42
