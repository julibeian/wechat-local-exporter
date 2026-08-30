from __future__ import annotations

from datetime import datetime
import hashlib
import html
import json
from pathlib import Path
import re
from urllib.parse import quote

from .models import Conversation, Moment, MomentMedia, MomentMediaFile


class MomentsArchiveWriter:
    """Write a portable HTML/JSON archive with untouched decrypted media."""

    def __init__(self, root: Path, conversation: Conversation):
        self.root = root
        self.conversation = conversation
        self.media_root = root / "media"
        self.posts: list[dict[str, object]] = []
        self.media_count = 0
        self.image_count = 0
        self.video_count = 0
        self.missing_count = 0

    def write(
        self,
        moment: Moment,
        resolved_media: tuple[
            tuple[MomentMedia, MomentMediaFile | None], ...
        ] = (),
    ) -> None:
        post_number = len(self.posts) + 1
        date_key = (
            moment.datetime.strftime("%Y-%m-%d")
            if moment.timestamp > 0
            else "日期未知"
        )
        time_text = (
            moment.datetime.strftime("%Y-%m-%d %H:%M:%S")
            if moment.timestamp > 0
            else "时间未知"
        )
        post_id = _safe_component(moment.post_id) or f"post-{post_number}"
        media_items: list[dict[str, object]] = []

        for media_index, (reference, resolved) in enumerate(
            resolved_media, start=1
        ):
            label = (
                "实况照片视频"
                if reference.role == "live_photo_video"
                else ("视频" if reference.kind == "video" else "照片")
            )
            item: dict[str, object] = {
                "index": media_index,
                "kind": reference.kind,
                "role": reference.role,
                "label": label,
                "width": reference.width,
                "height": reference.height,
                "declared_size": reference.total_size,
            }
            if resolved is None:
                item.update({"status": "missing", "path": None})
                self.missing_count += 1
                media_items.append(item)
                continue

            date_dir = self.media_root / date_key
            date_dir.mkdir(parents=True, exist_ok=True)
            kind_label = "video" if reference.kind == "video" else "image"
            filename = (
                f"{post_number:04d}_{post_id}_{media_index:02d}_"
                f"{kind_label}.{resolved.extension}"
            )
            target = date_dir / filename
            target.write_bytes(resolved.data)
            relative = target.relative_to(self.root).as_posix()
            digest = hashlib.sha256(resolved.data).hexdigest()
            item.update(
                {
                    "status": "ok",
                    "path": relative,
                    "mime_type": resolved.mime_type,
                    "bytes": len(resolved.data),
                    "sha256": digest,
                    "source": resolved.source,
                    "is_thumbnail": resolved.is_thumbnail,
                }
            )
            self.media_count += 1
            if reference.kind == "video":
                self.video_count += 1
            else:
                self.image_count += 1
            media_items.append(item)

        self.posts.append(
            {
                "id": moment.post_id,
                "timestamp": moment.timestamp,
                "datetime": time_text,
                "date": date_key,
                "pinned": moment.is_pinned,
                "visibility": moment.visibility,
                "visibility_label": _visibility_label(
                    moment.visibility, is_self=self.conversation.is_self
                ),
                "content": moment.content,
                "location": moment.location,
                "media": media_items,
            }
        )

    def finish(self) -> tuple[Path, Path, Path]:
        self.root.mkdir(parents=True, exist_ok=True)
        generated_at = datetime.now().astimezone().isoformat(timespec="seconds")
        visibility_counts: dict[str, int] = {}
        for post in self.posts:
            label = str(post["visibility_label"])
            visibility_counts[label] = visibility_counts.get(label, 0) + 1
        payload = {
            "schema": "wechat-moments-offline-archive",
            "schema_version": 1,
            "generated_at": generated_at,
            "contact": {"display_name": self.conversation.display_name},
            "scope": (
                "本机已同步的本人朋友圈记录，包含私密和分组可见动态"
                if self.conversation.is_self
                else "当前账号本机已同步且仍可见的联系人朋友圈记录"
            ),
            "summary": {
                "posts": len(self.posts),
                "media_exported": self.media_count,
                "images": self.image_count,
                "videos": self.video_count,
                "media_missing": self.missing_count,
                "visibility": visibility_counts,
            },
            "posts": self.posts,
        }
        json_path = self.root / "moments.json"
        json_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

        html_path = self.root / "index.html"
        html_path.write_text(self._render_html(generated_at), encoding="utf-8")

        manifest_path = self.root / "manifest-sha256.txt"
        hashed_paths = sorted(
            [path for path in self.media_root.rglob("*") if path.is_file()]
            + [json_path, html_path],
            key=lambda path: path.relative_to(self.root).as_posix(),
        )
        manifest_path.write_text(
            "".join(
                f"{_file_sha256(path)} *{path.relative_to(self.root).as_posix()}\n"
                for path in hashed_paths
            ),
            encoding="utf-8",
        )
        return html_path, json_path, manifest_path

    def _render_html(self, generated_at: str) -> str:
        pinned = [post for post in self.posts if post["pinned"]]
        dated: dict[str, list[dict[str, object]]] = {}
        for post in self.posts:
            if post["pinned"]:
                continue
            dated.setdefault(str(post["date"]), []).append(post)

        sections: list[str] = []
        if pinned:
            sections.append(self._render_section("置顶", pinned, pinned=True))
        for date_key, posts in dated.items():
            sections.append(self._render_section(date_key, posts))

        display_name = html.escape(self.conversation.display_name)
        summary = (
            f"{len(self.posts)} 条动态 · {self.image_count} 张照片 · "
            f"{self.video_count} 个视频"
        )
        if self.missing_count:
            summary += f" · {self.missing_count} 个媒体缺失"
        body = "\n".join(sections) or '<p class="empty">没有可显示的动态。</p>'
        return f"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{display_name}的朋友圈离线归档</title>
