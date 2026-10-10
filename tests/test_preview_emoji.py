import os
import tkinter.font as tkfont

import pytest

from test_gui_v13 import app, select_conversations, tk_host
from wechat_exporter.models import Conversation
from wechat_exporter.preview_emoji import emoji_runs, preview_runs, render_color_emoji, render_native_face
from wechat_exporter.native_face_assets import native_face_path


@pytest.mark.parametrize("face", ["🌹", "🍉", "🤝", "☕", "👌", "❤️", "😮‍💨", "🫠"])
@pytest.mark.skipif(os.name != "nt", reason="uses the Windows color font")
def test_native_font_has_actual_color_pixels_at_text_size(face):
    small = render_color_emoji(face, 20)
    large = render_color_emoji(face, 30)
    assert small is not None and large is not None
    assert max(small.size) == 20 and max(large.size) == 30
    assert any(a > 100 and max(r, g, b) - min(r, g, b) > 35
               for r, g, b, a in (small.getpixel((x, y))
                                 for y in range(small.height) for x in range(small.width)))


def test_mixed_text_keeps_whole_emoji_sequences_and_unknown_labels():
    source = "谢谢🌹 👌🏽 [图片]【未知】 ❤️ 😮‍💨 🇨🇳 1️⃣"
    runs = emoji_runs(source)
    assert "".join(text for text, _ in runs) == source
    assert [text for text, face in runs if face] == ["🌹", "👌🏽", "❤️", "😮‍💨", "🇨🇳", "1️⃣"]
    assert any("[图片]【未知】" in text and not face for text, face in runs)


def test_wechat_labels_use_original_artwork_without_reinterpreting_unicode():
    source = "谢谢[西瓜][吃瓜][握手][咖啡] 🍉 [图片][未知表情]"
    runs = preview_runs(source)
    assert "".join(part for part, _ in runs) == source
    assert [part for part, kind in runs if kind == "native"] == ["[西瓜]", "[吃瓜]", "[握手]", "[咖啡]"]
    assert [part for part, kind in runs if kind == "unicode"] == ["🍉"]
    assert native_face_path("[西瓜]") != native_face_path("[吃瓜]")
    for label in ("[西瓜]", "[吃瓜]", "[握手]", "[咖啡]"):
        bitmap = render_native_face(label, 20)
        assert bitmap is not None and max(bitmap.size) <= 20
    assert render_native_face("[未知表情]", 20) is None


@pytest.mark.parametrize("summary_width", [200, 1600])
def test_color_cells_follow_scroll_resize_search_and_native_multiselection(app, summary_width):
    conversations = [Conversation(f"test-{i}", f"验收{i:03d}", summary="谢谢[玫瑰] [吃瓜][握手][咖啡][OK]")
                     for i in range(65)]
    select_conversations(app, *conversations)
    app.tree.column("summary", width=summary_width, stretch=False)
    app.root.attributes("-alpha", 0)
    app.root.deiconify()
    app.root.update()
    renderer = app.conversation_emoji_cells
    assert renderer.cells
    initial = set(renderer.cells)
    iid, cell = next(iter(renderer.cells.items()))
    x, y, width, height = app.tree.bbox(iid, "summary")
    assert (cell.winfo_x(), cell.winfo_y(), cell.winfo_height()) == (x, y, height)
    # A column can extend beyond the viewport; the overlay must stop at the
    # visible table edge while retaining the full width of an in-bounds cell.
    viewport_right = app.tree.winfo_width() - 1
    assert cell.winfo_width() > 0
    assert cell.winfo_x() + cell.winfo_width() == min(x + width, viewport_right)
    # Bitmap edges fit in the same line height as the text, centered vertically.
    font = tkfont.Font(root=app.root, font=renderer._font)
    for item in cell.find_all():
        if cell.type(item) == "image":
            left, top, right, bottom = cell.bbox(item)
            assert 0 <= top < bottom <= height and bottom - top <= font.metrics("linespace")
    cell.event_generate("<ButtonPress-1>", x=8, y=height // 2)
    cell.event_generate("<ButtonRelease-1>", x=8, y=height // 2)
    app.root.update()
    assert app.tree.selection() == (iid,)
    other_iid, other_cell = next((k, v) for k, v in renderer.cells.items() if k != iid)
    other_cell.event_generate("<ButtonPress-1>", x=8, y=height // 2, state=4)  # Ctrl
    other_cell.event_generate("<ButtonRelease-1>", x=8, y=height // 2, state=4)
    app.root.update()
    assert set(app.tree.selection()) == {iid, other_iid}
    other_cell.event_generate("<MouseWheel>", delta=-120, x=10, y=height // 2)
    app.root.update()
    assert app.tree.yview()[0] > 0
    app.tree.yview_moveto(1)
    app.root.update()
    assert renderer.cells and not initial.intersection(renderer.cells)
    app.root.geometry("1050x850")
    app.root.update()
    for row, overlay in renderer.cells.items():
        box = app.tree.bbox(row, "summary")
        assert overlay.winfo_x() == box[0] and overlay.winfo_y() == box[1]
    app.search_var.set("验收064")
    app.root.update()
    assert len(app._conversation_by_iid) == len(renderer.cells) == 1
    app.search_var.set("找不到的联系人")
    app.root.update()
    assert not renderer.cells
