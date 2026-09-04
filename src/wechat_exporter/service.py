from __future__ import annotations

from collections import deque
from collections.abc import Callable, Iterable, Iterator
from concurrent.futures import Future, ThreadPoolExecutor, TimeoutError as FutureTimeoutError
from dataclasses import replace
import errno
import hashlib
from pathlib import Path
import shutil
import tempfile
import threading
import time

from .archive import SenderCalibration, WeChatArchive
from .attachments import AttachmentResolver, export_conversation_attachments
from .crypto import (
    DecryptedWorkspace,
    collect_required_databases,
    extract_database_keys,
)
from .exporters import (
    PdfTranscriptWriter,
    TxtTranscriptWriter,
    safe_filename,
)
from .jsonl_package import ChatVideoLocator, export_conversation_jsonl_package
from .json_exporter import JsonTextTranscriptWriter
from .key_capture import KeyCapturePreparation, capture_keys_during_wechat_start
from .media import MediaResolver
from .moments_archive import MomentsArchiveWriter
from .models import (
    AccountLocation,
    ChatFileExportRequest,
    Conversation,
    ExportRequest,
    ExportResult,
    ExportWorkload,
    JsonlPackageRequest,
    Message,
    PdfImage,
)
from .windows import list_wechat_processes
from .content import WECHAT_VOICE_TEXT_PREFIX
from .database_cache import AccountDatabaseCache, PersistentDecryptedWorkspace


_DIRECTORY_PUBLISH_RETRY_DELAYS = (0.12, 0.3, 0.7, 1.4, 2.5)


class RestartRequired(RuntimeError):
    pass


class ExportCancelled(RuntimeError):
    """Raised after a user-cancelled export has removed its new artifacts."""


