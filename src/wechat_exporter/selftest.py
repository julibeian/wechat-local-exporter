from __future__ import annotations

import json
import io
import shutil
import zipfile
from datetime import datetime
from pathlib import Path

import zstandard
from PIL import Image

from .chat_html_archive import (
    CHAT_HTML_SCHEMA,
    export_chat_html_archive,
)
from .content import decode_database_content, parse_message_text
from .exporters import PdfTranscriptWriter, TxtTranscriptWriter
from .json_exporter import JsonTextTranscriptWriter
from .jsonl_exporter import JsonlTranscriptWriter
from .jsonl_package import export_conversation_jsonl_package
from .integrity import verify_signature
from .models import (
    Conversation,
    AccountLocation,
    HtmlExportOptions,
    JsonlPackageRequest,
    MediaReference,
    Message,
    Moment,
    MomentMedia,
    MomentMediaFile,
    PdfImage,
)
from .moments_archive import MomentsArchiveWriter
from .windows import discover_accounts, find_weixin_executable


_SELF_TEST_CONVERSATION_ID = "selftest_contact"
_SELF_TEST_HTML_DIRECTORY = "packaged-self-test-chat-html"


def _prepare_self_test_html_root(output_dir: Path) -> Path:
    """Return an empty owned path without deleting an unrelated directory."""

    output_root = output_dir.resolve()
    target = output_dir / _SELF_TEST_HTML_DIRECTORY
    if not target.exists() and not target.is_symlink():
        return target
    if target.is_symlink() or (
        hasattr(target, "is_junction") and target.is_junction()
    ):
        raise RuntimeError("聊天 HTML 自检目录不能是符号链接或目录联接")
    resolved_target = target.resolve()
    if resolved_target.parent != output_root or not resolved_target.is_dir():
        raise RuntimeError("聊天 HTML 自检目录不是预期的安全子目录")
    manifest_path = resolved_target / "data" / "manifest.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise RuntimeError("聊天 HTML 自检目录已存在且无法确认归属，未执行清理") from error
    conversation = manifest.get("conversation")
    if (
        manifest.get("schema") != CHAT_HTML_SCHEMA
        or not isinstance(conversation, dict)
        or conversation.get("id") != _SELF_TEST_CONVERSATION_ID
    ):
        raise RuntimeError("聊天 HTML 自检目录已存在且不属于本次自检，未执行清理")
    shutil.rmtree(resolved_target)
    return target


