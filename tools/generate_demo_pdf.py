from __future__ import annotations

import io
from datetime import datetime, timedelta
from pathlib import Path

from PIL import Image, ImageDraw

from wechat_exporter.exporters import PdfTranscriptWriter
from wechat_exporter.models import Conversation, Message, PdfImage


def _sample_images() -> tuple[PdfImage, PdfImage]:
    photo = Image.new("RGB", (1600, 900), "#eef4ff")
    draw = ImageDraw.Draw(photo)
    draw.rounded_rectangle((80, 70, 1520, 830), radius=40, fill="#ffffff", outline="#91a8cc", width=6)
    draw.rectangle((150, 150, 680, 720), fill="#2457a7")
    for index, width in enumerate((650, 570, 690, 510, 620, 430)):
        y = 175 + index * 82
        draw.rounded_rectangle((760, y, 760 + width, y + 34), radius=12, fill="#a8b8cf")
    photo_stream = io.BytesIO()
    photo.save(photo_stream, format="JPEG", quality=94)

    sticker = Image.new("RGBA", (480, 480), (0, 0, 0, 0))
    draw = ImageDraw.Draw(sticker)
    draw.ellipse((40, 40, 440, 440), fill="#ffd66b", outline="#6b4b17", width=12)
    draw.ellipse((140, 165, 190, 225), fill="#222222")
    draw.ellipse((290, 165, 340, 225), fill="#222222")
    draw.arc((145, 175, 335, 350), start=20, end=160, fill="#9b3b32", width=16)
    sticker_stream = io.BytesIO()
    sticker.save(sticker_stream, format="PNG")
    return (
        PdfImage(photo_stream.getvalue(), "JPEG", 1600, 900, "合成原图"),
        PdfImage(sticker_stream.getvalue(), "PNG", 480, 480, "合成表情图片"),
    )


def main() -> None:
    output = Path("output/pdf/微信聊天图片导出验收样例.pdf")
    conversation = Conversation("wxid_demo", "示例好友")
    start = datetime(2026, 8, 22, 20, 15)
    photo, sticker = _sample_images()
    samples = [
        (False, "示例好友", "晚上好，这是一份完全由合成数据生成的排版样例。", 1, None),
        (True, "我", "收到。PDF 中的文字可以搜索和复制。", 1, None),
        (False, "示例好友", "较长消息会自动换行；跨页时会保留页眉、页码和日期分隔。" * 3, 1, None),
        (True, "我", "[图片]", 3, photo),
        (False, "示例好友", "[动画表情]", 47, sticker),
        (False, "示例好友", "中文、English、数字 12345 和 emoji 🙂 都会保留。", 1, None),
    ]
    with PdfTranscriptWriter(output, conversation) as writer:
        for index in range(18):
            outgoing, sender, content, message_type, image = samples[index % len(samples)]
            timestamp = int((start + timedelta(minutes=index * 17)).timestamp())
            writer.write(
                Message(
                    local_id=index + 1,
                    timestamp=timestamp,
                    message_type=message_type,
                    sender_id="wxid_self" if outgoing else "wxid_demo",
                    sender_name=sender,
                    is_outgoing=outgoing,
                    content=content,
                    sort_seq=index,
                ),
                image=image,
            )
    print(output.resolve())


if __name__ == "__main__":
    main()