class ExporterService:
    def __init__(self, account: AccountLocation, *, process_id: int | None = None):
        self.account = account
        self.process_id = process_id
        self.targets = collect_required_databases(account.db_dir)
        self.workspace: DecryptedWorkspace | PersistentDecryptedWorkspace | None = None
        self.archive: WeChatArchive | None = None
        self.using_saved_account_cache = False
        self._export_operation_lock = threading.RLock()

    def close(self) -> None:
        with self._export_operation_lock:
            if self.workspace:
                self.workspace.close()
                self.workspace = None
                self.archive = None

    def connect_without_restart(
        self,
        *,
        progress: Callable[[str, float], None] | None = None,
        calibrations: list[SenderCalibration] | None = None,
    ) -> WeChatArchive:
        self.using_saved_account_cache = False
        cache = AccountDatabaseCache(self.account)
        cached_keys = cache.load_keys(self.targets)
        if cached_keys is not None:
            if progress:
                progress("已验证同账号本机缓存，正在增量同步数据库...", 0.08)
            return self._prepare(
                cached_keys,
                progress=progress,
                calibrations=calibrations,
                database_cache=cache,
            )
        try:
            keys = extract_database_keys(self.targets, progress=progress, process_id=self.process_id)
        except RuntimeError:
            raise RestartRequired(
                "当前微信版本需要在启动瞬间从本机进程内存中临时捕获数据库主密钥。"
            ) from None
        return self._prepare(
            keys,
            progress=progress,
            calibrations=calibrations,
            database_cache=cache,
            save_cached_keys=True,
        )

    def connect_from_saved_cache(
        self,
        *,
        progress: Callable[[str, float], None] | None = None,
        calibrations: list[SenderCalibration] | None = None,
    ) -> WeChatArchive:
        """Open the last confirmed account without requiring WeChat to run."""

        self.using_saved_account_cache = True
        cache = AccountDatabaseCache(self.account)
        keys = cache.load_keys(self.targets)
        if keys is None:
            raise RestartRequired("本机尚无可验证的账号缓存，需要启动微信完成首次连接。")
        if progress:
            progress("已验证上次账号缓存，正在快速打开...", 0.08)
        return self._prepare(
            keys,
            progress=progress,
            calibrations=calibrations,
            database_cache=cache,
        )

    def connect_during_wechat_start(
        self,
        executable: Path,
        *,
        progress: Callable[[str, float], None] | None = None,
        calibrations: list[SenderCalibration] | None = None,
        capture_preparation: KeyCapturePreparation | None = None,
    ) -> WeChatArchive:
        self.using_saved_account_cache = False
        if list_wechat_processes():
            raise RuntimeError("检测到微信仍在运行，自动退出尚未完成。请重新点击连接微信。")

        def capture_progress(message: str) -> None:
            if progress:
                progress(message, 0.25)

        # Resolve again after login: the user may choose a different account.
        from .connection import resolve_running_account

        resolved = []

        def current_targets(pid):
            current = resolve_running_account(pid)
            if current is None:
                return []
            resolved[:] = [current]
            return collect_required_databases(current.account.db_dir)

        keys = capture_keys_during_wechat_start(
            executable,
            [],
            progress=capture_progress,
            preparation=capture_preparation,
            target_resolver=current_targets,
        )
        if not resolved or resolve_running_account(resolved[0].pid) != resolved[0]:
            raise RuntimeError("当前账号已变化，请重新连接")
        self.account = resolved[0].account
        self.process_id = resolved[0].pid
        self.targets = collect_required_databases(self.account.db_dir)
        return self._prepare(
            keys,
            progress=progress,
            calibrations=calibrations,
            database_cache=AccountDatabaseCache(self.account),
            save_cached_keys=True,
        )

    def _prepare(
        self,
        keys,
        *,
        progress: Callable[[str, float], None] | None,
        calibrations: list[SenderCalibration] | None,
        database_cache: AccountDatabaseCache | None = None,
        save_cached_keys: bool = False,
    ) -> WeChatArchive:
        self.close()
        self.workspace = (
            database_cache.workspace(keys)
            if database_cache is not None
            else DecryptedWorkspace(self.account.db_dir, keys)
        )

        def snapshot_progress(message: str, fraction: float) -> None:
            if progress:
                progress(message, 0.35 + fraction * 0.6)

        try:
            self.workspace.prepare(progress=snapshot_progress)
            self.archive = WeChatArchive(self.account, self.workspace, calibrations)
            self.archive.load_metadata()
            if save_cached_keys and database_cache is not None:
                try:
                    database_cache.save_keys(keys, self.targets)
                except OSError:
                    # A cache write failure must not invalidate a usable live session.
                    pass
        except BaseException:
            self.close()
            raise
        if progress:
            if isinstance(self.workspace, PersistentDecryptedWorkspace):
                progress(
                    f"会话索引已就绪（复用 {self.workspace.reused_count} 个，"
                    f"刷新 {self.workspace.refreshed_count} 个数据库）",
                    1.0,
                )
            else:
                progress("会话索引已就绪", 1.0)
        return self.archive

    def export(
        self,
        request: ExportRequest,
        *,
        progress: Callable[[str, float], None] | None = None,
        cancelled: threading.Event | None = None,
    ) -> ExportResult:
        # A newly requested export may start while a cancelled predecessor is
        # still closing files. Serialize the service work so their output
        # reservations and cleanup can never overlap.
        with self._export_operation_lock:
            return self._export(
                request,
                progress=progress,
                cancelled=cancelled,
            )

    def _export(
        self,
        request: ExportRequest,
        *,
        progress: Callable[[str, float], None] | None = None,
        cancelled: threading.Event | None = None,
    ) -> ExportResult:
        if not self.archive:
            raise RuntimeError("请先读取会话")
        if request.include_jsonl:
            raise ValueError("JSONL 已改为 AI 完整资料包，请使用高级资料包导出。")
        selected_formats = sum(
            (request.include_json, request.include_txt, request.include_pdf)
        )
        if selected_formats != 1:
            raise ValueError("普通聊天每次只能选择 JSON、TXT、PDF 中的一种格式。")
        if any(conversation.is_self for conversation in request.conversations):
            raise ValueError("“我自己”只用于朋友圈归档，不能导出聊天记录。")
        started_at = time.perf_counter()
        _raise_if_cancelled(cancelled)
        if progress:
            progress("正在统计聊天消息数量...", 0.0)
        workload = self.archive.export_workload(
            request.conversations,
            start_timestamp=request.start_timestamp,
            end_timestamp=request.end_timestamp,
        )
        _raise_if_cancelled(cancelled)
        total_messages = workload.message_count
        completed_messages = 0
        progress_step = max(1, total_messages // 200)
        last_progress_at = started_at
        created_directories: list[Path] = []
        created_files: list[Path] = []
        _mkdir_tracked(request.output_dir, created_directories)
        result = ExportResult()
        voice_count = 0
        official_voice_count = 0
        missing_voice_count = 0
        skipped_media_count = 0
        media_resolver = (
            MediaResolver(self.account, self.archive.self_wxid)
            if request.include_pdf and request.include_pdf_images
            else None
        )
        try:
            for conversation in request.conversations:
                _raise_if_cancelled(cancelled)
                conversation_dir = _conversation_output_dir(
                    request.output_dir,
                    conversation,
                )
                _mkdir_tracked(conversation_dir, created_directories)
                stem = _available_stem(
                    conversation_dir,
                    safe_filename(conversation.display_name),
                    include_txt=request.include_txt,
                    include_pdf=request.include_pdf,
                    include_jsonl=False,
                    include_json=request.include_json,
                )
                json_writer = None
                txt_writer = None
                pdf_writer = None
                if request.include_json:
                    json_path = conversation_dir / f"{stem}.json"
                    json_writer = JsonTextTranscriptWriter(
                        json_path,
                        conversation,
                        start_timestamp=request.start_timestamp,
                        end_timestamp=request.end_timestamp,
                    )
                    created_files.append(json_path)
                if request.include_txt:
                    txt_path = conversation_dir / f"{stem}.txt"
                    txt_writer = TxtTranscriptWriter(
                        txt_path,
                        conversation,
                        start_timestamp=request.start_timestamp,
                        end_timestamp=request.end_timestamp,
                    )
                    created_files.append(txt_path)
                if request.include_pdf:
                    pdf_path = conversation_dir / f"{stem}.pdf"
                    pdf_writer = PdfTranscriptWriter(
                        pdf_path,
                        conversation,
                        start_timestamp=request.start_timestamp,
                        end_timestamp=request.end_timestamp,
                    )
                    created_files.append(pdf_path)
                count = 0
                try:
                    messages = self.archive.iter_messages(
                        conversation,
                        start_timestamp=request.start_timestamp,
                        end_timestamp=request.end_timestamp,
                    )
                    messages_with_images = _iter_messages_with_images(
                        messages,
                        media_resolver,
                        cancelled=cancelled,
                    )
                    for message, image in messages_with_images:
                        _raise_if_cancelled(cancelled)
                        if message.message_type == 34:
                            voice_count += 1
                            has_official_text = message.content.startswith(
                                WECHAT_VOICE_TEXT_PREFIX
                            )
                            if request.include_wechat_voice_text:
                                if has_official_text:
                                    official_voice_count += 1
                                else:
                                    missing_voice_count += 1
                                    message = replace(
                                        message,
                                        content="[语音]（微信尚未生成转文字）",
                                    )
                            else:
                                message = replace(message, content="[语音]")
                        if json_writer:
                            json_writer.write(message)
                        if txt_writer:
                            txt_writer.write(message)
                        if pdf_writer:
                            if message.media is not None and media_resolver is None:
                                skipped_media_count += 1
                            pdf_writer.write(message, image=image)
                        count += 1
                        completed_messages += 1
                        now = time.perf_counter()
                        if progress and (
                            completed_messages >= total_messages
                            or completed_messages % progress_step == 0
                            or now - last_progress_at >= 0.2
                        ):
                            raw_fraction = min(
                                1.0,
                                completed_messages / total_messages
                                if total_messages > 0
                                else 1.0,
                            )
                            fraction = min(0.98, raw_fraction * 0.98)
                            remaining = (
                                (now - started_at)
                                / completed_messages
                                * max(0, total_messages - completed_messages)
                                if completed_messages > 0
                                else 0.0
                            )
                            mode = (
                                "正在导出聊天图片 PDF"
                                if request.include_pdf and request.include_pdf_images
                                else "正在快速导出聊天"
                            )
                            progress(
                                f"{mode} {raw_fraction * 100:.0f}% · "
                                f"{completed_messages}/{total_messages} 条"
                                + (
                                    f" · 剩余约 {format_duration(remaining)}"
                                    if remaining >= 1
                                    else ""
                                ),
                                fraction,
                            )
                            last_progress_at = now
                finally:
                    if json_writer:
                        json_writer.close()
                    if txt_writer:
                        txt_writer.close()
                    if pdf_writer:
                        pdf_writer.close()
                _raise_if_cancelled(cancelled)
                if json_writer:
                    result.files.append(json_writer.path)
                    result.file_conversations[json_writer.path] = conversation
                    result.file_categories[json_writer.path] = "chat"
                if txt_writer:
                    result.files.append(txt_writer.path)
                    result.file_conversations[txt_writer.path] = conversation
                    result.file_categories[txt_writer.path] = "chat"
                if pdf_writer:
                    result.files.append(pdf_writer.path)
                    result.file_conversations[pdf_writer.path] = conversation
                    result.file_categories[pdf_writer.path] = "chat"
                result.message_counts[conversation.username] = count
        except Exception:
            _cleanup_cancelled_files(created_files, created_directories)
            raise
        if request.include_wechat_voice_text and voice_count:
            result.warnings.append(
                f"微信语音转文字：已写入 {official_voice_count} 条，"
                f"微信尚未生成 {missing_voice_count} 条。"
            )
        if skipped_media_count:
            result.warnings.append(
                f"快速 PDF：跳过 {skipped_media_count} 张图片/表情的读取，"
                "已用可搜索占位文字写入。"
            )
        if media_resolver and media_resolver.stats.requested:
            result.warnings.append(media_resolver.stats.summary())
            result.warnings.extend(sorted(media_resolver.stats.issues))
        _cleanup_then_raise_if_cancelled(
            cancelled,
            files=created_files,
            directories=created_directories,
        )
        if progress:
            progress("正在完成聊天文件...", 0.99)
        result.duration_seconds = time.perf_counter() - started_at
        _cleanup_then_raise_if_cancelled(
            cancelled,
            files=created_files,
            directories=created_directories,
        )
        if progress:
            progress(
                f"聊天导出 100% · {completed_messages} 条 · "
                f"实际用时 {format_duration(result.duration_seconds)}",
                1.0,
            )
        return result

    def export_jsonl_package(
        self,
        request: JsonlPackageRequest,
        *,
        progress: Callable[[str, float], None] | None = None,
        cancelled: threading.Event | None = None,
    ) -> ExportResult:
        with self._export_operation_lock:
            return self._export_jsonl_package(
                request,
                progress=progress,
                cancelled=cancelled,
            )

    def _export_jsonl_package(
        self,
        request: JsonlPackageRequest,
        *,
        progress: Callable[[str, float], None] | None = None,
        cancelled: threading.Event | None = None,
    ) -> ExportResult:
        if not self.archive:
            raise RuntimeError("请先读取会话")
        if not request.conversations:
            raise ValueError("请至少选择一个联系人或群聊。")
        if any(conversation.is_self for conversation in request.conversations):
            raise ValueError("“我自己”只用于朋友圈归档，不能导出聊天资料包。")
        if request.include_videos and request.max_video_size_bytes <= 0:
            raise ValueError("包含视频时，单个视频最大体积必须大于 0。")

        started_at = time.perf_counter()
        _raise_if_cancelled(cancelled)
        created_directories: list[Path] = []
        created_files: list[Path] = []
        _mkdir_tracked(request.output_dir, created_directories)
        result = ExportResult()
        resolver = MediaResolver(
            self.account,
            self.archive.self_wxid,
            allow_network=request.allow_network_media,
        )
        video_locator = ChatVideoLocator(self.account)
        conversation_total = len(request.conversations)
        try:
            for conversation_index, conversation in enumerate(
                request.conversations,
                start=1,
            ):
                _raise_if_cancelled(cancelled)
                conversation_dir = _conversation_output_dir(
                    request.output_dir,
                    conversation,
                )
                _mkdir_tracked(conversation_dir, created_directories)
                if progress:
                    progress(
                        f"正在生成 AI 聊天资料包 {conversation_index}/{conversation_total} · "
                        f"当前：{conversation.display_name}",
                        (conversation_index - 1) / conversation_total,
                    )

                def package_progress(message: str, fraction: float) -> None:
                    if progress:
                        overall = (
                            (conversation_index - 1) + max(0.0, min(1.0, fraction))
                        ) / conversation_total
                        progress(
                            f"资料包 {conversation_index}/{conversation_total} · "
                            f"{conversation.display_name} · {message}",
                            min(0.99, overall),
                        )

                messages = self.archive.iter_messages(
                    conversation,
                    start_timestamp=request.start_timestamp,
                    end_timestamp=request.end_timestamp,
                )
                archive_result = export_conversation_jsonl_package(
                    account=self.account,
                    self_wxid=self.archive.self_wxid,
                    conversation=conversation,
                    messages=messages,
                    output_dir=conversation_dir,
                    request=request,
                    image_resolver=resolver,
                    video_locator=video_locator,
                    progress=package_progress,
                    check_cancelled=lambda: _raise_if_cancelled(cancelled),
                )
                created_files.append(archive_result.path)
                _raise_if_cancelled(cancelled)
                result.files.append(archive_result.path)
                result.file_conversations[archive_result.path] = conversation
                result.file_categories[archive_result.path] = "chat_package"
                result.message_counts[conversation.username] = archive_result.message_count
                counts = archive_result.status_counts
                unavailable = counts.get("not_available_locally", 0)
                too_large = counts.get("too_large", 0)
                read_errors = counts.get("read_error", 0)
                if unavailable or too_large or read_errors:
                    result.warnings.append(
                        f"{conversation.display_name}：媒体本机缺失 {unavailable}，"
                        f"视频过大 {too_large}，读取错误 {read_errors}；"
                        "每一项都已在 messages.jsonl 中保留原因。"
                    )
        except ExportCancelled:
            _cleanup_cancelled_files(created_files, created_directories)
            raise

        result.duration_seconds = time.perf_counter() - started_at
        _cleanup_then_raise_if_cancelled(
            cancelled,
            files=created_files,
            directories=created_directories,
        )
        if progress:
            progress(
                f"AI 聊天资料包 100% · {len(result.files)} 个会话 ZIP · "
                f"实际用时 {format_duration(result.duration_seconds)}",
                1.0,
            )
        return result

    def export_chat_files(
        self,
        request: ChatFileExportRequest,
        *,
        progress: Callable[[str, float], None] | None = None,
        cancelled: threading.Event | None = None,
    ) -> ExportResult:
        with self._export_operation_lock:
            return self._export_chat_files(
                request,
                progress=progress,
                cancelled=cancelled,
            )

    def _export_chat_files(
        self,
        request: ChatFileExportRequest,
        *,
        progress: Callable[[str, float], None] | None = None,
        cancelled: threading.Event | None = None,
    ) -> ExportResult:
        if not self.archive:
            raise RuntimeError("请先读取会话")
        if not request.conversations:
            raise ValueError("请至少选择一个联系人或群聊。")
        if any(conversation.is_self for conversation in request.conversations):
            raise ValueError("“我自己”不能参与聊天文件导出。")
        if request.max_file_size_bytes < 0:
            raise ValueError("单个文件最大体积不能小于 0。")

        started_at = time.perf_counter()
        _raise_if_cancelled(cancelled)
        created_directories: list[Path] = []
        created_files: list[Path] = []
        _mkdir_tracked(request.output_dir, created_directories)
        if progress:
            progress("正在索引当前微信账号的本机聊天附件...", 0.01)
        resolver = AttachmentResolver(self.account, request.conversations)
        result = ExportResult()
        conversation_total = len(request.conversations)
        try:
            for conversation_index, conversation in enumerate(
                request.conversations,
                start=1,
            ):
                _raise_if_cancelled(cancelled)
                conversation_dir = _conversation_output_dir(
                    request.output_dir,
                    conversation,
                )
                _mkdir_tracked(conversation_dir, created_directories)
                if progress:
                    progress(
                        f"正在导出聊天文件 {conversation_index}/{conversation_total} · "
                        f"当前：{conversation.display_name}",
                        (conversation_index - 1) / conversation_total,
                    )

                def conversation_progress(message: str, fraction: float) -> None:
                    if progress:
                        overall = (
                            (conversation_index - 1) + max(0.0, min(1.0, fraction))
                        ) / conversation_total
                        progress(
                            f"正在导出聊天文件 {conversation_index}/{conversation_total} · "
                            f"当前：{conversation.display_name} · {message}",
                            min(0.99, overall),
                        )

                messages = self.archive.iter_messages(
                    conversation,
                    start_timestamp=request.start_timestamp,
                    end_timestamp=request.end_timestamp,
                )
                archive_result = export_conversation_attachments(
                    output_dir=conversation_dir,
                    conversation=conversation,
                    messages=messages,
                    resolver=resolver,
                    categories=request.categories,
                    max_file_size_bytes=request.max_file_size_bytes,
                    start_timestamp=request.start_timestamp,
                    end_timestamp=request.end_timestamp,
                    progress=conversation_progress,
                    check_cancelled=lambda: _raise_if_cancelled(cancelled),
                )
                created_files.append(archive_result.path)
                _raise_if_cancelled(cancelled)
                result.files.append(archive_result.path)
                result.file_conversations[archive_result.path] = conversation
                result.file_categories[archive_result.path] = "chat_files"
                result.message_counts[conversation.username] = archive_result.message_count
                counts = archive_result.status_counts
                unavailable = counts["not_available_locally"]
                too_large = counts["too_large"]
                read_errors = counts["read_error"]
                unsupported = counts["unsupported"]
                if unavailable or too_large or read_errors or unsupported:
                    result.warnings.append(
                        f"{conversation.display_name}：成功 {counts['exported']}，"
                        f"过大 {too_large}，本机缺失 {unavailable}，"
                        f"筛选未包含 {unsupported}，读取错误 {read_errors}；"
                        "详情见 ZIP 内 files.jsonl。"
                    )
        except ExportCancelled:
            _cleanup_cancelled_files(created_files, created_directories)
            raise
        result.duration_seconds = time.perf_counter() - started_at
        _cleanup_then_raise_if_cancelled(
            cancelled,
            files=created_files,
            directories=created_directories,
        )
        if progress:
            progress(
                f"聊天文件导出 100% · {len(result.files)} 个会话 ZIP · "
                f"实际用时 {format_duration(result.duration_seconds)}",
                1.0,
            )
        return result

    def export_moments_archive(
        self,
        conversation: Conversation,
        output_dir: Path,
        *,
        progress: Callable[[str, float], None] | None = None,
        cancelled: threading.Event | None = None,
    ) -> ExportResult:
        with self._export_operation_lock:
            return self._export_moments_archive(
                conversation,
                output_dir,
                progress=progress,
                cancelled=cancelled,
            )

    def _export_moments_archive(
        self,
        conversation: Conversation,
        output_dir: Path,
        *,
        progress: Callable[[str, float], None] | None = None,
        cancelled: threading.Event | None = None,
    ) -> ExportResult:
        """Export one contact's or the user's own Moments as an offline archive."""
        if not self.archive:
            raise RuntimeError("请先读取会话")
        if conversation.is_group:
            raise ValueError("朋友圈归档只支持单个联系人，不支持群聊。")

        started_at = time.perf_counter()
        _raise_if_cancelled(cancelled)
        if progress:
            progress("正在读取该联系人的全部朋友圈...", 0.02)
        moments = self.archive.contact_moments(conversation)
        _raise_if_cancelled(cancelled)
        if not moments:
            raise ValueError(
                "本机朋友圈库中没有找到该联系人的公开内容。"
                "请先在微信中打开该联系人的朋友圈并滚动同步历史内容，然后重新连接。"
            )

        created_directories: list[Path] = []
        _mkdir_tracked(output_dir, created_directories)
        moments_dir = _conversation_output_dir(output_dir, conversation) / "朋友圈"
        _mkdir_tracked(moments_dir, created_directories)
        archive_dir = _available_directory(
            moments_dir, safe_filename(f"{conversation.display_name}_朋友圈离线归档")
        )
        resolver = MediaResolver(self.account, self.archive.self_wxid)
        total = len(moments)
        media_total = sum(len(moment.media) for moment in moments)

        published = False
        try:
            with tempfile.TemporaryDirectory(
                prefix=".朋友圈归档构建-", dir=moments_dir
            ) as temporary:
                temporary_dir = Path(temporary)
                writer = MomentsArchiveWriter(temporary_dir, conversation)
                executor = ThreadPoolExecutor(
                    max_workers=5,
                    thread_name_prefix="wechat-moments-media",
                )
                try:
                    for index, moment in enumerate(moments, start=1):
                        _raise_if_cancelled(cancelled)
                        futures = [
                            executor.submit(resolver.resolve_moment_file, media)
                            for media in moment.media
                        ]
                        resolved = tuple(
                            (
                                media,
                                _future_result_with_cancel(future, cancelled),
                            )
                            for media, future in zip(
                                moment.media, futures, strict=True
                            )
                        )
                        _raise_if_cancelled(cancelled)
                        writer.write(moment, resolved)
                        if progress:
                            fraction = 0.04 + (index / total) * 0.94
                            progress(
                                f"正在归档朋友圈 {index}/{total} 条 · 媒体 {media_total} 个",
                                min(0.98, fraction),
                            )
                finally:
                    was_cancelled = cancelled is not None and cancelled.is_set()
                    executor.shutdown(
                        wait=not was_cancelled,
                        cancel_futures=was_cancelled,
                    )
                _raise_if_cancelled(cancelled)
                writer.finish()
                _raise_if_cancelled(cancelled)
                if progress:
                    progress("正在完成朋友圈归档并写入输出目录...", 0.99)
                _publish_directory(
                    temporary_dir,
                    archive_dir,
                    cancelled=cancelled,
                )
                published = True
                _raise_if_cancelled(cancelled)
        except ExportCancelled:
            if published or archive_dir.exists():
                _remove_cancelled_tree(archive_dir)
            _cleanup_cancelled_files((), created_directories)
            raise

        html_path = archive_dir / "index.html"
        json_path = archive_dir / "moments.json"
        manifest_path = archive_dir / "manifest-sha256.txt"

        result = ExportResult(
            files=[html_path, json_path, manifest_path],
            file_conversations={
                html_path: conversation,
                json_path: conversation,
                manifest_path: conversation,
            },
            message_counts={conversation.username: len(moments)},
        )
        for path in result.files:
            result.file_categories[path] = "moments"
        if conversation.is_self:
            result.warnings.append(
                "本人朋友圈范围：已导出本机库中属于当前账号的全部记录，"
                "包括私密（仅自己可见）和分组可见动态；微信尚未同步到本机的记录无法离线导出。"
            )
        else:
            result.warnings.append(
                "朋友圈范围：已导出当前账号本机已同步且仍可见的全部记录；"
                "微信尚未同步到本机的更早内容无法离线导出。"
            )
        requested = resolver.stats.requested
        if requested:
            result.warnings.append(
                resolver.stats.moments_summary(fallback_count=writer.fallback_count)
            )
            result.warnings.extend(sorted(resolver.stats.issues))
            if writer.fallback_count:
                result.warnings.append(
                    f"实况照片兜底：{writer.fallback_count} 个动态媒体未能取得，"
                    "HTML 已优先显示对应的静态主图，不再显示为错误卡片。"
                )
            if resolver.stats.embedded < requested:
                result.warnings.append(
                    "媒体未全部导出：请在微信中打开该联系人的朋友圈，从最新往下滚动浏览，"
                    "让图片和视频重新加载后再返回本工具导出。"
                    "有静态主图的实况照片已用停止画面兜底，其余缺失项已在 HTML 和 JSON 中明确标注。"
                )
        result.duration_seconds = time.perf_counter() - started_at
        _cleanup_then_raise_if_cancelled(
            cancelled,
            directories=created_directories,
            published_directory=archive_dir,
        )
        if progress:
            progress(
                f"朋友圈归档 100% · {len(moments)} 条 · {media_total} 个媒体 · "
                f"实际用时 {format_duration(result.duration_seconds)}",
                1.0,
            )
        return result

    def export_moments_pdf(
        self,
        conversation: Conversation,
        output_dir: Path,
        *,
        progress: Callable[[str, float], None] | None = None,
        cancelled: threading.Event | None = None,
    ) -> ExportResult:
        """Compatibility wrapper retained for callers from version 1.1."""
        return self.export_moments_archive(
            conversation,
            output_dir,
            progress=progress,
            cancelled=cancelled,
        )


def estimate_export_seconds(
    workload: ExportWorkload,
    *,
    conversation_count: int,
    include_txt: bool,
    include_pdf: bool,
    include_pdf_images: bool,
    include_jsonl: bool = False,
    include_json: bool = False,
) -> tuple[float, float]:
    """Return a conservative local-machine estimate range before export."""
    if workload.message_count <= 0 or not (
        include_json or include_jsonl or include_txt or include_pdf
    ):
        return 0.0, 0.0

    fixed = 0.35 + max(1, conversation_count) * 0.12
    text_seconds = workload.message_count * (
        (0.00013 if include_json else 0.0)
        + (0.00014 if include_jsonl else 0.0)
        + (0.00012 if include_txt else 0.0)
        + (0.0009 if include_pdf else 0.0)
    )
    likely = fixed + text_seconds
    if include_pdf and include_pdf_images:
        likely += workload.image_count * 0.06 + workload.emoticon_count * 0.55
        lower = likely * 0.65
        upper = (
            fixed
            + text_seconds * 1.8
            + workload.image_count * 0.22
            + workload.emoticon_count * 3.0
        )
    else:
        lower = likely * 0.7
        upper = likely * 1.6
    return max(0.5, lower), max(1.0, upper)


def estimate_moments_export_seconds(
    *,
    post_count: int,
    media_count: int,
) -> tuple[float, float]:
    """Return a broad estimate for a local Moments archive.

    Database parsing is fast; the range is intentionally driven by media I/O
    because local cache hits and WeChat CDN downloads vary substantially.
    """
    if post_count <= 0:
        return 0.0, 0.0
    fixed = 0.8 + post_count * 0.012
    lower = fixed + max(0, media_count) * 0.22
    upper = fixed * 1.8 + max(0, media_count) * 2.8
    return max(1.0, lower), max(2.0, upper)


def format_duration(seconds: float) -> str:
    seconds = max(0, int(seconds + 0.5))
    if seconds < 60:
        return f"{seconds} 秒"
    minutes, remaining_seconds = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes} 分 {remaining_seconds} 秒"
    hours, remaining_minutes = divmod(minutes, 60)
    return f"{hours} 小时 {remaining_minutes} 分"


