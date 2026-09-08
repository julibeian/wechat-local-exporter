from __future__ import annotations

import argparse
import json
from pathlib import Path
import shutil
import tempfile
import time
import tracemalloc

from wechat_exporter.chat_html_archive import export_chat_html_archive
from wechat_exporter.models import Conversation, HtmlExportOptions, Message


def _messages(count: int):
    for index in range(count):
        yield Message(
            local_id=index,
            timestamp=1_700_000_000 + index,
            message_type=1,
            sender_id="wxid_friend" if index % 2 else "wxid_self",
            sender_name="好友" if index % 2 else "我",
            is_outgoing=index % 2 == 0,
            content=f"第 {index + 1} 条合成聊天记录 [爱心]",
            conversation_id="wxid_friend",
            server_id=index + 1,
        )


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Benchmark the offline HTML writer using synthetic messages only."
    )
    parser.add_argument("--messages", type=int, default=50_000)
    parser.add_argument("--chunk-size", type=int, default=2_000)
    parser.add_argument("--track-memory", action="store_true")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.messages < 0 or args.chunk_size <= 0:
        parser.error("--messages must be non-negative and --chunk-size must be positive")

    cleanup_root: Path | None = None
    if args.output is None:
        cleanup_root = Path(tempfile.mkdtemp(prefix="wechat-html-benchmark-"))
        output = cleanup_root / "archive"
    else:
        output = args.output.resolve()
        if output.exists():
            parser.error(f"output already exists: {output}")

    if args.track_memory:
        tracemalloc.start()
    started_at = time.perf_counter()
    try:
        result = export_chat_html_archive(
            root=output,
            conversation=Conversation("wxid_friend", "合成压力测试"),
            messages=_messages(args.messages),
            options=HtmlExportOptions(include_images=False, include_stickers=False),
            chunk_size=args.chunk_size,
        )
        elapsed = time.perf_counter() - started_at
        archive_bytes = sum(
            path.stat().st_size for path in result.root.rglob("*") if path.is_file()
        )
        report = {
            "messages": result.message_count,
            "seconds": round(elapsed, 3),
            "messages_per_second": round(result.message_count / elapsed, 1)
            if elapsed
            else None,
            "chunk_size": args.chunk_size,
            "chunks": result.chunk_count,
            "archive_mib": round(archive_bytes / (1024 * 1024), 2),
        }
        if args.track_memory:
            _, peak = tracemalloc.get_traced_memory()
            report["peak_tracemalloc_mib"] = round(peak / (1024 * 1024), 2)
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0
    finally:
        if args.track_memory:
            tracemalloc.stop()
        if cleanup_root is not None:
            shutil.rmtree(cleanup_root)


if __name__ == "__main__":
    raise SystemExit(main())