<style>
:root {{ color-scheme: light; --ink:#1f2937; --muted:#667085; --line:#e5e7eb; --accent:#16784b; --paper:#fff; --bg:#f5f7f6; }}
* {{ box-sizing:border-box; }}
body {{ margin:0; color:var(--ink); background:var(--bg); font:16px/1.65 "Microsoft YaHei UI","PingFang SC",sans-serif; }}
header {{ position:sticky; top:0; z-index:3; padding:20px max(20px,calc((100% - 1040px)/2)); color:#fff; background:rgba(24,93,65,.96); backdrop-filter:blur(12px); }}
h1 {{ margin:0; font-size:clamp(22px,4vw,32px); }}
header p {{ margin:4px 0 0; opacity:.88; }}
main {{ width:min(1040px,calc(100% - 28px)); margin:26px auto 70px; }}
.notice {{ margin-bottom:24px; padding:14px 17px; border:1px solid #cfe5d9; border-radius:12px; background:#eff9f3; color:#24543f; }}
.section-title {{ display:flex; gap:10px; align-items:center; margin:30px 0 12px; font-size:20px; }}
.badge {{ padding:2px 9px; border-radius:999px; color:#875d00; background:#fff1b8; font-size:13px; }}
.visibility {{ padding:1px 8px; border-radius:999px; color:#31516a; background:#eaf3f8; }}
.post {{ margin:0 0 16px; padding:20px; border:1px solid var(--line); border-radius:16px; background:var(--paper); box-shadow:0 5px 18px rgba(31,41,55,.05); }}
.meta {{ display:flex; flex-wrap:wrap; gap:8px 14px; color:var(--muted); font-size:14px; }}
.content {{ margin:12px 0; white-space:pre-wrap; overflow-wrap:anywhere; }}
.location {{ margin:8px 0; color:#52616b; font-size:14px; }}
.media-grid {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(min(100%,260px),1fr)); gap:12px; margin-top:14px; }}
.media {{ position:relative; min-width:0; overflow:hidden; border:1px solid var(--line); border-radius:12px; background:#111; }}
.media img,.media video {{ display:block; width:100%; max-height:76vh; object-fit:contain; background:#111; }}
.media a {{ display:block; }}
.media-label {{ position:absolute; left:8px; bottom:8px; padding:3px 8px; border-radius:7px; color:#fff; background:rgba(0,0,0,.68); font-size:12px; pointer-events:none; }}
.missing {{ padding:28px 14px; color:#8a4b35; background:#fff4ed; text-align:center; }}
.footer {{ margin-top:30px; color:var(--muted); font-size:13px; text-align:center; }}
.empty {{ padding:30px; text-align:center; color:var(--muted); }}
@media (max-width:600px) {{ header {{ padding:15px 16px; }} main {{ width:calc(100% - 18px); margin-top:14px; }} .post {{ padding:14px; }} }}
</style>
</head>
<body>
<header><h1>{display_name}的朋友圈</h1><p>{html.escape(summary)}</p></header>
<main>
<div class="notice">离线归档：照片保持解密后的原始字节，点击照片可在新窗口查看原图；视频可直接播放。JSON 索引与 SHA-256 清单位于同一目录。</div>
{body}
<p class="footer">生成时间：{html.escape(generated_at)} · 无需联网即可阅读</p>
</main>
</body>
</html>
"""

    def _render_section(
        self,
        title: str,
        posts: list[dict[str, object]],
        *,
        pinned: bool = False,
    ) -> str:
        badge = '<span class="badge">置顶</span>' if pinned else ""
        rendered = "\n".join(self._render_post(post) for post in posts)
        return (
            f'<section><h2 class="section-title">{html.escape(title)}{badge}</h2>'
            f"{rendered}</section>"
        )

    def _render_post(self, post: dict[str, object]) -> str:
        content = html.escape(str(post["content"] or "[无文字内容]"))
        location = str(post["location"] or "")
        location_html = (
            f'<p class="location">📍 {html.escape(location)}</p>' if location else ""
        )
        media_html = "".join(
            self._render_media(item)
            for item in post["media"]  # type: ignore[union-attr]
        )
        media_grid = f'<div class="media-grid">{media_html}</div>' if media_html else ""
        pinned = " · 置顶" if post["pinned"] else ""
        visibility = html.escape(str(post["visibility_label"]))
        return f"""<article class="post">
<div class="meta"><time>{html.escape(str(post['datetime']))}</time><span>{html.escape(pinned)}</span><span class="visibility">{visibility}</span></div>
<p class="content">{content}</p>{location_html}{media_grid}
</article>"""

    @staticmethod
    def _render_media(item: dict[str, object]) -> str:
        label = html.escape(str(item["label"]))
        if item["status"] != "ok" or not item.get("path"):
            return f'<div class="media missing">{label}未能导出</div>'
        path = quote(str(item["path"]), safe="/")
        thumbnail = " · 缩略图兜底" if item.get("is_thumbnail") else ""
        label_html = f'<span class="media-label">{label}{thumbnail}</span>'
        if item["kind"] == "video":
            mime_type = html.escape(str(item.get("mime_type") or "video/mp4"))
            return (
                '<div class="media">'
                f'<video controls preload="metadata" playsinline><source src="{path}" '
                f'type="{mime_type}">浏览器不支持此视频格式。</video>{label_html}</div>'
            )
        return (
            '<div class="media">'
            f'<a href="{path}" target="_blank" rel="noopener" title="打开原图">'
            f'<img src="{path}" loading="lazy" decoding="async" alt="{label}"></a>'
            f"{label_html}</div>"
        )


def _safe_component(value: str) -> str:
    normalized = re.sub(r"[^0-9A-Za-z_-]+", "-", value or "").strip("-_")
    return normalized[:48]


def _visibility_label(value: str, *, is_self: bool) -> str:
    if value == "private":
        return "仅自己可见"
    if value == "selected":
        return "部分可见"
    if value == "excluded":
        return "不给谁看"
    return "好友/分组可见（本机记录未细分）" if is_self else "当前账号可见"


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
