from __future__ import annotations

import json
import io
from datetime import datetime
from pathlib import Path

import zstandard
from PIL import Image

from .content import decode_database_content, parse_message_text
from .exporters import PdfTranscriptWriter, TxtTranscriptWriter
from .integrity import verify_signature
from .models import Conversation, Message, PdfImage
from .windows import discover_accounts, find_weixin_executable, select_current_account


def run_packaged_self_test(
    output_dir: Path, *, check_environment: bool = False
) -> Path:
    """Exercise the packaged TXT/PDF path without touching any WeChat data."""
    output_dir.mkdir(parents=True, exist_ok=True)
    zstd_probe = "成品内置 zstd 解压自检"
    compressed_probe = zstandard.ZstdCompressor().compress(zstd_probe.encode("utf-8"))
    if decode_database_content(b"", compressed_probe) != zstd_probe:
        raise RuntimeError("zstd 压缩消息自检失败")
    if not verify_signature():
        raise RuntimeError("个人签名完整性自检失败")

    environment_status = "skipped"
    if check_environment:
        executable = find_weixin_executable()
        account = select_current_account(
            discover_accounts(include_process_memory=False)
        )
        if not executable.is_file() or account is None:
            raise RuntimeError("没有自动识别到微信程序或当前账号")
        environment_status = "ok"
    conversation = Conversation(
        username="selftest_contact",
        display_name="成品自检会话 🙂",
        summary="仅使用内置合成消息",
    )
    messages = (
        Message(
            local_id=1,
            timestamp=int(datetime(2026, 8, 23, 9, 15, 0).timestamp()),
            message_type=1,
            sender_id="self",
            sender_name="我",
            is_outgoing=True,
            content="这是一条中文 TXT/PDF 打包自检消息。",
        ),
        Message(
            local_id=2,
            timestamp=int(datetime(2026, 8, 23, 9, 16, 0).timestamp()),
            message_type=1,
            sender_id="selftest_contact",
            sender_name="测试联系人",
            is_outgoing=False,
            content="第二行包含换行、English 和表情 🙂\n不会访问真实微信数据。",
        ),
        Message(
            local_id=3,
            timestamp=int(datetime(2026, 8, 23, 9, 17, 0).timestamp()),
            message_type=34,
            sender_id="selftest_contact",
            sender_name="测试联系人",
            is_outgoing=False,
            content=parse_message_text(
                34,
                '<msg><voicemsg/><voicetrans transtext="微信官方语音转写自检" istransend="1"/></msg>',
            ),
        ),
        Message(
            local_id=4,
            timestamp=int(datetime(2026, 8, 23, 9, 18, 0).timestamp()),
            message_type=3,
            sender_id="self",
            sender_name="我",
            is_outgoing=True,
            content="[图片]",
        ),
    )
    image_stream = io.BytesIO()
    Image.new("RGB", (320, 180), (35, 122, 86)).save(image_stream, format="PNG")
    synthetic_image = PdfImage(
        data=image_stream.getvalue(),
        image_format="PNG",
        width=320,
        height=180,
        source="内置合成图片",
    )
    txt_path = output_dir / "packaged-self-test.txt"
    pdf_path = output_dir / "packaged-self-test.pdf"
    with TxtTranscriptWriter(txt_path, conversation) as writer:
        for message in messages:
            writer.write(message)
        txt_count = writer.count
    with PdfTranscriptWriter(pdf_path, conversation) as writer:
        for message in messages:
            writer.write(
                message,
                image=synthetic_image if message.local_id == 4 else None,
            )
        pdf_count = writer.count

    receipt_path = output_dir / "self-test-result.json"
    receipt_path.write_text(
        json.dumps(
            {
                "status": "ok",
                "message_count": len(messages),
                "txt_count": txt_count,
                "pdf_count": pdf_count,
                "zstd": "ok",
                "wechat_voice_text": "ok",
                "pdf_image": "ok",
                "auto_discovery": environment_status,
                "txt": txt_path.name,
                "pdf": pdf_path.name,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    return receipt_path
