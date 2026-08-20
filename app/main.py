"""
نقطه ورود اپلیکیشن.

در فاز ۰ فقط قرارداد و mock را سرو می‌کند تا فرانت‌اند بتواند شروع کند.
منطق واقعی در فازهای بعد به /api/chat وصل می‌شود.
"""

from __future__ import annotations

import logging
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from app.contracts import ChatRequest, ErrorEvent, sse
from app.mock import mock_chat_stream
from app.settings import settings

logging.basicConfig(
    level=settings.log_level,
    format='{"level":"%(levelname)s","logger":"%(name)s","msg":"%(message)s"}',
)
log = logging.getLogger("app")

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"


@asynccontextmanager
async def lifespan(app: FastAPI):
    # فازهای بعد: اینجا vectors.npy و chunks.json و bm25.pkl لود می‌شوند
    log.info("startup mock=%s llm_configured=%s", settings.use_mock, settings.llm_configured)
    yield
    log.info("shutdown")


app = FastAPI(
    title="دستیار مستندات لیارا",
    version="0.1.0",
    lifespan=lifespan,
    docs_url="/api/docs",
    openapi_url="/api/openapi.json",
)


# ---------------------------------------------------------------- health

@app.get("/health")
async def health():
    """برای health check لیارا و مانیتورینگ."""
    return {
        "status": "ok",
        "version": app.version,
        "mock": settings.use_mock,
        "llm_configured": settings.llm_configured,
        "index_loaded": False,  # فاز ۲
    }


# ---------------------------------------------------------------- chat

@app.post("/api/chat")
async def chat(req: ChatRequest, request: Request):
    """
    استریم پاسخ به قالب SSE. شکل رویدادها در app/contracts.py قفل شده.
    """
    started = time.perf_counter()

    if len(req.message) > settings.max_message_chars:
        return JSONResponse(
            status_code=413,
            content={"message": "پیام بیش از حد طولانی است.", "code": "message_too_long"},
        )

    async def stream():
        try:
            # فاز ۲ و ۳: اگر mock نبود، حلقه‌ی ایجنت واقعی اینجا صدا زده می‌شود
            async for event in mock_chat_stream(req.message, req.session_id):
                if await request.is_disconnected():
                    log.info("client disconnected session=%s", req.session_id)
                    return
                yield event
        except Exception:
            log.exception("chat stream failed session=%s", req.session_id)
            yield sse("error", ErrorEvent(
                message="مشکلی در پردازش پیش آمد. لطفاً دوباره تلاش کنید.",
                code="internal_error",
            ))
        finally:
            elapsed = int((time.perf_counter() - started) * 1000)
            log.info("chat done session=%s ms=%d", req.session_id, elapsed)

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",  # جلوگیری از بافر شدن استریم توسط پروکسی
            "Connection": "keep-alive",
        },
    )


# ---------------------------------------------------------------- static
# آخرین mount باشد تا روت‌های /api را سایه نیندازد.

if STATIC_DIR.exists():
    app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