def _raise_if_cancelled(cancelled: threading.Event | None) -> None:
    if cancelled is not None and cancelled.is_set():
        raise ExportCancelled("导出已由用户中断")


def _cleanup_then_raise_if_cancelled(
    cancelled: threading.Event | None,
    *,
    files: Iterable[Path] = (),
    directories: Iterable[Path] = (),
    published_directory: Path | None = None,
) -> None:
    if cancelled is None or not cancelled.is_set():
        return
    if published_directory is not None and published_directory.exists():
        _remove_cancelled_tree(published_directory)
    _cleanup_cancelled_files(files, directories)
    raise ExportCancelled("导出已由用户中断")


def _future_result_with_cancel(
    future: Future,
    cancelled: threading.Event | None,
):
    while True:
        _raise_if_cancelled(cancelled)
        try:
            return future.result(timeout=0.1)
        except FutureTimeoutError:
            continue


def _mkdir_tracked(path: Path, created_directories: list[Path]) -> None:
    missing: list[Path] = []
    cursor = path
    while not cursor.exists():
        missing.append(cursor)
        parent = cursor.parent
        if parent == cursor:
            break
        cursor = parent
    path.mkdir(parents=True, exist_ok=True)
    known = set(created_directories)
    for directory in reversed(missing):
        if directory.exists() and directory not in known:
            created_directories.append(directory)
            known.add(directory)


