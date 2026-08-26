from __future__ import annotations

import io
import re
from datetime import datetime
from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.cidfonts import UnicodeCIDFont
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas
from reportlab.lib.utils import ImageReader

from .models import Conversation, Message, PdfImage


def safe_filename(value: str, fallback: str = "微信聊天") -> str:
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", value).strip(" .")
    cleaned = re.sub(r"\s+", " ", cleaned)
    return (cleaned[:100] or fallback).strip()


class TxtTranscriptWriter:
    def __init__(
        self,
        path: Path,
        conversation: Conversation,
        *,
        start_timestamp: int = 0,
        end_timestamp: int = 0,
    ):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._stream = path.open("w", encoding="utf-8-sig", newline="\n")
        self.count = 0
        self._stream.write("微信聊天记录（本机只读导出）\n")
        self._stream.write(f"会话：{conversation.display_name}\n")
        self._stream.write(f"会话标识：{conversation.username}\n")
        self._stream.write(f"时间范围：{_range_text(start_timestamp, end_timestamp)}\n")
        self._stream.write(f"导出时间：{datetime.now():%Y-%m-%d %H:%M:%S}\n")
        self._stream.write(
            "说明：优先导出微信已生成的语音转文字；其他非文本消息以方括号占位；"
            "本工具不会上传聊天内容。\n"
        )
        self._stream.write("=" * 64 + "\n\n")

    def write(self, message: Message) -> None:
        timestamp = message.datetime.strftime("%Y-%m-%d %H:%M:%S")
        text = message.content or "[空消息]"
        lines = text.splitlines() or [""]
        self._stream.write(f"[{timestamp}] {message.sender_name}: {lines[0]}\n")
        for line in lines[1:]:
            self._stream.write(f"    {line}\n")
        self.count += 1

    def close(self) -> None:
        if self._stream.closed:
            return
        self._stream.write("\n" + "=" * 64 + "\n")
        self._stream.write(f"共导出 {self.count} 条消息。\n")
        self._stream.close()

    def __enter__(self) -> TxtTranscriptWriter:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


