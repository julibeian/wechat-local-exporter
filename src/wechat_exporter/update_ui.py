"""Small optional Tk update UI; every network/disk-heavy operation runs off Tk."""
from __future__ import annotations

import queue
import re
import sys
import threading
import time
import tkinter as tk
import webbrowser
from pathlib import Path
from tkinter import messagebox, ttk

from . import PROJECT_URL, __version__
from .config import app_data_dir
from .errors import user_message
from .update import UpdateManager
from .updater import cleanup_finished_updates, installation_kind, stage_update


def bundled_release_notes() -> str:
    root = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parents[2]))
    try:
        return (root / "RELEASE_NOTES.md").read_text(encoding="utf-8")
    except OSError:
        return f"v{__version__}\n本地更新说明不可用，联网检查后可查看 Release 内容。"


def _plain_release_notes(notes: str) -> str:
    """Keep release content readable in a plain Tk text view."""

    normalized = notes.replace("\r\n", "\n").strip()
    normalized = re.sub(r"<!--.*?-->", "", normalized, flags=re.DOTALL)
    marker = "\n## 更新内容\n"
    if marker in normalized:
        normalized = normalized.split(marker, 1)[1].strip()
    normalized = re.sub(r"^发布日期：[^。\n]+。?\s*", "", normalized)
    normalized = re.sub(r"\[([^]]+)]\([^)]+\)", r"\1", normalized)
    normalized = normalized.replace("**", "")
    normalized = re.sub(r"(?m)^#{1,6}\s+", "", normalized)
    return normalized.strip()


def bundled_release_sections() -> tuple[tuple[str, str, str], ...]:
    """Split bundled markdown so the dialog shows one release at a time."""

    notes = bundled_release_notes().replace("\r\n", "\n")
    headings = list(
        re.finditer(r"(?m)^# v(\d+\.\d+\.\d+)\s*$", notes)
    )
    sections: list[tuple[str, str, str]] = []
    for index, heading in enumerate(headings):
        end = headings[index + 1].start() if index + 1 < len(headings) else len(notes)
        body = notes[heading.end():end].strip()
        date_match = re.search(r"(?m)^发布日期：([^。\n]+)", body)
        published = date_match.group(1).strip() if date_match else "离线版本说明"
        sections.append((heading.group(1), published, _plain_release_notes(body)))
    if sections:
        return tuple(sections)
    return ((__version__, "离线版本说明", _plain_release_notes(notes)),)


