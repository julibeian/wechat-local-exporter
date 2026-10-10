"""Original WeChat faces and color Unicode emoji in Tk conversation cells.

Windows' Treeview paints text with GDI, which ignores the color glyph layers.
Only cells containing emoji get a Canvas; their text stays in the Treeview for
selection, search and accessibility. Mouse events go back to the native table.
"""
from __future__ import annotations

import os
import tkinter as tk
from functools import lru_cache
from pathlib import Path
from tkinter import font as tkfont, ttk

from PIL import Image, ImageDraw, ImageFont, ImageTk

from .native_face_assets import native_face_path, native_face_runs


def _emoji_base(character: str) -> bool:
    code = ord(character)
    return (0x1F000 <= code <= 0x1FAFF or 0x2600 <= code <= 0x27BF
            or code in {0x231A, 0x231B, 0x23F0, 0x23F3, 0x2B50, 0x2B55})


def emoji_runs(text: str) -> tuple[tuple[str, bool], ...]:
    """Keep variation selectors, skin tones, flags and ZWJ sequences together."""
    runs: list[tuple[str, bool]] = []
    index = plain_start = 0
    while index < len(text):
        keycap = (text[index] in "0123456789#*"
                  and text[index + 1:index + 3].lstrip("\ufe0f").startswith("\u20e3"))
        if not _emoji_base(text[index]) and not keycap:
            index += 1
            continue
        if index > plain_start:
            runs.append((text[plain_start:index], False))
        end = index + 1
        if (0x1F1E6 <= ord(text[index]) <= 0x1F1FF and end < len(text)
                and 0x1F1E6 <= ord(text[end]) <= 0x1F1FF):
            end += 1
        while end < len(text):
            code = ord(text[end])
            if code in {0xFE0E, 0xFE0F, 0x20E3} or 0x1F3FB <= code <= 0x1F3FF:
                end += 1
            elif code == 0x200D and end + 1 < len(text) and _emoji_base(text[end + 1]):
                end += 2
            elif 0xE0020 <= code <= 0xE007F:  # subdivision flag tags
                end += 1
            else:
                break
        runs.append((text[index:end], True))
        index = plain_start = end
    if plain_start < len(text):
        runs.append((text[plain_start:], False))
    return tuple(runs)


@lru_cache(maxsize=1)
def color_emoji_font() -> str | None:
    fonts = [Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts/seguiemj.ttf",
             Path("/System/Library/Fonts/Apple Color Emoji.ttc"),
             Path("/usr/share/fonts/truetype/noto/NotoColorEmoji.ttf")]
    return next((str(path) for path in fonts if path.is_file()), None)


@lru_cache(maxsize=512)
def render_color_emoji(text: str, pixels: int) -> Image.Image | None:
    """Rasterize the font's own color artwork; no bundled or remote emoji."""
    path = color_emoji_font()
    if path is None:
        return None
    try:
        font = ImageFont.truetype(path, 64)
        left, top, right, bottom = font.getbbox(text)
        image = Image.new("RGBA", (max(1, right - left), max(1, bottom - top)))
        ImageDraw.Draw(image).text((-left, -top), text, font=font, embedded_color=True)
        bounds = image.getbbox()
        if bounds is None:
            return None
        image = image.crop(bounds)
        ratio = min(pixels / image.width, pixels / image.height)
        return image.resize((max(1, round(image.width * ratio)),
                             max(1, round(image.height * ratio))), Image.Resampling.LANCZOS)
    except (OSError, ValueError):
        return None


@lru_cache(maxsize=512)
def render_native_face(label: str, pixels: int) -> Image.Image | None:
    path = native_face_path(label)
    if path is None:
        return None
    try:
        with Image.open(path) as source:
            bitmap = source.convert("RGBA")
        ratio = pixels / max(bitmap.size)
        return bitmap.resize((max(1, round(bitmap.width * ratio)), max(1, round(bitmap.height * ratio))),
                             Image.Resampling.LANCZOS)
    except (OSError, ValueError):
        return None


def preview_runs(text: str) -> tuple[tuple[str, str], ...]:
    runs: list[tuple[str, str]] = []
    for part, native in native_face_runs(text):
        if native:
            runs.append((part, "native"))
        else:
            runs.extend((value, "unicode" if emoji else "text") for value, emoji in emoji_runs(part))
    return tuple(runs)