class PdfTranscriptWriter:
    def __init__(
        self,
        path: Path,
        conversation: Conversation,
        *,
        start_timestamp: int = 0,
        end_timestamp: int = 0,
    ):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conversation = conversation
        self.display_name = _pdf_safe_text(conversation.display_name)
        self.start_timestamp = start_timestamp
        self.end_timestamp = end_timestamp
        self.font_name = _register_cjk_font()
        self.bold_font_name = self.font_name
        self.page_width, self.page_height = A4
        self.left = 42
        self.right = 42
        self.top = 44
        self.bottom = 42
        self.page_number = 0
        self.count = 0
        self.current_date = None
        safe_title = f"微信聊天 - {self.display_name}"
        self._canvas = canvas.Canvas(
            str(path), pagesize=A4, pageCompression=1, title=safe_title
        )
        self._canvas.setTitle(safe_title)
        self._canvas.setAuthor("微信 TXT/PDF 本地导出工具")
        self._canvas.setSubject("本机微信聊天记录只读导出")
        self.y = 0.0
        self._start_page(first=True)

    def _start_page(self, *, first: bool = False) -> None:
        self.page_number += 1
        self.y = self.page_height - self.top
        if first:
            self._canvas.setFont(self.bold_font_name, 18)
            self._canvas.setFillColor(colors.HexColor("#172033"))
            self._canvas.drawString(self.left, self.y, "微信聊天记录")
            self.y -= 27
            self._canvas.setFont(self.font_name, 10)
            self._canvas.setFillColor(colors.HexColor("#43506A"))
            metadata = [
                f"会话：{self.display_name}",
                f"会话标识：{self.conversation.username}",
                f"时间范围：{_range_text(self.start_timestamp, self.end_timestamp)}",
                f"导出时间：{datetime.now():%Y-%m-%d %H:%M:%S}",
                "说明：图片直接嵌入 PDF；页面仅等比显示缩放，不做额外有损压缩。",
                "语音优先使用微信已有转文字；其他非文本消息以方括号占位。",
            ]
            for line in metadata:
                self._canvas.drawString(self.left, self.y, line)
                self.y -= 15
            self.y -= 6
            self._canvas.setStrokeColor(colors.HexColor("#CBD5E1"))
            self._canvas.line(self.left, self.y, self.page_width - self.right, self.y)
            self.y -= 18
        else:
            self._canvas.setFont(self.font_name, 9)
            self._canvas.setFillColor(colors.HexColor("#64748B"))
            self._canvas.drawString(
                self.left, self.y, f"微信聊天 - {self.display_name}"
            )
            self.y -= 20

    def _finish_page(self) -> None:
        self._canvas.setFont(self.font_name, 8)
        self._canvas.setFillColor(colors.HexColor("#7C879D"))
        self._canvas.drawCentredString(
            self.page_width / 2, 22, f"第 {self.page_number} 页"
        )

    def _new_page(self) -> None:
        self._finish_page()
        self._canvas.showPage()
        self._start_page()

    def _ensure_space(self, height: float) -> None:
        if self.y - height < self.bottom:
            self._new_page()

    def write(self, message: Message, image: PdfImage | None = None) -> None:
        date_value = message.datetime.date()
        if date_value != self.current_date:
            self._ensure_space(28)
            self.current_date = date_value
            label = message.datetime.strftime("%Y 年 %m 月 %d 日")
            label_width = pdfmetrics.stringWidth(label, self.font_name, 9)
            center = self.page_width / 2
            self._canvas.setStrokeColor(colors.HexColor("#D7DEE9"))
            self._canvas.line(self.left, self.y - 4, center - label_width / 2 - 10, self.y - 4)
            self._canvas.line(center + label_width / 2 + 10, self.y - 4, self.page_width - self.right, self.y - 4)
            self._canvas.setFont(self.font_name, 9)
            self._canvas.setFillColor(colors.HexColor("#65748B"))
            self._canvas.drawCentredString(center, self.y - 7, label)
            self.y -= 23

        if image is not None:
            self._write_image_message(message, image)
            self.count += 1
            return

        content_lines = _wrap_text(
            _pdf_safe_text(message.content or "[空消息]"),
            self.font_name,
            9.5,
            self.page_width - self.left - self.right - 8,
        )
        block_height = 18 + len(content_lines) * 13 + 7
        if block_height > self.page_height - self.top - self.bottom - 30:
            block_height = 35
        self._ensure_space(min(block_height, 90))

        color = (
            colors.HexColor("#2457A7")
            if message.is_outgoing is True
            else colors.HexColor("#1C6B48")
            if message.is_outgoing is False
            else colors.HexColor("#5F6570")
        )
        header = _pdf_safe_text(f"{message.datetime:%H:%M:%S}  {message.sender_name}")
        self._canvas.setFont(self.bold_font_name, 9)
        self._canvas.setFillColor(color)
        self._canvas.drawString(self.left, self.y, header)
        self.y -= 14
        self._canvas.setFont(self.font_name, 9.5)
        self._canvas.setFillColor(colors.HexColor("#1E293B"))
        for line in content_lines:
            if self.y - 13 < self.bottom:
                self._new_page()
                self._canvas.setFont(self.font_name, 9.5)
                self._canvas.setFillColor(colors.HexColor("#1E293B"))
            self._canvas.drawString(self.left + 8, self.y, line)
            self.y -= 13
        self.y -= 7
        self.count += 1

    def _write_image_message(self, message: Message, image: PdfImage) -> None:
        available_width = self.page_width - self.left - self.right - 16
        available_height = self.page_height - self.top - self.bottom - 72
        if message.message_type == 47:
            available_width = min(available_width, 240)
            available_height = min(available_height, 300)
        scale = min(
            1.0,
            available_width / image.width,
            available_height / image.height,
        )
        display_width = max(1.0, image.width * scale)
        display_height = max(1.0, image.height * scale)
        block_height = 14 + display_height + 18 + 7
        self._ensure_space(block_height)

        color = (
            colors.HexColor("#2457A7")
            if message.is_outgoing is True
            else colors.HexColor("#1C6B48")
            if message.is_outgoing is False
            else colors.HexColor("#5F6570")
        )
        header = _pdf_safe_text(f"{message.datetime:%H:%M:%S}  {message.sender_name}")
        self._canvas.setFont(self.bold_font_name, 9)
        self._canvas.setFillColor(color)
        self._canvas.drawString(self.left, self.y, header)
        self.y -= 14

        reader = ImageReader(io.BytesIO(image.data))
        self._canvas.drawImage(
            reader,
            self.left + 8,
            self.y - display_height,
            width=display_width,
            height=display_height,
            preserveAspectRatio=True,
            mask="auto",
        )
        self.y -= display_height + 12
        details = (
            f"{image.source} · {image.image_format} · "
            f"{image.width}×{image.height} 像素"
        )
        if image.is_animated:
            details += " · PDF 显示动画首帧"
        self._canvas.setFont(self.font_name, 8)
        self._canvas.setFillColor(colors.HexColor("#64748B"))
        self._canvas.drawString(self.left + 8, self.y, _pdf_safe_text(details))
        self.y -= 13

    def close(self) -> None:
        if self._canvas is None:
            return
        self._ensure_space(30)
        self._canvas.setStrokeColor(colors.HexColor("#CBD5E1"))
        self._canvas.line(self.left, self.y, self.page_width - self.right, self.y)
        self.y -= 17
        self._canvas.setFont(self.font_name, 9)
        self._canvas.setFillColor(colors.HexColor("#556277"))
        self._canvas.drawString(self.left, self.y, f"共导出 {self.count} 条消息。")
        self._finish_page()
        self._canvas.save()
        self._canvas = None

    def __enter__(self) -> PdfTranscriptWriter:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


