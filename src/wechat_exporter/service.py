from __future__ import annotations

from collections import deque
from collections.abc import Callable, Iterable, Iterator
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import replace
import hashlib
from pathlib import Path
import tempfile
import time

from .archive import SenderCalibration, WeChatArchive
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
from .key_capture import KeyCapturePreparation, capture_keys_during_wechat_start
from .media import MediaResolver
from .moments_archive import MomentsArchiveWriter
from .models import (
    AccountLocation,
    Conversation,
    ExportRequest,
    ExportResult,
    ExportWorkload,
    Message,
    PdfImage,
)
from .windows import list_wechat_processes
from .content import WECHAT_VOICE_TEXT_PREFIX


class RestartRequired(RuntimeError):
    pass


class ExporterService:
    def __init__(self, account: AccountLocation):
        self.account = account
        self.targets = collect_required_databases(account.db_dir)
        self.workspace: DecryptedWorkspace | None = None
        self.archive: WeChatArchive | None = None

    def close(self) -> None:
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
        try:
            keys = extract_database_keys(self.targets, progress=progress)
        except RuntimeError as error:
            raise RestartRequired(
                "当前微信版本需要在启动瞬间从本机进程内存中临时捕获数据库主密钥。"
            ) from error
        return self._prepare(keys, progress=progress, calibrations=calibrations)

    def connect_during_wechat_start(
        self,
        executable: Path,
        *,
        progress: Callable[[str, float], None] | None = None,
        calibrations: list[SenderCalibration] | None = None,
        capture_preparation: KeyCapturePreparation | None = None,
    ) -> WeChatArchive:
        if list_wechat_processes():
            raise RuntimeError("检测到微信仍在运行，自动退出尚未完成。请重新点击连接微信。")

        def capture_progress(message: str) -> None:
            if progress:
                progress(message, 0.25)

        keys = capture_keys_during_wechat_start(
            executable,
            self.targets,
            progress=capture_progress,
            preparation=capture_preparation,
        )
        return self._prepare(keys, progress=progress, calibrations=calibrations)

    def _prepare(
        self,
        keys,
        *,
        progress: Callable[[str, float], None] | None,
        calibrations: list[SenderCalibration] | None,
    ) -> WeChatArchive:
        self.close()
        self.workspace = DecryptedWorkspace(self.account.db_dir, keys)

        def snapshot_progress(message: str, fraction: float) -> None:
            if progress:
                progress(message, 0.35 + fraction * 0.6)

        self.workspace.prepare(progress=snapshot_progress)
        self.archive = WeChatArchive(self.account, self.workspace, calibrations)
        self.archive.load_metadata()
        if progress:
            progress("会话索引已就绪", 1.0)
        return self.archive

    def export(
        self,
        request: ExportRequest,
        *,
        progress: Callable[[str, float], None] | None = None,
    ) -> ExportResult:
        if not self.archive:
            raise RuntimeError("请先读取会话")
        started_at = time.perf_counter()
        if progress:
            progress("正在统计消息数量...", 0.0)
        workload = self.archive.export_workload(
            request.conversations,
            start_timestamp=request.start_timestamp,
            end_timestamp=request.end_timestamp,
        )
        total_messages = workload.message_count
        completed_messages = 0
        progress_step = max(1, total_messages // 200)
        last_progress_at = started_at
        request.output_dir.mkdir(parents=True, exist_ok=True)
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
        for conversation in request.conversations:
            conversation_dir = _conversation_output_dir(
                request.output_dir,
                conversation,
            )
            stem = _available_stem(
                conversation_dir,
                safe_filename(conversation.display_name),
                include_txt=request.include_txt,
                include_pdf=request.include_pdf,
            )
            txt_writer = (
                TxtTranscriptWriter(
                    conversation_dir / f"{stem}.txt",
                    conversation,
                    start_timestamp=request.start_timestamp,
                    end_timestamp=request.end_timestamp,
                )
                if request.include_txt
                else None
            )
            pdf_writer = (
                PdfTranscriptWriter(
                    conversation_dir / f"{stem}.pdf",
                    conversation,
                    start_timestamp=request.start_timestamp,
                    end_timestamp=request.end_timestamp,
                )
                if request.include_pdf
                else None
            )
            count = 0
            try:
                messages = self.archive.iter_messages(
                    conversation,
                    start_timestamp=request.start_timestamp,
                    end_timestamp=request.end_timestamp,
                )
                messages_with_images = _iter_messages_with_images(messages, media_resolver)
                for message, image in messages_with_images:
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
                            "正在导出图片 PDF"
                            if request.include_pdf and request.include_pdf_images
                            else "正在快速导出"
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
                if txt_writer:
                    txt_writer.close()
                if pdf_writer:
                    pdf_writer.close()
            if txt_writer:
                result.files.append(txt_writer.path)
                result.file_conversations[txt_writer.path] = conversation
            if pdf_writer:
                result.files.append(pdf_writer.path)
                result.file_conversations[pdf_writer.path] = conversation
            result.message_counts[conversation.username] = count
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
        if progress:
            progress("正在完成文件...", 0.99)
        result.duration_seconds = time.perf_counter() - started_at
        if progress:
            progress(
                f"导出 100% · {completed_messages} 条 · "
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
    ) -> ExportResult:
        """Export one contact's or the user's own Moments as an offline archive."""
        if not self.archive:
            raise RuntimeError("请先读取会话")
        if conversation.is_group:
            raise ValueError("朋友圈归档只支持单个联系人，不支持群聊。")

        started_at = time.perf_counter()
        if progress:
            progress("正在读取该联系人的全部朋友圈...", 0.02)
        moments = self.archive.contact_moments(conversation)
        if not moments:
            raise ValueError(
                "本机朋友圈库中没有找到该联系人的公开内容。"
                "请先在微信中打开该联系人的朋友圈并滚动同步历史内容，然后重新连接。"
            )

        output_dir.mkdir(parents=True, exist_ok=True)
        moments_dir = _conversation_output_dir(output_dir, conversation) / "朋友圈"
        moments_dir.mkdir(parents=True, exist_ok=True)
        archive_dir = _available_directory(
            moments_dir, safe_filename(f"{conversation.display_name}_朋友圈离线归档")
        )
        resolver = MediaResolver(self.account, self.archive.self_wxid)
        total = len(moments)
        media_total = sum(len(moment.media) for moment in moments)

        with tempfile.TemporaryDirectory(
            prefix=".朋友圈归档构建-", dir=moments_dir
        ) as temporary:
            temporary_dir = Path(temporary)
            writer = MomentsArchiveWriter(temporary_dir, conversation)
            with ThreadPoolExecutor(
                max_workers=5,
                thread_name_prefix="wechat-moments-media",
            ) as executor:
                for index, moment in enumerate(moments, start=1):
                    futures = [
                        executor.submit(resolver.resolve_moment_file, media)
                        for media in moment.media
                    ]
                    resolved = tuple(
                        (media, future.result())
                        for media, future in zip(moment.media, futures, strict=True)
                    )
                    writer.write(moment, resolved)
                    if progress:
                        fraction = 0.04 + (index / total) * 0.94
                        progress(
                            f"正在归档朋友圈 {index}/{total} 条 · 媒体 {media_total} 个",
                            min(0.98, fraction),
                        )
            writer.finish()
            temporary_dir.replace(archive_dir)

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
            result.warnings.append(resolver.stats.moments_summary())
            result.warnings.extend(sorted(resolver.stats.issues))
            if resolver.stats.embedded < requested:
                result.warnings.append(
                    "媒体未全部导出：请在微信中打开该联系人的朋友圈，从最新往下滚动浏览，"
                    "让图片和视频重新加载后再返回本工具导出。缺失项已在 HTML 和 JSON 中明确标注。"
                )
        result.duration_seconds = time.perf_counter() - started_at
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
    ) -> ExportResult:
        """Compatibility wrapper retained for callers from version 1.1."""
        return self.export_moments_archive(
            conversation, output_dir, progress=progress
        )