class UpdateController:
    def __init__(self, root, title_row, outer, manager: UpdateManager, *, busy, shutdown):
        self.root, self.manager, self.busy, self.shutdown = root, manager, busy, shutdown
        self.events = queue.Queue()
        self.active = False
        self.installing = False
        self.closed = False
        self.cancelled = threading.Event()
        self.dialog = None
        self.download = None
        self.plan_path = None
        self.status = tk.StringVar(value=manager.result.text if manager.config.get("last_update_check") else "等待检查更新")
        self.download_status = tk.StringVar(value="检查更新和下载均可选，不影响离线导出。")
        self.dialog_current = tk.StringVar(value=f"v{__version__}")
        self.dialog_latest = tk.StringVar(value="暂未取得")
        self.dialog_state = tk.StringVar(value=self.status.get())
        self.release_heading = tk.StringVar(value="版本说明")
        self.release_date = tk.StringVar(value="")
        self._release_entries: tuple[tuple[str, str, str], ...] = ()
        self._release_versions: tuple[str, ...] = ()
        self.version_button = ttk.Button(
            title_row,
            text=f"v{__version__}",
            command=self.show,
        )
        self.version_button.pack(side="left", padx=(8, 4))
        self.github_link = ttk.Label(
            title_row,
            text="GitHub ↗",
            foreground="#315C91",
            cursor="hand2",
            takefocus=True,
        )
        self.github_link.pack(side="left", padx=(4, 0))
        self.github_link.bind("<Button-1>", self.open_project)
        self.github_link.bind("<Return>", self.open_project)
        self.github_link.bind("<space>", self.open_project)
        self.banner = ttk.Frame(outer, padding=(8, 3))
        self.banner_text = tk.StringVar()
        ttk.Label(self.banner, textvariable=self.banner_text).pack(side="left")
        ttk.Button(self.banner, text="查看更新 ›", command=self.show).pack(side="left", padx=8)
        ttk.Button(self.banner, text="×", width=3, command=self.dismiss).pack(side="right")
        self._banner_before = None
        self._poll_id = root.after(100, self._poll)
        self._check_id = root.after(4000, self.check)
        threading.Thread(target=cleanup_finished_updates, args=(app_data_dir() / "updates",), daemon=True).start()

    def place_banner(self, before):
        self._banner_before = before
        self.refresh()

    def refresh(self):
        self.status.set(self.manager.result.text)
        if self.manager.should_show_banner():
            self.banner_text.set(f"新版本 v{self.manager.result.latest_version} 可用")
            self.banner.pack(fill="x", before=self._banner_before, pady=(2, 6))
        else:
            self.banner.pack_forget()
        if self.dialog is not None and self.dialog.winfo_exists():
            result = self.manager.result
            self.dialog_current.set(f"v{__version__}")
            self.dialog_latest.set(
                f"v{result.latest_version}" if result.latest_version else "暂未取得"
            )
            self.dialog_state.set(self.status.get())
            self._populate_release_list()
            self.check_button.configure(state="disabled" if self.active or self.installing else "normal")
            can_download = (result.status == "available" and result.releases and self.manager.source is not None
                            and installation_kind() != "source")
            self.download_button.configure(state="normal" if can_download and not self.active and not self.installing else "disabled",
                                           text="安装已下载更新" if self.download else "下载并更新")

    def dismiss(self):
        self.manager.dismiss()
        self.refresh()

    def open_project(self, _event=None):
        """Open the public project page without affecting update checks."""
        return webbrowser.open(PROJECT_URL, new=2)

    def _worker(self, operation, function):
        def run():
            try:
                self.events.put((operation, function()))
            except Exception as error:
                self.events.put(("failure", user_message(error)))
        threading.Thread(target=run, name=f"update-{operation}", daemon=True).start()

    def check(self, automatic=True):
        if self.active or self.closed or self.installing:
            return
        self.active = True
        self.status.set("正在检查更新…")
        self.dialog_state.set("正在检查更新…")
        self._worker("checked", lambda: self.manager.check(automatic=automatic))

    def show(self):
        if self.closed:
            return
        if self.dialog is not None and self.dialog.winfo_exists():
            self.dialog.lift()
            return
        self.dialog = tk.Toplevel(self.root)
        self.dialog.title("版本与更新")
        self.dialog.geometry("860x620")
        self.dialog.minsize(720, 500)
        self.dialog.transient(self.root)  # Deliberately non-modal: no grab_set.
        frame = ttk.Frame(self.dialog, padding=16)
        frame.pack(fill="both", expand=True)

        summary = ttk.LabelFrame(frame, text="更新状态", padding=(12, 9))
        summary.pack(fill="x")
        for column in range(3):
            summary.columnconfigure(column, weight=1)
        ttk.Label(summary, text="当前版本").grid(row=0, column=0, sticky="w")
        ttk.Label(summary, text="最新版本").grid(row=0, column=1, sticky="w")
        ttk.Label(summary, text="检查结果").grid(row=0, column=2, sticky="w")
        ttk.Label(
            summary,
            textvariable=self.dialog_current,
            font=("Microsoft YaHei UI", 13, "bold"),
        ).grid(row=1, column=0, sticky="w", pady=(3, 0))
        ttk.Label(
            summary,
            textvariable=self.dialog_latest,
            font=("Microsoft YaHei UI", 13, "bold"),
        ).grid(row=1, column=1, sticky="w", pady=(3, 0))
        ttk.Label(summary, textvariable=self.dialog_state).grid(
            row=1, column=2, sticky="w", pady=(3, 0)
        )

        self.release_area = ttk.Panedwindow(frame, orient="horizontal")
        self.release_area.pack(fill="both", expand=True, pady=(12, 0))
        navigation = ttk.LabelFrame(self.release_area, text="版本记录", padding=7)
        details = ttk.LabelFrame(self.release_area, text="更新内容", padding=10)
        self.release_area.add(navigation, weight=1)
        self.release_area.add(details, weight=4)
        self.release_list = tk.Listbox(
            navigation,
            width=18,
            exportselection=False,
            activestyle="none",
            font=("Microsoft YaHei UI", 10),
        )
        self.release_list.pack(fill="both", expand=True)
        self.release_list.bind("<<ListboxSelect>>", self._release_selected)
        ttk.Label(
            details,
            textvariable=self.release_heading,
            font=("Microsoft YaHei UI", 13, "bold"),
        ).pack(anchor="w")
        ttk.Label(details, textvariable=self.release_date).pack(anchor="w", pady=(2, 8))
        notes_frame = ttk.Frame(details)
        notes_frame.pack(fill="both", expand=True)
        self.notes = tk.Text(
            notes_frame,
            wrap="word",
            height=18,
            font=("Microsoft YaHei UI", 10),
            padx=10,
            pady=8,
            spacing1=2,
            spacing3=7,
        )
        scrollbar = ttk.Scrollbar(notes_frame, command=self.notes.yview)
        self.notes.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side="right", fill="y")
        self.notes.pack(side="left", fill="both", expand=True)
        self.bar = ttk.Progressbar(frame, maximum=100)
        self.bar.pack(fill="x", pady=(10, 4))
        ttk.Label(frame, textvariable=self.download_status, wraplength=720).pack(anchor="w")
        buttons = ttk.Frame(frame)
        buttons.pack(fill="x", pady=(10, 0))
        self.check_button = ttk.Button(buttons, text="重新检查", command=lambda: self.check(False))
        self.check_button.pack(side="left")
        self.download_button = ttk.Button(buttons, text="下载并更新", command=self.start_download)
        self.download_button.pack(side="right")
        if installation_kind() == "source":
            self.download_status.set("源码运行模式仅查看更新；自动安装仅用于 Windows 安装版。")
        self.refresh()
        if not self.manager.result.releases:
            self.check(False)

    def _available_release_entries(self) -> tuple[tuple[str, str, str], ...]:
        if self.manager.result.releases:
            return tuple(
                (
                    release.version,
                    release.published_at or "GitHub 正式版本",
                    _plain_release_notes(release.notes),
                )
                for release in self.manager.result.releases
            )
        return bundled_release_sections()

    def _populate_release_list(self) -> None:
        previous = ""
        if hasattr(self, "release_list"):
            selection = self.release_list.curselection()
            if selection and int(selection[0]) < len(self._release_versions):
                previous = self._release_versions[int(selection[0])]
        self._release_entries = self._available_release_entries()
        self._release_versions = tuple(entry[0] for entry in self._release_entries)
        self.release_list.delete(0, "end")
        for version in self._release_versions:
            self.release_list.insert("end", f"v{version}")
        preferred = previous or self.manager.result.latest_version or __version__
        index = (
            self._release_versions.index(preferred)
            if preferred in self._release_versions
            else 0
        )
        if self._release_entries:
            self.release_list.selection_set(index)
            self.release_list.activate(index)
            self.release_list.see(index)
            self._show_release(index)

    def _release_selected(self, _event=None) -> None:
        selection = self.release_list.curselection()
        if selection:
            self._show_release(int(selection[0]))

    def _show_release(self, index: int) -> None:
        version, published, notes = self._release_entries[index]
        self.release_heading.set(f"v{version}")
        self.release_date.set(published)
        self.notes.configure(state="normal")
        self.notes.delete("1.0", "end")
        self.notes.insert("1.0", notes or "本版本没有附加说明。")
        self.notes.configure(state="disabled")

    def start_download(self):
        if self.active or self.installing:
            return
        if self.download is not None:
            self.confirm_install()
            return
        result = self.manager.result
        source = self.manager.source
        release = next((r for r in result.releases if r.version == result.latest_version), None)
        if source is None or release is None:
            return
        self.active = True
        self.cancelled.clear()
        self.download_status.set("正在下载更新；当前程序可继续使用。")
        self.refresh()
        self._worker("downloaded", lambda: source.download(release, installation_kind(),
            progress=lambda count, total: self.events.put(("progress", (count, total))), cancelled=self.cancelled))

    def confirm_install(self):
        if self.busy():
            self.download_status.set("下载完成且 SHA256 校验通过。请等当前连接或导出结束，再点击安装。")
            return
        if not messagebox.askyesno("准备更新", "下载完成且 SHA256 校验通过。现在退出本工具、安装并重新启动新版吗？\n微信不会因此退出。", parent=self.dialog):
            return
        if self.busy():
            self.download_status.set("当前操作尚未结束，请完成后再点击安装。")
            return
        self.installing = True
        self.download_status.set("正在准备独立更新程序…")
        self.refresh()
        self._worker("staged", lambda: stage_update(self.download))

    def _poll(self):
        if self.closed:
            return
        try:
            while True:
                operation, value = self.events.get_nowait()
                if operation == "checked":
                    self.active = False
                    if self.download is not None and self.download.version != self.manager.result.latest_version:
                        self.download = None
                    self.refresh()
                elif operation == "progress":
                    count, total = value
                    self.download_status.set(f"正在下载 {count / 1024**2:.1f} / {total / 1024**2:.1f} MB")
                    if self.dialog is not None and self.dialog.winfo_exists():
                        self.bar["value"] = count / total * 100
                elif operation == "downloaded":
                    self.active = False
                    self.download = value
                    self.download_status.set("下载完成，SHA256 校验通过。")
                    self.refresh()
                    if self.dialog is not None and self.dialog.winfo_exists():
                        self.confirm_install()
                elif operation == "staged":
                    self.plan_path = value
                    self._ready_deadline = time.monotonic() + 45
                elif operation == "failure":
                    self.active = self.installing = False
                    self.download_status.set(str(value))
                    self.refresh()
        except queue.Empty:
            pass
        if self.plan_path is not None:
            directory = self.plan_path.parent
            if time.monotonic() > self._ready_deadline or (directory / "finished").is_file():
                try:
                    (directory / "abort").touch()
                except OSError:
                    pass
                self.plan_path = None
                self.installing = False
                self.download_status.set("独立更新程序未就绪，当前程序保持运行。可稍后重试。")
                self.refresh()
            elif (directory / "ready").is_file():
                self.plan_path = None
                self.shutdown()
                return
        self._poll_id = self.root.after(100, self._poll)

    def close(self):
        self.closed = True
        self.cancelled.set()
        for identifier in (self._poll_id, self._check_id):
            try:
                self.root.after_cancel(identifier)
            except tk.TclError:
                pass