def run_packaged_self_test(
    output_dir: Path, *, check_environment: bool = False
) -> Path:
    """Exercise packaged chat formats and Moments without touching WeChat data."""
    output_dir.mkdir(parents=True, exist_ok=True)
    zstd_probe = "成品内置 zstd 解压自检"
    compressed_probe = zstandard.ZstdCompressor().compress(zstd_probe.encode("utf-8"))
    if decode_database_content(b"", compressed_probe) != zstd_probe:
        raise RuntimeError("zstd 压缩消息自检失败")
    if not verify_signature():
        raise RuntimeError("个人签名完整性自检失败")

    environment_status = "skipped"
    if check_environment:
        executable = find_weixin_executable()
        # This optional environment probe checks local availability, not login
        # identity. Only the connection flow can confirm the current account.
        accounts = discover_accounts(include_process_memory=False)
        if not executable.is_file() or not accounts:
            raise RuntimeError("没有自动识别到微信程序或本地账号目录")
        environment_status = "ok"
    conversation = Conversation(
        username=_SELF_TEST_CONVERSATION_ID,
        display_name="成品自检会话 🙂",
        summary="仅使用内置合成消息",
    )
    messages = (
        Message(
            local_id=1,
            timestamp=int(datetime(2026, 8, 23, 9, 15, 0).timestamp()),
            message_type=1,
            sender_id="self",
            sender_name="我",
            is_outgoing=True,
            content="这是一条中文聊天格式打包自检消息。",
        ),
        Message(
            local_id=2,
            timestamp=int(datetime(2026, 8, 23, 9, 16, 0).timestamp()),
            message_type=1,
            sender_id="selftest_contact",
            sender_name="测试联系人",
            is_outgoing=False,
            content=(
                "第二行包含换行、English、表情 🙂 和微信内置表情[爱心]\n"
                "不会访问真实微信数据。"
            ),
        ),
        Message(
            local_id=3,
            timestamp=int(datetime(2026, 8, 23, 9, 17, 0).timestamp()),
            message_type=34,
            sender_id="selftest_contact",
            sender_name="测试联系人",
            is_outgoing=False,
            content=parse_message_text(
                34,
                '<msg><voicemsg/><voicetrans transtext="微信官方语音转写自检" istransend="1"/></msg>',
            ),
        ),
        Message(
            local_id=4,
            timestamp=int(datetime(2026, 8, 23, 9, 18, 0).timestamp()),
            message_type=3,
            sender_id="self",
            sender_name="我",
            is_outgoing=True,
            content="[图片]",
            conversation_id=conversation.username,
            media=MediaReference("image", md5="synthetic"),
        ),
    )
    image_stream = io.BytesIO()
    Image.new("RGB", (320, 180), (35, 122, 86)).save(image_stream, format="PNG")
    synthetic_image = PdfImage(
        data=image_stream.getvalue(),
        image_format="PNG",
        width=320,
        height=180,
        source="内置合成图片",
    )
    txt_path = output_dir / "packaged-self-test.txt"
    pdf_path = output_dir / "packaged-self-test.pdf"
    json_path = output_dir / "packaged-self-test.json"
    jsonl_path = output_dir / "packaged-self-test.jsonl"
    with JsonTextTranscriptWriter(json_path, conversation) as writer:
        for message in messages:
            writer.write(message)
        json_count = writer.count
    with JsonlTranscriptWriter(jsonl_path, conversation) as writer:
        for message in messages:
            writer.write(message)
        jsonl_count = writer.count
    with TxtTranscriptWriter(txt_path, conversation) as writer:
        for message in messages:
            writer.write(message)
        txt_count = writer.count
    with PdfTranscriptWriter(pdf_path, conversation) as writer:
        for message in messages:
            writer.write(
                message,
                image=synthetic_image if message.local_id == 4 else None,
            )
        pdf_count = writer.count

    class SyntheticImageResolver:
        def resolve(self, message: Message) -> PdfImage | None:
            return synthetic_image if message.message_type == 3 else None

        def resolve_card_cover(self, _urls) -> PdfImage | None:
            return None

    html_root = _prepare_self_test_html_root(output_dir)
    html_archive = export_chat_html_archive(
        root=html_root,
        conversation=conversation,
        messages=messages,
        options=HtmlExportOptions(
            include_images=True,
            include_stickers=True,
            include_file_metadata=True,
            include_file_binaries=False,
            include_videos=False,
            allow_network_completion=False,
        ),
        media_resolver=SyntheticImageResolver(),
        chunk_size=2,
    )
    html_chunk_text = "\n".join(
        path.read_text(encoding="utf-8")
        for path in sorted((html_archive.root / "data").glob("messages-*.js"))
    )
    required_html_files = (
        html_archive.entrypoint,
        html_archive.manifest,
        html_archive.checksum_manifest,
        html_archive.root / "assets" / "viewer.css",
        html_archive.root / "assets" / "viewer.js",
    )
    if (
        html_archive.status != "complete"
        or html_archive.message_count != len(messages)
        or not all(path.is_file() for path in required_html_files)
        or "❤️" not in html_chunk_text
        or "[爱心]" not in html_chunk_text
    ):
        raise RuntimeError("聊天离线 HTML 自检失败")

    package_request = JsonlPackageRequest(
        conversations=(conversation,),
        output_dir=output_dir / "package",
        include_videos=False,
    )
    package = export_conversation_jsonl_package(
        account=AccountLocation(output_dir / "synthetic-account", "self", "self-test"),
        self_wxid="self",
        conversation=conversation,
        messages=messages,
        output_dir=package_request.output_dir,
        request=package_request,
        image_resolver=SyntheticImageResolver(),  # type: ignore[arg-type]
    )
    with zipfile.ZipFile(package.path) as package_zip:
        if not {"messages.jsonl", "manifest.json", "导出说明.txt"}.issubset(
            package_zip.namelist()
        ):
            raise RuntimeError("AI 完整资料包自检失败")

    moments_path = output_dir / "packaged-self-test-moments"
    moment_media = MomentMedia(md5="0" * 32)
    moment = Moment(
        post_id="selftest-moment",
        username=conversation.username,
        timestamp=int(datetime(2026, 8, 23, 10, 0, 0).timestamp()),
        content="朋友圈离线归档打包自检，不访问真实微信数据。",
        media=(moment_media,),
        is_pinned=True,
    )
    archive_media = MomentMediaFile(
        data=synthetic_image.data,
        extension="png",
        mime_type="image/png",
        source="内置合成图片",
    )
    moments_writer = MomentsArchiveWriter(moments_path, conversation)
    moments_writer.write(moment, ((moment_media, archive_media),))
    moments_html, moments_json, moments_manifest = moments_writer.finish()
    moments_count = len(moments_writer.posts)

    receipt_path = output_dir / "self-test-result.json"
    receipt_path.write_text(
        json.dumps(
            {
                "status": "ok",
                "message_count": len(messages),
                "json_count": json_count,
                "jsonl_count": jsonl_count,
                "txt_count": txt_count,
                "pdf_count": pdf_count,
                "html_count": html_archive.message_count,
                "zstd": "ok",
                "wechat_voice_text": "ok",
                "pdf_image": "ok",
                "html_archive": "ok",
                "moments_archive": "ok",
                "moments_count": moments_count,
                "jsonl_package": "ok",
                "jsonl_package_count": package.message_count,
                "auto_discovery": environment_status,
                "txt": txt_path.name,
                "pdf": pdf_path.name,
                "json": json_path.name,
                "jsonl": jsonl_path.name,
                "html": html_archive.entrypoint.relative_to(output_dir).as_posix(),
                "html_manifest": html_archive.manifest.relative_to(
                    output_dir
                ).as_posix(),
                "html_checksum": html_archive.checksum_manifest.relative_to(
                    output_dir
                ).as_posix(),
                "jsonl_package_path": package.path.relative_to(output_dir).as_posix(),
                "moments": moments_html.relative_to(output_dir).as_posix(),
                "moments_json": moments_json.relative_to(output_dir).as_posix(),
                "moments_manifest": moments_manifest.relative_to(output_dir).as_posix(),
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    return receipt_path
