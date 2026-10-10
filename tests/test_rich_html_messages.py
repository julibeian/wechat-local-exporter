import json
from types import SimpleNamespace

from wechat_exporter.chat_html_archive import export_chat_html_archive
from wechat_exporter.content import parse_message_text
from wechat_exporter.models import Conversation, Message


def test_quote_uses_contact_name_and_separates_reply_from_original(tmp_path):
    raw = ('<msg><appmsg><type>57</type><title>可行[OK]</title><refermsg>'
           '<type>1</type><fromusr>wxid_parent</fromusr><content>今天准备出发</content>'
           '</refermsg></appmsg></msg>')
    messages = [Message(1, 1788307200, 49, 'self', '我', True,
                        parse_message_text(49, raw), raw_content=raw)]
    result = export_chat_html_archive(root=tmp_path / 'quote', conversation=Conversation('wxid_parent', '爸'),
                                     messages=messages, profiles={'wxid_parent': SimpleNamespace(remark='爸')})
    chunk = next((result.root / 'data').glob('messages-*.js')).read_text(encoding='utf-8')
    records = json.loads(chunk[chunk.index(',') + 1:chunk.rindex(')')])
    assert records[0]['text'] == '可行[OK]'
    assert records[0]['quote'] == {'sender_name': '爸', 'text': '今天准备出发'}
    assert '引用原文' not in records[0]['text']


def test_transfer_exports_amount_and_actual_status_without_protocol_fields(tmp_path):
    raw = ('<msg><appmsg><title>微信转账</title><wcpayinfo><paysubtype>3</paysubtype>'
           '<feedesc>￥31.00</feedesc><transferid>private-protocol-id</transferid></wcpayinfo></appmsg></msg>')
    result = export_chat_html_archive(root=tmp_path / 'transfer', conversation=Conversation('friend', '好友'),
                                     messages=[Message(1, 1788307200, 8589934592049, 'self', '我', True,
                                                       parse_message_text(8589934592049, raw), raw_content=raw)])
    chunk = next((result.root / 'data').glob('messages-*.js')).read_text(encoding='utf-8')
    records = json.loads(chunk[chunk.index(',') + 1:chunk.rindex(')')])
    assert records[0]['payment']['amount'] == '￥31.00'
    assert records[0]['payment']['paysubtype'] == 3
    assert 'private-protocol-id' not in chunk


def test_recall_displays_replacement_without_protocol_timestamp():
    raw = '<sysmsg><revokemsg><replacemsg>你撤回了一条消息</replacemsg><time>1786624894</time></revokemsg></sysmsg>'
    assert parse_message_text(10002, raw) == '你撤回了一条消息'


def test_transfer_reminder_keeps_visible_text_without_cdata_or_private_link(tmp_path):
    raw = ('<?xml version="1.0"?><sysmsg type="paymsg"><content><![CDATA['
           '你有一笔待接收的<_wc_custom_link_ href="weixin://wxpay/transfer/remindrcvmsg?'
           'transferid=private-transaction-id">转账</_wc_custom_link_>，将于2小时后过期'
           ']]></content></sysmsg>')
    text = parse_message_text(10000, raw)
    assert text == '你有一笔待接收的转账，将于2小时后过期'
    result = export_chat_html_archive(
        root=tmp_path / 'reminder', conversation=Conversation('friend', '好友'),
        messages=[Message(1, 1767276276, 10000, '系统', '系统', False, text, raw_content=raw)])
    chunk = next((result.root / 'data').glob('messages-*.js')).read_text(encoding='utf-8')
    record = json.loads(chunk[chunk.index(',') + 1:chunk.rindex(')')])[0]
    assert record['type'] == 'system'
    assert record['payment'] == {'kind': 'reminder', 'status_text': text}
    assert 'amount' not in record['payment']
    assert 'private-transaction-id' not in chunk and ']]>' not in chunk
