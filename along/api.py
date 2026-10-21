import asyncio
import json
import io
import logging
import secrets
import sqlite3
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi import Path as FastAPIPath
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.exceptions import HTTPException as StarletteHTTPException

from along.config import Settings
from along.engine import Engine
from along.i18n import localize_error, use_language
from along.media import CapacityError, MediaStore
from along.memory import Memory
from along.providers.vlm import ProviderError, create_vlm_provider
from along.providers.web import DeepSeekWebProvider
from along.schemas import ChatInput, Mutation
from along.speech import Speech
from along.store import Conflict, Store

log = logging.getLogger(__name__)
STATIC = Path(__file__).parent / "static"


class Goal(BaseModel):
    goal: str = Field(min_length=2, max_length=2000)


class PrepareInput(BaseModel):
    frame_id: str | None = Field(default=None, max_length=100)
    url: str | None = Field(default=None, max_length=2000)
    reason: str = Field(default="", max_length=2000)


class TextSource(BaseModel):
    title: str = Field(min_length=1, max_length=300)
    content: str = Field(min_length=20, max_length=40000)


class BodyLimit:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        size = 0

        async def limited_receive():
            nonlocal size
            message = await receive()
            size += len(message.get("body", b""))
            if size > 40 * 1024 * 1024:
                raise HTTPException(413, "上传超过 40 MB")
            return message

        await self.app(scope, limited_receive, send)