def _register_cjk_font() -> str:
    name = "WeChatExportCJK"
    if name in pdfmetrics.getRegisteredFontNames():
        return name
    candidates = [
        Path("C:/Windows/Fonts/msyh.ttc"),
        Path("C:/Windows/Fonts/msyh.ttf"),
        Path("C:/Windows/Fonts/simhei.ttf"),
        Path("C:/Windows/Fonts/simsun.ttc"),
    ]
    for font_path in candidates:
        if not font_path.is_file():
            continue
        try:
            pdfmetrics.registerFont(TTFont(name, str(font_path), subfontIndex=0))
            return name
        except Exception:
            try:
                pdfmetrics.registerFont(TTFont(name, str(font_path)))
                return name
            except Exception:
                continue
    fallback = "STSong-Light"
    if fallback not in pdfmetrics.getRegisteredFontNames():
        pdfmetrics.registerFont(UnicodeCIDFont(fallback))
    return fallback


def _wrap_text(text: str, font_name: str, font_size: float, max_width: float) -> list[str]:
    result: list[str] = []
    for paragraph in text.splitlines() or [""]:
        if not paragraph:
            result.append("")
            continue
        current = ""
        for character in paragraph:
            candidate = current + character
            if current and pdfmetrics.stringWidth(candidate, font_name, font_size) > max_width:
                result.append(current)
                current = character
            else:
                current = candidate
        result.append(current)
    return result or [""]


def _range_text(start_timestamp: int, end_timestamp: int) -> str:
    start = datetime.fromtimestamp(start_timestamp).strftime("%Y-%m-%d") if start_timestamp else "最早"
    end = datetime.fromtimestamp(end_timestamp).strftime("%Y-%m-%d") if end_timestamp else "最新"
    return f"{start} 至 {end}"


def _pdf_safe_text(value: str) -> str:
    """Keep PDF searchable when the selected CJK font lacks non-BMP emoji."""
    result: list[str] = []
    in_emoji = False
    for character in value:
        codepoint = ord(character)
        if codepoint > 0xFFFF or codepoint in {0xFE0E, 0xFE0F, 0x200D}:
            if not in_emoji:
                result.append("[表情]")
                in_emoji = True
            continue
        in_emoji = False
        result.append(character)
    return "".join(result)