def estimate_export_seconds(
    workload: ExportWorkload,
    *,
    conversation_count: int,
    include_txt: bool,
    include_pdf: bool,
    include_pdf_images: bool,
) -> tuple[float, float]:
    """Return a conservative local-machine estimate range before export."""
    if workload.message_count <= 0 or not (include_txt or include_pdf):
        return 0.0, 0.0

    fixed = 0.35 + max(1, conversation_count) * 0.12
    text_seconds = workload.message_count * (
        (0.00012 if include_txt else 0.0)
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


def format_duration(seconds: float) -> str:
    seconds = max(0, int(seconds + 0.5))
    if seconds < 60:
        return f"{seconds} 秒"
    minutes, remaining_seconds = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes} 分 {remaining_seconds} 秒"
    hours, remaining_minutes = divmod(minutes, 60)
    return f"{hours} 小时 {remaining_minutes} 分"


def _iter_messages_with_images(
    messages: Iterable[Message],
    media_resolver: MediaResolver | None,
    *,
    max_workers: int = 4,
    max_pending: int = 8,
) -> Iterator[tuple[Message, PdfImage | None]]:
    """Keep message order while overlapping bounded media I/O and CDN waits."""
    if media_resolver is None:
        for message in messages:
            yield message, None
        return

    pending: deque[tuple[Message, Future[PdfImage | None] | None]] = deque()
    with ThreadPoolExecutor(
        max_workers=max_workers,
        thread_name_prefix="wechat-media",
    ) as executor:
        for message in messages:
            future = (
                executor.submit(media_resolver.resolve, message)
                if message.media is not None
                else None
            )
            pending.append((message, future))
            if len(pending) >= max_pending:
                queued_message, queued_future = pending.popleft()
                yield queued_message, queued_future.result() if queued_future else None

        while pending:
            queued_message, queued_future = pending.popleft()
            yield queued_message, queued_future.result() if queued_future else None


def _available_stem(
    output_dir: Path,
    base: str,
    *,
    include_txt: bool,
    include_pdf: bool,
) -> str:
    extensions = [extension for extension, enabled in ((".txt", include_txt), (".pdf", include_pdf)) if enabled]
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