def _cleanup_cancelled_files(
    files: Iterable[Path],
    created_directories: Iterable[Path],
) -> None:
    for path in reversed(tuple(dict.fromkeys(files))):
        last_error: OSError | None = None
        for delay in (0.0, 0.05, 0.15, 0.3, 0.6):
            if delay:
                time.sleep(delay)
            try:
                path.unlink(missing_ok=True)
                last_error = None
                break
            except OSError as error:
                last_error = error
        if last_error is not None and path.exists():
            raise last_error
    unique_directories = tuple(dict.fromkeys(created_directories))
    for directory in sorted(
        unique_directories,
        key=lambda item: len(item.parts),
        reverse=True,
    ):
        try:
            directory.rmdir()
        except FileNotFoundError:
            continue
        except OSError:
            # Never remove a non-empty directory: it may contain data that
            # existed before this export or was created by another process.
            continue


def _remove_cancelled_tree(path: Path) -> None:
    last_error: OSError | None = None
    for delay in (0.0, 0.05, 0.15, 0.3, 0.6):
        if delay:
            time.sleep(delay)
        try:
            shutil.rmtree(path)
            return
        except FileNotFoundError:
            return
        except OSError as error:
            last_error = error
    if last_error is not None and path.exists():
        raise last_error


def _iter_messages_with_images(
    messages: Iterable[Message],
    media_resolver: MediaResolver | None,
    *,
    max_workers: int = 4,
    max_pending: int = 8,
    cancelled: threading.Event | None = None,
) -> Iterator[tuple[Message, PdfImage | None]]:
    """Keep message order while overlapping bounded media I/O and CDN waits."""
    if media_resolver is None:
        for message in messages:
            _raise_if_cancelled(cancelled)
            yield message, None
        return

    pending: deque[tuple[Message, Future[PdfImage | None] | None]] = deque()
    executor = ThreadPoolExecutor(
        max_workers=max_workers,
        thread_name_prefix="wechat-media",
    )
    try:
        for message in messages:
            _raise_if_cancelled(cancelled)
            future = (
                executor.submit(media_resolver.resolve, message)
                if message.media is not None
                else None
            )
            pending.append((message, future))
            if len(pending) >= max_pending:
                queued_message, queued_future = pending.popleft()
                yield (
                    queued_message,
                    _future_result_with_cancel(queued_future, cancelled)
                    if queued_future
                    else None,
                )

        while pending:
            _raise_if_cancelled(cancelled)
            queued_message, queued_future = pending.popleft()
            yield (
                queued_message,
                _future_result_with_cancel(queued_future, cancelled)
                if queued_future
                else None,
            )
    finally:
        was_cancelled = cancelled is not None and cancelled.is_set()
        executor.shutdown(wait=not was_cancelled, cancel_futures=was_cancelled)


