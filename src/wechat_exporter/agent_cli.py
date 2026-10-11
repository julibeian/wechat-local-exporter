"""Headless, local-only export commands with a versioned JSON result contract."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from dataclasses import asdict
from datetime import date, datetime, time as day_time, timedelta
import json
from pathlib import Path
import signal
import sys
import threading
import time

from . import __version__
from .config import LocalConfig
from .connection import ConnectionManager
from .errors import UserFacingError, user_message
from .history import append_export_history
from .instance_control import claim_headless_instance
from .models import (
    CHAT_FILE_CATEGORIES, ChatFileExportRequest, Conversation, ExportRequest,
    HtmlExportOptions, JsonlPackageRequest,
)
from .service import ExportCancelled, RestartRequired
from .windows import discover_accounts, list_wechat_processes, read_wechat_version


SCHEMA_VERSION = 1
EXPORT_TASKS = ("export", "package", "files", "moments")
MAX_JOB_BYTES = 2 * 1024 * 1024
MAX_JOBS = 1000


class CliError(Exception):
    def __init__(self, code: str, message: str, exit_code: int = 2, *, action: str = ""):
        super().__init__(message)
        self.code, self.exit_code, self.action = code, exit_code, action


class JsonArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise CliError("INVALID_ARGUMENT", message)


def _positive(value: str) -> int:
    number = int(value)
    if not 1 <= number <= 1024 * 1024:
        raise argparse.ArgumentTypeError("需要 1 到 1048576 之间的整数")
    return number


def build_parser() -> JsonArgumentParser:
    parser = JsonArgumentParser(
        prog="wechatexporter", description="本机微信批量导出 CLI；结果为 UTF-8 JSON，进度写入 stderr。",
    )
    parser.add_argument("--version", action="version", version=__version__)
    commands = parser.add_subparsers(dest="command", required=True)
    for name, help_text in (
        ("version", "版本与运行能力"), ("schema", "命令参数与批量任务格式"),
        ("diagnose", "只读检测微信版本、进程及数据目录"), ("accounts", "列出本机数据账号"),
    ):
        commands.add_parser(name, help=help_text)
    connect = commands.add_parser("connect", help="验证连接；首次连接可显式允许重启微信")
    _connection_options(connect)
    conversations = commands.add_parser("conversations", help="列出稳定会话 ID，供后续导出使用")
    _connection_options(conversations)
    conversations.add_argument("--query", help="按会话名称或 ID 包含匹配")
    conversations.add_argument("--type", choices=("all", "group", "contact"), default="all")
    conversations.add_argument("--include-self", action="store_true", help="附带用于朋友圈归档的本人 ID")
    for name, help_text in (
        ("export", "批量导出 JSON、TXT、HTML 或 PDF"),
        ("package", "批量导出 JSONL + 本机媒体的 AI 资料包"),
        ("files", "批量导出聊天附件 ZIP"), ("moments", "批量归档本机可见朋友圈"),
    ):
        command = commands.add_parser(name, help=help_text)
        _connection_options(command)
        selection = command.add_mutually_exclusive_group(required=True)
        selection.add_argument("--conversation", action="append", help="精确会话 ID；可以重复")
        selection.add_argument("--query", help="匹配名称或 ID，导出所有匹配会话")
        selection.add_argument("--all", action="store_true", help="全部会话，可用 --type 限定")
        if name == "moments":
            selection.add_argument("--self", action="store_true", help="导出本人朋友圈")
        command.add_argument("--type", choices=("all", "group", "contact"), default="all")
        command.add_argument("--output", type=Path, required=True, help="输出目录")
        command.add_argument("--dry-run", action="store_true", help="解析选择并返回计划，不写出归档")
        command.add_argument("--fail-fast", action="store_true", help="首个失败后停止剩余会话")
        if name != "moments":
            command.add_argument("--since", help="开始日期 YYYY-MM-DD 或 ISO 8601 时间")
            command.add_argument("--until", help="结束日期（包含整天）或 ISO 8601 时间")
        if name == "export":
            command.add_argument("--format", choices=("json", "txt", "html", "pdf"), default="json")
            command.add_argument("--pdf-images", action="store_true", help="PDF 收录本机图片")
            command.add_argument("--include-files", action="store_true", help="HTML 收录本机文件实体")
        if name in ("export", "package"):
            command.add_argument("--include-videos", action="store_true", help="HTML / AI 资料包收录本机视频")
            command.add_argument("--allow-network-media", action="store_true", help="允许补全本机缺失的表情等资源")
        if name in ("package", "files"):
            command.add_argument("--max-file-mb", type=_positive, default=100, help="单文件/视频大小上限，默认 100 MB")
        if name == "files":
            command.add_argument("--category", action="append", choices=sorted(CHAT_FILE_CATEGORIES), help="附件类型；可以重复")
    batch = commands.add_parser("batch", help="一次连接执行 JSON 中的多组任务")
    _connection_options(batch)
    batch.add_argument("--job", type=Path, required=True, help="版本 1 的任务文件")
    batch.add_argument("--dry-run", action="store_true")
    batch.add_argument("--fail-fast", action="store_true")
    test = commands.add_parser("self-test", help="用合成数据验证导出能力，不访问真实聊天")
    test.add_argument("--output", type=Path, required=True)
    return parser


def _connection_options(parser) -> None:
    parser.add_argument("--account", help="预期账号 ID；不匹配时停止，不切换账号")
    parser.add_argument("--allow-restart", action="store_true", help="显式允许退出并启动微信以获取密钥；仍需本人完成登录")
    parser.add_argument("--quiet", action="store_true", help="关闭 stderr 进度")


def _timestamp(value: str | None, *, end: bool = False) -> int:
    if value is None:
        return 0
    try:
        if len(value) == 10:
            parsed_date = date.fromisoformat(value)
            # Advance the calendar date before converting, including across DST.
            boundary = datetime.combine(parsed_date + timedelta(days=int(end)), day_time.min)
            result = int(boundary.timestamp()) - int(end)
        else:
            result = int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp())
        if result <= 0:
            raise ValueError
        return result
    except (ValueError, TypeError, OverflowError, OSError):
        raise CliError("INVALID_DATE", f"日期无效: {value}；请使用 YYYY-MM-DD 或 ISO 8601。") from None


def _validate_task(args) -> None:
    args.output = args.output.expanduser().resolve()
    args.start_timestamp = _timestamp(getattr(args, "since", None))
    args.end_timestamp = _timestamp(getattr(args, "until", None), end=True)
    if args.start_timestamp and args.end_timestamp and args.start_timestamp > args.end_timestamp:
        raise CliError("INVALID_DATE_RANGE", "开始日期不能晚于结束日期。")
    if args.query is not None and not args.query.strip():
        raise CliError("EMPTY_SELECTOR", "--query 不能为空；全部导出请显式使用 --all。")
    if args.conversation and any(not value.strip() for value in args.conversation):
        raise CliError("EMPTY_SELECTOR", "会话 ID 不能为空。")
    if args.command == "export":
        if args.pdf_images and args.format != "pdf":
            raise CliError("INVALID_OPTION", "--pdf-images 仅适用于 PDF。")
        if (args.include_videos or args.include_files or args.allow_network_media) and args.format != "html":
            raise CliError("INVALID_OPTION", "普通聊天的媒体选项仅适用于 HTML；JSONL 媒体包请用 package。")
    if args.command == "moments" and args.type == "group":
        raise CliError("INVALID_OPTION", "朋友圈归档不支持群聊。")


# JSON fields are explicitly mapped to the same parser used by terminal calls.
# Process restart is deliberately a command-line option, never a job-file field.
JOB_FIELDS = {
    "conversations": ("--conversation", "list"), "query": ("--query", "str"),
    "all": ("--all", "bool"), "self": ("--self", "bool"),
    "type": ("--type", "str"), "output": ("--output", "str"),
    "format": ("--format", "str"), "since": ("--since", "str"), "until": ("--until", "str"),
    "pdf_images": ("--pdf-images", "bool"), "include_files": ("--include-files", "bool"),
    "include_videos": ("--include-videos", "bool"),
    "allow_network_media": ("--allow-network-media", "bool"),
    "max_file_mb": ("--max-file-mb", "int"), "categories": ("--category", "list"),
}


def _load_jobs(args) -> list[argparse.Namespace]:
    path = args.job.expanduser().resolve()
    try:
        if path.stat().st_size > MAX_JOB_BYTES:
            raise CliError("INVALID_JOB", "任务文件超过 2 MB。")
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError, UnicodeError):
        raise CliError("INVALID_JOB", "无法读取 UTF-8 JSON 任务文件。") from None
    if not isinstance(payload, dict) or set(payload) - {"version", "account", "jobs"}:
        raise CliError("INVALID_JOB", "任务根对象仅接受 version、account、jobs。")
    if type(payload.get("version")) is not int or payload["version"] != SCHEMA_VERSION:
        raise CliError("INVALID_JOB_VERSION", "任务文件 version 必须为 1。")
    jobs = payload.get("jobs")
    if not isinstance(jobs, list) or not 1 <= len(jobs) <= MAX_JOBS:
        raise CliError("INVALID_JOB", "jobs 必须包含 1 到 1000 个任务。")
    account = payload.get("account")
    if account is not None:
        if not isinstance(account, str) or not account.strip():
            raise CliError("INVALID_JOB", "account 必须是非空账号 ID。")
        if args.account and account != args.account:
            raise CliError("ACCOUNT_MISMATCH", "命令行与任务文件的预期账号不一致。", 4)
        args.account = account
    planned = []
    for number, item in enumerate(jobs, 1):
        if not isinstance(item, dict) or set(item) - ({"task"} | JOB_FIELDS.keys()):
            raise CliError("INVALID_JOB", f"任务 {number} 包含不支持的字段。")
        if item.get("task") not in EXPORT_TASKS:
            raise CliError("INVALID_JOB", f"任务 {number} 的 task 无效。")
        parser = build_parser()
        commands = next(action for action in parser._actions if isinstance(action, argparse._SubParsersAction))
        supported_flags = {flag for action in commands.choices[item["task"]]._actions for flag in action.option_strings}
        argv = [item["task"]]
        for key, value in item.items():
            if key == "task":
                continue
            flag, kind = JOB_FIELDS[key]
            if flag not in supported_flags:
                raise CliError("INVALID_JOB", f"任务 {number} 不支持 {key} 字段。")
            valid = (
                (kind == "str" and isinstance(value, str) and bool(value.strip()))
                or (kind == "bool" and type(value) is bool)
                or (kind == "int" and type(value) is int)
                or (kind == "list" and isinstance(value, list) and bool(value)
                    and all(isinstance(v, str) and v.strip() for v in value))
            )
            if not valid:
                raise CliError("INVALID_JOB", f"任务 {number} 的 {key} 类型无效。")
            if kind == "bool":
                if value:
                    argv.append(flag)
            elif kind == "list":
                for entry in value:
                    argv.extend((flag, entry))
            else:
                if key == "output":
                    destination = Path(value).expanduser()
                    value = str(destination if destination.is_absolute() else path.parent / destination)
                argv.extend((flag, str(value)))
        job = parser.parse_args(argv)
        _validate_task(job)
        planned.append(job)
    return planned


def _select(args, conversations: list[Conversation], self_conversation: Conversation) -> tuple[Conversation, ...]:
    pool = [item for item in conversations if not item.is_self]
    if args.command == "moments":
        pool = [item for item in pool if not item.is_group]
        pool.append(self_conversation)
    if args.type != "all":
        pool = [item for item in pool if item.is_group == (args.type == "group")]
    if args.conversation:
        by_id = {item.username: item for item in pool}
        missing = [value for value in args.conversation if value not in by_id]
        if missing:
            raise CliError("CONVERSATION_NOT_FOUND", "会话 ID 不在所选范围内: " + ", ".join(missing))
        selected = [by_id[value] for value in dict.fromkeys(args.conversation)]
    elif getattr(args, "self", False):
        selected = [self_conversation]
    elif args.query is not None:
        query = args.query.casefold()
        selected = [item for item in pool if query in item.display_name.casefold() or query in item.username.casefold()]
    else:
        # --all for Moments covers contacts; own Moments remains an explicit --self choice.
        selected = [item for item in pool if not item.is_self]
    if not selected:
        raise CliError("NO_CONVERSATIONS", "未找到符合条件的会话。")
    return tuple(selected)


def _conversation_record(item: Conversation) -> dict:
    return {
        "id": item.username, "name": item.display_name,
        "type": "self" if item.is_self else "group" if item.is_group else "contact",
        "last_timestamp": item.last_timestamp,
    }


def _error_record(error: BaseException) -> dict:
    if isinstance(error, CliError):
        result = {"code": error.code, "message": str(error)}
        if error.action:
            result["action"] = error.action
        return result
    if isinstance(error, RestartRequired):
        return {"code": "CONNECTION_REQUIRED", "message": str(error), "action": "connect --allow-restart"}
    if isinstance(error, UserFacingError):
        return {"code": "LOGIN_REQUIRED", "message": str(error), "action": "complete_wechat_login"}
    if isinstance(error, (ExportCancelled, KeyboardInterrupt)):
        return {"code": "CANCELLED", "message": "任务已取消；已完成的其他会话保留。"}
    if isinstance(error, ValueError):
        return {"code": "EXPORT_REJECTED", "message": str(error)}
    return {"code": "OPERATION_FAILED", "message": user_message(error)}


def _progress(quiet: bool):
    last = [0.0]

    def report(message: str, fraction: float) -> None:
        now = time.monotonic()
        if not quiet and (fraction >= 1 or now - last[0] >= 0.25):
            print(json.dumps({"event": "progress", "message": message, "fraction": round(fraction, 4)}, ensure_ascii=False), file=sys.stderr, flush=True)
            last[0] = now

    return report


@contextmanager
def _cancellation(event: threading.Event):
    if threading.current_thread() is not threading.main_thread():
        yield
        return
    previous = signal.getsignal(signal.SIGINT)
    signal.signal(signal.SIGINT, lambda *_args: event.set())
    try:
        yield
    finally:
        signal.signal(signal.SIGINT, previous)


def _export_one(service, args, conversation: Conversation, progress, cancelled):
    selection = (conversation,)
    dates = {"start_timestamp": args.start_timestamp, "end_timestamp": args.end_timestamp}
    if args.command == "export":
        request = ExportRequest(
            selection, args.output, include_txt=args.format == "txt", include_json=args.format == "json",
            include_html=args.format == "html", include_pdf=args.format == "pdf", include_pdf_images=args.pdf_images,
            html_options=HtmlExportOptions(include_videos=args.include_videos,
                include_file_binaries=args.include_files, allow_network_completion=args.allow_network_media),
            **dates,
        )
        return service.export(request, progress=progress, cancelled=cancelled)
    if args.command == "package":
        request = JsonlPackageRequest(selection, args.output, include_videos=args.include_videos,
            max_video_size_bytes=args.max_file_mb * 1024**2, allow_network_media=args.allow_network_media, **dates)
        return service.export_jsonl_package(request, progress=progress, cancelled=cancelled)
    if args.command == "files":
        request = ChatFileExportRequest(selection, args.output,
            categories=frozenset(args.category or CHAT_FILE_CATEGORIES), max_file_size_bytes=args.max_file_mb * 1024**2, **dates)
        return service.export_chat_files(request, progress=progress, cancelled=cancelled)
    return service.export_moments_archive(conversation, args.output, progress=progress, cancelled=cancelled)


def _run_exports(service, args, jobs, progress) -> tuple[dict, int]:
    conversations = service.archive.conversations()
    own = service.archive.self_conversation()
    # Resolve every job before writing any archive, including misspelled IDs.
    plan = [(job, _select(job, conversations, own)) for job in jobs]
    total = sum(len(selected) for _, selected in plan)
    if args.dry_run:
        return {"status": "planned", "summary": {"requested": total}, "jobs": [
            {"job": number, "task": job.command, "output": str(job.output),
             "conversations": [_conversation_record(item) for item in selected],
             "start_timestamp": job.start_timestamp, "end_timestamp": job.end_timestamp}
            for number, (job, selected) in enumerate(plan, 1)
        ]}, 0
    results, succeeded, failed = [], 0, 0
    stopped = False
    cancelled = threading.Event()
    with _cancellation(cancelled):
        for number, (job, selected) in enumerate(plan, 1):
            for conversation in selected:
                if cancelled.is_set() or stopped:
                    break
                item = {"job": number, "task": job.command, "conversation": _conversation_record(conversation)}
                try:
                    result = _export_one(service, job, conversation, progress, cancelled)
                    try:
                        append_export_history(result, account_wxid=service.account.wxid)
                    except OSError:
                        result.warnings.append("归档已生成，但导出历史未能保存。")
                    count = result.message_counts.get(conversation.username, 0)
                    unit = "attachments" if job.command == "files" else "moments" if job.command == "moments" else "messages"
                    item.update(status="ok", files=[str(path.resolve()) for path in result.files],
                        counts={unit: count},
                        warnings=result.warnings, duration_seconds=round(result.duration_seconds, 3))
                    if unit == "messages":
                        item["message_count"] = count
                    succeeded += 1
                except (Exception, KeyboardInterrupt) as error:
                    item.update(status="failed", error=_error_record(error))
                    failed += 1
                    if isinstance(error, (ExportCancelled, KeyboardInterrupt)):
                        cancelled.set()
                        item["status"] = "cancelled"
                    stopped = args.fail_fast
                results.append(item)
            if cancelled.is_set() or stopped:
                break
    exit_code = 130 if cancelled.is_set() else 6 if failed and succeeded else 1 if failed else 0
    return {"status": "cancelled" if cancelled.is_set() else "partial" if failed and succeeded else "failed" if failed else "ok",
        "summary": {"requested": total, "completed": succeeded, "failed": failed, "skipped": total - succeeded - failed},
        "results": results}, exit_code


def _connected(args, jobs) -> tuple[dict, int]:
    lease = claim_headless_instance()
    if lease is None:
        raise CliError("BUSY", "本机导出工具或另一个 CLI 任务正在运行，请稍后重试。", 5, action="retry_after_exporter_exits")
    service = None
    try:
        progress = _progress(args.quiet)
        manager = ConnectionManager(LocalConfig())
        service = manager.connect(allow_restart=args.allow_restart, progress=progress)
        if args.account and args.account not in {service.account.wxid, service.archive.self_wxid}:
            raise CliError("ACCOUNT_MISMATCH", "当前连接账号与 --account 不一致，已停止。", 4, action="login_expected_account")
        account = {"id": service.account.wxid, "wxid": service.archive.self_wxid,
            "using_saved_account_cache": service.using_saved_account_cache}
        if args.command == "connect":
            return {"status": "connected", "account": account}, 0
        if args.command == "conversations":
            values = service.archive.conversations()
            if args.include_self:
                values.append(service.archive.self_conversation())
            if args.type != "all":
                values = [item for item in values if item.is_group == (args.type == "group")]
            if args.query is not None:
                if not args.query.strip():
                    raise CliError("EMPTY_SELECTOR", "--query 不能为空。")
                query = args.query.casefold()
                values = [item for item in values if query in item.username.casefold() or query in item.display_name.casefold()]
            return {"account": account, "conversations": [_conversation_record(item) for item in values]}, 0
        payload, exit_code = _run_exports(service, args, jobs, progress)
        payload["account"] = account
        return payload, exit_code
    finally:
        try:
            if service is not None:
                service.close()
        finally:
            lease.close()


def _command_schema() -> dict:
    parser = build_parser()
    subparsers = next(action for action in parser._actions if isinstance(action, argparse._SubParsersAction))
    descriptions = {}
    for name, command in subparsers.choices.items():
        fields = []
        for action in command._actions:
            if action.dest == "help":
                continue
            field = {"name": action.dest, "flags": action.option_strings, "required": action.required}
            if action.choices is not None:
                field["choices"] = list(action.choices)
            if isinstance(action.default, (str, int, float, bool, list)):
                field["default"] = action.default
            fields.append(field)
        descriptions[name] = {"usage": command.format_usage().strip(), "arguments": fields}
    return {"commands": descriptions, "job_file": {"version": 1, "max_jobs": MAX_JOBS,
        "tasks": list(EXPORT_TASKS), "fields": {key: kind for key, (_, kind) in JOB_FIELDS.items()},
        "relative_outputs": "relative to the job file directory"}}


def _dispatch(args) -> tuple[dict, int]:
    if hasattr(args, "account") and args.account is not None and not args.account.strip():
        raise CliError("EMPTY_ACCOUNT", "预期账号 ID 不能为空。")
    if args.command == "version":
        return {"version": __version__, "platform": "windows-x64", "headless": True, "tasks": list(EXPORT_TASKS)}, 0
    if args.command == "schema":
        return _command_schema(), 0
    if args.command in ("diagnose", "accounts"):
        accounts = [{"id": item.wxid, "data_directory": str(item.account_dir), "source": item.source}
            for item in discover_accounts()]
        if args.command == "accounts":
            return {"accounts": accounts}, 0
        return {"wechat_version": read_wechat_version(), "processes": [asdict(item) for item in list_wechat_processes()], "accounts": accounts}, 0
    if args.command == "self-test":
        from .selftest import run_packaged_self_test

        receipt = run_packaged_self_test(args.output.expanduser().resolve(), check_gui=False)
        return {"status": "ok", "receipt": str(receipt), "checks": json.loads(receipt.read_text(encoding="utf-8"))}, 0
    jobs = []
    if args.command == "batch":
        jobs = _load_jobs(args)
    elif args.command in EXPORT_TASKS:
        _validate_task(args)
        jobs = [args]
    elif args.command == "conversations" and args.query is not None and not args.query.strip():
        raise CliError("EMPTY_SELECTOR", "--query 不能为空。")
    return _connected(args, jobs)


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    args = None
    try:
        arguments = list(sys.argv[1:] if argv is None else argv)
        if not arguments:
            build_parser().print_help()
            return 0
        args = build_parser().parse_args(arguments)
        payload, exit_code = _dispatch(args)
        payload = {"schema_version": SCHEMA_VERSION, "success": exit_code == 0, "command": args.command, **payload}
    except SystemExit as error:
        return int(error.code or 0)
    except (Exception, KeyboardInterrupt) as error:
        exit_code = (error.exit_code if isinstance(error, CliError) else 3 if isinstance(error, (RestartRequired, UserFacingError))
            else 130 if isinstance(error, (ExportCancelled, KeyboardInterrupt)) else 1)
        payload = {"schema_version": SCHEMA_VERSION, "success": False, "command": args.command if args else None, "error": _error_record(error)}
    print(json.dumps(payload, ensure_ascii=False), flush=True)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
