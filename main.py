from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import json
import os
import re
import secrets
import sqlite3
import uuid
from collections import deque
from datetime import UTC, datetime, timedelta
from pathlib import Path
from time import monotonic
from typing import Any
from urllib.parse import urlparse

from aiohttp import web

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import Image
from astrbot.api.star import Context, Star, register
from astrbot.core.utils.astrbot_path import get_astrbot_data_path
from astrbot.core.utils.io import download_image_by_url

SUPPORTED_IMAGE_TYPES: dict[str, tuple[str, str]] = {
    "jpeg": ("jpg", "image/jpeg"),
    "png": ("png", "image/png"),
    "gif": ("gif", "image/gif"),
    "webp": ("webp", "image/webp"),
    "bmp": ("bmp", "image/bmp"),
    "tiff": ("tiff", "image/tiff"),
}


def detect_image_type(image_bytes: bytes) -> tuple[str, str] | None:
    if len(image_bytes) < 16:
        return None

    if image_bytes.startswith(b"\xff\xd8\xff"):
        return SUPPORTED_IMAGE_TYPES["jpeg"]

    if image_bytes.startswith(b"\x89PNG\r\n\x1a\n"):
        return SUPPORTED_IMAGE_TYPES["png"]

    if image_bytes.startswith(b"GIF87a") or image_bytes.startswith(b"GIF89a"):
        return SUPPORTED_IMAGE_TYPES["gif"]

    if image_bytes.startswith(b"BM"):
        return SUPPORTED_IMAGE_TYPES["bmp"]

    if image_bytes[:4] == b"RIFF" and image_bytes[8:12] == b"WEBP":
        return SUPPORTED_IMAGE_TYPES["webp"]

    if image_bytes.startswith((b"II*\x00", b"MM\x00*")):
        return SUPPORTED_IMAGE_TYPES["tiff"]

    return None


def utc_now_iso() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


class UploadRateLimiter:
    """Simple in-memory per-key sliding window limiter."""

    def __init__(self):
        self._events: dict[str, deque[float]] = {}
        self._lock = asyncio.Lock()

    async def check(self, key: str, limit: int, window_sec: int) -> tuple[bool, int]:
        if limit <= 0 or window_sec <= 0:
            return True, 0

        now = monotonic()
        cutoff = now - window_sec

        async with self._lock:
            bucket = self._events.get(key)
            if bucket is None:
                bucket = deque()
                self._events[key] = bucket

            while bucket and bucket[0] <= cutoff:
                bucket.popleft()

            if len(bucket) >= limit:
                retry_after = int(window_sec - (now - bucket[0])) + 1
                if retry_after < 1:
                    retry_after = 1
                return False, retry_after

            bucket.append(now)

            # Prevent unbounded growth under bot scans.
            if len(self._events) > 4096:
                stale_keys = [k for k, v in self._events.items() if not v or v[-1] <= cutoff]
                for stale_key in stale_keys:
                    self._events.pop(stale_key, None)

            return True, 0


