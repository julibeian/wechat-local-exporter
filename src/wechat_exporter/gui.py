from __future__ import annotations

import calendar
import ctypes
import os
import queue
import threading
import time as clock
import tkinter as tk
import unicodedata
import webbrowser
from datetime import date, datetime, time, timedelta
from pathlib import Path
from tkinter import filedialog, font as tkfont, messagebox, ttk

from . import PROJECT_URL, __version__
from .archive import CalibrationSample
from .history import ExportHistoryEntry, append_export_history, load_export_history
from .integrity import require_signature_integrity
from .key_capture import KeyCapturePreparation, prepare_key_capture
from .models import AccountLocation, Conversation, ExportRequest, ExportWorkload
from .service import (
    ExporterService,
    estimate_export_seconds,
    estimate_moments_export_seconds,
    format_duration,
)
from .windows import (
    discover_accounts,
    find_weixin_executable,
    list_wechat_processes,
    read_wechat_version,
    request_wechat_exit,
    select_current_account,
)


STAR_PROMPT_DELAY_SECONDS = 60.0
HISTORY_DIALOG_SIZE = (1380, 760)
HISTORY_DIALOG_MIN_SIZE = (1080, 580)
VOICE_TEXT_GUIDE_STEPS = (
    "在微信中打开含语音消息的聊天。",
    "电脑版：右键语音气泡，选择“转文字”；手机端：长按语音气泡，选择“转文字”。",
    "等待文字完整显示在语音气泡下方。不要在转写仍进行时立即关闭微信。",
    "回到本工具连接微信，再导出 TXT 或 PDF；两种格式都会默认写入微信已有转写。",
)


def _enable_windows_high_dpi() -> None:
    """Opt into native per-monitor rendering before Tk creates any window."""
    if os.name != "nt":
        return
    try:
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        user32.SetProcessDpiAwarenessContext.argtypes = [ctypes.c_void_p]
        user32.SetProcessDpiAwarenessContext.restype = ctypes.c_bool
        if user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4)):
            return
    except (AttributeError, OSError):
        pass
    try:
        shcore = ctypes.WinDLL("shcore", use_last_error=True)
        shcore.SetProcessDpiAwareness(2)
    except (AttributeError, OSError):
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except (AttributeError, OSError):
            pass


def _configure_native_fonts(root: tk.Tk) -> None:
    dpi = root.winfo_fpixels("1i")
    root.tk.call("tk", "scaling", max(1.0, dpi / 72.0))
    for name, size in (
        ("TkDefaultFont", 10),
        ("TkTextFont", 10),
        ("TkMenuFont", 10),
        ("TkHeadingFont", 10),
        ("TkCaptionFont", 10),
        ("TkSmallCaptionFont", 9),
        ("TkIconFont", 10),
        ("TkTooltipFont", 9),
    ):
        try:
            tkfont.nametofont(name).configure(family="Microsoft YaHei UI", size=size)
        except tk.TclError:
            continue
    style = ttk.Style(root)
    style.configure("Treeview", rowheight=30)
    style.configure("SelectedDay.TButton", font=("Microsoft YaHei UI", 10, "bold"))


