"""Small optional Tk update UI; every network/disk-heavy operation runs off Tk."""
from __future__ import annotations

import queue
import sys
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import messagebox, ttk

from . import __version__
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
        ttk.Button(title_row, text=f"v{__version__}", command=self.show).pack(side="left", padx=(8, 4))
        ttk.Label(title_row, textvariable=self.status, foreground="#315C91").pack(side="left")
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
            self.notes.configure(state="normal")
            self.notes.delete("1.0", "end")
            result = self.manager.result
            self.notes.insert("end", f"当前版本：v{__version__}\n最新版本：{('v' + result.latest_version) if result.latest_version else '暂未取得'}\n\n")
            if result.releases:
                for release in result.releases:
                    self.notes.insert("end", f"v{release.version}  {release.published_at}\n{release.notes}\n\n")
            else:
                self.notes.insert("end", "以下为随程序附带的版本历史（离线可读）：\n\n" + bundled_release_notes())
            self.notes.configure(state="disabled")
            self.check_button.configure(state="disabled" if self.active or self.installing else "normal")
            can_download = (result.status == "available" and result.releases and self.manager.source is not None
                            and installation_kind() != "source")
            self.download_button.configure(state="normal" if can_download and not self.active and not self.installing else "disabled",
                                           text="安装已下载更新" if self.download else "下载并更新")

    def dismiss(self):
        self.manager.dismiss()
        self.refresh()

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
        self._worker("checked", lambda: self.manager.check(automatic=automatic))

    def show(self):
        if self.closed:
            return
        if self.dialog is not None and self.dialog.winfo_exists():
            self.dialog.lift()
            return
        self.dialog = tk.Toplevel(self.root)
        self.dialog.title("版本与更新")
        self.dialog.geometry("780x620")
        self.dialog.minsize(620, 450)
        self.dialog.transient(self.root)  # Deliberately non-modal: no grab_set.
        frame = ttk.Frame(self.dialog, padding=14)
        frame.pack(fill="both", expand=True)
        notes_frame = ttk.Frame(frame)
        notes_frame.pack(fill="both", expand=True)
        self.notes = tk.Text(notes_frame, wrap="word", height=20, font=("Microsoft YaHei UI", 10))
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
            self.download_status.set("源码运行模式仅查看更新；自动安装用于 Windows 安装版和便携版。")
        self.refresh()
        if not self.manager.result.releases:
            self.check(False)

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