class ConversationEmojiCells:
    def __init__(self, tree: ttk.Treeview, scrollbar: ttk.Scrollbar):
        self.tree = tree
        self.style = ttk.Style(tree)
        self.cells: dict[str, tk.Canvas] = {}
        self._photos: dict[tuple[str, int], ImageTk.PhotoImage] = {}
        self._after: str | None = None
        self._closed = False
        self._font = tkfont.Font(root=tree, font=self.style.lookup("Treeview", "font") or "TkDefaultFont")
        self._scrollbar = scrollbar
        tree.configure(yscrollcommand=self._scroll_changed)
        for event in ("<Configure>", "<Map>", "<Expose>", "<FocusIn>", "<FocusOut>",
                      "<<TreeviewSelect>>", "<KeyRelease>", "<ButtonRelease-1>"):
            tree.bind(event, self.refresh, add="+")
        tree.bind("<Destroy>", self._destroy, add="+")

    def _scroll_changed(self, first: str, last: str) -> None:
        self._scrollbar.set(first, last)
        self.refresh()

    def refresh(self, _event: tk.Event | None = None) -> None:
        if not self._closed and self._after is None:
            self._after = self.tree.after_idle(self._paint)

    def _paint(self) -> None:
        self._after = None
        if self._closed or not self.tree.winfo_ismapped():
            return
        visible: set[str] = set()
        selected = set(self.tree.selection())
        font = self._font
        font.configure(**tkfont.Font(root=self.tree, font=self.style.lookup("Treeview", "font") or "TkDefaultFont").actual())
        pixels = max(12, round(font.metrics("linespace") * .85))
        for iid in self.tree.get_children():
            box = self.tree.bbox(iid, "summary")
            if not box:
                continue
            text = str(self.tree.set(iid, "summary"))
            runs = preview_runs(text)
            if not any(kind != "text" for _, kind in runs):
                continue
            x, y, width, height = box
            if y < 0 or x >= self.tree.winfo_width():
                continue
            width = min(width, self.tree.winfo_width() - x - 1)
            if width <= 0:
                continue
            visible.add(iid)
            states = ("selected",) if iid in selected else ()
            background = self.style.lookup("Treeview", "background", states) or "white"
            foreground = self.style.lookup("Treeview", "foreground", states) or "black"
            if not states:
                for tag in self.tree.item(iid, "tags"):
                    background = self.tree.tag_configure(tag, "background") or background
                    foreground = self.tree.tag_configure(tag, "foreground") or foreground
            cell = self.cells.get(iid)
            if cell is None:
                cell = tk.Canvas(self.tree, highlightthickness=0, borderwidth=0, takefocus=False)
                self.cells[iid] = cell
                for event in ("<ButtonPress-1>", "<ButtonRelease-1>", "<B1-Motion>",
                              "<ButtonPress-3>", "<ButtonRelease-3>", "<MouseWheel>"):
                    cell.bind(event, lambda e, sequence=event: self._forward(e, sequence))
            cell.configure(background=background)
            cell.place(x=x, y=y, width=width, height=height)
            cell.delete("all")
            cursor = 2
            for part, kind in runs:
                photo = None
                if kind != "text":
                    key = (part, pixels)
                    photo = self._photos.get(key)
                    if photo is None:
                        bitmap = render_native_face(part, pixels) if kind == "native" else render_color_emoji(part, pixels)
                        if bitmap is not None:
                            photo = ImageTk.PhotoImage(bitmap, master=self.tree)
                            self._photos[key] = photo
                if photo is None:
                    cell.create_text(cursor, height / 2, text=part, font=font,
                                     anchor="w", fill=foreground)
                    cursor += font.measure(part)
                else:
                    cell.create_image(cursor + pixels / 2, height / 2, image=photo)
                    cursor += pixels + 1
                if cursor >= width:
                    break
        for iid in set(self.cells) - visible:
            self.cells.pop(iid).destroy()

    def _forward(self, event: tk.Event, sequence: str) -> str:
        x = event.x + event.widget.winfo_x()
        y = event.y + event.widget.winfo_y()
        if sequence == "<ButtonPress-1>":
            self.tree.focus_set()
        kwargs = {"x": x, "y": y, "state": event.state}
        if sequence == "<MouseWheel>":
            kwargs["delta"] = event.delta
        self.tree.event_generate(sequence, **kwargs)
        self.refresh()
        return "break"

    def _destroy(self, event: tk.Event) -> None:
        if event.widget == self.tree:
            self._closed = True
            if self._after is not None:
                self.tree.after_cancel(self._after)
            self._photos.clear()
            self.cells.clear()