class _CalendarPane(ttk.Frame):
    def __init__(self, parent: tk.Misc, title: str, value: date | None):
        super().__init__(parent, padding=8)
        self._selected = value
        self._month = (value or date.today()).replace(day=1)
        self._month_var = tk.StringVar()
        self._value_var = tk.StringVar()

        ttk.Label(self, text=title, font=("Microsoft YaHei UI", 11, "bold")).grid(
            row=0, column=0, columnspan=7, pady=(0, 7)
        )
        ttk.Button(self, text="‹", width=3, command=lambda: self._move_month(-1)).grid(
            row=1, column=0
        )
        ttk.Label(self, textvariable=self._month_var, anchor="center").grid(
            row=1, column=1, columnspan=5, sticky="ew"
        )
        ttk.Button(self, text="›", width=3, command=lambda: self._move_month(1)).grid(
            row=1, column=6
        )
        for column, weekday in enumerate("一二三四五六日"):
            ttk.Label(self, text=weekday, anchor="center").grid(
                row=2, column=column, padx=2, pady=(7, 3), sticky="ew"
            )
            self.columnconfigure(column, weight=1)
        self._days = ttk.Frame(self)
        self._days.grid(row=3, column=0, columnspan=7, sticky="nsew")
        for column in range(7):
            self._days.columnconfigure(column, weight=1)
        footer = ttk.Frame(self)
        footer.grid(row=4, column=0, columnspan=7, sticky="ew", pady=(7, 0))
        ttk.Label(footer, textvariable=self._value_var).pack(side="left")
        ttk.Button(footer, text="不限", command=lambda: self.set_date(None)).pack(side="right")
        self._render()

    def get_date(self) -> date | None:
        return self._selected

    def set_date(self, value: date | None) -> None:
        self._selected = value
        if value is not None:
            self._month = value.replace(day=1)
        self._render()

    def _move_month(self, offset: int) -> None:
        month_index = self._month.year * 12 + self._month.month - 1 + offset
        self._month = date(month_index // 12, month_index % 12 + 1, 1)
        self._render()

    def _select_day(self, day: int) -> None:
        self._selected = self._month.replace(day=day)
        self._render()

    def _render(self) -> None:
        self._month_var.set(f"{self._month.year} 年 {self._month.month} 月")
        self._value_var.set(
            self._selected.isoformat() if self._selected is not None else "不限日期"
        )
        for child in self._days.winfo_children():
            child.destroy()
        weeks = calendar.Calendar(firstweekday=0).monthdayscalendar(
            self._month.year, self._month.month
        )
        for row, week in enumerate(weeks):
            for column, day_number in enumerate(week):
                if not day_number:
                    ttk.Label(self._days, text="", width=3).grid(
                        row=row, column=column, padx=1, pady=1
                    )
                    continue
                selected = (
                    self._selected is not None
                    and self._selected.year == self._month.year
                    and self._selected.month == self._month.month
                    and self._selected.day == day_number
                )
                ttk.Button(
                    self._days,
                    text=str(day_number),
                    width=3,
                    style="SelectedDay.TButton" if selected else "TButton",
                    command=lambda day=day_number: self._select_day(day),
                ).grid(row=row, column=column, padx=1, pady=1, sticky="ew")


class DateRangeDialog(tk.Toplevel):
    def __init__(
        self,
        parent: tk.Tk,
        start_value: date | None,
        end_value: date | None,
    ):
        super().__init__(parent)
        self.result: tuple[date | None, date | None] | None = None
        self.title("选择日期范围")
        self.resizable(False, False)
        self.transient(parent)
        self.protocol("WM_DELETE_WINDOW", self.destroy)

        outer = ttk.Frame(self, padding=14)
        outer.pack(fill="both", expand=True)
        ttk.Label(outer, text="选择聊天记录的开始和结束日期").pack(anchor="w")
        calendars = ttk.Frame(outer)
        calendars.pack(fill="x", pady=8)
        self.start_calendar = _CalendarPane(calendars, "开始日期", start_value)
        self.start_calendar.pack(side="left", padx=(0, 8))
        self.end_calendar = _CalendarPane(calendars, "结束日期", end_value)
        self.end_calendar.pack(side="left")

        quick = ttk.Frame(outer)
        quick.pack(fill="x", pady=(0, 10))
        ttk.Label(quick, text="快捷范围：").pack(side="left")
        ttk.Button(quick, text="全部", command=lambda: self._set_quick(None, None)).pack(side="left", padx=3)
        today = date.today()
        ttk.Button(quick, text="最近 7 天", command=lambda: self._set_quick(today - timedelta(days=6), today)).pack(side="left", padx=3)
        ttk.Button(quick, text="最近 30 天", command=lambda: self._set_quick(today - timedelta(days=29), today)).pack(side="left", padx=3)
        month_start = today.replace(day=1)
        month_end = date(today.year, today.month, calendar.monthrange(today.year, today.month)[1])
        ttk.Button(quick, text="本月", command=lambda: self._set_quick(month_start, month_end)).pack(side="left", padx=3)

        buttons = ttk.Frame(outer)
        buttons.pack(fill="x")
        ttk.Button(buttons, text="取消", command=self.destroy).pack(side="right")
        ttk.Button(buttons, text="确定", command=self._confirm).pack(side="right", padx=8)

        self.update_idletasks()
        x = parent.winfo_rootx() + max(0, (parent.winfo_width() - self.winfo_width()) // 2)
        y = parent.winfo_rooty() + max(0, (parent.winfo_height() - self.winfo_height()) // 2)
        self.geometry(f"+{x}+{y}")
        self.grab_set()
        self.focus_force()

    def _set_quick(self, start_value: date | None, end_value: date | None) -> None:
        self.start_calendar.set_date(start_value)
        self.end_calendar.set_date(end_value)

    def _confirm(self) -> None:
        start_value = self.start_calendar.get_date()
        end_value = self.end_calendar.get_date()
        if start_value and end_value and start_value > end_value:
            messagebox.showerror("日期范围错误", "开始日期不能晚于结束日期。", parent=self)
            return
        self.result = (start_value, end_value)
        self.destroy()


class ExportHistoryDialog(tk.Toplevel):
    def __init__(self, parent: tk.Misc):
        super().__init__(parent)
        self.title("导出历史")
        width, height = HISTORY_DIALOG_SIZE
        min_width, min_height = HISTORY_DIALOG_MIN_SIZE
        self.geometry(f"{width}x{height}")
        self.minsize(min_width, min_height)
        self.transient(parent)
        self.entries: list[ExportHistoryEntry] = []

        outer = ttk.Frame(self, padding=12)
        outer.pack(fill="both", expand=True)
        ttk.Label(
            outer,
            text="历史导出记录",
            font=("Microsoft YaHei UI", 14, "bold"),
        ).pack(anchor="w")
        ttk.Label(
            outer,
            text="双击记录可打开文件；文件被移动或删除后，历史路径仍会保留。",
            foreground="#6A7280",
        ).pack(anchor="w", pady=(2, 9))

        table_frame = ttk.Frame(outer)
        table_frame.pack(fill="both", expand=True)
        table_frame.rowconfigure(0, weight=1)
        table_frame.columnconfigure(0, weight=1)
        columns = ("time", "type", "name", "format", "messages", "path")
        self.tree = ttk.Treeview(
            table_frame,
            columns=columns,
            show="headings",
            selectmode="browse",
        )
        for column, title in (
            ("time", "导出时间"),
            ("type", "类型"),
            ("name", "联系人/群聊"),
            ("format", "格式"),
            ("messages", "消息数"),
            ("path", "文件地址"),
        ):
            self.tree.heading(column, text=title)
        self.tree.column("time", width=150, stretch=False)
        self.tree.column("type", width=65, anchor="center", stretch=False)
        self.tree.column("name", width=210)
        self.tree.column("format", width=55, anchor="center", stretch=False)
        self.tree.column("messages", width=70, anchor="e", stretch=False)
        self.tree.column("path", width=680)
        y_scrollbar = ttk.Scrollbar(
            table_frame, orient="vertical", command=self.tree.yview
        )
        x_scrollbar = ttk.Scrollbar(
            table_frame, orient="horizontal", command=self.tree.xview
        )
        self.tree.configure(
            yscrollcommand=y_scrollbar.set,
            xscrollcommand=x_scrollbar.set,
        )
        self.tree.grid(row=0, column=0, sticky="nsew")
        y_scrollbar.grid(row=0, column=1, sticky="ns")
        x_scrollbar.grid(row=1, column=0, sticky="ew")
        self.tree.bind("<Double-1>", lambda _event: self._open_file())

        actions = ttk.Frame(outer)
        actions.pack(fill="x", pady=(10, 0))
        ttk.Button(actions, text="打开文件", command=self._open_file).pack(side="left")
        ttk.Button(
            actions,
            text="打开所在文件夹",
            command=self._open_folder,
        ).pack(side="left", padx=8)
        ttk.Button(actions, text="刷新", command=self._refresh).pack(side="left")
        ttk.Button(actions, text="关闭", command=self.destroy).pack(side="right")

        self._refresh()
        self.update_idletasks()
        x = parent.winfo_rootx() + max(0, (parent.winfo_width() - self.winfo_width()) // 2)
        y = parent.winfo_rooty() + max(0, (parent.winfo_height() - self.winfo_height()) // 2)
        self.geometry(f"+{x}+{y}")
        self.focus_force()

    def _refresh(self) -> None:
        self.entries = load_export_history()
        for item in self.tree.get_children():
            self.tree.delete(item)
        for index, entry in enumerate(self.entries):
            try:
                exported_at = datetime.fromisoformat(entry.exported_at).strftime(
                    "%Y-%m-%d %H:%M:%S"
                )
            except ValueError:
                exported_at = entry.exported_at
            self.tree.insert(
                "",
                "end",
                iid=str(index),
                values=(
                    exported_at,
                    entry.conversation_type,
                    entry.conversation_name,
                    entry.file_format,
                    f"{entry.message_count:,}",
                    entry.file_path,
                ),
            )

    def _selected_entry(self) -> ExportHistoryEntry | None:
        selection = self.tree.selection()
        if not selection:
            messagebox.showwarning("未选择记录", "请先选择一条导出历史。", parent=self)
            return None
        try:
            return self.entries[int(selection[0])]
        except (ValueError, IndexError):
            return None

    def _open_file(self) -> None:
        entry = self._selected_entry()
        if entry is not None:
            self._open_path(Path(entry.file_path), kind="文件")

    def _open_folder(self) -> None:
        entry = self._selected_entry()
        if entry is not None:
            self._open_path(Path(entry.file_path).parent, kind="文件夹")

    def _open_path(self, target: Path, *, kind: str) -> None:
        if not target.exists():
            messagebox.showwarning(
                f"{kind}不存在",
                f"历史地址仍已保留，但当前路径不存在：\n{target}",
                parent=self,
            )
            return
        try:
            os.startfile(str(target))  # type: ignore[attr-defined]
        except (AttributeError, OSError) as error:
            messagebox.showerror(f"无法打开{kind}", str(error), parent=self)


class _VoiceGuideIllustration(tk.Canvas):
    """Privacy-safe WeChat operation diagram rendered as a screenshot card."""

    def __init__(self, parent: tk.Misc, *, mobile: bool):
        super().__init__(
            parent,
            width=350,
            height=220,
            background="#F5F7FA",
            highlightbackground="#CBD5E1",
            highlightthickness=1,
        )
        self._mobile = mobile
        self._draw()

    def _draw(self) -> None:
        title = "手机微信（界面示意）" if self._mobile else "电脑微信（界面示意）"
        action = "长按语音" if self._mobile else "右键语音"
        self.create_text(
            16,
            16,
            anchor="nw",
            text=title,
            fill="#172033",
            font=("Microsoft YaHei UI", 10, "bold"),
        )
        self.create_rectangle(16, 43, 334, 201, fill="#FFFFFF", outline="#D8DEE8")
        self.create_oval(34, 63, 66, 95, fill="#DCE5F2", outline="")
        self.create_text(50, 79, text="友", fill="#526078", font=("Microsoft YaHei UI", 9, "bold"))
        self.create_rectangle(80, 62, 220, 101, fill="#95EC69", outline="#78CF52")
        self.create_text(150, 81, text=")))  0:12", fill="#1F2937", font=("Microsoft YaHei UI", 10))
        self.create_text(
            80,
            112,
            anchor="nw",
            text=f"① {action}",
            fill="#C2410C",
            font=("Microsoft YaHei UI", 9, "bold"),
        )
        self.create_line(223, 81, 255, 81, fill="#EA580C", width=2, arrow=tk.LAST)
        self.create_rectangle(256, 56, 321, 118, fill="#FFFFFF", outline="#AAB4C3")
        self.create_text(288, 75, text="复制", fill="#64748B", font=("Microsoft YaHei UI", 8))
        self.create_rectangle(259, 88, 318, 111, fill="#E8F5E3", outline="")
        self.create_text(288, 99, text="转文字", fill="#16803C", font=("Microsoft YaHei UI", 9, "bold"))
        self.create_text(
            80,
            145,
            anchor="nw",
            text="② 点击“转文字”  →  ③ 等待文字显示 ✓",
            fill="#2457A7",
            font=("Microsoft YaHei UI", 9, "bold"),
        )
        self.create_text(
            80,
            174,
            anchor="nw",
            text="文字出现后，再连接本工具并导出",
            fill="#526078",
            font=("Microsoft YaHei UI", 8),
        )


class VoiceTextGuideDialog(tk.Toplevel):
    def __init__(self, parent: tk.Misc):
        super().__init__(parent)
        self.title("语音转文字 · 详细说明")
        self.geometry("940x760")
        self.minsize(760, 620)
        self.transient(parent)

        canvas = tk.Canvas(self, background="#FFFFFF", highlightthickness=0)
        scrollbar = ttk.Scrollbar(self, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True)
        content = ttk.Frame(canvas, padding=20)
        window_id = canvas.create_window((0, 0), window=content, anchor="nw")
        content.bind(
            "<Configure>",
            lambda _event: canvas.configure(scrollregion=canvas.bbox("all")),
        )
        canvas.bind(
            "<Configure>",
            lambda event: canvas.itemconfigure(window_id, width=event.width),
        )

        ttk.Label(
            content,
            text="语音转文字怎么导出？",
            font=("Microsoft YaHei UI", 16, "bold"),
        ).pack(anchor="w")
        ttk.Label(
            content,
            text="TXT 和 PDF 默认都会使用微信已经生成的转写文字，无需在本工具里额外勾选。",
            foreground="#526078",
            wraplength=850,
        ).pack(anchor="w", pady=(4, 14))

        notice = tk.Frame(
            content,
            background="#FFF7E8",
            highlightbackground="#F2C879",
            highlightthickness=1,
            padx=13,
            pady=10,
        )
        notice.pack(fill="x")
        tk.Label(
            notice,
            text="！ 这不是语音识别功能",
            background="#FFF7E8",
            foreground="#9A5A00",
            font=("Microsoft YaHei UI", 10, "bold"),
        ).pack(anchor="w")
        tk.Label(
            notice,
            text="本工具不会上传音频，也不会自行识别语音；它只读取微信已生成并保存在聊天记录里的文字。当前电脑版微信没有单独的语音转文字总开关。",
            background="#FFF7E8",
            foreground="#6F4A15",
            justify="left",
            wraplength=830,
        ).pack(anchor="w", pady=(3, 0))

        ttk.Label(
            content,
            text="按下面 4 步操作",
            font=("Microsoft YaHei UI", 12, "bold"),
        ).pack(anchor="w", pady=(18, 8))
        for index, step in enumerate(VOICE_TEXT_GUIDE_STEPS, start=1):
            row = ttk.Frame(content)
            row.pack(fill="x", pady=4)
            tk.Label(
                row,
                text=str(index),
                width=2,
                background="#2457A7",
                foreground="#FFFFFF",
                font=("Microsoft YaHei UI", 9, "bold"),
            ).pack(side="left", anchor="n", padx=(0, 9))
            ttk.Label(row, text=step, wraplength=800, justify="left").pack(
                side="left", fill="x", expand=True
            )

        ttk.Label(
            content,
            text="截图与符号引导",
            font=("Microsoft YaHei UI", 12, "bold"),
        ).pack(anchor="w", pady=(18, 8))
        diagrams = ttk.Frame(content)
        diagrams.pack(fill="x")
        _VoiceGuideIllustration(diagrams, mobile=False).pack(
            side="left", fill="x", expand=True, padx=(0, 8)
        )
        _VoiceGuideIllustration(diagrams, mobile=True).pack(
            side="left", fill="x", expand=True, padx=(8, 0)
        )

        ttk.Label(
            content,
            text="✓ 判断是否成功：微信语音气泡下方已经出现可阅读文字。若导出结果显示“微信尚未生成转文字”，请回到微信完成上述操作后重新连接并导出。",
            foreground="#16803C",
            wraplength=850,
            justify="left",
        ).pack(anchor="w", pady=(16, 12))
        ttk.Button(content, text="我知道了", command=self.destroy).pack(anchor="e")

        self.bind("<Escape>", lambda _event: self.destroy())
        self.update_idletasks()
        x = parent.winfo_rootx() + max(0, (parent.winfo_width() - self.winfo_width()) // 2)
        y = parent.winfo_rooty() + max(0, (parent.winfo_height() - self.winfo_height()) // 2)
        self.geometry(f"+{x}+{y}")
        self.focus_force()


class StarPrompt(tk.Frame):
    """Dismissible GitHub prompt rendered inside the main application."""

    def __init__(self, parent: tk.Misc, *, on_close) -> None:
        super().__init__(parent, background="#D9E2F0")
        self._on_close = on_close

        card = tk.Frame(
            self,
            background="#FFFFFF",
            highlightbackground="#B8C6DB",
            highlightthickness=1,
            padx=11,
            pady=8,
        )
        card.pack(fill="both", expand=True)
        tk.Label(
            card,
            text="喜欢这个工具？",
            background="#FFFFFF",
            foreground="#172033",
            font=("Microsoft YaHei UI", 9, "bold"),
        ).pack(side="left")
        tk.Label(
            card,
            text="好用的话，欢迎点亮 Star 支持一下。",
            background="#FFFFFF",
            foreground="#526078",
            font=("Microsoft YaHei UI", 8),
        ).pack(side="left", padx=(12, 18))
        tk.Button(
            card,
            text="⭐ 点亮 Star",
            command=self._open_project,
            relief="flat",
            padx=9,
            pady=4,
            background="#2457A7",
            activebackground="#1D4789",
            foreground="#FFFFFF",
            activeforeground="#FFFFFF",
            font=("Microsoft YaHei UI", 8, "bold"),
            cursor="hand2",
        ).pack(side="left")
        tk.Button(
            card,
            text="忽略",
            command=self._close,
            relief="flat",
            padx=9,
            pady=4,
            background="#EEF2F7",
            activebackground="#E2E8F0",
            foreground="#526078",
            activeforeground="#172033",
            font=("Microsoft YaHei UI", 8),
            cursor="hand2",
        ).pack(side="left", padx=(8, 0))
        tk.Button(
            card,
            text="×",
            command=self._close,
            relief="flat",
            borderwidth=0,
            background="#FFFFFF",
            activebackground="#EEF2F7",
            foreground="#64748B",
            font=("Microsoft YaHei UI", 11, "bold"),
            cursor="hand2",
        ).pack(side="right")
        self.bind("<Escape>", lambda _event: self._close())

    def _close(self) -> None:
        self.destroy()
        self._on_close()

    def _open_project(self) -> None:
        webbrowser.open_new_tab(PROJECT_URL)
        self._close()


class ExporterApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title(f"微信聊天 TXT / PDF 本地导出 v{__version__}")
        self.root.geometry("1060x900")
        self.root.minsize(900, 800)
        self.service: ExporterService | None = None
        self.account: AccountLocation | None = None
        self.wechat_executable: Path | None = None
        self.capture_preparation: KeyCapturePreparation | None = None
        self.conversations: list[Conversation] = []
        self.visible_conversations: list[Conversation] = []
        self._conversation_by_iid: dict[str, Conversation] = {}
        self.login_prompted = False
        self.events: queue.Queue[tuple[str, object]] = queue.Queue()
        self._estimate_generation = 0
        self._estimate_after_id: str | None = None
        self._estimate_cache: dict[
            tuple[tuple[str, ...], int, int], ExportWorkload
        ] = {}
        self._moments_estimate_generation = 0
        self._moments_estimate_after_id: str | None = None
        self._moments_estimate_cache: dict[str, tuple[int, int]] = {}
        self._export_started_at: float | None = None
        self._latest_export_status = ""
        self._worker_active = False
        self._star_prompt_shown = False
        self._star_prompt_after_id: str | None = None

        self.account_var = tk.StringVar(value="正在自动识别当前微信账号...")
        self.search_var = tk.StringVar()
        self.conversation_type_var = tk.StringVar(value="all")
        self.output_var = tk.StringVar(value=str(Path.home() / "Desktop" / "微信聊天导出"))
        self.start_var = tk.StringVar()
        self.end_var = tk.StringVar()
        self.range_var = tk.StringVar(value="全部日期")
        self.txt_var = tk.BooleanVar(value=True)
        self.pdf_var = tk.BooleanVar(value=True)
        self.pdf_images_var = tk.BooleanVar(value=False)
        self.estimate_var = tk.StringVar(value="选择会话后自动估算")
        self.moments_estimate_var = tk.StringVar(value="单选联系人后自动估算")
        self.status_var = tk.StringVar(value="准备就绪")
        self.version_var = tk.StringVar(value="微信版本：检测中...")

        self._build_ui()
        self.search_var.trace_add("write", lambda *_: self._filter_conversations())
        self.conversation_type_var.trace_add(
            "write", lambda *_: self._type_filter_changed()
        )
        for variable in (
            self.txt_var,
            self.pdf_var,
            self.pdf_images_var,
        ):
            variable.trace_add("write", lambda *_: self._schedule_estimate())
        self.pdf_var.trace_add("write", lambda *_: self._sync_pdf_mode_state())
        self._sync_pdf_mode_state()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self.root.after(100, self._poll_events)
        self._run_worker("detect", self._detect)

    def _build_ui(self) -> None:
        outer = ttk.Frame(self.root, padding=16)
        self.outer = outer
        outer.pack(fill="both", expand=True)

        title = ttk.Label(
            outer,
            text=f"微信聊天 TXT / PDF 本地导出 v{__version__}",
            font=("Microsoft YaHei UI", 17, "bold"),
        )
        title.pack(anchor="w")
        ttk.Label(
            outer,
            text="新版功能：联系人/群聊独立目录 · 导出历史 · 历史文件快捷打开",
            foreground="#2457A7",
            font=("Microsoft YaHei UI", 9, "bold"),
        ).pack(anchor="w", pady=(2, 0))
        ttk.Label(
            outer,
            text="仅处理本人本机数据 · 只读快照 · 密钥只保存在内存 · 不上传 · 不修改微信",
            foreground="#315C91",
        ).pack(anchor="w", pady=(3, 12))

        self.star_prompt_host = ttk.Frame(outer)

        source = ttk.LabelFrame(outer, text="1. 连接微信", padding=10)
        self.source_frame = source
        source.pack(fill="x")
        source.columnconfigure(1, weight=1)
        ttk.Label(source, textvariable=self.version_var).grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 6))
        ttk.Label(source, text="当前账号").grid(row=1, column=0, sticky="w")
        ttk.Label(source, textvariable=self.account_var, foreground="#315C91").grid(
            row=1, column=1, sticky="w", padx=(8, 0)
        )
        self.connect_button = ttk.Button(
            source,
            text="连接微信并读取会话",
            command=self._connect_clicked,
            state="disabled",
        )
        self.connect_button.grid(row=2, column=1, sticky="w", pady=(9, 0))
        ttk.Label(
            source,
            text="无需选择微信目录；确认连接后会重新启动微信，登录完成即自动显示会话。",
            foreground="#6A7280",
        ).grid(row=3, column=1, sticky="w", pady=(5, 0))

        middle = ttk.Panedwindow(outer, orient="vertical")
        self.middle = middle
        middle.pack(fill="both", expand=True, pady=12)
        sessions_frame = ttk.LabelFrame(middle, text="2. 选择联系人或群聊", padding=8)
        middle.add(sessions_frame, weight=4)
        sessions_frame.rowconfigure(1, weight=1)
        sessions_frame.columnconfigure(0, weight=1)
        search_row = ttk.Frame(sessions_frame)
        search_row.grid(row=0, column=0, sticky="ew", pady=(0, 6))
        ttk.Label(search_row, text="搜索").pack(side="left")
        ttk.Entry(search_row, textvariable=self.search_var, width=36).pack(side="left", padx=8)
        ttk.Label(
            search_row,
            text="点击表头“类型 ▾”筛选联系人或群聊",
            foreground="#6A7280",
        ).pack(side="left", padx=(8, 0))
        ttk.Label(search_row, text="可按 Ctrl / Shift 多选").pack(side="right")

        columns = ("type", "name", "last", "summary")
        self.tree = ttk.Treeview(
            sessions_frame,
            columns=columns,
            show="headings",
            selectmode="extended",
            height=5,
        )
        self.tree.heading("type", text="类型 ▾")
        self.tree.heading("name", text="会话")
        self.tree.heading("last", text="最后时间")
        self.tree.heading("summary", text="最近消息")
        self.tree.column("type", width=72, anchor="center", stretch=False)
        self.tree.column("name", width=220, anchor="w")
        self.tree.column("last", width=145, anchor="w")
        self.tree.column("summary", width=500, anchor="w")
        scrollbar = ttk.Scrollbar(sessions_frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scrollbar.set)
        self.tree.tag_configure(
            "section",
            background="#E8EEF8",
            foreground="#2457A7",
            font=("Microsoft YaHei UI", 10, "bold"),
        )
        self.tree.grid(row=1, column=0, sticky="nsew")
        scrollbar.grid(row=1, column=1, sticky="ns")
        self.tree.bind(
            "<<TreeviewSelect>>",
            lambda _event: self._selection_changed(),
        )
        self.tree.bind("<Button-1>", self._tree_heading_clicked, add="+")
        self.type_filter_menu = tk.Menu(self.root, tearoff=False)
        for value, label in (
            ("all", "✓  全部类型"),
            ("contact", "联系人"),
            ("group", "群聊"),
        ):
            self.type_filter_menu.add_radiobutton(
                label=label,
                value=value,
                variable=self.conversation_type_var,
            )

        export = ttk.LabelFrame(middle, text="3. 导出", padding=10)
        middle.add(export, weight=2)
        export.columnconfigure(1, weight=1)
        ttk.Label(export, text="日期范围").grid(row=0, column=0, sticky="w")
        date_row = ttk.Frame(export)
        date_row.grid(row=0, column=1, sticky="w")
        ttk.Entry(date_row, textvariable=self.range_var, width=29, state="readonly").pack(side="left")
        ttk.Button(date_row, text="选择日期...", command=self._choose_date_range).pack(side="left", padx=8)

        ttk.Label(export, text="输出目录").grid(row=1, column=0, sticky="w", pady=7)
        ttk.Entry(export, textvariable=self.output_var).grid(row=1, column=1, sticky="ew", padx=(0, 8), pady=7)
        ttk.Button(export, text="选择...", command=self._browse_output).grid(row=1, column=2, pady=7)
        ttk.Label(export, text="格式").grid(row=2, column=0, sticky="w")
        format_row = ttk.Frame(export)
        format_row.grid(row=2, column=1, columnspan=2, sticky="w")
        ttk.Checkbutton(format_row, text="TXT", variable=self.txt_var).pack(side="left")
        ttk.Checkbutton(format_row, text="PDF", variable=self.pdf_var).pack(side="left", padx=14)
        ttk.Label(
            format_row,
            text="语音转文字：TXT / PDF 默认写入微信已有转写",
            foreground="#7A8494",
        ).pack(side="left", padx=(2, 0))
        tk.Button(
            format_row,
            text="详细 ›",
            command=self._show_voice_text_guide,
            relief="flat",
            borderwidth=0,
            background="#F0F0F0",
            activebackground="#E5EAF2",
            foreground="#2457A7",
            activeforeground="#173E78",
            font=("Microsoft YaHei UI", 9, "underline"),
            cursor="hand2",
            padx=4,
            pady=1,
        ).pack(side="left", padx=(4, 0))
        ttk.Label(export, text="聊天预计耗时").grid(
            row=3, column=0, sticky="w", pady=(7, 0)
        )
        ttk.Label(
            export,
            textvariable=self.estimate_var,
            foreground="#2457A7",
            font=("Microsoft YaHei UI", 9, "bold"),
        ).grid(row=3, column=1, columnspan=2, sticky="w", pady=(7, 0))
        ttk.Label(export, text="朋友圈预计耗时").grid(
            row=4, column=0, sticky="w", pady=(7, 0)
        )
        ttk.Label(
            export,
            textvariable=self.moments_estimate_var,
            foreground="#1C6B48",
            font=("Microsoft YaHei UI", 9, "bold"),
        ).grid(row=4, column=1, columnspan=2, sticky="w", pady=(7, 0))
        ttk.Label(export, text="导出模式").grid(row=5, column=0, sticky="w", pady=(7, 0))
        mode_row = ttk.Frame(export)
        mode_row.grid(row=5, column=1, columnspan=2, sticky="w", pady=(5, 0))
        self.quick_mode_button = ttk.Radiobutton(
            mode_row,
            text="快速（默认）",
            variable=self.pdf_images_var,
            value=False,
        )
        self.quick_mode_button.pack(side="left")
        self.full_mode_button = ttk.Radiobutton(
            mode_row,
            text="完整（手动选择）",
            variable=self.pdf_images_var,
            value=True,
        )
        self.full_mode_button.pack(side="left", padx=(16, 0))
        ttk.Label(
            export,
            text="快速/完整只影响聊天 PDF。朋友圈另存为离线 HTML + JSON + 原始照片/视频，不受聊天日期和模式影响。",
            foreground="#6A7280",
            wraplength=790,
            justify="left",
        ).grid(row=6, column=1, columnspan=2, sticky="w", pady=(3, 0))

        ttk.Separator(export, orient="horizontal").grid(
            row=7, column=0, columnspan=4, sticky="ew", pady=(11, 9)
        )
        actions = ttk.Frame(export)
        actions.grid(row=8, column=0, columnspan=4, sticky="ew")
        self.history_button = tk.Button(
            actions,
            text="◷  导出历史",
            command=self._show_history,
            background="#E8EEF8",
            activebackground="#D8E3F4",
            foreground="#234B82",
            activeforeground="#173E78",
            relief="flat",
            borderwidth=0,
            font=("Microsoft YaHei UI", 10, "bold"),
            padx=17,
            pady=8,
            cursor="hand2",
        )
        self.history_button.pack(side="left")
        self.export_button = tk.Button(
            actions,
            text="⇩  导出选中会话",
            command=self._export_clicked,
            state="disabled",
            disabledforeground="#9AA3B2",
            background="#F0F0F0",
            activebackground="#E5EAF2",
            foreground="#2457A7",
            activeforeground="#173E78",
            relief="groove",
            borderwidth=1,
            font=("Microsoft YaHei UI", 12, "bold"),
            padx=22,
            pady=9,
            cursor="hand2",
        )
        self.export_button.pack(side="right")
        self.moments_button = tk.Button(
            actions,
            text="▣  导出该人朋友圈归档",
            command=self._export_moments_clicked,
            state="disabled",
            disabledforeground="#9AA3B2",
            background="#E8F4EE",
            activebackground="#D8EADF",
            foreground="#1C6B48",
            activeforeground="#14523A",
            relief="groove",
            borderwidth=1,
            font=("Microsoft YaHei UI", 10, "bold"),
            padx=16,
            pady=9,
            cursor="hand2",
        )
        self.moments_button.pack(side="right", padx=(0, 10))

        status_row = ttk.Frame(outer)
        status_row.pack(fill="x")
        self.progress = ttk.Progressbar(status_row, maximum=100, mode="determinate")
        self.progress.pack(side="left", fill="x", expand=True)
        ttk.Label(status_row, textvariable=self.status_var, width=62).pack(side="left", padx=(10, 0))
        self.root.after_idle(self._set_initial_sash)

    def _set_initial_sash(self) -> None:
        try:
            available = self.middle.winfo_height()
            session_height = max(180, min(225, int(available * 0.40)))
            self.middle.sashpos(0, session_height)
        except tk.TclError:
            pass

    def _browse_output(self) -> None:
        selected = filedialog.askdirectory(title="选择导出目录")
        if selected:
            self.output_var.set(selected)

    def _show_history(self) -> None:
        ExportHistoryDialog(self.root)

    def _show_voice_text_guide(self) -> None:
        VoiceTextGuideDialog(self.root)

    def _sync_pdf_mode_state(self) -> None:
        enabled = self.pdf_var.get()
        if not enabled:
            self.pdf_images_var.set(False)
        state = "normal" if enabled else "disabled"
        self.quick_mode_button.configure(state=state)
        self.full_mode_button.configure(state=state)
        self._sync_moments_button_state()

    def _selection_changed(self) -> None:
        for item_id in self.tree.selection():
            if item_id not in self._conversation_by_iid:
                self.tree.selection_remove(item_id)
        self._schedule_estimate()
        self._schedule_moments_estimate()
        self._sync_moments_button_state()

    def _sync_moments_button_state(self) -> None:
        if not hasattr(self, "moments_button"):
            return
        conversations = self._selected_conversations()
        eligible, _reason = _moments_export_eligibility(conversations)
        connected = bool(self.service and self.service.archive)
        self.moments_button.configure(
            state=(
                "normal"
                if eligible and connected and not self._worker_active
                else "disabled"
            )
        )

    def _schedule_star_prompt_after_connection(self) -> None:
        if self._star_prompt_shown:
            return
        if self._star_prompt_after_id is not None:
            try:
                self.root.after_cancel(self._star_prompt_after_id)
            except tk.TclError:
                pass
        self._star_prompt_after_id = self.root.after(
            int(STAR_PROMPT_DELAY_SECONDS * 1000),
            self._show_star_prompt,
        )

    def _show_star_prompt(self) -> None:
        self._star_prompt_after_id = None
        if self._star_prompt_shown or not self.root.winfo_exists():
            return
        self._star_prompt_shown = True
        for child in self.star_prompt_host.winfo_children():
            child.destroy()
        StarPrompt(
            self.star_prompt_host,
            on_close=self._hide_star_prompt,
        ).pack(fill="x")
        self.star_prompt_host.pack(
            fill="x",
            pady=(0, 9),
            before=self.source_frame,
        )

    def _hide_star_prompt(self) -> None:
        self.star_prompt_host.pack_forget()

    def _choose_date_range(self) -> None:
        start_value = date.fromisoformat(self.start_var.get()) if self.start_var.get() else None
        end_value = date.fromisoformat(self.end_var.get()) if self.end_var.get() else None
        dialog = DateRangeDialog(self.root, start_value, end_value)
        self.root.wait_window(dialog)
        if dialog.result is None:
            return
        start_value, end_value = dialog.result
        self.start_var.set(start_value.isoformat() if start_value else "")
        self.end_var.set(end_value.isoformat() if end_value else "")
        self.range_var.set(_format_date_range(start_value, end_value))
        self._schedule_estimate()

    def _run_worker(self, name: str, func) -> None:
        if self._worker_active:
            return
        self._worker_active = True
        self.connect_button.configure(state="disabled")
        self.export_button.configure(state="disabled")
        self.moments_button.configure(state="disabled")
        thread = threading.Thread(target=self._worker_wrapper, args=(name, func), daemon=True)
        thread.start()

    def _worker_wrapper(self, name: str, func) -> None:
        try:
            value = func()
            self.events.put((f"{name}:ok", value))
        except BaseException as error:
            self.events.put((f"{name}:error", error))

    def _progress_callback(self, message: str, fraction: float) -> None:
        self.events.put(("progress", (message, fraction)))

    def _detect(self):
        version = read_wechat_version()
        accounts = discover_accounts(progress=lambda message: self._progress_callback(message, 0.1))
        executable = find_weixin_executable()
        preparation = prepare_key_capture(
            executable,
            progress=lambda message: self._progress_callback(message, 0.18),
        )
        return version, accounts, executable, preparation

    def _connect_clicked(self) -> None:
        if not self.account or not self.wechat_executable:
            messagebox.showerror(
                "尚未识别微信",
                "没有自动识别到本机微信账号。请先正常打开并登录一次微信，然后重新启动本软件。",
            )
            return
        running = bool(list_wechat_processes())
        action = (
            "软件将关闭当前微信并重新启动。请先保存尚未发送的内容；若微信只缩到托盘，软件会结束其剩余进程。\n\n"
            if running
            else "软件将启动微信。\n\n"
        )
        if not messagebox.askyesno(
            "连接微信",
            action
            + "微信出现登录确认后，请在微信窗口点击“登录”。登录成功后，本软件会自动回到前台并显示可选对话。\n\n"
            + "连接过程中会从本机进程内存临时捕获数据库主密钥；密钥不写入磁盘。继续吗？",
        ):
            self.connect_button.configure(state="normal")
            self.status_var.set("已取消连接；可随时点击“连接微信并读取会话”")
            return
        if self.service:
            self.service.close()
        self.service = ExporterService(self.account)
        self.status_var.set("正在启动微信，请稍候...")
        self.progress.configure(mode="indeterminate")
        self.progress.start(12)
        self.root.update_idletasks()
        self._run_worker("connect", self._connect_and_load)

    def _connect_and_load(self):
        assert self.service and self.wechat_executable
        if list_wechat_processes():
            request_wechat_exit(
                progress=lambda message: self._progress_callback(message, 0.12)
            )
        archive = self.service.connect_during_wechat_start(
            self.wechat_executable,
            progress=self._progress_callback,
            capture_preparation=self.capture_preparation,
        )
        return [archive.self_conversation(), *archive.conversations()]

    def _type_filter_changed(self) -> None:
        heading = {
            "all": "类型 ▾",
            "contact": "类型：联系人 ▾",
            "group": "类型：群聊 ▾",
        }.get(self.conversation_type_var.get(), "类型 ▾")
        self.tree.heading("type", text=heading)
        self._filter_conversations()

    def _tree_heading_clicked(self, event: tk.Event) -> str | None:
        if (
            self.tree.identify_region(event.x, event.y) == "heading"
            and self.tree.identify_column(event.x) == "#1"
        ):
            try:
                self.type_filter_menu.tk_popup(event.x_root, event.y_root)
            finally:
                self.type_filter_menu.grab_release()
            return "break"
        return None

    def _filter_conversations(self) -> None:
        query = self.search_var.get().strip().lower()
        filtered = [
            item
            for item in self.conversations
            if _conversation_matches_filters(
                item,
                query=query,
                type_filter=self.conversation_type_var.get(),
            )
        ]
        grouped = _group_conversations(filtered)
        self.visible_conversations = [
            conversation
            for _section, conversations in grouped
            for conversation in conversations
        ]
        self._conversation_by_iid.clear()
        for item in self.tree.get_children():
            self.tree.delete(item)
        row_index = 0
        for section, conversations in grouped:
            self.tree.insert(
                "",
                "end",
                iid=f"section:{section}",
                values=("", section, "", ""),
                tags=("section",),
            )
            for conversation in conversations:
                last = (
                    datetime.fromtimestamp(conversation.last_timestamp).strftime(
                        "%Y-%m-%d %H:%M"
                    )
                    if conversation.last_timestamp
                    else ""
                )
                item_id = f"conversation:{row_index}"
                row_index += 1
                self._conversation_by_iid[item_id] = conversation
                self.tree.insert(
                    "",
                    "end",
                    iid=item_id,
                    values=(
                        (
                            "本人"
                            if conversation.is_self
                            else ("群聊" if conversation.is_group else "联系人")
                        ),
                        conversation.display_name,
                        last,
                        conversation.summary.replace("\n", " "),
                    ),
                )
        self._schedule_estimate()
        self._schedule_moments_estimate()
        self._sync_moments_button_state()

    def _selected_conversations(self) -> tuple[Conversation, ...]:
        return tuple(
            self._conversation_by_iid[item_id]
            for item_id in self.tree.selection()
            if item_id in self._conversation_by_iid
        )

    def _schedule_estimate(self) -> None:
        self._estimate_generation += 1
        generation = self._estimate_generation
        if self._estimate_after_id is not None:
            try:
                self.root.after_cancel(self._estimate_after_id)
            except tk.TclError:
                pass
            self._estimate_after_id = None

        conversations = self._selected_conversations()
        if not conversations:
            self.estimate_var.set("选择会话后自动估算聊天导出时间")
            return
        if any(conversation.is_self for conversation in conversations):
            self.estimate_var.set("“我自己”不导出聊天，请查看下方朋友圈预计耗时")
            return
        if not self.service or not self.service.archive:
            self.estimate_var.set("连接微信后自动估算聊天导出时间")
            return
        try:
            start_timestamp, end_timestamp = self._parse_dates()
        except ValueError:
            self.estimate_var.set("日期范围无效")
            return
        key = (
            tuple(conversation.username for conversation in conversations),
            start_timestamp,
            end_timestamp,
        )
        cached = self._estimate_cache.get(key)
        if cached is not None:
            self._show_estimate(cached, len(conversations))
            return

        self.estimate_var.set("正在统计聊天消息、图片和表情...")
        self._estimate_after_id = self.root.after(
            120,
            lambda: self._start_estimate(
                generation,
                key,
                conversations,
                start_timestamp,
                end_timestamp,
            ),
        )

    def _start_estimate(
        self,
        generation: int,
        key: tuple[tuple[str, ...], int, int],
        conversations: tuple[Conversation, ...],
        start_timestamp: int,
        end_timestamp: int,
    ) -> None:
        self._estimate_after_id = None
        archive = self.service.archive if self.service else None
        if archive is None:
            return

        def count_workload() -> None:
            try:
                workload = archive.export_workload(
                    conversations,
                    start_timestamp=start_timestamp,
                    end_timestamp=end_timestamp,
                )
                self.events.put(
                    ("estimate:ok", (generation, key, workload, len(conversations)))
                )
            except BaseException as error:
                self.events.put(("estimate:error", (generation, error)))

        threading.Thread(
            target=count_workload,
            name="wechat-export-estimate",
            daemon=True,
        ).start()

    def _show_estimate(
        self,
        workload: ExportWorkload,
        conversation_count: int,
    ) -> None:
        if not self.txt_var.get() and not self.pdf_var.get():
            self.estimate_var.set("聊天导出：请先勾选 TXT 或 PDF")
            return
        if workload.message_count <= 0:
            self.estimate_var.set("聊天导出：当前会话和日期范围内没有消息")
            return
        lower, upper = estimate_export_seconds(
            workload,
            conversation_count=conversation_count,
            include_txt=self.txt_var.get(),
            include_pdf=self.pdf_var.get(),
            include_pdf_images=self.pdf_images_var.get(),
        )
        media_note = (
            " · 完整图片受磁盘/CDN影响"
            if self.pdf_var.get() and self.pdf_images_var.get()
            else " · 快速模式"
        )
        self.estimate_var.set(
            f"约 {format_duration(lower)}–{format_duration(upper)} · "
            f"{workload.message_count:,} 条 · 图片 {workload.image_count:,} · "
            f"表情 {workload.emoticon_count:,}{media_note}"
        )

    def _schedule_moments_estimate(self) -> None:
        self._moments_estimate_generation += 1
        generation = self._moments_estimate_generation
        if self._moments_estimate_after_id is not None:
            try:
                self.root.after_cancel(self._moments_estimate_after_id)
            except tk.TclError:
                pass
            self._moments_estimate_after_id = None

        conversations = self._selected_conversations()
        eligible, _reason = _moments_export_eligibility(conversations)
        if not eligible:
            self.moments_estimate_var.set("单选一个联系人或“我自己”后自动估算")
            return
        if not self.service or not self.service.archive:
            self.moments_estimate_var.set("连接微信后自动估算朋友圈归档时间")
            return
        conversation = conversations[0]
        cached = self._moments_estimate_cache.get(conversation.username)
        if cached is not None:
            self._show_moments_estimate(*cached)
            return

        self.moments_estimate_var.set("正在统计朋友圈条目和媒体...")
        self._moments_estimate_after_id = self.root.after(
            120,
            lambda: self._start_moments_estimate(generation, conversation),
        )

    def _start_moments_estimate(
        self,
        generation: int,
        conversation: Conversation,
    ) -> None:
        self._moments_estimate_after_id = None
        archive = self.service.archive if self.service else None
        if archive is None:
            return

        def count_moments() -> None:
            try:
                moments = archive.contact_moments(conversation)
                media_count = sum(len(moment.media) for moment in moments)
                self.events.put(
                    (
                        "moments-estimate:ok",
                        (generation, conversation.username, len(moments), media_count),
                    )
                )
            except BaseException as error:
                self.events.put(("moments-estimate:error", (generation, error)))

        threading.Thread(
            target=count_moments,
            name="wechat-moments-estimate",
            daemon=True,
        ).start()

    def _show_moments_estimate(self, post_count: int, media_count: int) -> None:
        if post_count <= 0:
            self.moments_estimate_var.set("朋友圈归档：本机暂未找到该联系人的动态")
            return
        lower, upper = estimate_moments_export_seconds(
            post_count=post_count,
            media_count=media_count,
        )
        self.moments_estimate_var.set(
            f"约 {format_duration(lower)}–{format_duration(upper)} · "
            f"{post_count:,} 条 · 媒体 {media_count:,} 个 · 受本机缓存/CDN影响"
        )

    def _export_clicked(self) -> None:
        conversations = self._selected_conversations()
        if not conversations:
            messagebox.showwarning("未选择会话", "请至少选择一个会话。")
            return
        if any(conversation.is_self for conversation in conversations):
            messagebox.showwarning(
                "请选择朋友圈归档",
                "“我自己”条目用于导出自己的朋友圈，请点击绿色“导出该人朋友圈归档”按钮。",
            )
            return
        if not self.txt_var.get() and not self.pdf_var.get():
            messagebox.showwarning("未选择格式", "请勾选 TXT 或 PDF。")
            return
        try:
            start, end = self._parse_dates()
        except ValueError as error:
            messagebox.showerror("日期格式错误", str(error))
            return
        if start and end and start > end:
            messagebox.showerror("日期范围错误", "开始日期不能晚于结束日期。")
            return
        if not self._calibrate_selected(conversations):
            return
        request = ExportRequest(
            conversations=conversations,
            output_dir=Path(self.output_var.get()).expanduser(),
            include_txt=self.txt_var.get(),
            include_pdf=self.pdf_var.get(),
            include_pdf_images=self.pdf_images_var.get(),
            include_wechat_voice_text=True,
            start_timestamp=start,
            end_timestamp=end,
        )
        self._export_started_at = clock.perf_counter()
        self._latest_export_status = "准备导出聊天 0%"
        self.progress["value"] = 0
        self.status_var.set("准备导出聊天 0% · 已用 0 秒")
        self._run_worker("export", lambda: self.service.export(request, progress=self._progress_callback))

    def _export_moments_clicked(self) -> None:
        conversations = self._selected_conversations()
        eligible, reason = _moments_export_eligibility(conversations)
        if not eligible:
            messagebox.showwarning("无法导出朋友圈归档", reason)
            return
        if not self.service or not self.service.archive:
            messagebox.showwarning("尚未连接微信", "请先连接微信并读取联系人。")
            return
        conversation = conversations[0]
        self._export_started_at = clock.perf_counter()
        self._latest_export_status = "准备导出朋友圈归档 0%"
        self.progress["value"] = 0
        self.status_var.set("准备导出朋友圈归档 0% · 已用 0 秒")
        output_dir = Path(self.output_var.get()).expanduser()
        self._run_worker(
            "export",
            lambda: self.service.export_moments_archive(
                conversation,
                output_dir,
                progress=self._progress_callback,
            ),
        )

    def _parse_dates(self) -> tuple[int, int]:
        start_text = self.start_var.get().strip()
        end_text = self.end_var.get().strip()
        try:
            start_value = date.fromisoformat(start_text) if start_text else None
            end_value = date.fromisoformat(end_text) if end_text else None
        except ValueError as error:
            raise ValueError("日期选择无效，请重新打开日期窗口选择。") from error
        return _date_range_timestamps(start_value, end_value)

    def _calibrate_selected(self, conversations: tuple[Conversation, ...]) -> bool:
        assert self.service and self.service.archive
        seen: set[tuple[str, int]] = set()
        for conversation in conversations:
            if conversation.is_group:
                continue
            samples = self.service.archive.calibration_samples(conversation, limit_per_sender=1)
            for sample in samples:
                key = (sample.source_db, sample.sender_id)
                if key in seen:
                    continue
                seen.add(key)
                answer = messagebox.askyesnocancel(
                    "校准旧聊天发送者",
                    _calibration_prompt(sample),
                )
                if answer is None:
                    return False
                self.service.archive.set_calibration(
                    sample.source_db, sample.sender_id, "self" if answer else "other"
                )
        return True

    def _poll_events(self) -> None:
        try:
            while True:
                kind, payload = self.events.get_nowait()
                if kind in {
                    "detect:ok",
                    "detect:error",
                    "connect:ok",
                    "connect:error",
                    "export:ok",
                    "export:error",
                }:
                    self._worker_active = False
                if kind == "progress":
                    message, fraction = payload
                    if str(self.progress.cget("mode")) == "indeterminate":
                        self.progress.stop()
                        self.progress.configure(mode="determinate")
                    self._latest_export_status = str(message)
                    if self._export_started_at is None:
                        self.status_var.set(str(message))
                    self.progress["value"] = max(0, min(100, float(fraction) * 100))
                elif kind == "estimate:ok":
                    generation, key, workload, conversation_count = payload
                    if int(generation) == self._estimate_generation:
                        self._estimate_cache[key] = workload
                        self._show_estimate(workload, int(conversation_count))
                elif kind == "estimate:error":
                    generation, _error = payload
                    if int(generation) == self._estimate_generation:
                        self.estimate_var.set("聊天导出暂时无法估算；仍可正常导出")
                elif kind == "moments-estimate:ok":
                    generation, username, post_count, media_count = payload
                    if int(generation) == self._moments_estimate_generation:
                        counts = (int(post_count), int(media_count))
                        self._moments_estimate_cache[str(username)] = counts
                        self._show_moments_estimate(*counts)
                elif kind == "moments-estimate:error":
                    generation, _error = payload
                    if int(generation) == self._moments_estimate_generation:
                        self.moments_estimate_var.set(
                            "朋友圈归档暂时无法估算；仍可正常导出"
                        )
                elif kind == "detect:ok":
                    version, accounts, executable, preparation = payload
                    self.wechat_executable = executable
                    self.capture_preparation = preparation
                    self.version_var.set(f"微信版本：{version or '未运行'}")
                    self.account = select_current_account(accounts)
                    if self.account:
                        self.account_var.set(f"已自动识别：{self.account.wxid}")
                        self.status_var.set("已识别微信，等待连接确认")
                        self.connect_button.configure(state="normal")
                        if not self.login_prompted:
                            self.login_prompted = True
                            self.root.after(250, self._connect_clicked)
                    else:
                        self.account_var.set("未找到本机微信账号")
                        self.status_var.set("请先正常打开并登录一次微信")
                        self.connect_button.configure(state="disabled")
                        messagebox.showerror(
                            "未找到微信账号",
                            "没有自动识别到微信数据。请先正常打开并登录一次微信，再重新启动本软件。",
                        )
                elif kind == "connect:ok":
                    self.progress.stop()
                    self.progress.configure(mode="determinate")
                    self.connect_button.configure(text="重新连接微信", state="normal")
                    self.conversations = list(payload)
                    self._estimate_cache.clear()
                    self._moments_estimate_cache.clear()
                    self._filter_conversations()
                    self.export_button.configure(state="normal")
                    self._sync_moments_button_state()
                    self.progress["value"] = 100
                    self.status_var.set(
                        f"已读取 {len(self.conversations)} 个联系人/群聊（已隐藏公众号）"
                    )
                    self._schedule_star_prompt_after_connection()
                    self._focus_window()
                elif kind == "export:ok":
                    result = payload
                    self._export_started_at = None
                    self._latest_export_status = ""
                    self.connect_button.configure(state="normal")
                    self.export_button.configure(state="normal")
                    self._sync_moments_button_state()
                    self.progress["value"] = 100
                    actual_duration = format_duration(result.duration_seconds)
                    try:
                        append_export_history(
                            result,
                            account_wxid=self.account.wxid if self.account else "",
                        )
                    except OSError as error:
                        result.warnings.append(f"导出历史保存失败：{error}")
                    self.status_var.set(
                        f"导出完成：{len(result.files)} 个文件 · 实际用时 {actual_duration}"
                    )
                    messagebox.showinfo(
                        "导出完成",
                        f"已生成 {len(result.files)} 个文件\n"
                        f"实际总时长：{actual_duration}\n"
                        f"保存到：\n{self.output_var.get()}"
                        + ("\n\n" + "\n".join(result.warnings) if result.warnings else ""),
                    )
                elif kind.endswith(":error"):
                    if kind == "export:error":
                        self._export_started_at = None
                        self._latest_export_status = ""
                    self.progress.stop()
                    self.progress.configure(mode="determinate")
                    if self.account and self.wechat_executable:
                        self.connect_button.configure(state="normal")
                    if self.service and self.service.archive:
                        self.export_button.configure(state="normal")
                        self._sync_moments_button_state()
                    self.status_var.set("操作失败")
                    messagebox.showerror("操作失败", str(payload))
        except queue.Empty:
            pass
        if self._export_started_at is not None:
            elapsed = clock.perf_counter() - self._export_started_at
            base = self._latest_export_status or "正在导出 0%"
            self.status_var.set(f"{base} · 已用 {format_duration(elapsed)}")
        self.root.after(100, self._poll_events)

    def _focus_window(self) -> None:
        self.root.deiconify()
        self.root.lift()
        self.root.attributes("-topmost", True)
        self.root.after(700, lambda: self.root.attributes("-topmost", False))
        self.root.focus_force()

    def _on_close(self) -> None:
        if self._star_prompt_after_id is not None:
            try:
                self.root.after_cancel(self._star_prompt_after_id)
            except tk.TclError:
                pass
        if self._moments_estimate_after_id is not None:
            try:
                self.root.after_cancel(self._moments_estimate_after_id)
            except tk.TclError:
                pass
        if self.service:
            self.service.close()
        self.root.destroy()


_PINYIN_INITIAL_BOUNDARIES = (
    (-20319, "A"),
    (-20284, "B"),
    (-19776, "C"),
    (-19219, "D"),
    (-18711, "E"),
    (-18527, "F"),
    (-18240, "G"),
    (-17923, "H"),
    (-17418, "J"),
    (-16475, "K"),
    (-16213, "L"),
    (-15641, "M"),
    (-15166, "N"),
    (-14923, "O"),
    (-14915, "P"),
    (-14631, "Q"),
    (-14150, "R"),
    (-14091, "S"),
    (-13319, "T"),
    (-12839, "W"),
    (-12557, "X"),
    (-11848, "Y"),
    (-11056, "Z"),
)


def _character_initial(character: str) -> str:
    normalized = unicodedata.normalize("NFKD", character)
    for candidate in normalized:
        if "A" <= candidate.upper() <= "Z":
            return candidate.upper()
    try:
        encoded = character.encode("gbk")
    except UnicodeEncodeError:
        return "#"
    if len(encoded) != 2:
        return "#"
    code = encoded[0] * 256 + encoded[1] - 65536
    for boundary, initial in reversed(_PINYIN_INITIAL_BOUNDARIES):
        if code >= boundary:
            return initial
    return "#"


def _name_initials(value: str) -> str:
    initials = []
    for character in value.strip():
        if character.isspace() or unicodedata.category(character).startswith("P"):
            continue
        initial = _character_initial(character)
        initials.append(initial)
    return "".join(initials) or "#"


def _conversation_section(conversation: Conversation) -> str:
    if conversation.is_self:
        return "★ 本人"
    initial = _name_initials(conversation.display_name)[:1]
    return initial if "A" <= initial <= "Z" else "#"


def _group_conversations(
    conversations: list[Conversation],
) -> list[tuple[str, list[Conversation]]]:
    grouped: dict[str, list[Conversation]] = {}
    for conversation in conversations:
        grouped.setdefault(_conversation_section(conversation), []).append(conversation)
    section_order = {letter: index for index, letter in enumerate("ABCDEFGHIJKLMNOPQRSTUVWXYZ")}

    def section_key(section: str) -> tuple[int, int]:
        if section == "★ 本人":
            return (0, 0)
        if section in section_order:
            return (1, section_order[section])
        return (2, 0)

    result: list[tuple[str, list[Conversation]]] = []
    for section in sorted(grouped, key=section_key):
        values = sorted(
            grouped[section],
            key=lambda item: (
                _name_initials(item.display_name),
                item.display_name.casefold(),
                item.username.casefold(),
            ),
        )
        result.append((section, values))
    return result


def _conversation_matches_filters(
    conversation: Conversation,
    *,
    query: str,
    type_filter: str,
) -> bool:
    if type_filter == "contact" and conversation.is_group:
        return False
    if type_filter == "group" and not conversation.is_group:
        return False
    return (
        not query
        or query in conversation.display_name.lower()
        or query in conversation.username.lower()
        or query in conversation.summary.lower()
    )


def _moments_export_eligibility(
    conversations: tuple[Conversation, ...],
) -> tuple[bool, str]:
    if len(conversations) != 1:
        return False, "请只选择一个联系人后再导出其朋友圈。"
    if conversations[0].is_group:
        return False, "朋友圈导出只支持联系人，不支持群聊。"
    return True, ""


def _calibration_prompt(sample: CalibrationSample) -> str:
    when = datetime.fromtimestamp(sample.timestamp).strftime("%Y-%m-%d %H:%M") if sample.timestamp else "未知时间"
    return (
        "旧版分库没有可靠的‘我/对方’标记，需要你确认一条样本。\n\n"
        f"数据库：{sample.source_db}\n时间：{when}\n消息：{sample.text}\n\n"
        "这条消息是你发送的吗？\n选择“是”=我，“否”=对方，“取消”=停止导出。"
    )


def _format_date_range(start_value: date | None, end_value: date | None) -> str:
    if start_value is None and end_value is None:
        return "全部日期"
    return f"{start_value.isoformat() if start_value else '不限'}  至  {end_value.isoformat() if end_value else '不限'}"


def _date_range_timestamps(
    start_value: date | None, end_value: date | None
) -> tuple[int, int]:
    start = (
        int(datetime.combine(start_value, time.min).timestamp()) if start_value else 0
    )
    end = int(datetime.combine(end_value, time.max).timestamp()) if end_value else 0
    return start, end


def main() -> None:
    try:
        require_signature_integrity()
    except RuntimeError as error:
        root = tk.Tk()
        root.withdraw()
        messagebox.showerror("软件完整性校验失败", str(error), parent=root)
        root.destroy()
        return
    _enable_windows_high_dpi()
    root = tk.Tk()
    _configure_native_fonts(root)
    ExporterApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
