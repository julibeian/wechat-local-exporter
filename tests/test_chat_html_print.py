from __future__ import annotations

from html.parser import HTMLParser
from pathlib import Path

from wechat_exporter.chat_html_archive import export_chat_html_archive
from wechat_exporter.models import Conversation, HtmlExportOptions, Message


class _IdCollector(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.ids: list[str] = []

    def handle_starttag(
        self, _tag: str, attrs: list[tuple[str, str | None]]
    ) -> None:
        for name, value in attrs:
            if name == "id" and value:
                self.ids.append(value)


def _build_archive(root: Path) -> Path:
    conversation = Conversation("wxid_friend", "打印测试")
    messages = tuple(
        Message(
            local_id=index,
            timestamp=1_780_000_000 + index,
            message_type=1,
            sender_id="wxid_self" if index % 2 else "wxid_friend",
            sender_name="我" if index % 2 else "朋友",
            is_outgoing=bool(index % 2),
            content=f"第 {index} 条",
            conversation_id=conversation.username,
        )
        for index in range(1, 6)
    )
    result = export_chat_html_archive(
        root=root,
        conversation=conversation,
        messages=messages,
        options=HtmlExportOptions(include_images=False, include_stickers=False),
        chunk_size=3,
    )
    return result.entrypoint


def test_generated_archive_has_explicit_bounded_print_flow(tmp_path: Path) -> None:
    entrypoint = _build_archive(tmp_path / "archive")
    html = entrypoint.read_text(encoding="utf-8")
    javascript = (entrypoint.parent / "assets" / "viewer.js").read_text(
        encoding="utf-8"
    )

    collector = _IdCollector()
    collector.feed(html)
    assert len(collector.ids) == len(set(collector.ids))
    assert {
        "print-button",
        "print-dialog",
        "print-description",
        "print-confirm",
        "print-document",
        "print-range",
        "print-count",
        "print-scope-note",
        "print-message-list",
    } <= set(collector.ids)

    # The UI must not suggest that a multi-chunk archive is printed in full.
    assert "打印当前分块 / 另存为 PDF" in html
    assert "不会加载或打印其他分块" in html
    assert "超大归档因此不会一次性占满浏览器内存" in html
    assert "在浏览器打印面板中选择“另存为 PDF”" in html

    # Both the explicit button and Ctrl+P build from the complete, already-loaded
    # current chunk rather than cloning only the 160 visible message rows.
    assert 'window.addEventListener("beforeprint"' in javascript
    assert "state.currentRecords.forEach" in javascript
    assert "preparePrintDocument(state)" in javascript
    assert "window.print()" in javascript
    assert "本次只打印第 " in javascript
    assert "不包含归档中的其他分块" in javascript


def test_print_styles_isolate_a4_document_and_keep_message_context(
    tmp_path: Path,
) -> None:
    entrypoint = _build_archive(tmp_path / "archive")
    css = (entrypoint.parent / "assets" / "viewer.css").read_text(encoding="utf-8")

    assert "@page" in css
    assert "size: A4" in css
    assert "@media print" in css
    assert ".app-shell," in css
    assert ".print-document" in css
    assert "page-break-inside: avoid" in css
    assert ".print-document .message-time" in css
    assert "opacity: 1" in css
    assert "print-color-adjust: exact" in css