class ImageStore:
    def __init__(self, base_dir: Path):
        self.base_dir = base_dir
        self.images_dir = base_dir / "images"
        self.db_path = base_dir / "images.db"
        self._conn: sqlite3.Connection | None = None
        self._lock = asyncio.Lock()

    async def initialize(self) -> None:
        self.images_dir.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.db_path)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS images (
                id TEXT PRIMARY KEY,
                sha256 TEXT NOT NULL UNIQUE,
                filename TEXT NOT NULL,
                mime TEXT NOT NULL,
                size INTEGER NOT NULL,
                source TEXT NOT NULL DEFAULT '',
                original_name TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL
            )
            """
        )
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_images_created_at ON images(created_at DESC)"
        )
        self._conn.commit()

    async def close(self) -> None:
        if self._conn:
            self._conn.close()
            self._conn = None

    def _ensure_conn(self) -> sqlite3.Connection:
        if not self._conn:
            raise RuntimeError("ImageStore is not initialized")
        return self._conn

    @staticmethod
    def _row_to_dict(row: sqlite3.Row, deduplicated: bool = False) -> dict[str, Any]:
        data = dict(row)
        data["deduplicated"] = deduplicated
        return data

    async def get_image(self, image_id: str) -> dict[str, Any] | None:
        async with self._lock:
            conn = self._ensure_conn()
            row = conn.execute(
                "SELECT id, sha256, filename, mime, size, source, original_name, created_at FROM images WHERE id = ?",
                (image_id,),
            ).fetchone()
        if not row:
            return None
        return dict(row)

    async def delete_image(self, image_id: str) -> dict[str, Any]:
        async with self._lock:
            conn = self._ensure_conn()
            row = conn.execute(
                "SELECT id, filename FROM images WHERE id = ?",
                (image_id,),
            ).fetchone()
            if not row:
                return {"deleted": False, "reason": "not_found"}

            record = dict(row)
            file_removed = False
            file_path = self.images_dir / str(record["filename"])
            if file_path.exists():
                try:
                    file_path.unlink()
                    file_removed = True
                except Exception:
                    logger.exception("[ImageBed] failed to remove file: %s", file_path)

            conn.execute("DELETE FROM images WHERE id = ?", (image_id,))
            conn.commit()

        return {"deleted": True, "file_removed": file_removed}

    async def cleanup_older_than(self, days: int, limit: int = 1000) -> dict[str, Any]:
        if days < 1:
            raise ValueError("days 必须大于 0")
        if limit < 1:
            raise ValueError("limit 必须大于 0")

        cutoff = (datetime.now(UTC) - timedelta(days=days)).replace(microsecond=0).isoformat()

        async with self._lock:
            conn = self._ensure_conn()
            rows = conn.execute(
                """
                SELECT id, filename, created_at
                FROM images
                WHERE created_at < ?
                ORDER BY created_at ASC
                LIMIT ?
                """,
                (cutoff, limit),
            ).fetchall()

            if not rows:
                return {
                    "cutoff": cutoff,
                    "matched": 0,
                    "deleted": 0,
                    "files_removed": 0,
                }

            delete_ids: list[tuple[str]] = []
            files_removed = 0
            for row in rows:
                record = dict(row)
                image_id = str(record["id"])
                file_path = self.images_dir / str(record["filename"])
                if file_path.exists():
                    try:
                        file_path.unlink()
                        files_removed += 1
                    except Exception:
                        logger.exception("[ImageBed] failed to remove file during cleanup: %s", file_path)
                delete_ids.append((image_id,))

            conn.executemany("DELETE FROM images WHERE id = ?", delete_ids)
            conn.commit()

        return {
            "cutoff": cutoff,
            "matched": len(rows),
            "deleted": len(delete_ids),
            "files_removed": files_removed,
        }

    def file_path(self, record: dict[str, Any]) -> Path:
        return self.images_dir / str(record["filename"])

    async def save_image(
        self,
        image_bytes: bytes,
        source: str,
        original_name: str,
        deduplicate: bool = True,
    ) -> dict[str, Any]:
        image_type = detect_image_type(image_bytes)
        if not image_type:
            raise ValueError("仅支持常见图片格式：jpg/png/gif/webp/bmp/tiff")

        ext, mime = image_type
        sha256 = hashlib.sha256(image_bytes).hexdigest()
        size = len(image_bytes)

        async with self._lock:
            conn = self._ensure_conn()
            if deduplicate:
                row = conn.execute(
                    "SELECT id, sha256, filename, mime, size, source, original_name, created_at FROM images WHERE sha256 = ?",
                    (sha256,),
                ).fetchone()
                if row:
                    existing = dict(row)
                    existing_path = self.file_path(existing)
                    if existing_path.exists():
                        return self._row_to_dict(row, deduplicated=True)
                    conn.execute("DELETE FROM images WHERE id = ?", (existing["id"],))
                    conn.commit()

            image_id = uuid.uuid4().hex[:16]
            filename = f"{image_id}.{ext}"
            final_path = self.images_dir / filename
            final_path.write_bytes(image_bytes)

            created_at = utc_now_iso()
            conn.execute(
                """
                INSERT INTO images (id, sha256, filename, mime, size, source, original_name, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    image_id,
                    sha256,
                    filename,
                    mime,
                    size,
                    source,
                    original_name,
                    created_at,
                ),
            )
            conn.commit()

            row = conn.execute(
                "SELECT id, sha256, filename, mime, size, source, original_name, created_at FROM images WHERE id = ?",
                (image_id,),
            ).fetchone()
            if not row:
                raise RuntimeError("图片写入成功但未找到数据库记录")

        return self._row_to_dict(row, deduplicated=False)