def create_app(settings: Settings | None = None, vlm=None, web=None):
    settings = settings or Settings()
    provider = vlm or create_vlm_provider(settings)
    store = Store(settings.data_dir)
    media = MediaStore(store, settings)
    memory = Memory(store, media, provider, settings)
    web_provider = web or DeepSeekWebProvider(settings)
    engine = Engine(store, memory, provider, web_provider)
    speech = Speech(settings)
    token_path = settings.data_dir / "access-token"
    if not token_path.exists():
        token_path.write_text(secrets.token_urlsafe(32))
        token_path.chmod(0o600)
    token = token_path.read_text().strip()

    @asynccontextmanager
    async def lifespan(app):
        if settings.worker_enabled:
            memory.worker_task = asyncio.create_task(memory.worker())
        try:
            yield
        finally:
            await memory.close()
            await provider.close()
            close_web = getattr(web_provider, "close", None)
            if close_web:
                await close_web()
            store.close()

    app = FastAPI(title="ALONG", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.store, app.state.engine, app.state.memory = store, engine, memory
    app.state.token = token
    app.add_middleware(BodyLimit)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost", "[::1]", "testserver"])

    @app.middleware("http")
    async def guard(request: Request, call_next):
        with use_language(request.headers.get("x-along-language")):
            if request.url.path.startswith("/api/"):
                supplied = request.headers.get("x-along-token", "")
                if not secrets.compare_digest(supplied, token):
                    return JSONResponse(
                        {"detail": localize_error("缺少本地访问令牌，请从 ALONG 页面进入")}, status_code=403
                    )
            response = await call_next(request)
            response.headers["Cache-Control"] = "no-store"
            response.headers["X-Content-Type-Options"] = "nosniff"
            response.headers["Referrer-Policy"] = "no-referrer"
            response.headers["Content-Security-Policy"] = (
                "default-src 'self'; script-src 'self'; style-src 'self'; "
                "img-src 'self' blob: data:; media-src 'self' blob:; connect-src 'self'; frame-ancestors 'none'"
            )
            return response

    @app.exception_handler(StarletteHTTPException)
    async def http_error(request, exc):
        detail = localize_error(exc.detail) if isinstance(exc.detail, str) else exc.detail
        return JSONResponse({"detail": detail}, status_code=exc.status_code, headers=exc.headers)

    @app.exception_handler(Conflict)
    async def conflict(request, exc):
        return JSONResponse({"detail": localize_error(str(exc))}, status_code=409)

    @app.exception_handler(KeyError)
    async def not_found(request, exc):
        return JSONResponse({"detail": localize_error(str(exc).strip("'"))}, status_code=404)

    @app.exception_handler(CapacityError)
    async def capacity(request, exc):
        return JSONResponse({"detail": localize_error(str(exc))}, status_code=507)

    @app.exception_handler(ValueError)
    async def invalid(request, exc):
        return JSONResponse({"detail": localize_error(str(exc))}, status_code=422)

    @app.exception_handler(ProviderError)
    async def provider_error(request, exc):
        return JSONResponse({"detail": localize_error(str(exc))}, status_code=502)

    @app.exception_handler(sqlite3.IntegrityError)
    async def integrity(request, exc):
        return JSONResponse({"detail": localize_error("记录冲突，请刷新；原始记录未被覆盖")}, status_code=409)

    @app.exception_handler(OSError)
    async def io_error(request, exc):
        log.error("storage or device IO failed type=%s", type(exc).__name__)
        return JSONResponse(
            {"detail": localize_error("本地存储或设备读写失败，请检查磁盘和权限")}, status_code=503
        )

    @app.get("/")
    async def index():
        return FileResponse(STATIC / "index.html")

    @app.get("/bootstrap")
    async def bootstrap():
        return {
            "token": token,
            "frame_interval": settings.frame_interval,
            "retention_days": settings.retention_days,
            "max_storage_mb": settings.max_storage_mb,
        }

    @app.get("/api/state")
    async def state():
        stats = store.one(
            "SELECT COUNT(*) AS count,COALESCE(SUM(bytes),0) AS bytes FROM media WHERE expired=0"
        )
        jobs = store.rows("SELECT status,COUNT(*) AS count FROM jobs GROUP BY status")
        return {
            "task": store.current(),
            "tasks": store.rows("SELECT id,goal,status,version FROM tasks ORDER BY created DESC"),
            "jobs": jobs,
            "media": stats,
            "worker_error": memory.last_error,
            "model": settings.model,
            "semantic_enabled": settings.semantic_enabled,
            "busy": engine.interaction.locked(),
            "metrics": getattr(provider, "last_metrics", {}),
        }

    @app.post("/api/chat")
    async def chat(body: ChatInput):
        async with asyncio.timeout(300):
            return await engine.chat(body)

    @app.exception_handler(TimeoutError)
    async def timeout(request, exc):
        return JSONResponse(
            {"detail": localize_error("操作超过时限，未完成的结果没有被当作成功；请查看任务状态后重试")},
            status_code=504,
        )

    @app.post("/api/tasks")
    async def create_task(body: Goal):
        return store.create_task(body.goal.strip())

    @app.get("/api/tasks/{task_id}")
    async def get_task(task_id: str):
        return store.task(task_id)

    @app.post("/api/tasks/{task_id}/prepare")
    async def prepare(task_id: str, body: PrepareInput):
        if engine.interaction.locked():
            raise Conflict("已有交互正在处理")
        async with engine.interaction:
            memory.user_busy = True
            try:
                async with asyncio.timeout(240):
                    return await engine.prepare(task_id, body.url, body.reason, body.frame_id)
            finally:
                memory.user_busy = False

    @app.post("/api/tasks/{task_id}/change")
    async def change(task_id: str, body: Mutation):
        return engine.mutate(task_id, body)

    @app.post("/api/tasks/{task_id}/sources")
    async def add_source(task_id: str, body: TextSource):
        task = store.task(task_id)
        if task["status"] not in ("draft", "paused") or engine.interaction.locked():
            raise Conflict("请在任务草稿或暂停时补充资料")
        return engine.save_source(task_id, body.title, "user-provided", body.content)

    @app.post("/api/tasks/{task_id}/documents")
    async def upload_document(task_id: str, file: Annotated[UploadFile, File()]):
        task = store.task(task_id)
        if task["status"] not in ("draft", "paused") or engine.interaction.locked():
            raise Conflict("请在任务草稿或暂停时导入资料")
        raw = await file.read(10 * 1024 * 1024 + 1)
        if len(raw) > 10 * 1024 * 1024:
            raise ValueError("说明书不得超过 10 MB")
        title = Path(file.filename or "用户说明书").name[:300]
        if raw.startswith(b"%PDF"):

            def extract():
                from pypdf import PdfReader

                try:
                    reader = PdfReader(io.BytesIO(raw))
                    if reader.is_encrypted:
                        raise ValueError("暂不支持加密 PDF，请提供未加密文字资料")
                    if len(reader.pages) > 80:
                        raise ValueError("说明书超过 80 页，请提取与当前任务有关的部分")
                    parts = []
                    for index, page in enumerate(reader.pages):
                        text = page.extract_text() or ""
                        if text.strip():
                            parts.append(f"第 {index + 1} 页\n{text}")
                        if sum(len(part) for part in parts) > 40000:
                            raise ValueError("说明书文字过多，请提取当前任务相关部分")
                    return "\n\n".join(parts)
                except ValueError:
                    raise
                except Exception as exc:
                    raise ValueError("PDF 无法解析，请改用文字说明书") from exc

            text = await asyncio.to_thread(extract)
            if len(text.strip()) < 20:
                raise ValueError("PDF 未提取到足够文字；扫描型 PDF 暂不支持 OCR，请提供文字或网页")
        else:
            try:
                text = raw.decode("utf-8-sig")
            except UnicodeDecodeError as exc:
                raise ValueError("请提供 UTF-8 文字或可提取文字的 PDF") from exc
        if not 20 <= len(text) <= 40000:
            raise ValueError("说明书文字应在 20 到 40000 字符之间")
        return engine.save_source(task_id, title, "user-provided", text)

    @app.delete("/api/tasks/{task_id}")
    async def delete_task(task_id: str):
        if engine.interaction.locked():
            raise Conflict("正在处理交互，请完成后再删除")
        store.delete_task(task_id)
        return {"deleted": True}

    @app.post("/api/memory/retry")
    async def retry():
        with store.tx() as db:
            db.execute(
                "UPDATE jobs SET status='pending',attempts=0,available=? WHERE status='failed'",
                (time.time(),),
            )
        return {"queued": True}

    @app.get("/api/tasks/{task_id}/timeline")
    async def timeline(task_id: str):
        store.task(task_id)
        events = store.rows("SELECT * FROM events WHERE task_id=? ORDER BY start DESC LIMIT 80", (task_id,))
        for event in events:
            event["data"] = json.loads(event["data"])
            event["frame_ids"] = json.loads(event["frame_ids"])
            event.pop("embedding", None)
        return {
            "events": events,
            "ledger": store.rows(
                "SELECT * FROM ledger WHERE task_id=? ORDER BY created DESC LIMIT 100", (task_id,)
            ),
            "media": store.rows(
                "SELECT id,kind,captured,seq,expired FROM media WHERE task_id=? "
                "ORDER BY captured DESC LIMIT 100",
                (task_id,),
            ),
        }

    @app.get("/api/sources/{source_id}")
    async def source(source_id: str):
        row = store.one("SELECT * FROM sources WHERE id=?", (source_id,))
        if not row:
            raise KeyError("资料不存在")
        return row

    @app.get("/api/messages/{conversation_id}")
    async def messages(conversation_id: str, task_id: str | None = None):
        if task_id:
            rows = store.rows(
                "SELECT * FROM messages WHERE task_id=? ORDER BY created DESC LIMIT 50", (task_id,)
            )
        else:
            rows = store.rows(
                "SELECT * FROM messages WHERE conversation_id=? AND task_id IS NULL "
                "ORDER BY created DESC LIMIT 50",
                (conversation_id,),
            )
        return rows[::-1]

    @app.post("/api/media")
    async def upload(
        file: Annotated[UploadFile, File()],
        media_id: Annotated[str, Form(pattern=r"^[a-zA-Z0-9_-]{8,100}$")],
        conversation_id: Annotated[str, Form(pattern=r"^[a-zA-Z0-9_-]{8,100}$")],
        captured: Annotated[float, Form()],
        seq: Annotated[int, Form(ge=0)],
        task_id: Annotated[str | None, Form()] = None,
        kind: Annotated[str, Form(pattern="^(frame|clip)$")] = "frame",
        origin: Annotated[str, Form(pattern="^(camera|upload|replay|reference)$")] = "camera",
    ):
        raw = await file.read(33 * 1024 * 1024)
        return media.save(media_id, raw, task_id or None, conversation_id, captured, seq, kind, origin)

    @app.get("/api/media/{media_id}")
    async def get_media(media_id: str):
        row, path = media.path(media_id)
        return FileResponse(
            path,
            media_type="image/jpeg"
            if row["kind"] == "frame"
            else ("video/mp4" if path.suffix == ".mp4" else "video/webm"),
        )

    @app.get("/api/sheets/{sheet_id}")
    async def get_sheet(sheet_id: Annotated[str, FastAPIPath(pattern="^sheet_[0-9a-f]{20}$")]):
        path = store.directory / "media" / "sheets" / f"{sheet_id}.jpg"
        if not path.is_file():
            raise KeyError("拼图缓存已清理，可查看对应原始帧")
        return FileResponse(path, media_type="image/jpeg")

    @app.post("/api/speech")
    async def transcribe(file: Annotated[UploadFile, File()]):
        raw = await file.read(12 * 1024 * 1024 + 1)
        try:
            return {"text": await speech.transcribe(
                raw, file.filename or "voice.webm", file.content_type or "application/octet-stream"
            )}
        finally:
            await file.close()

    app.mount("/static", StaticFiles(directory=STATIC), name="static")
    return app
