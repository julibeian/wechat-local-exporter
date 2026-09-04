from __future__ import annotations

import calendar
import base64
import ctypes
import os
import queue
import subprocess
import sys
import threading
import time as clock
import tkinter as tk
import unicodedata
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from datetime import date, datetime, time, timedelta
from pathlib import Path
from tkinter import filedialog, font as tkfont, messagebox, ttk

from . import __version__
from .archive import CalibrationSample
from .history import ExportHistoryEntry, append_export_history, load_export_history
from .instance_control import claim_primary_instance
from .integrity import require_signature_integrity
from .config import LocalConfig
from .connection import ConnectionManager
from .errors import user_message
from .update import UpdateManager
from .update_ui import UpdateController
from .updater import installation_kind
from .tray import create_tray_icon
from .models import (
    AccountLocation,
    ChatFileExportRequest,
    Conversation,
    ExportRequest,
    ExportWorkload,
    JsonlPackageRequest,
)
from .service import (
    ExportCancelled,
    ExporterService,
    RestartRequired,
    estimate_export_seconds,
    estimate_moments_export_seconds,
    format_duration,
)
from .windows import read_wechat_version


HISTORY_DIALOG_SIZE = (1380, 760)
HISTORY_DIALOG_MIN_SIZE = (1080, 580)
PANE_SASH_ALLOWANCE = 8
VOICE_TEXT_GUIDE_STEPS = (
    "在微信中打开含语音消息的聊天。",
    "电脑版：右键语音气泡，选择“转文字”；手机端：长按语音气泡，选择“转文字”。",
    "等待文字完整显示在语音气泡下方。不要在转写仍进行时立即关闭微信。",
    "回到本工具连接微信，再导出 JSON、TXT、PDF 或高级聊天资料包；这些方式都会保留微信已有转写。",
)
LOCAL_DATA_NOTICE = (
    "建议在该微信账号长期使用的 Windows 电脑上运行。本工具仅读取这台电脑本地已经保存的微信数据库，"
    "不会从手机或微信云端补齐完整历史记录。"
)

USAGE_GUIDE_SECTIONS = (
    (
        "选择导出方式",
        "聊天文字\n适合日常查看和文字分析。JSON 最快，TXT 最通用，PDF 适合阅读。\n\n"
        "AI 完整资料包\n每个会话生成一个 ZIP，包含 messages.jsonl、清单以及本机已有媒体。\n\n"
        "批量聊天文件\n集中提取聊天中发送的 Word、PDF、Excel、压缩包等普通附件。\n\n"
        "朋友圈归档\n生成可离线浏览的 HTML、JSON 和能够取得的原始媒体。",
    ),
    (
        "JSON、TXT 与 PDF",
        "JSON：默认且最快，只保存对话文字，适合程序或 AI 快速读取。\n\n"
        "TXT：纯文本，兼容范围最广。\n\n"
        "PDF 快速版：保留阅读排版，图片和表情写成文字占位。\n\n"
        "PDF 完整版：额外读取本机已有图片和表情，信息更完整，但耗时明显增加。",
    ),
    (
        "AI 完整资料包",
        "JSONL 是一行一条消息的结构化文本，媒体通过 ZIP 内相对路径与消息关联。\n\n"
        "默认包含本机已有图片、表情和不超过设定上限的视频；普通文档附件只记录元数据，"
        "请使用“批量聊天文件”提取。\n\n"
        "原始语音不装入 ZIP。微信已有转录会保存为文字，没有转录时如实标记。",
    ),
    (
        "媒体与语音",
        "本机未缓存、超过体积上限或读取失败的媒体不会被静默丢弃，导出索引会记录状态和原因。\n\n"
        "需要语音文字时，请先在微信中右键或长按语音并选择“转文字”，等待文字显示后再连接本工具。"
        "本工具不会上传音频，也不会自行运行语音识别。",
    ),
    (
        "本机缓存与隐私",
        "聊天数据库和导出文件都在本机处理，不上传聊天内容，也不修改微信数据库。\n\n"
        "同电脑、同账号再次打开时会复用经过验证的账号缓存，只刷新发生变化的数据库。"
        "缓存密钥受 Windows 当前用户 DPAPI 保护；可查询的数据库快照仍属于本机敏感数据。",
    ),
    (
        "版本与更新",
        "软件会静默检查本项目的 GitHub 正式版本。只有发现更高版本时才显示提示；网络失败不会占用主页面。\n\n"
        "更新文件下载完成后必须通过 SHA-256 校验，随后由独立更新程序完成替换。"
        "检查更新不携带微信账号、数据库路径或聊天内容。",
    ),
)


def _sync_desktop_shortcut() -> None:
    """Keep the standard shortcut on the stable installed executable.

    The frozen application has no dependency on pywin32, so the Windows Shell
    shortcut is updated through a short-lived hidden PowerShell/COM call. This
    is deliberately best-effort: a missing or redirected Desktop must never
    prevent the exporter from starting. Portable builds must not take over the
    installed application's desktop entry.
    """
    if os.name != "nt" or not getattr(sys, "frozen", False):
        return
    executable = Path(sys.executable).resolve()
    if not executable.is_file() or installation_kind(executable) != "installer":
        return
    script = (
        "$target=$env:WECHAT_EXPORTER_SHORTCUT_TARGET; "
        "$working=$env:WECHAT_EXPORTER_SHORTCUT_WORKING; "
        "$desktop=[Environment]::GetFolderPath('Desktop'); "
        "if (-not $desktop) { exit 0 }; "
        "$path=Join-Path $desktop '微信聊天本地导出工具.lnk'; "
        "$shell=New-Object -ComObject WScript.Shell; "
        "$shortcut=$shell.CreateShortcut($path); "
        "$shortcut.TargetPath=$target; "
        "$shortcut.WorkingDirectory=$working; "
        "$shortcut.IconLocation=\"$target,0\"; "
        "$shortcut.Description='WeChat local chat and Moments exporter'; "
        "$shortcut.WindowStyle=1; $shortcut.Save()"
    )
    encoded = base64.b64encode(script.encode("utf-16le")).decode("ascii")
    environment = os.environ.copy()
    environment["WECHAT_EXPORTER_SHORTCUT_TARGET"] = str(executable)
    environment["WECHAT_EXPORTER_SHORTCUT_WORKING"] = str(executable.parent)
    try:
        subprocess.run(
            [
                "powershell.exe",
                "-NoProfile",
                "-NonInteractive",
                "-WindowStyle",
                "Hidden",
                "-EncodedCommand",
                encoded,
            ],
            env=environment,
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=8,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.SubprocessError):
        pass


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

    def _confirm(self) -> None:
        start_value = self.start_calendar.get_date()
        end_value = self.end_calendar.get_date()
        if start_value and end_value and start_value > end_value:
            messagebox.showerror("日期范围错误", "开始日期不能晚于结束日期。", parent=self)
            return
        self.result = (start_value, end_value)
        self.destroy()


@dataclass(frozen=True, slots=True)
class ChatFileExportOptions:
    categories: frozenset[str]
    max_file_size_bytes: int


