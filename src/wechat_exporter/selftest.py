from __future__ import annotations

import json
import io
import hashlib
import os
import shutil
import struct
import zipfile
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import zstandard
from PIL import Image
from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from .chat_html_archive import (
    CHAT_HTML_SCHEMA,
    export_chat_html_archive,
)
from .content import decode_database_content, parse_message_text
from .exporters import PdfTranscriptWriter, TxtTranscriptWriter
from .estimates import build_chat_size_profile
from .json_exporter import JsonTextTranscriptWriter
from .jsonl_exporter import JsonlTranscriptWriter
from .jsonl_package import ChatVideoLocator, export_conversation_jsonl_package
from .database_cache import AccountDatabaseCache
from .media import MediaResolver, derive_emoticon_key, extract_media_reference
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
from .windows import discover_accounts, file_resource_users, find_weixin_executable, process_start_time


_SELF_TEST_CONVERSATION_ID = "selftest_contact"
_SELF_TEST_HTML_DIRECTORY = "packaged-self-test-chat-html"


def _verify_local_media_pipeline(output_dir: Path, message: Message, data: bytes) -> tuple[str, str]:
    """Use owned synthetic cache files, including real Windows DPAPI in the EXE."""
    account = AccountLocation(output_dir / "synthetic-account", "self", "self-test")
    keys = (b"0123456789abcdef", 123)  # Public synthetic fixture, never an account secret.
    image_path = (
        account.account_dir / "msg/attach"
        / hashlib.md5(message.conversation_id.encode()).hexdigest()
        / message.datetime.strftime("%Y-%m") / "Img" / f"{message.media.md5}.dat"
    )
    image_path.parent.mkdir(parents=True, exist_ok=True)
    padder = padding.PKCS7(128).padder()
    padded = padder.update(data[:32]) + padder.finalize()
    cipher = Cipher(algorithms.AES(keys[0]), modes.ECB()).encryptor()
    image_path.write_bytes(
        b"\x07\x08\x56\x32\x08\x07" + struct.pack("<II", 32, 8) + b"\x01"
        + cipher.update(padded) + cipher.finalize() + data[32:-8]
        + bytes(value ^ keys[1] for value in data[-8:])
    )
    image_cache_status = "skipped_non_windows"
    sticker_cache_status = "skipped_non_windows"
    if os.name == "nt":
        cache = AccountDatabaseCache(account, root=output_dir / "synthetic-media-cache")
        if not cache.save_image_keys("self", keys):
            raise RuntimeError("合成图片密钥加密保存自检失败")
        resolved = MediaResolver(account, "self", image_key_cache=cache).resolve(message)
        if resolved is None or resolved.data != data:
            raise RuntimeError("不依赖微信进程的图片解密自检失败")
        image_cache_status = "ok"
        sticker_stream = io.BytesIO()
        Image.new("RGB", (32, 24), "red").save(sticker_stream, format="GIF", save_all=True,
            append_images=[Image.new("RGB", (32, 24), "green")], duration=100, loop=0)
        sticker_data = sticker_stream.getvalue()
        sticker_md5 = hashlib.md5(sticker_data).hexdigest()
        sticker_key = derive_emoticon_key(123456, "self")
        sticker_path = account.account_dir / "business/emoticon/Persist" / sticker_md5[:2] / sticker_md5
        sticker_path.parent.mkdir(parents=True, exist_ok=True)
        padder = padding.PKCS7(128).padder()
        padded = padder.update(sticker_data) + padder.finalize()
        cipher = Cipher(algorithms.AES(sticker_key), modes.CBC(sticker_key)).encryptor()
        sticker_path.write_bytes(cipher.update(padded) + cipher.finalize())
        if not cache.save_emoticon_key("self", sticker_key):
            raise RuntimeError("合成表情密钥加密保存自检失败")
        sticker_message = replace(message, message_type=47, content="[动画表情]",
            media=MediaReference("emoticon", md5=sticker_md5))
        sticker_resolved = MediaResolver(account, "self", image_key_cache=cache).resolve(sticker_message)
        if sticker_resolved is None or sticker_resolved.data != sticker_data or not sticker_resolved.is_animated:
            raise RuntimeError("不依赖微信进程的动画表情解密自检失败")
        sticker_cache_status = "ok"

    local_md5, content_md5 = "b" * 32, "c" * 32
    video_bytes = b"\x00\x00\x00\x18ftypisom" + bytes(64)
    video_path = account.account_dir / "msg/video" / message.datetime.strftime("%Y-%m") / f"{local_md5}.mp4"
    video_path.parent.mkdir(parents=True, exist_ok=True)
    video_path.write_bytes(video_bytes)
    reference = extract_media_reference(
        43, f'<msg><videomsg md5="{content_md5}" length="{len(video_bytes)}"/></msg>',
        b"\x08\x3d\x10\x01\x22\x22" + local_md5.encode(),
    )
    video_message = replace(message, message_type=43, content="[视频]", media=reference)
    if ChatVideoLocator(account).find(video_message) != video_path:
        raise RuntimeError("视频本地缓存标识匹配自检失败")
    profile = build_chat_size_profile(
        SimpleNamespace(iter_messages=lambda *args, **kwargs: iter((message, video_message))),
        account, (Conversation(message.conversation_id, "成品大小估算自检"),),
    )
    without_video = profile.estimate("jsonl_package", include_videos=False)
    with_video = profile.estimate("jsonl_package", include_videos=True)
    if not len(video_bytes) <= with_video.byte_count - without_video.byte_count <= len(video_bytes) + 300:
        raise RuntimeError("导出大小随视频选项更新的成品自检失败")
    from .gui import _conversation_preview
    if _conversation_preview("收到[玫瑰]【花】[OK]") != "收到[玫瑰]【花】[OK]":
        raise RuntimeError("最近消息表情显示成品自检失败")
    if os.name == "nt":
        from .preview_emoji import render_native_face
        import tkinter as tk
        from PIL import ImageTk
        preview_root = tk.Tk()
        preview_root.withdraw()
        try:
            for face in ("[玫瑰]", "[西瓜]", "[吃瓜]", "[握手]", "[咖啡]", "[OK]"):
                bitmap = render_native_face(face, 22)
                if bitmap is None or not any(a > 100 and max(r, g, b) - min(r, g, b) > 35
                                             for r, g, b, a in (bitmap.getpixel((x, y))
                                                 for y in range(bitmap.height) for x in range(bitmap.width))):
                    raise RuntimeError("最近消息彩色表情成品自检失败")
                photo = ImageTk.PhotoImage(bitmap, master=preview_root)
                if (photo.width(), photo.height()) != bitmap.size:
                    raise RuntimeError("最近消息彩色位图与 Tk 桥接成品自检失败")
        finally:
            preview_root.destroy()
    return image_cache_status, sticker_cache_status


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

    runtime_query_status = "skipped_non_windows"
    if os.name == "nt":
        resource = output_dir / "self-test-resource.bin"
        resource.write_bytes(b"synthetic startup resource ownership probe")
        with resource.open("rb"):
            if (os.getpid(), process_start_time(os.getpid())) not in file_resource_users((resource,)):
                raise RuntimeError("运行中账号快速核验所需的系统查询自检失败")
        runtime_query_status = "ok"

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
            media=MediaReference("image", md5="a" * 32),
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
    image_cache_status, sticker_cache_status = _verify_local_media_pipeline(output_dir, messages[3], synthetic_image.data)
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
        or "[爱心]" not in html_chunk_text
        or not any((html_archive.root / "assets" / "faces").glob("*.png"))
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
                "image_key_cache": image_cache_status,
                "emoticon_key_cache": sticker_cache_status,
                "runtime_resource_query": runtime_query_status,
                "local_video_lookup": "ok",
                "export_size_estimates": "ok",
                "conversation_preview_faces": "ok",
                "conversation_preview_colors": "ok" if os.name == "nt" else "skipped_non_windows",
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