def _available_stem(
    output_dir: Path,
    base: str,
    *,
    include_txt: bool,
    include_pdf: bool,
    include_jsonl: bool = False,
    include_json: bool = False,
) -> str:
    extensions = [
        extension
        for extension, enabled in (
            (".json", include_json),
            (".jsonl", include_jsonl),
            (".txt", include_txt),
            (".pdf", include_pdf),
        )
        if enabled
    ]
    candidate = base
    suffix = 2
    while any((output_dir / f"{candidate}{extension}").exists() for extension in extensions):
        candidate = f"{base} ({suffix})"
        suffix += 1
    return candidate


def _available_directory(parent: Path, base: str) -> Path:
    candidate = parent / base
    suffix = 2
    while candidate.exists():
        candidate = parent / f"{base} ({suffix})"
        suffix += 1
    return candidate


def _publish_directory(
    source: Path,
    destination: Path,
    *,
    retry_delays: tuple[float, ...] = _DIRECTORY_PUBLISH_RETRY_DELAYS,
    cancelled: threading.Event | None = None,
) -> Path:
    """Publish a completed directory despite brief Windows file locks.

    Renaming is preferred because it is atomic on the same volume. Windows
    indexers and security scanners can briefly hold a newly written file open,
    so transient access/share errors are retried. If they persist, copytree is
    used while the source temporary directory still exists; an incomplete
    destination is removed before the error is propagated.
    """
    last_error: OSError | None = None
    for attempt in range(len(retry_delays) + 1):
        _raise_if_cancelled(cancelled)
        try:
            return source.replace(destination)
        except OSError as error:
            if not _is_transient_publish_error(error):
                raise
            last_error = error
            if attempt < len(retry_delays):
                time.sleep(max(0.0, retry_delays[attempt]))

    _raise_if_cancelled(cancelled)
    if destination.exists():
        assert last_error is not None
        raise last_error
    try:
        shutil.copytree(source, destination, copy_function=shutil.copy2)
        _raise_if_cancelled(cancelled)
    except BaseException:
        if destination.exists():
            shutil.rmtree(destination, ignore_errors=True)
        raise
    return destination


def _is_transient_publish_error(error: OSError) -> bool:
    if isinstance(error, PermissionError):
        return True
    if getattr(error, "winerror", None) in {5, 32, 33}:
        return True
    return error.errno in {errno.EACCES, errno.EBUSY, errno.EPERM}


def _conversation_output_dir(
    output_dir: Path,
    conversation: Conversation,
) -> Path:
    category = (
        "本人"
        if conversation.is_self
        else ("群聊" if conversation.is_group else "联系人")
    )
    display_name = safe_filename(conversation.display_name)[:72]
    stable_id = hashlib.sha256(conversation.username.encode("utf-8")).hexdigest()[:8]
    return output_dir / category / f"{display_name} [{stable_id}]"