class ChatFileExportDialog(tk.Toplevel):
    def __init__(self, parent: tk.Misc):
        super().__init__(parent)
        self.title("批量导出聊天文件")
        self.resizable(False, False)
        self.transient(parent)
        self.result: ChatFileExportOptions | None = None
        self.category_vars = {
            category: tk.BooleanVar(value=True)
            for category in (
                "pdf",
                "word",
                "excel",
                "powerpoint",
                "archive",
                "other",
            )
        }
        self.max_size_var = tk.StringVar(value="100")

        outer = ttk.Frame(self, padding=16)
        outer.pack(fill="both", expand=True)
        ttk.Label(
            outer,
            text="聊天文件筛选",
            font=("Microsoft YaHei UI", 14, "bold"),
        ).pack(anchor="w")
        ttk.Label(
            outer,
            text="每个选中的联系人或群聊会分别生成一份 ZIP。",
            foreground="#526174",
        ).pack(anchor="w", pady=(2, 12))

        types = ttk.LabelFrame(outer, text="文件类型（默认全部）", padding=10)
        types.pack(fill="x")
        for index, (category, label) in enumerate(
            (
                ("pdf", "PDF"),
                ("word", "Word"),
                ("excel", "Excel"),
                ("powerpoint", "PowerPoint"),
                ("archive", "压缩包"),
                ("other", "其他文件"),
            )
        ):
            ttk.Checkbutton(
                types,
                text=label,
                variable=self.category_vars[category],
            ).grid(row=index // 3, column=index % 3, sticky="w", padx=(0, 28), pady=3)

        size_row = ttk.Frame(outer)
        size_row.pack(fill="x", pady=(13, 0))
        ttk.Label(size_row, text="单个文件最大体积").pack(side="left")
        ttk.Entry(size_row, textvariable=self.max_size_var, width=10).pack(
            side="left", padx=(10, 5)
        )
        ttk.Label(size_row, text="MB（0 表示不设置上限）").pack(side="left")
        ttk.Label(
            outer,
            text="未在本机缓存、无法可靠读取的附件会记录在索引中，但不会伪装成已成功导出。",
            foreground="#A04B38",
            wraplength=560,
            justify="left",
        ).pack(anchor="w", pady=(13, 0))

        actions = ttk.Frame(outer)
        actions.pack(fill="x", pady=(16, 0))
        ttk.Button(actions, text="取消", command=self.destroy).pack(side="right")
        ttk.Button(actions, text="开始导出", command=self._confirm).pack(
            side="right", padx=(0, 8)
        )
        self.bind("<Escape>", lambda _event: self.destroy())
        self.bind("<Return>", lambda _event: self._confirm())
        self.protocol("WM_DELETE_WINDOW", self.destroy)
        self.grab_set()
        self.update_idletasks()
        x = parent.winfo_rootx() + max(0, (parent.winfo_width() - self.winfo_width()) // 2)
        y = parent.winfo_rooty() + max(0, (parent.winfo_height() - self.winfo_height()) // 2)
        self.geometry(f"+{x}+{y}")
        self.focus_force()

    def _confirm(self) -> None:
        categories = frozenset(
            category
            for category, variable in self.category_vars.items()
            if variable.get()
        )
        if not categories:
            messagebox.showwarning(
                "未选择文件类型",
                "请至少选择一种聊天文件类型。",
                parent=self,
            )
            return
        try:
            max_bytes = _max_file_size_bytes(self.max_size_var.get())
        except ValueError as error:
            messagebox.showerror("体积上限无效", str(error), parent=self)
            return
        self.result = ChatFileExportOptions(categories, max_bytes)
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
        columns = ("time", "category", "type", "name", "format", "messages", "path")
        self.tree = ttk.Treeview(
            table_frame,
            columns=columns,
            show="headings",
            selectmode="browse",
        )
        for column, title in (
            ("time", "导出时间"),
            ("category", "导出类别"),
            ("type", "类型"),
            ("name", "联系人/群聊"),
            ("format", "格式"),
            ("messages", "消息数"),
            ("path", "文件地址"),
        ):
            self.tree.heading(column, text=title)
        self.tree.column("time", width=150, stretch=False)
        self.tree.column("category", width=80, anchor="center", stretch=False)
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
                    {
                        "chat": "聊天记录",
                        "chat_package": "AI 资料包",
                        "chat_files": "聊天文件",
                        "moments": "朋友圈",
                    }.get(entry.export_category, entry.export_category),
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


class UsageGuideDialog(tk.Toplevel):
    """One offline secondary document for explanations removed from the main flow."""

    def __init__(self, parent: tk.Misc):
        super().__init__(parent)
        self.title("使用说明")
        self.geometry("820x570")
        self.minsize(680, 460)
        self.transient(parent)

        outer = ttk.Frame(self, padding=16)
        outer.pack(fill="both", expand=True)
        ttk.Label(
            outer,
            text="使用说明",
            font=("Microsoft YaHei UI", 16, "bold"),
        ).pack(anchor="w")
        ttk.Label(
            outer,
            text="选择左侧主题查看说明；主页面只保留当前操作需要的选项。",
        ).pack(anchor="w", pady=(3, 12))

        content = ttk.Panedwindow(outer, orient="horizontal")
        content.pack(fill="both", expand=True)
        navigation = ttk.Frame(content, padding=(0, 0, 10, 0))
        article = ttk.Frame(content)
        content.add(navigation, weight=1)
        content.add(article, weight=4)

        self.section_list = tk.Listbox(
            navigation,
            exportselection=False,
            activestyle="none",
            width=20,
            font=("Microsoft YaHei UI", 10),
        )
        self.section_list.pack(fill="both", expand=True)
        for title, _body in USAGE_GUIDE_SECTIONS:
            self.section_list.insert("end", title)
        self.section_list.bind("<<ListboxSelect>>", self._section_changed)

        self.article_title = tk.StringVar()
        ttk.Label(
            article,
            textvariable=self.article_title,
            font=("Microsoft YaHei UI", 13, "bold"),
        ).pack(anchor="w", padx=(10, 0), pady=(2, 8))
        text_frame = ttk.Frame(article)
        text_frame.pack(fill="both", expand=True, padx=(10, 0))
        self.article = tk.Text(
            text_frame,
            wrap="word",
            relief="flat",
            font=("Microsoft YaHei UI", 10),
            padx=12,
            pady=10,
            spacing1=2,
            spacing3=7,
        )
        scrollbar = ttk.Scrollbar(text_frame, command=self.article.yview)
        self.article.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side="right", fill="y")
        self.article.pack(side="left", fill="both", expand=True)

        ttk.Button(outer, text="关闭", command=self.destroy).pack(anchor="e", pady=(12, 0))
        self.section_list.selection_set(0)
        self._show_section(0)
        self.bind("<Escape>", lambda _event: self.destroy())

    def _section_changed(self, _event=None) -> None:
        selection = self.section_list.curselection()
        if selection:
            self._show_section(int(selection[0]))

    def _show_section(self, index: int) -> None:
        title, body = USAGE_GUIDE_SECTIONS[index]
        self.article_title.set(title)
        self.article.configure(state="normal")
        self.article.delete("1.0", "end")
        self.article.insert("1.0", body)
        self.article.configure(state="disabled")


class ExporterApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title(f"微信聊天本地导出工具 v{__version__}")
        self.root.geometry("1060x900")
        self.root.minsize(900, 800)
        self.service: ExporterService | None = None
        self.account: AccountLocation | None = None
        self.wechat_executable: Path | None = None
        self.config = LocalConfig()
        self.connection = ConnectionManager(self.config)
        self._threads: list[threading.Thread] = []
        self._background_threads: list[threading.Thread] = []
        self._closing = False
        self._closed = False
        self._tray_hidden = False
        self._tray_icon = None
        self._external_update_pending = False
        self._initial_sash_after_id: str | None = None
        self._initial_sash_done = False
        self._default_selection_pending = True
        self.help_dialog: UsageGuideDialog | None = None
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
        self._active_worker_name: str | None = None
        self._export_generation = 0
        self._active_export_generation: int | None = None
        self._export_cancel_events: dict[int, threading.Event] = {}
        self._export_threads: dict[int, threading.Thread] = {}

        self.account_var = tk.StringVar(value="尚未确认当前登录账号")
        self.search_var = tk.StringVar()
        self.conversation_type_var = tk.StringVar(value="all")
        self.output_var = tk.StringVar(value=str(Path.home() / "Desktop" / "微信聊天导出"))
        self.start_var = tk.StringVar()
        self.end_var = tk.StringVar()
        self.range_var = tk.StringVar(value="全部日期")
        self.task_var = tk.StringVar(value="")
        self.chat_format_var = tk.StringVar(value="json")
        self.pdf_images_var = tk.BooleanVar(value=False)
        self.package_include_videos_var = tk.BooleanVar(value=True)
        self.package_video_limit_var = tk.StringVar(value="100")
        self.package_network_var = tk.BooleanVar(value=False)
        self.chat_file_category_vars = {
            name: tk.BooleanVar(value=True)
            for name in ("pdf", "word", "excel", "powerpoint", "archive", "other")
        }
        self.chat_file_limit_var = tk.StringVar(value="100")
        self.selection_summary_var = tk.StringVar(value="请先在上方选择对象")
        self.confirmation_var = tk.StringVar(value="选择任务后显示导出摘要")
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
            self.task_var,
            self.chat_format_var,
            self.pdf_images_var,
            self.package_include_videos_var,
            self.package_video_limit_var,
            self.package_network_var,
            self.chat_file_limit_var,
            self.output_var,
            self.range_var,
            *self.chat_file_category_vars.values(),
        ):
            variable.trace_add("write", lambda *_: self._flow_option_changed())
        self._sync_export_flow()
        self.root.protocol("WM_DELETE_WINDOW", self._hide_to_tray)
        self.root.bind("<Map>", self._on_root_mapped, add="+")
        self._initial_sash_after_id = self.root.after_idle(self._set_initial_sash)
        self.root.after(100, self._poll_events)
        if getattr(sys, "frozen", False):
            threading.Thread(target=_sync_desktop_shortcut, daemon=True).start()
        self._run_worker("detect", self._detect)

    def _build_ui(self) -> None:
        outer = ttk.Frame(self.root, padding=16)
        self.outer = outer
        outer.pack(fill="both", expand=True)

        title_row = ttk.Frame(outer)
        title_row.pack(fill="x")
        title = ttk.Label(
            title_row,
            text="微信聊天本地导出工具",
            font=("Microsoft YaHei UI", 17, "bold"),
        )
        title.pack(side="left")
        self.updates = UpdateController(self.root, title_row, outer, UpdateManager(self.config),
                                        busy=self._is_busy, shutdown=lambda: self._on_close(for_update=True))
        self.help_button = ttk.Button(
            title_row,
            text="使用说明",
            command=self._show_usage_guide,
        )
        self.help_button.pack(
            side="left",
            padx=(4, 0),
            before=self.updates.github_link,
        )
        self.header_history_button = ttk.Button(
            title_row,
            text="导出历史",
            command=self._show_history,
        )
        self.header_history_button.pack(side="right")

        source = ttk.LabelFrame(outer, text="1. 连接微信", padding=10)
        self.source_frame = source
        source.pack(fill="x")
        self.updates.place_banner(source)
        source.columnconfigure(1, weight=1)
        ttk.Label(source, textvariable=self.version_var).grid(row=0, column=2, sticky="e", padx=(8, 0))
        ttk.Label(source, text="当前微信账号").grid(row=0, column=0, sticky="w")
        ttk.Label(source, textvariable=self.account_var, foreground="#315C91").grid(
            row=0, column=1, sticky="w", padx=(8, 0)
        )
        self.connect_button = ttk.Button(
            source,
            text="连接微信并读取会话",
            command=self._connect_clicked,
            state="disabled",
        )
        self.connect_button.grid(row=1, column=0, sticky="w", pady=(8, 0))
        ttk.Label(
            source,
            text="启动时自动尝试直接连接；只有需要重新启动微信时才会请你确认。",
            foreground="#6A7280",
        ).grid(row=1, column=1, columnspan=2, sticky="w", padx=(8, 0), pady=(8, 0))

        middle = ttk.Panedwindow(outer, orient="vertical")
        self.middle = middle
        middle.pack(fill="both", expand=True, pady=12)
        middle.bind("<Configure>", lambda _event: self.root.after_idle(self._fit_export_pane))
        sessions_frame = ttk.LabelFrame(middle, text="2. 选择联系人或群聊", padding=8)
        self.sessions_frame = sessions_frame
        sessions_frame.configure(height=205)
        sessions_frame.grid_propagate(False)
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
        self.export_frame = export
        middle.add(export, weight=2)
        self._build_progressive_export_panel(export)
        self.history_button = self.header_history_button

        status_row = ttk.Frame(outer)
        # Reserve the status row before the expanding paned window.  When native
        # fonts make the requested content taller than a small window, packing
        # it after ``middle`` can place the row below the visible client area.
        status_row.pack(fill="x", side="bottom", before=middle)
        self.progress = ttk.Progressbar(status_row, maximum=100, mode="determinate")
        self.progress.pack(side="left", fill="x", expand=True)
        ttk.Label(status_row, textvariable=self.status_var, width=62).pack(side="left", padx=(10, 0))

    def _build_progressive_export_panel(self, export: ttk.LabelFrame) -> None:
        export.columnconfigure(0, weight=1)

        selection = ttk.Frame(export)
        selection.grid(row=0, column=0, sticky="ew")
        ttk.Label(selection, text="已选对象", font=("Microsoft YaHei UI", 9, "bold")).pack(side="left")
        ttk.Label(selection, textvariable=self.selection_summary_var).pack(side="left", padx=(10, 0))

        self.task_frame = ttk.Frame(export)
        self.task_frame.grid(row=1, column=0, sticky="ew", pady=(4, 0))
        ttk.Label(
            self.task_frame,
            text="1. 选择任务",
            font=("Microsoft YaHei UI", 9, "bold"),
        ).pack(side="left", padx=(0, 12))
        self.task_buttons: dict[str, ttk.Radiobutton] = {}
        for value, label in (
            ("chat", "聊天文字"),
            ("jsonl_package", "AI 完整资料包"),
            ("chat_files", "批量聊天文件"),
            ("moments", "朋友圈归档"),
        ):
            button = ttk.Radiobutton(
                self.task_frame,
                text=label,
                variable=self.task_var,
                value=value,
            )
            button.pack(side="left", padx=(0, 18))
            self.task_buttons[value] = button

        self.date_frame = ttk.Frame(export)
        self.date_frame.grid(row=2, column=0, sticky="ew", pady=(4, 0))
        date_row = ttk.Frame(self.date_frame)
        date_row.pack(anchor="w")
        ttk.Label(
            date_row,
            text="2. 选择时间",
            font=("Microsoft YaHei UI", 9, "bold"),
        ).pack(side="left", padx=(0, 12))
        ttk.Entry(date_row, textvariable=self.range_var, width=24, state="readonly").pack(side="left")
        self.all_dates_button = ttk.Button(
            date_row,
            text="全部",
            command=lambda: self._set_quick_date_range(None),
        )
        self.all_dates_button.pack(side="left", padx=(8, 3))
        self.seven_days_button = ttk.Button(
            date_row,
            text="最近 7 天",
            command=lambda: self._set_quick_date_range(7),
        )
        self.seven_days_button.pack(side="left", padx=3)
        self.one_month_button = ttk.Button(
            date_row,
            text="最近一个月",
            command=lambda: self._set_quick_date_range(30),
        )
        self.one_month_button.pack(side="left", padx=3)
        self.custom_dates_button = ttk.Button(
            date_row,
            text="自定义…",
            command=self._choose_date_range,
        )
        self.custom_dates_button.pack(side="left", padx=(3, 0))

        self.chat_options_frame = ttk.LabelFrame(
            export,
            text="3. 选择聊天文字格式",
            padding=(9, 6),
        )
        self.chat_options_frame.grid(row=3, column=0, sticky="ew", pady=(4, 0))
        format_row = ttk.Frame(self.chat_options_frame)
        format_row.pack(fill="x")
        for value, label in (
            ("json", "JSON（最快）"),
            ("txt", "TXT（纯文本）"),
            ("pdf", "PDF（阅读版）"),
        ):
            ttk.Radiobutton(
                format_row,
                text=label,
                variable=self.chat_format_var,
                value=value,
            ).pack(side="left", padx=(0, 18))
        chat_details_row = ttk.Frame(self.chat_options_frame)
        chat_details_row.pack(fill="x", pady=(6, 0))
        self.pdf_mode_frame = ttk.Frame(chat_details_row)
        ttk.Label(self.pdf_mode_frame, text="PDF 模式").pack(side="left")
        self.quick_mode_button = ttk.Radiobutton(
            self.pdf_mode_frame,
            text="快速版（媒体占位）",
            variable=self.pdf_images_var,
            value=False,
        )
        self.quick_mode_button.pack(side="left", padx=(10, 14))
        self.full_mode_button = ttk.Radiobutton(
            self.pdf_mode_frame,
            text="完整版（图片和表情）",
            variable=self.pdf_images_var,
            value=True,
        )
        self.full_mode_button.pack(side="left")
        self.chat_estimate_label = ttk.Label(
            chat_details_row,
            textvariable=self.estimate_var,
            foreground="#2457A7",
        )
        self.chat_estimate_label.pack(side="left", anchor="w")

        self.package_options_frame = ttk.LabelFrame(
            export,
            text="3. 设置 AI 完整资料包",
            padding=(7, 4),
        )
        self.package_options_frame.grid(row=4, column=0, sticky="ew", pady=(4, 0))
        ttk.Label(
            self.package_options_frame,
            text="每个会话一个 ZIP，包含 JSONL 与本机媒体。",
            wraplength=920,
            justify="left",
        ).pack(anchor="w")
        package_row = ttk.Frame(self.package_options_frame)
        package_row.pack(fill="x", pady=(5, 0))
        ttk.Checkbutton(
            package_row,
            text="包含本机视频",
            variable=self.package_include_videos_var,
        ).pack(side="left")
        self.video_limit_frame = ttk.Frame(package_row)
        self.video_limit_frame.pack(side="left", padx=(10, 18))
        ttk.Label(self.video_limit_frame, text="单个视频上限").pack(side="left")
        ttk.Entry(
            self.video_limit_frame,
            textvariable=self.package_video_limit_var,
            width=7,
        ).pack(side="left", padx=5)
        ttk.Label(self.video_limit_frame, text="MB").pack(side="left")
        ttk.Checkbutton(
            package_row,
            text="联网补全可用表情",
            variable=self.package_network_var,
        ).pack(side="left")

        self.chat_files_options_frame = ttk.LabelFrame(
            export,
            text="3. 设置批量聊天文件",
            padding=(9, 6),
        )
        self.chat_files_options_frame.grid(row=5, column=0, sticky="ew", pady=(4, 0))
        files_row = ttk.Frame(self.chat_files_options_frame)
        files_row.pack(fill="x")
        labels = {
            "pdf": "PDF",
            "word": "Word",
            "excel": "Excel",
            "powerpoint": "PPT",
            "archive": "压缩包",
            "other": "其他",
        }
        for name, variable in self.chat_file_category_vars.items():
            ttk.Checkbutton(files_row, text=labels[name], variable=variable).pack(
                side="left", padx=(0, 10)
            )
        ttk.Label(files_row, text="单个文件上限").pack(side="left", padx=(12, 0))
        ttk.Entry(files_row, textvariable=self.chat_file_limit_var, width=7).pack(
            side="left", padx=5
        )
        ttk.Label(files_row, text="MB（0=不限）").pack(side="left")

        self.moments_options_frame = ttk.LabelFrame(
            export,
            text="2. 朋友圈归档范围",
            padding=(9, 6),
        )
        self.moments_options_frame.grid(row=6, column=0, sticky="ew", pady=(4, 0))
        ttk.Label(
            self.moments_options_frame,
            text="本机已同步且仍可见的全部朋友圈。",
        ).pack(anchor="w")
        ttk.Label(
            self.moments_options_frame,
            textvariable=self.moments_estimate_var,
            foreground="#1C6B48",
        ).pack(anchor="w", pady=(4, 0))

        self.output_frame = ttk.Frame(export)
        self.output_frame.grid(row=7, column=0, sticky="ew", pady=(4, 0))
        self.output_frame.columnconfigure(1, weight=1)
        self.output_step_label = ttk.Label(
            self.output_frame,
            text="4. 保存位置",
            font=("Microsoft YaHei UI", 9, "bold"),
        )
        self.output_step_label.grid(row=0, column=0, sticky="w", padx=(0, 12))
        ttk.Entry(self.output_frame, textvariable=self.output_var).grid(
            row=0, column=1, sticky="ew", padx=(0, 8)
        )
        ttk.Button(self.output_frame, text="选择…", command=self._browse_output).grid(
            row=0, column=2
        )

        self.confirm_frame = ttk.Frame(export)
        self.confirm_frame.grid(row=8, column=0, sticky="ew", pady=(4, 0))
        self.confirm_step_label = ttk.Label(
            self.confirm_frame,
            text="5. 确认",
            font=("Microsoft YaHei UI", 9, "bold"),
        )
        self.confirm_step_label.pack(side="left", padx=(0, 12), anchor="n")
        ttk.Label(
            self.confirm_frame,
            textvariable=self.confirmation_var,
            wraplength=820,
            justify="left",
        ).pack(side="left", anchor="w")

        # Keep every task's primary action at the same bottom-right coordinate.
        # The spacer absorbs the shorter option panels without moving the button.
        export.rowconfigure(9, weight=1)
        actions = ttk.Frame(export)
        self.actions_frame = actions
        actions.grid(row=10, column=0, sticky="se", pady=(2, 0))
        self.cancel_export_button = tk.Button(
            actions,
            text="取消导出",
            command=self._cancel_export_clicked,
            state="disabled",
            disabledforeground="#9AA3B2",
            background="#FDECEC",
            activebackground="#F8D5D5",
            foreground="#A12D2D",
            relief="groove",
            borderwidth=1,
            font=("Microsoft YaHei UI", 10, "bold"),
            padx=14,
            pady=8,
            cursor="hand2",
        )
        self.export_button = tk.Button(
            actions,
            text="导出",
            command=self._dispatch_export_clicked,
            state="disabled",
            disabledforeground="#9AA3B2",
            background="#E8EEF8",
            activebackground="#D8E3F4",
            foreground="#2457A7",
            relief="groove",
            borderwidth=1,
            font=("Microsoft YaHei UI", 11, "bold"),
            width=18,
            padx=22,
            pady=8,
            cursor="hand2",
        )
        self.export_button.pack(side="right")

        for frame in (
            self.task_frame,
            self.date_frame,
            self.chat_options_frame,
            self.package_options_frame,
            self.chat_files_options_frame,
            self.moments_options_frame,
            self.output_frame,
            self.confirm_frame,
        ):
            frame.grid_remove()
        self._chat_export_pane_height = self._measure_chat_export_pane_height()

    def _measure_chat_export_pane_height(self) -> int:
        """Measure the default chat-text panel once using the active DPI/fonts."""
        chat_frames = (
            self.task_frame,
            self.date_frame,
            self.chat_options_frame,
            self.output_frame,
            self.confirm_frame,
        )
        self.pdf_mode_frame.pack_forget()
        for frame in chat_frames:
            frame.grid()
        self.root.update_idletasks()
        height = self.export_frame.winfo_reqheight()
        for frame in chat_frames:
            frame.grid_remove()
        return max(1, height)

    def _on_root_mapped(self, event: tk.Event) -> None:
        if event.widget is not self.root or self._initial_sash_done or self._closed:
            return
        if self._initial_sash_after_id is None:
            self._initial_sash_after_id = self.root.after_idle(self._set_initial_sash)

    def _set_initial_sash(self) -> None:
        self._initial_sash_after_id = None
        if self._closed:
            return
        try:
            available = self.middle.winfo_height()
            if available < 120 or not self.middle.winfo_viewable():
                return
            self.middle.sashpos(0, self._preferred_session_height())
            self._initial_sash_done = True
        except tk.TclError:
            pass

    def _preferred_session_height(self) -> int:
        """Keep the lower pane at the measured default chat-text height."""
        return max(
            60,
            self.middle.winfo_height()
            - self._chat_export_pane_height
            - PANE_SASH_ALLOWANCE,
        )

    def _fit_export_pane(self) -> None:
        if self._closed:
            return
        try:
            preferred = self._preferred_session_height()
            if abs(self.middle.sashpos(0) - preferred) > 1:
                self.middle.sashpos(0, preferred)
        except tk.TclError:
            pass

    def _browse_output(self) -> None:
        selected = filedialog.askdirectory(title="选择导出目录")
        if selected:
            self.output_var.set(selected)

    def _show_history(self) -> None:
        ExportHistoryDialog(self.root)

    def _show_usage_guide(self) -> None:
        if self.help_dialog is not None and self.help_dialog.winfo_exists():
            self.help_dialog.lift()
            self.help_dialog.focus_force()
            return
        self.help_dialog = UsageGuideDialog(self.root)

    def _show_voice_text_guide(self) -> None:
        VoiceTextGuideDialog(self.root)

    def _flow_option_changed(self) -> None:
        self._sync_export_flow()
        self._schedule_estimate()
        self._schedule_moments_estimate()

    def _sync_pdf_mode_state(self) -> None:
        """Compatibility hook retained for older callers and focused tests."""
        self._sync_export_flow()

    def _selection_changed(self) -> None:
        for item_id in self.tree.selection():
            if item_id not in self._conversation_by_iid:
                self.tree.selection_remove(item_id)
        self._sync_export_flow()
        self._schedule_estimate()
        self._schedule_moments_estimate()

    def _eligible_tasks(
        self,
        conversations: tuple[Conversation, ...],
    ) -> set[str]:
        if not conversations:
            return set()
        if any(conversation.is_self for conversation in conversations):
            return {"moments"} if len(conversations) == 1 and conversations[0].is_self else set()
        tasks = {"chat", "jsonl_package", "chat_files"}
        if len(conversations) == 1 and not conversations[0].is_group:
            tasks.add("moments")
        return tasks

    def _sync_export_flow(self) -> None:
        if not hasattr(self, "task_frame"):
            return
        conversations = self._selected_conversations()
        eligible = self._eligible_tasks(conversations)
        if not conversations:
            summary = "请先在上方选择联系人、群聊或“我自己”"
        elif len(conversations) == 1:
            item = conversations[0]
            kind = "我自己" if item.is_self else ("群聊" if item.is_group else "联系人")
            summary = f"{kind} · {item.display_name}"
        elif any(item.is_self for item in conversations):
            summary = "“我自己”不能与其他会话同时选择"
        else:
            contacts = sum(not item.is_group for item in conversations)
            groups = sum(item.is_group for item in conversations)
            summary = f"共 {len(conversations)} 个会话（联系人 {contacts}，群聊 {groups}）"
        self.selection_summary_var.set(summary)

        for name, button in self.task_buttons.items():
            button.configure(state="normal" if name in eligible else "disabled")
        self.task_frame.grid() if conversations else self.task_frame.grid_remove()

        task = self.task_var.get()
        if eligible and task not in eligible:
            default_task = next(
                name
                for name in ("chat", "jsonl_package", "chat_files", "moments")
                if name in eligible
            )
            self.task_var.set(default_task)
            return
        if task and task not in eligible:
            self.task_var.set("")
            return
        for frame in (
            self.date_frame,
            self.chat_options_frame,
            self.package_options_frame,
            self.chat_files_options_frame,
            self.moments_options_frame,
            self.output_frame,
            self.confirm_frame,
        ):
            frame.grid_remove()
        if not task:
            self.confirmation_var.set("选择任务后显示导出摘要")
            self.export_button.configure(state="disabled", text="选择任务后继续")
            self.root.after_idle(self._fit_export_pane)
            return

        if task != "moments":
            self.date_frame.grid()
        if task == "chat":
            self.chat_options_frame.grid()
            if self.chat_format_var.get() == "pdf":
                if not self.pdf_mode_frame.winfo_manager():
                    self.pdf_mode_frame.pack(
                        side="left",
                        padx=(0, 18),
                        before=self.chat_estimate_label,
                    )
            else:
                if self.pdf_images_var.get():
                    self.pdf_images_var.set(False)
                    return
                self.pdf_mode_frame.pack_forget()
        elif task == "jsonl_package":
            self.package_options_frame.grid()
            if self.package_include_videos_var.get():
                if not self.video_limit_frame.winfo_manager():
                    self.video_limit_frame.pack(side="left", padx=(10, 18))
            else:
                self.video_limit_frame.pack_forget()
        elif task == "chat_files":
            self.chat_files_options_frame.grid()
        elif task == "moments":
            self.moments_options_frame.grid()

        self.output_step_label.configure(
            text="3. 保存位置" if task == "moments" else "4. 保存位置"
        )
        self.confirm_step_label.configure(
            text="4. 确认" if task == "moments" else "5. 确认"
        )
        self.output_frame.grid()
        self.confirm_frame.grid()
        valid, reason = self._flow_options_valid(task)
        self.confirmation_var.set(reason or self._confirmation_summary(task, conversations))
        label = {
            "chat": "导出聊天文字",
            "jsonl_package": "生成 AI 完整资料包",
            "chat_files": "批量导出聊天文件",
            "moments": "导出朋友圈归档",
        }[task]
        connected = bool(self.service and self.service.archive)
        self.export_button.configure(
            text=label,
            state="normal" if connected and valid and not self._worker_active else "disabled",
        )
        self.root.after_idle(self._fit_export_pane)

    def _flow_options_valid(self, task: str) -> tuple[bool, str]:
        if not self.output_var.get().strip():
            return False, "请选择保存位置。"
        try:
            start, end = self._parse_dates() if task != "moments" else (0, 0)
        except ValueError as error:
            return False, str(error)
        if start and end and start > end:
            return False, "开始日期不能晚于结束日期。"
        if task == "jsonl_package" and self.package_include_videos_var.get():
            try:
                _video_size_bytes(self.package_video_limit_var.get())
            except ValueError as error:
                return False, str(error)
        if task == "chat_files":
            if not any(variable.get() for variable in self.chat_file_category_vars.values()):
                return False, "请至少选择一种聊天文件类型。"
            try:
                _max_file_size_bytes(self.chat_file_limit_var.get())
            except ValueError as error:
                return False, str(error)
        return True, ""

    def _confirmation_summary(
        self,
        task: str,
        conversations: tuple[Conversation, ...],
    ) -> str:
        target = conversations[0].display_name if len(conversations) == 1 else f"{len(conversations)} 个会话"
        date_text = self.range_var.get()
        if task == "chat":
            format_name = {"json": "JSON 纯文字", "txt": "TXT 纯文本", "pdf": "PDF"}.get(
                self.chat_format_var.get(),
                "聊天文字",
            )
            if self.chat_format_var.get() == "pdf":
                format_name += " 完整版" if self.pdf_images_var.get() else " 快速版"
            return f"将 {target} 在 {date_text} 内的记录导出为 {format_name}；一次只生成这一种格式。"
        if task == "jsonl_package":
            video = (
                f"包含单个不超过 {self.package_video_limit_var.get()} MB 的本机视频"
                if self.package_include_videos_var.get()
                else "不包含视频"
            )
            network = "允许微信官方媒体地址补全" if self.package_network_var.get() else "仅使用本机媒体"
            return f"将为 {target} 各生成一个 JSONL + 媒体 ZIP（{date_text}；{video}；{network}；语音仅存微信转录）。"
        if task == "chat_files":
            count = sum(variable.get() for variable in self.chat_file_category_vars.values())
            return f"将批量导出 {target} 在 {date_text} 内的 {count} 类普通文件；单个上限 {self.chat_file_limit_var.get()} MB。"
        return f"将导出 {target} 在本机已同步且仍可见的全部朋友圈，不使用聊天日期范围。"

    def _choose_date_range(self) -> None:
        start_value = date.fromisoformat(self.start_var.get()) if self.start_var.get() else None
        end_value = date.fromisoformat(self.end_var.get()) if self.end_var.get() else None
        dialog = DateRangeDialog(self.root, start_value, end_value)
        self.root.wait_window(dialog)
        if dialog.result is None:
            return
        start_value, end_value = dialog.result
        self._set_date_range(start_value, end_value)

    def _set_date_range(
        self,
        start_value: date | None,
        end_value: date | None,
    ) -> None:
        self.start_var.set(start_value.isoformat() if start_value else "")
        self.end_var.set(end_value.isoformat() if end_value else "")
        self.range_var.set(_format_date_range(start_value, end_value))
        self._schedule_estimate()

    def _set_quick_date_range(
        self,
        days: int | None,
        *,
        today: date | None = None,
    ) -> None:
        if days is None:
            self._set_date_range(None, None)
            return
        current = today or date.today()
        self._set_date_range(current - timedelta(days=days - 1), current)

    def _run_worker(
        self,
        name: str,
        func,
        *,
        operation_id: int | None = None,
    ) -> None:
        if self._worker_active or self._closing or self.updates.installing:
            return
        self._worker_active = True
        self._active_worker_name = name
        self.connect_button.configure(state="disabled")
        self.export_button.configure(state="disabled")
        if name == "export" and not self.cancel_export_button.winfo_manager():
            self.cancel_export_button.pack(side="right", padx=(0, 10))
        elif name != "export":
            self.cancel_export_button.pack_forget()
        self.cancel_export_button.configure(
            state="normal" if name == "export" else "disabled",
            text="取消导出",
        )
        thread = threading.Thread(
            target=self._worker_wrapper,
            args=(name, func, operation_id),
            daemon=True,
        )
        self._threads = [t for t in self._threads if t.is_alive()]
        self._threads.append(thread)
        if operation_id is not None:
            self._export_threads[operation_id] = thread
        thread.start()

    def _worker_wrapper(
        self,
        name: str,
        func,
        operation_id: int | None = None,
    ) -> None:
        def payload(value):
            return (operation_id, value) if operation_id is not None else value

        try:
            value = func()
            self.events.put((f"{name}:ok", payload(value)))
        except RestartRequired:
            self.events.put(("connect:restart-required", None))
        except ExportCancelled:
            self.events.put((f"{name}:cancelled", payload(None)))
        except Exception as error:
            self.events.put((f"{name}:error", payload(user_message(error))))

    def _progress_callback(self, message: str, fraction: float) -> None:
        self.events.put(("progress", (message, fraction)))

    def _export_progress_callback(
        self,
        generation: int,
        message: str,
        fraction: float,
    ) -> None:
        self.events.put(("export:progress", (generation, (message, fraction))))

    def _detect(self):
        version = read_wechat_version()
        executable = self.connection.executable()
        return version, executable

    def _connect_clicked(self, *, automatic: bool = False) -> None:
        if self._worker_active or self._closing or self.updates.installing:
            return
        self.account = None
        self.account_var.set("尚未确认当前登录账号")
        self.conversations = []
        self._default_selection_pending = True
        self.task_var.set("")
        self._filter_conversations()
        self._estimate_generation += 1
        self._moments_estimate_generation += 1
        self.status_var.set(
            "正在自动确认当前账号并尝试连接…"
            if automatic
            else "正在确认当前账号并尝试直接连接…"
        )
        self.progress.configure(mode="indeterminate")
        self.progress.start(12)
        self.root.update_idletasks()
        self._run_worker("connect", self._connect_and_load)

    def _connect_and_load(self, allow_restart=False):
        for thread in self._background_threads:
            thread.join()
        self._background_threads.clear()
        if self.service:
            self.service.close()
            self.service = None
        self.service = self.connection.connect(allow_restart=allow_restart, progress=self._progress_callback)
        archive = self.service.archive
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
        first_a_contact: str | None = None
        first_contact: str | None = None
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
                if not conversation.is_self and not conversation.is_group:
                    if first_contact is None:
                        first_contact = item_id
                    if section == "A" and first_a_contact is None:
                        first_a_contact = item_id
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
        if (
            self._default_selection_pending
            and self.conversations
            and not query
            and self.conversation_type_var.get() == "all"
        ):
            self._default_selection_pending = False
            default_item = first_a_contact or first_contact
            if default_item is not None:
                self.tree.selection_set(default_item)
                self.tree.focus(default_item)
                self.tree.see(default_item)
        self._schedule_estimate()
        self._schedule_moments_estimate()
        self._sync_export_flow()

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

        task = self.task_var.get()
        if task not in {"chat", "jsonl_package"}:
            self.estimate_var.set("选择聊天文字或 AI 完整资料包后自动估算")
            return
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
        if self._worker_active or self._closing or self.updates.installing:
            return
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

        thread = threading.Thread(
            target=count_workload,
            name="wechat-export-estimate",
            daemon=True,
        )
        self._background_threads = [t for t in self._background_threads if t.is_alive()]
        self._background_threads.append(thread)
        thread.start()

    def _show_estimate(
        self,
        workload: ExportWorkload,
        conversation_count: int,
    ) -> None:
        task = self.task_var.get()
        if task not in {"chat", "jsonl_package"}:
            return
        if workload.message_count <= 0:
            self.estimate_var.set("聊天导出：当前会话和日期范围内没有消息")
            return
        chat_format = self.chat_format_var.get()
        is_package = task == "jsonl_package"
        lower, upper = estimate_export_seconds(
            workload,
            conversation_count=conversation_count,
            include_txt=task == "chat" and chat_format == "txt",
            include_pdf=task == "chat" and chat_format == "pdf",
            include_pdf_images=(
                task == "chat" and chat_format == "pdf" and self.pdf_images_var.get()
            ),
            include_jsonl=is_package,
            include_json=task == "chat" and chat_format == "json",
        )
        media_note = (
            " · 媒体复制时间取决于本机缓存和视频大小"
            if is_package
            else " · 完整图片受本机缓存/网络影响"
            if chat_format == "pdf" and self.pdf_images_var.get()
            else " · 快速 PDF"
            if chat_format == "pdf"
            else " · 无媒体读取"
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

        if self.task_var.get() != "moments":
            self.moments_estimate_var.set("选择朋友圈归档后自动估算")
            return
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
        if self._worker_active or self._closing or self.updates.installing:
            return
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

        thread = threading.Thread(
            target=count_moments,
            name="wechat-moments-estimate",
            daemon=True,
        )
        self._background_threads = [t for t in self._background_threads if t.is_alive()]
        self._background_threads.append(thread)
        thread.start()

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

    def _new_export_operation(self) -> tuple[int, threading.Event]:
        self._export_generation += 1
        generation = self._export_generation
        cancelled = threading.Event()
        self._active_export_generation = generation
        self._export_cancel_events[generation] = cancelled
        return generation, cancelled

    def _dispatch_export_clicked(self) -> None:
        task = self.task_var.get()
        if task == "chat":
            self._export_clicked()
        elif task == "jsonl_package":
            self._export_jsonl_package_clicked()
        elif task == "chat_files":
            self._export_chat_files_clicked()
        elif task == "moments":
            self._export_moments_clicked()

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
        if not self.service or not self.service.archive:
            messagebox.showwarning("尚未连接微信", "请先连接微信并读取会话。")
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
        selected_format = self.chat_format_var.get()
        if selected_format not in {"json", "txt", "pdf"}:
            messagebox.showwarning("未选择格式", "请选择 JSON、TXT 或 PDF。")
            return
        request = ExportRequest(
            conversations=conversations,
            output_dir=Path(self.output_var.get()).expanduser(),
            include_json=selected_format == "json",
            include_jsonl=False,
            include_txt=selected_format == "txt",
            include_pdf=selected_format == "pdf",
            include_pdf_images=(
                selected_format == "pdf" and self.pdf_images_var.get()
            ),
            include_wechat_voice_text=True,
            start_timestamp=start,
            end_timestamp=end,
        )
        generation, cancelled = self._new_export_operation()
        self._export_started_at = clock.perf_counter()
        self._latest_export_status = "准备导出聊天 0%"
        self.progress["value"] = 0
        self.status_var.set("准备导出聊天 0% · 已用 0 秒")
        self._run_worker(
            "export",
            lambda: self.service.export(
                request,
                progress=lambda message, fraction: self._export_progress_callback(
                    generation,
                    message,
                    fraction,
                ),
                cancelled=cancelled,
            ),
            operation_id=generation,
        )

    def _export_jsonl_package_clicked(self) -> None:
        conversations = self._selected_conversations()
        if not conversations or any(item.is_self for item in conversations):
            messagebox.showwarning(
                "无法生成资料包",
                "请选择一个或多个联系人/群聊，不要包含“我自己”。",
            )
            return
        if not self.service or not self.service.archive:
            messagebox.showwarning("尚未连接微信", "请先连接微信并读取会话。")
            return
        try:
            start, end = self._parse_dates()
            video_limit = (
                _video_size_bytes(self.package_video_limit_var.get())
                if self.package_include_videos_var.get()
                else 0
            )
        except ValueError as error:
            messagebox.showerror("设置错误", str(error))
            return
        if start and end and start > end:
            messagebox.showerror("日期范围错误", "开始日期不能晚于结束日期。")
            return
        if not self._calibrate_selected(conversations):
            return
        request = JsonlPackageRequest(
            conversations=conversations,
            output_dir=Path(self.output_var.get()).expanduser(),
            include_videos=self.package_include_videos_var.get(),
            max_video_size_bytes=video_limit,
            allow_network_media=self.package_network_var.get(),
            start_timestamp=start,
            end_timestamp=end,
        )
        generation, cancelled = self._new_export_operation()
        self._export_started_at = clock.perf_counter()
        self._latest_export_status = "准备生成 AI 完整资料包 0%"
        self.progress["value"] = 0
        self.status_var.set("准备生成 AI 完整资料包 0% · 已用 0 秒")
        self._run_worker(
            "export",
            lambda: self.service.export_jsonl_package(
                request,
                progress=lambda message, fraction: self._export_progress_callback(
                    generation,
                    message,
                    fraction,
                ),
                cancelled=cancelled,
            ),
            operation_id=generation,
        )

    def _export_chat_files_clicked(self) -> None:
        conversations = self._selected_conversations()
        if not conversations:
            messagebox.showwarning("未选择会话", "请至少选择一个联系人或群聊。")
            return
        if any(conversation.is_self for conversation in conversations):
            messagebox.showwarning(
                "无法导出聊天文件",
                "“我自己”不能参与聊天文件导出；请选择联系人或群聊。",
            )
            return
        if not self.service or not self.service.archive:
            messagebox.showwarning("尚未连接微信", "请先连接微信并读取会话。")
            return
        try:
            start, end = self._parse_dates()
        except ValueError as error:
            messagebox.showerror("日期格式错误", str(error))
            return
        if start and end and start > end:
            messagebox.showerror("日期范围错误", "开始日期不能晚于结束日期。")
            return

        categories = frozenset(
            name
            for name, variable in self.chat_file_category_vars.items()
            if variable.get()
        )
        if not categories:
            messagebox.showwarning("未选择文件类型", "请至少选择一种聊天文件类型。")
            return
        try:
            max_file_size_bytes = _max_file_size_bytes(self.chat_file_limit_var.get())
        except ValueError as error:
            messagebox.showerror("设置错误", str(error))
            return
        if not self._calibrate_selected(conversations):
            return
        request = ChatFileExportRequest(
            conversations=conversations,
            output_dir=Path(self.output_var.get()).expanduser(),
            categories=categories,
            max_file_size_bytes=max_file_size_bytes,
            start_timestamp=start,
            end_timestamp=end,
        )
        generation, cancelled = self._new_export_operation()
        self._export_started_at = clock.perf_counter()
        self._latest_export_status = "准备导出聊天文件 0%"
        self.progress["value"] = 0
        self.status_var.set("准备导出聊天文件 0% · 已用 0 秒")
        self._run_worker(
            "export",
            lambda: self.service.export_chat_files(
                request,
                progress=lambda message, fraction: self._export_progress_callback(
                    generation,
                    message,
                    fraction,
                ),
                cancelled=cancelled,
            ),
            operation_id=generation,
        )

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
        generation, cancelled = self._new_export_operation()
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
                progress=lambda message, fraction: self._export_progress_callback(
                    generation,
                    message,
                    fraction,
                ),
                cancelled=cancelled,
            ),
            operation_id=generation,
        )

    def _cancel_export_clicked(self) -> None:
        generation = self._active_export_generation
        if generation is None or self._closing:
            return
        worker = self._export_threads.get(generation)
        cancelled = self._export_cancel_events.get(generation)
        if worker is None or not worker.is_alive() or cancelled is None:
            return
        cancelled.set()
        self._worker_active = False
        self._active_worker_name = None
        self._active_export_generation = None
        self._export_started_at = None
        self._latest_export_status = ""
        self.progress.stop()
        self.progress.configure(mode="determinate")
        self.progress["value"] = 0
        self.connect_button.configure(state="normal")
        if self.service and self.service.archive:
            self._sync_export_flow()
        self.cancel_export_button.configure(state="disabled", text="取消导出")
        self.cancel_export_button.pack_forget()
        self.status_var.set("导出已取消，可继续操作")

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
        if self._closed:
            return
        try:
            while True:
                kind, payload = self.events.get_nowait()
                if kind == "tray:show":
                    self._restore_from_tray()
                    continue
                if kind == "tray:exit":
                    self._request_real_exit()
                    if self._closed:
                        return
                    continue
                if kind == "instance:update-exit":
                    self._request_external_update_exit()
                    if self._closed:
                        return
                    continue
                if kind.startswith("export:"):
                    generation, export_payload = payload
                    generation = int(generation)
                    if kind == "export:progress":
                        if generation != self._active_export_generation:
                            continue
                        message, fraction = export_payload
                        self._latest_export_status = str(message)
                        self.progress["value"] = max(
                            0,
                            min(100, float(fraction) * 100),
                        )
                        continue
                    self._export_cancel_events.pop(generation, None)
                    self._export_threads.pop(generation, None)
                    if generation != self._active_export_generation:
                        # A cancelled predecessor finishes silently. It must
                        # never reset or overwrite a newer export's page state.
                        continue
                    self._active_export_generation = None
                    payload = export_payload
                if kind in {
                    "detect:ok",
                    "detect:error",
                    "connect:ok",
                    "connect:error",
                    "connect:restart-required",
                    "export:ok",
                    "export:error",
                    "export:cancelled",
                }:
                    self._worker_active = False
                    self._active_worker_name = None
                if kind.startswith("export:") and kind != "export:progress":
                    self.cancel_export_button.configure(
                        state="disabled",
                        text="取消导出",
                    )
                    self.cancel_export_button.pack_forget()
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
                    version, executable = payload
                    self.wechat_executable = executable
                    self.version_var.set(f"微信版本：{version or '未运行'}")
                    self.account_var.set("尚未确认当前登录账号")
                    self.status_var.set("已找到微信，正在自动确认当前账号…")
                    self.connect_button.configure(state="normal")
                    if not self._closing:
                        self._connect_clicked(automatic=True)
                elif kind == "connect:restart-required":
                    self.progress.stop()
                    self.progress.configure(mode="determinate")
                    self.connect_button.configure(state="normal")
                    if not self._closing and messagebox.askyesno(
                        "需要重新启动微信",
                        "当前微信需要重新启动一次才能读取数据库。是否继续？\n\n"
                        "请先保存尚未发送的内容。确认后会关闭并启动微信（若尚未运行则启动）；"
                        "必要时会结束托盘中的剩余微信进程。登录由微信自身负责。\n\n" + LOCAL_DATA_NOTICE,
                    ):
                        self._run_worker("connect", lambda: self._connect_and_load(True))
                    else:
                        self.status_var.set("已取消重新启动微信，可随时重新连接")
                elif kind == "connect:ok":
                    self.progress.stop()
                    self.progress.configure(mode="determinate")
                    self.connect_button.configure(text="重新连接微信", state="normal")
                    self.conversations = list(payload)
                    self.account = self.service.account
                    if getattr(self.service, "using_saved_account_cache", False):
                        self.account_var.set(
                            f"{self.account.wxid}  ·  ✓ 已从同账号本机缓存快速打开"
                        )
                    else:
                        self.account_var.set(
                            f"{self.account.wxid}  ·  ✓ 已确认与当前微信进程一致"
                        )
                    self._estimate_cache.clear()
                    self._moments_estimate_cache.clear()
                    self._filter_conversations()
                    self.export_button.configure(state="normal")
                    self._sync_export_flow()
                    self.progress["value"] = 100
                    self.status_var.set(
                        f"已读取 {len(self.conversations)} 个联系人/群聊（已隐藏公众号）"
                    )
                    if not self._closing and not self._tray_hidden:
                        self._focus_window()
                elif kind == "export:ok":
                    result = payload
                    self._export_started_at = None
                    self._latest_export_status = ""
                    self.connect_button.configure(state="normal")
                    self.export_button.configure(state="normal")
                    self._sync_export_flow()
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
                    if self._closing:
                        continue
                    completion_message = (
                        f"已生成 {len(result.files)} 个文件；实际用时 {actual_duration}。"
                    )
                    if self._external_update_pending:
                        pass
                    elif self._tray_hidden:
                        self._notify_from_tray("导出完成", completion_message)
                    else:
                        messagebox.showinfo(
                            "导出完成",
                            f"已生成 {len(result.files)} 个文件\n"
                            f"实际总时长：{actual_duration}\n"
                            f"保存到：\n{self.output_var.get()}"
                            + ("\n\n" + "\n".join(result.warnings) if result.warnings else ""),
                        )
                elif kind == "export:cancelled":
                    self._export_started_at = None
                    self._latest_export_status = ""
                    self.progress.stop()
                    self.progress.configure(mode="determinate")
                    self.progress["value"] = 0
                    self.connect_button.configure(state="normal")
                    if self.service and self.service.archive:
                        self.export_button.configure(state="normal")
                        self._sync_export_flow()
                    self.status_var.set("导出已取消，本次导出数据已清理且未保留")
                elif kind.endswith(":error"):
                    if kind == "export:error":
                        self._export_started_at = None
                        self._latest_export_status = ""
                    self.progress.stop()
                    self.progress.configure(mode="determinate")
                    self.connect_button.configure(state="normal")
                    if self.service and self.service.archive:
                        self.export_button.configure(state="normal")
                        self._sync_export_flow()
                    self.status_var.set("操作失败")
                    if kind == "detect:error":
                        self.version_var.set("微信版本：尚未检测到")
                        self.status_var.set("请打开并登录微信，然后点击连接")
                    elif not self._closing and not self._external_update_pending:
                        if kind == "export:error" and self._tray_hidden:
                            self._notify_from_tray("导出失败", str(payload), error=True)
                        else:
                            messagebox.showerror("操作失败", str(payload))
        except queue.Empty:
            pass
        if self._export_started_at is not None:
            elapsed = clock.perf_counter() - self._export_started_at
            base = self._latest_export_status or "正在导出 0%"
            self.status_var.set(f"{base} · 已用 {format_duration(elapsed)}")
        if self._external_update_pending and not self._is_busy():
            self._external_update_pending = False
            self._on_close(for_update=True)
            return
        self.root.after(100, self._poll_events)

    def _focus_window(self) -> None:
        self._tray_hidden = False
        self.root.deiconify()
        self.root.lift()
        self.root.attributes("-topmost", True)
        self.root.after(700, lambda: self.root.attributes("-topmost", False))
        self.root.focus_force()

    def _ensure_tray_icon(self):
        if self._tray_icon is not None:
            return self._tray_icon
        icon = create_tray_icon(
            tooltip=f"微信聊天本地导出工具 v{__version__}",
            on_show=lambda: self.events.put(("tray:show", None)),
            on_exit=lambda: self.events.put(("tray:exit", None)),
        )
        if icon is None or not icon.start():
            return None
        self._tray_icon = icon
        return icon

    def _hide_to_tray(self) -> None:
        if self._closing or self._closed:
            return
        if self.updates.installing:
            self.status_var.set("正在交接给更新程序，请稍候…")
            return
        icon = self._ensure_tray_icon()
        if icon is None:
            self.root.iconify()
            return
        self._tray_hidden = True
        self.root.withdraw()
        activity = (
            "当前导出会继续在后台完成。"
            if self._active_export_generation is not None
            else "程序仍在后台运行。"
        )
        icon.notify("已缩小到系统托盘", activity)

    def _restore_from_tray(self) -> None:
        if self._closing or self._closed:
            return
        self._focus_window()

    def _request_real_exit(self) -> None:
        if self._closing or self._closed:
            return
        generation = self._active_export_generation
        if generation is not None:
            self._restore_from_tray()
            if not messagebox.askyesno(
                "真正退出",
                "当前导出尚未完成。真正退出会取消本次导出并清理未完成文件，是否继续？",
                parent=self.root,
            ):
                return
            cancelled = self._export_cancel_events.get(generation)
            if cancelled is not None:
                cancelled.set()
            self.status_var.set("正在取消导出并安全退出…")
        self._on_close()

    def _request_external_update_exit(self) -> None:
        """Exit when current work is safe so a local installer can replace us."""
        if self._closing or self._closed:
            return
        self._external_update_pending = True
        self.connect_button.configure(state="disabled")
        self.export_button.configure(state="disabled")
        if self._is_busy():
            message = "新版已准备；当前任务完成后将自动退出并安装。"
            self.status_var.set(message)
            if self._tray_hidden:
                self._notify_from_tray("等待安全更新", message)
            return
        self._external_update_pending = False
        self._on_close(for_update=True)

    def _notify_from_tray(self, title: str, message: str, *, error: bool = False) -> None:
        icon = self._ensure_tray_icon()
        if icon is not None:
            icon.notify(title, message, error=error)

    def _is_busy(self) -> bool:
        return (
            self._closing
            or self._worker_active
            or any(
                thread.is_alive()
                for thread in self._threads + self._background_threads
            )
        )

    def _on_close(self, *, for_update=False) -> None:
        if self.updates.installing and not for_update:
            self.status_var.set("正在交接给更新程序，请稍候…")
            return
        self._closing = True
        self.updates.close()
        self.connect_button.configure(state="disabled")
        self.export_button.configure(state="disabled")
        # Do not remove SQLite snapshots while a worker is still querying/writing.
        if any(t.is_alive() for t in self._threads + self._background_threads):
            self.status_var.set("正在等待当前操作结束并清理临时数据库…")
            self.root.after(150, lambda: self._on_close(for_update=for_update))
            return
        if self._moments_estimate_after_id is not None:
            try:
                self.root.after_cancel(self._moments_estimate_after_id)
            except tk.TclError:
                pass
        if self._initial_sash_after_id is not None:
            try:
                self.root.after_cancel(self._initial_sash_after_id)
            except tk.TclError:
                pass
            self._initial_sash_after_id = None
        if self.service:
            try:
                self.service.close()
            except OSError:
                self.status_var.set("临时数据库仍被占用，正在重试清理…")
                self.root.after(300, lambda: self._on_close(for_update=for_update))
                return
        self._closed = True
        if self._tray_icon is not None:
            self._tray_icon.stop()
            self._tray_icon = None
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


def _max_file_size_bytes(value: str) -> int:
    try:
        megabytes = Decimal(value.strip())
    except (InvalidOperation, ValueError):
        raise ValueError("请输入大于或等于 0 的数字，例如 100；0 表示不限制。") from None
    if not megabytes.is_finite() or megabytes < 0:
        raise ValueError("请输入大于或等于 0 的数字，例如 100；0 表示不限制。")
    size = megabytes * Decimal(1024 * 1024)
    if size > Decimal(2**63 - 1):
        raise ValueError("单个文件体积上限过大，请输入较小的数值。")
    return int(size)


def _video_size_bytes(value: str) -> int:
    size = _max_file_size_bytes(value)
    if size <= 0:
        raise ValueError("视频上限必须大于 0 MB，例如 100。")
    return size


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
    coordinator = claim_primary_instance()
    if coordinator is None:
        return
    try:
        _enable_windows_high_dpi()
        root = tk.Tk()
        _configure_native_fonts(root)
        app = ExporterApp(root)
        coordinator.start(
            on_show=lambda: app.events.put(("tray:show", None)),
            on_update_exit=lambda: app.events.put(("instance:update-exit", None)),
        )
        root.mainloop()
    finally:
        coordinator.close()


if __name__ == "__main__":
    main()