@register(
    "astrbot_plugin_local_image_bed",
    "Ethan",
    "本地图床插件：上传图片到 AstrBot 插件目录并返回持久化链接",
    "1.0.0",
)
class LocalImageBedPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.plugin_name = "astrbot_plugin_local_image_bed"

        plugin_data_dir = Path(get_astrbot_data_path()) / "plugin_data" / self.plugin_name
        self.plugin_data_dir = plugin_data_dir
        self.store = ImageStore(plugin_data_dir)
        self._upload_rate_limiter = UploadRateLimiter()
        self._audit_lock = asyncio.Lock()

        self._http_app: web.Application | None = None
        self._http_runner: web.AppRunner | None = None
        self._http_site: web.TCPSite | None = None

    async def initialize(self):
        await self.store.initialize()
        await self._start_http_server()

        logger.info(
            "[ImageBed] initialized. data_dir=%s, public_base_url=%s",
            self.store.base_dir,
            self._public_base_url(),
        )

    async def terminate(self):
        await self._stop_http_server()
        await self.store.close()
        logger.info("[ImageBed] terminated")

    def _cfg_str(self, key: str, default: str = "") -> str:
        value = self.config.get(key, default)
        if value is None:
            return default
        return str(value).strip()

    def _cfg_int(self, key: str, default: int) -> int:
        value = self.config.get(key, default)
        try:
            return int(value)
        except Exception:
            return default

    def _cfg_bool(self, key: str, default: bool) -> bool:
        value = self.config.get(key, default)
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            lowered = value.strip().lower()
            if lowered in {"1", "true", "yes", "on"}:
                return True
            if lowered in {"0", "false", "no", "off"}:
                return False
        return bool(value)

    def _max_upload_bytes(self) -> int:
        max_mb = self._cfg_int("max_upload_mb", 10)
        if max_mb < 1:
            max_mb = 1
        if max_mb > 100:
            max_mb = 100
        return max_mb * 1024 * 1024

    def _public_base_url(self) -> str:
        configured = self._cfg_str("public_base_url", "").rstrip("/")
        if configured:
            return configured

        host = self._cfg_str("listen_host", "127.0.0.1")
        port = self._cfg_int("listen_port", 18345)
        if host in {"0.0.0.0", "::"}:
            host = "127.0.0.1"
        return f"http://{host}:{port}"

    def _build_image_url(self, image_id: str) -> str:
        return f"{self._public_base_url()}/i/{image_id}"

    def _upload_token(self) -> str:
        return self._cfg_str("upload_token", "")

    def _audit_log_path(self) -> Path:
        return self.plugin_data_dir / "audit_upload.jsonl"

    def _enable_url_upload(self) -> bool:
        return self._cfg_bool("enable_url_upload", False)

    def _rate_limit_config(self) -> tuple[int, int]:
        count = self._cfg_int("upload_rate_limit_count", 30)
        window_sec = self._cfg_int("upload_rate_limit_window_sec", 60)
        if count < 0:
            count = 0
        if window_sec < 0:
            window_sec = 0
        return count, window_sec

    def _request_client_id(self, request: web.Request) -> str:
        forwarded_for = request.headers.get("X-Forwarded-For", "").strip()
        if forwarded_for:
            first = forwarded_for.split(",")[0].strip()
            if first:
                return first

        real_ip = request.headers.get("X-Real-IP", "").strip()
        if real_ip:
            return real_ip

        if request.remote:
            return request.remote
        return "unknown"

    async def _check_upload_rate_limit(self, request: web.Request) -> tuple[bool, int]:
        limit, window_sec = self._rate_limit_config()
        if limit <= 0 or window_sec <= 0:
            return True, 0
        key = self._request_client_id(request)
        return await self._upload_rate_limiter.check(key, limit, window_sec)

    def _token_ok(self, request: web.Request) -> bool:
        required = self._upload_token()
        if not required:
            return True

        provided = request.headers.get("X-ImageBed-Token", "")
        if not provided:
            return False

        return secrets.compare_digest(required, provided)

    async def _write_audit_log(self, data: dict[str, Any]) -> None:
        path = self._audit_log_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(data, ensure_ascii=False)
        async with self._audit_lock:
            with path.open("a", encoding="utf-8") as f:
                f.write(line + "\n")

    async def _audit_upload_event(
        self,
        request: web.Request,
        *,
        status: int,
        result: str,
        reason: str = "",
        image_id: str = "",
        size: int = 0,
        mime: str = "",
        deduplicated: bool | None = None,
    ) -> None:
        record = {
            "ts": utc_now_iso(),
            "event": "http_upload",
            "status": status,
            "result": result,
            "reason": reason,
            "client_ip": self._request_client_id(request),
            "method": request.method,
            "path": request.path_qs,
            "content_type": request.content_type or "",
            "content_length": request.content_length or 0,
            "user_agent": request.headers.get("User-Agent", ""),
            "image_id": image_id,
            "size": size,
            "mime": mime,
            "deduplicated": deduplicated,
        }
        await self._write_audit_log(record)

    async def _audit_command_event(
        self,
        event: AstrMessageEvent,
        *,
        command: str,
        result: str,
        reason: str = "",
        image_id: str = "",
        days: int | None = None,
        limit: int | None = None,
        deleted: int | None = None,
    ) -> None:
        sender_id = ""
        try:
            sender_id = str(event.get_sender_id())
        except Exception:
            sender_id = ""
        record = {
            "ts": utc_now_iso(),
            "event": "command",
            "command": command,
            "result": result,
            "reason": reason,
            "sender_id": sender_id,
            "role": getattr(event, "role", ""),
            "image_id": image_id,
            "days": days,
            "limit": limit,
            "deleted": deleted,
            "umo": getattr(event, "unified_msg_origin", ""),
        }
        await self._write_audit_log(record)

    def _json_error(self, message: str, status: int = 400) -> web.Response:
        return web.json_response({"ok": False, "error": message}, status=status)

    async def _start_http_server(self) -> None:
        host = self._cfg_str("listen_host", "127.0.0.1")
        port = self._cfg_int("listen_port", 18345)

        self._http_app = web.Application(client_max_size=self._max_upload_bytes())
        self._http_app.add_routes(
            [
                web.get("/", self._http_index),
                web.get("/health", self._http_health),
                web.get("/i/{image_id}", self._http_get_image),
                web.post("/upload", self._http_upload),
            ]
        )

        self._http_runner = web.AppRunner(self._http_app)
        await self._http_runner.setup()
        self._http_site = web.TCPSite(self._http_runner, host=host, port=port)
        await self._http_site.start()

        logger.info("[ImageBed] HTTP server started at %s:%s", host, port)

    async def _stop_http_server(self) -> None:
        if self._http_site:
            await self._http_site.stop()
            self._http_site = None

        if self._http_runner:
            await self._http_runner.cleanup()
            self._http_runner = None

        self._http_app = None

    async def _http_index(self, request: web.Request) -> web.Response:
        return web.json_response(
            {
                "ok": True,
                "name": self.plugin_name,
                "endpoints": {
                    "health": "/health",
                    "upload": "POST /upload",
                    "image": "/i/{image_id}",
                },
                "max_upload_mb": self._cfg_int("max_upload_mb", 10),
                "token_required": bool(self._upload_token()),
            }
        )

    async def _http_health(self, request: web.Request) -> web.Response:
        return web.json_response({"ok": True, "status": "running", "time": utc_now_iso()})

    async def _http_get_image(self, request: web.Request) -> web.StreamResponse:
        image_id = request.match_info.get("image_id", "").strip()
        if not image_id:
            return self._json_error("缺少 image_id", status=400)

        record = await self.store.get_image(image_id)
        if not record:
            return self._json_error("图片不存在", status=404)

        file_path = self.store.file_path(record)
        if not file_path.exists():
            return self._json_error("图片文件缺失", status=404)

        response = web.FileResponse(path=file_path)
        response.content_type = str(record.get("mime") or "application/octet-stream")
        response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        response.headers["ETag"] = str(record.get("sha256") or "")
        response.headers["X-Image-Id"] = str(record.get("id") or "")
        return response

    async def _http_upload(self, request: web.Request) -> web.Response:
        allowed, retry_after = await self._check_upload_rate_limit(request)
        if not allowed:
            await self._audit_upload_event(
                request,
                status=429,
                result="rate_limited",
                reason=f"retry_after={retry_after}",
            )
            resp = self._json_error(f"上传过于频繁，请在 {retry_after} 秒后重试", status=429)
            resp.headers["Retry-After"] = str(retry_after)
            return resp

        if not self._token_ok(request):
            await self._audit_upload_event(
                request,
                status=401,
                result="unauthorized",
                reason="token_invalid_or_missing",
            )
            return self._json_error("未授权：token 不正确", status=401)

        try:
            image_bytes, original_name = await self._read_upload_payload(request)
            record = await self._save_image_bytes(
                image_bytes=image_bytes,
                source="http_upload",
                original_name=original_name,
            )
        except ValueError as exc:
            await self._audit_upload_event(
                request,
                status=400,
                result="rejected",
                reason=str(exc),
            )
            return self._json_error(str(exc), status=400)
        except web.HTTPRequestEntityTooLarge:
            await self._audit_upload_event(
                request,
                status=413,
                result="rejected",
                reason="payload_too_large",
            )
            return self._json_error("图片大小超出限制", status=413)
        except Exception as exc:
            logger.exception("[ImageBed] upload failed: %s", exc)
            await self._audit_upload_event(
                request,
                status=500,
                result="error",
                reason=str(exc),
            )
            return self._json_error("服务器内部错误", status=500)

        await self._audit_upload_event(
            request,
            status=200,
            result="ok",
            image_id=str(record.get("id") or ""),
            size=int(record.get("size") or 0),
            mime=str(record.get("mime") or ""),
            deduplicated=bool(record.get("deduplicated")),
        )
        return web.json_response({"ok": True, **record})

    async def _read_upload_payload(self, request: web.Request) -> tuple[bytes, str]:
        content_type = (request.content_type or "").lower()

        if content_type.startswith("multipart/"):
            reader = await request.multipart()
            while True:
                part = await reader.next()
                if part is None:
                    break

                if part.name in {"file", "image"}:
                    filename = part.filename or "upload"
                    image_bytes = await part.read(decode=False)
                    return image_bytes, filename

                if part.name in {"image_base64", "base64"}:
                    payload = (await part.text()).strip()
                    return self._decode_base64_payload(payload), "base64_upload"

            raise ValueError("缺少 file/image 字段")

        if content_type == "application/json":
            payload = await request.json()
            base64_str = str(payload.get("image_base64") or payload.get("base64") or "").strip()
            if not base64_str:
                raise ValueError("JSON 里缺少 image_base64/base64 字段")
            return self._decode_base64_payload(base64_str), str(payload.get("filename") or "base64_upload")

        raise ValueError("仅支持 multipart/form-data 或 application/json")

    def _decode_base64_payload(self, payload: str) -> bytes:
        if not payload:
            raise ValueError("base64 内容为空")

        body = payload.strip()
        match = re.match(r"^data:image/[^;]+;base64,(.*)$", body, flags=re.IGNORECASE | re.DOTALL)
        if match:
            body = match.group(1)

        body = "".join(body.split())
        try:
            return base64.b64decode(body, validate=True)
        except (binascii.Error, ValueError):
            raise ValueError("base64 格式无效")

    async def _save_image_bytes(self, image_bytes: bytes, source: str, original_name: str) -> dict[str, Any]:
        if not image_bytes:
            raise ValueError("图片内容为空")

        max_bytes = self._max_upload_bytes()
        if len(image_bytes) > max_bytes:
            raise ValueError(f"图片大小超出限制（最大 {max_bytes // (1024 * 1024)} MB）")

        record = await self.store.save_image(
            image_bytes=image_bytes,
            source=source,
            original_name=original_name,
            deduplicate=self._cfg_bool("enable_deduplicate", True),
        )

        return {
            "id": record["id"],
            "url": self._build_image_url(str(record["id"])),
            "mime": record["mime"],
            "size": record["size"],
            "created_at": record["created_at"],
            "deduplicated": bool(record.get("deduplicated", False)),
        }

    def _iter_message_components(self, event: AstrMessageEvent) -> list[Any]:
        chain = event.get_messages()
        if isinstance(chain, list):
            return chain
        if hasattr(chain, "chain") and isinstance(chain.chain, list):
            return chain.chain
        return []

    def _extract_first_url(self, text: str) -> str | None:
        if not text:
            return None
        match = re.search(r"https?://[^\s]+", text)
        if not match:
            return None
        return match.group(0).strip()

    async def _download_url_image(self, url: str) -> tuple[bytes, str]:
        image_path = await download_image_by_url(url)
        path = Path(image_path)
        if not path.exists():
            raise ValueError("下载图片失败")
        return path.read_bytes(), (path.name or os.path.basename(urlparse(url).path) or "remote_image")

    async def _extract_image_from_event(self, event: AstrMessageEvent) -> tuple[bytes, str] | None:
        for comp in self._iter_message_components(event):
            if isinstance(comp, Image):
                local_path = await comp.convert_to_file_path()
                path_obj = Path(local_path)
                if not path_obj.exists():
                    raise ValueError("消息中的图片文件不存在")
                return path_obj.read_bytes(), (path_obj.name or "message_image")
        return None

    @filter.command("图床上传")
    async def image_bed_upload(self, event: AstrMessageEvent):
        """上传消息中的图片（或命令中的 URL）并返回持久化链接"""

        try:
            result = await self._extract_image_from_event(event)
            if result is None:
                if not self._enable_url_upload():
                    yield event.plain_result(
                        "当前未启用 URL 上传（默认关闭）。\n"
                        "原因：开启后会让机器人主动下载外部链接，存在 SSRF/流量滥用风险。\n"
                        "如确有需要，请在插件配置中手动开启。"
                    )
                    await self._audit_command_event(
                        event,
                        command="图床上传",
                        result="rejected",
                        reason="url_upload_disabled",
                    )
                    return
                url = self._extract_first_url(event.message_str)
                if not url:
                    yield event.plain_result(
                        "请发送：`/图床上传` 并附带一张图片，或在命令里提供图片 URL。"
                    )
                    await self._audit_command_event(
                        event,
                        command="图床上传",
                        result="rejected",
                        reason="missing_image_or_url",
                    )
                    return
                result = await self._download_url_image(url)

            image_bytes, original_name = result
            saved = await self._save_image_bytes(
                image_bytes=image_bytes,
                source="command_upload",
                original_name=original_name,
            )

            dedupe_tag = "（命中去重）" if saved["deduplicated"] else ""
            yield event.plain_result(
                f"上传成功{dedupe_tag}\n"
                f"ID: {saved['id']}\n"
                f"URL: {saved['url']}"
            )
            await self._audit_command_event(
                event,
                command="图床上传",
                result="ok",
                image_id=str(saved["id"]),
            )
        except Exception as exc:
            logger.exception("[ImageBed] command upload failed: %s", exc)
            yield event.plain_result(f"上传失败：{exc}")
            await self._audit_command_event(
                event,
                command="图床上传",
                result="error",
                reason=str(exc),
            )

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("图床删除")
    async def image_bed_delete(self, event: AstrMessageEvent, image_id: str = ""):
        """管理员：按图片 ID 删除"""

        image_id = (image_id or "").strip()
        if not image_id:
            yield event.plain_result("用法：/图床删除 <image_id>")
            return

        try:
            result = await self.store.delete_image(image_id)
            if not result.get("deleted"):
                yield event.plain_result(f"未找到图片：{image_id}")
                await self._audit_command_event(
                    event,
                    command="图床删除",
                    result="not_found",
                    image_id=image_id,
                )
                return
            yield event.plain_result(
                f"删除成功：{image_id}\n"
                f"文件已删除：{'是' if result.get('file_removed') else '否（文件可能已不存在）'}"
            )
            await self._audit_command_event(
                event,
                command="图床删除",
                result="ok",
                image_id=image_id,
            )
        except Exception as exc:
            logger.exception("[ImageBed] admin delete failed: %s", exc)
            yield event.plain_result(f"删除失败：{exc}")
            await self._audit_command_event(
                event,
                command="图床删除",
                result="error",
                image_id=image_id,
                reason=str(exc),
            )

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("图床清理天数")
    async def image_bed_cleanup_days(
        self,
        event: AstrMessageEvent,
        days: str = "30",
        limit: str = "1000",
    ):
        """管理员：清理早于指定天数的图片"""

        try:
            days_int = int(days)
            limit_int = int(limit)
        except Exception:
            yield event.plain_result("参数错误。用法：/图床清理天数 <days> [limit]，例如 /图床清理天数 30 500")
            return

        if days_int < 1:
            yield event.plain_result("days 必须 >= 1。")
            return
        if limit_int < 1:
            yield event.plain_result("limit 必须 >= 1。")
            return
        if limit_int > 5000:
            limit_int = 5000

        try:
            result = await self.store.cleanup_older_than(days=days_int, limit=limit_int)
            yield event.plain_result(
                "清理完成：\n"
                f"阈值时间: {result['cutoff']}\n"
                f"匹配记录: {result['matched']}\n"
                f"删除记录: {result['deleted']}\n"
                f"删除文件: {result['files_removed']}"
            )
            await self._audit_command_event(
                event,
                command="图床清理天数",
                result="ok",
                days=days_int,
                limit=limit_int,
                deleted=int(result.get("deleted") or 0),
            )
        except Exception as exc:
            logger.exception("[ImageBed] admin cleanup failed: %s", exc)
            yield event.plain_result(f"清理失败：{exc}")
            await self._audit_command_event(
                event,
                command="图床清理天数",
                result="error",
                days=days_int,
                limit=limit_int,
                reason=str(exc),
            )

    @filter.command("图床状态")
    async def image_bed_status(self, event: AstrMessageEvent):
        """显示图床插件运行状态"""

        host = self._cfg_str("listen_host", "127.0.0.1")
        port = self._cfg_int("listen_port", 18345)
        token_on = "是" if self._upload_token() else "否"
        url_upload_on = "是" if self._enable_url_upload() else "否（默认关闭）"
        rate_limit_count, rate_limit_window = self._rate_limit_config()
        lines = [
            "图床插件状态：",
            f"监听地址: {host}:{port}",
            f"公开地址: {self._public_base_url()}",
            f"上传 token: {token_on}",
            "审计日志: 是（固定开启）",
            f"审计文件: {self._audit_log_path()}",
            f"URL 上传: {url_upload_on}",
            f"速率限制: {rate_limit_count} 次 / {rate_limit_window} 秒（任一项为 0 表示关闭）",
            f"最大上传: {self._max_upload_bytes() // (1024 * 1024)} MB",
            f"存储目录: {self.store.images_dir}",
            f"上传接口: {self._public_base_url()}/upload",
        ]
        yield event.plain_result("\n".join(lines))

    @filter.command("图床帮助")
    async def image_bed_help(self, event: AstrMessageEvent):
        """显示图床插件使用说明"""

        lines = [
            "图床插件指令：",
            "1) /图床上传 + 图片：上传消息中的第一张图片",
            "2) /图床上传 <URL>：下载 URL 图片后上传（需先开启 URL 上传）",
            "3) /图床状态：查看监听地址与配置",
            "4) /图床删除 <image_id>：管理员删除指定图片",
            "5) /图床清理天数 <days> [limit]：管理员批量清理旧图",
            "HTTP 上传：POST /upload",
            "- multipart 字段: file 或 image",
            "- 或 JSON 字段: image_base64/base64",
            "- 如配置 upload_token，请带请求头 X-ImageBed-Token",
            "- 不支持 query token（降低 token 泄露风险）",
            "- 默认关闭 URL 上传（因存在 SSRF/流量滥用风险）",
        ]
        yield event.plain_result("\n".join(lines))
