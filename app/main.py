"""
نقطه ورود اپلیکیشن.

در فاز ۰ فقط قرارداد و mock را سرو می‌کند تا فرانت‌اند بتواند شروع کند.
منطق واقعی در فازهای بعد به /api/chat وصل می‌شود.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import secrets
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, PlainTextResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.api_project import router as api_project_router
from app.api_sessions import router as api_sessions_router
from app.api_v1 import router as api_v1_router
from app.contracts import ChatRequest, ErrorEvent, sse
from app.mock import mock_chat_stream
from app.observability import (
    configure_logging,
    metrics,
    request_id_var,
    route_label,
)
from app.security import RateLimiter, client_ip, llm_capacity
from app.settings import settings

configure_logging(
    settings.log_level,
    secrets=(settings.liara_ai_api_key_value, settings.metrics_token_value),
)
log = logging.getLogger("app")

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"


@asynccontextmanager
async def lifespan(app: FastAPI):
    # ایندکس همین‌جا لود می‌شود نه در اولین درخواست: اگر داده خراب باشد،
    # باید موقع بالا آمدن بفهمیم، نه وسط دموی جلوی داور.
    index_loaded = False
    if not settings.use_mock:
        from app.agent import index_ready
        index_loaded = index_ready()
        if not index_loaded:
            log.error("index failed to load — /api/chat will return errors")

    log.info(
        "startup mock=%s llm_configured=%s index=%s",
        settings.use_mock, settings.llm_configured, index_loaded,
    )
    yield
    log.info("shutdown")


app = FastAPI(
    title="دستیار مستندات لیارا",
    version="0.1.0",
    lifespan=lifespan,
    docs_url="/api/docs",
    openapi_url="/api/openapi.json",
)

app.include_router(api_v1_router)
app.include_router(api_project_router)
app.include_router(api_sessions_router)


# ---------------------------------------------------------------- عملیات مشترک

_expensive_limiter = RateLimiter(settings.rate_limit_per_minute)
_api_limiter = RateLimiter(settings.api_rate_limit_per_minute)
_expensive_paths = {"/api/chat", "/api/v1/ask", "/api/v1/project/plan"}
_safe_request_id = re.compile(r"^[A-Za-z0-9._-]{8,64}$")


class RequestTooLarge(Exception):
    pass


def _caused_by_too_large(exc: BaseException) -> bool:
    if isinstance(exc, RequestTooLarge):
        return True
    if isinstance(exc, BaseExceptionGroup):
        return any(_caused_by_too_large(item) for item in exc.exceptions)
    return False


def _error(message: str, code: str, status: int, request_id: str) -> JSONResponse:
    return JSONResponse(
        status_code=status,
        content={"message": message, "code": code, "request_id": request_id},
    )


@app.middleware("http")
async def operational_middleware(request: Request, call_next):
    """Request ID، rate limit، metric، security headers و آخرین error boundary."""
    started = time.perf_counter()
    incoming = request.headers.get("x-request-id", "")
    request_id = incoming if _safe_request_id.fullmatch(incoming) else uuid.uuid4().hex
    request.state.request_id = request_id
    context_token = request_id_var.set(request_id)
    route = route_label(request.url.path)
    status = 500
    metrics.start_request()
    rate_headers: dict[str, str] = {}

    # Content-Length ممکن است وجود نداشته یا جعل شود. شمارش واقعی chunkها
    # هنگام خواندن body، سقف را در خود برنامه نیز enforce می‌کند.
    original_receive = request._receive
    received_bytes = 0

    async def limited_receive():
        nonlocal received_bytes
        message = await original_receive()
        if message.get("type") == "http.request":
            received_bytes += len(message.get("body", b""))
            if received_bytes > settings.max_request_body_bytes:
                request.state.body_too_large = True
                raise RequestTooLarge
        return message

    request._receive = limited_receive

    try:
        content_length = request.headers.get("content-length", "")
        body_too_large = (
            content_length.isdigit()
            and int(content_length) > settings.max_request_body_bytes
        )
        if body_too_large:
            response = _error(
                "حجم درخواست بیش از حد مجاز است.", "request_too_large", 413,
                request_id,
            )
        elif request.url.path.startswith("/api/"):
            expensive = request.method == "POST" and request.url.path in _expensive_paths
            limiter = _expensive_limiter if expensive else _api_limiter
            limit = (
                settings.rate_limit_per_minute
                if expensive else settings.api_rate_limit_per_minute
            )
            ip = client_ip(request, trust_proxy=settings.trust_proxy_headers)
            allowed, retry_after, remaining = limiter.check(f"{ip}:{route}")
            rate_headers = {
                "X-RateLimit-Limit": str(limit),
                "X-RateLimit-Remaining": str(remaining),
            }
            if not allowed:
                limiter.prune()
                metrics.rate_limit("llm" if expensive else "api")
                log.warning("rate limited route=%s", route)
                response = _error(
                    f"تعداد درخواست بیش از حد است؛ {retry_after} ثانیه بعد تلاش کنید.",
                    "rate_limited", 429, request_id,
                )
                response.headers["Retry-After"] = str(retry_after)
            else:
                response = await call_next(request)
        else:
            response = await call_next(request)

        status = response.status_code
        response.headers["X-Request-ID"] = request_id
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
        response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
        response.headers.update(rate_headers)
        return response
    except Exception as exc:
        if _caused_by_too_large(exc):
            status = 413
            metrics.failure("request_too_large")
            response = _error(
                "حجم درخواست بیش از حد مجاز است.", "request_too_large", 413,
                request_id,
            )
        else:
            metrics.failure("unhandled_http")
            log.exception("unhandled request failure route=%s", route)
            response = _error(
                "خطای داخلی رخ داد. با شناسه درخواست پیگیری کنید.",
                "internal_error", 500, request_id,
            )
        response.headers["X-Request-ID"] = request_id
        return response
    finally:
        elapsed = time.perf_counter() - started
        metrics.finish_request(request.method, route, status, elapsed)
        if route != "/static":
            log.info(
                "http method=%s route=%s status=%d ms=%d",
                request.method, route, status, int(elapsed * 1000),
            )
        request_id_var.reset(context_token)


@app.exception_handler(RequestValidationError)
async def validation_error(request: Request, exc: RequestValidationError):
    # جزئیات Pydantic ممکن است بخشی از ورودی کاربر را در خود داشته باشد؛ log
    # یا پاسخ داده نمی‌شود. فقط شکل خطای امن و پایدار برمی‌گردد.
    return _error(
        "ورودی درخواست معتبر نیست.", "validation_error", 422,
        getattr(request.state, "request_id", "-"),
    )


@app.exception_handler(RequestTooLarge)
async def request_too_large(request: Request, exc: RequestTooLarge):
    metrics.failure("request_too_large")
    return _error(
        "حجم درخواست بیش از حد مجاز است.", "request_too_large", 413,
        getattr(request.state, "request_id", "-"),
    )


@app.exception_handler(StarletteHTTPException)
async def http_error(request: Request, exc: StarletteHTTPException):
    if getattr(request.state, "body_too_large", False):
        metrics.failure("request_too_large")
        return _error(
            "حجم درخواست بیش از حد مجاز است.", "request_too_large", 413,
            getattr(request.state, "request_id", "-"),
        )
    message = str(exc.detail) if exc.status_code < 500 else "خطای سرویس."
    return _error(
        message, "not_found" if exc.status_code == 404 else "http_error",
        exc.status_code, getattr(request.state, "request_id", "-"),
    )


# ---------------------------------------------------------------- health

@app.get("/health")
async def health():
    """Liveness: اگر process پاسخ می‌دهد، نباید توسط orchestrator کشته شود."""
    return {
        "status": "ok",
        "version": app.version,
    }


@app.get("/ready")
async def ready():
    """Readiness اجزای داخلی؛ بدون تماس پرهزینه با LLM بیرونی."""
    from app.session import sessions

    components = {
        "database": sessions.healthcheck(),
        "llm_configured": settings.use_mock or settings.llm_configured,
        "index": settings.use_mock or _index_loaded(),
    }
    is_ready = all(components.values())
    return JSONResponse(
        status_code=200 if is_ready else 503,
        content={
            "status": "ready" if is_ready else "not_ready",
            "version": app.version,
            "mock": settings.use_mock,
            "components": components,
        },
    )


@app.get("/metrics", include_in_schema=False)
async def prometheus_metrics(request: Request):
    token = settings.metrics_token_value
    if token:
        authorization = request.headers.get("authorization", "")
        supplied = authorization.removeprefix("Bearer ").strip()
        if not supplied or not secrets.compare_digest(supplied, token):
            response = _error(
                "دسترسی غیرمجاز.", "unauthorized", 401,
                getattr(request.state, "request_id", "-"),
            )
            response.headers["WWW-Authenticate"] = "Bearer"
            return response
    return PlainTextResponse(metrics.render(), media_type="text/plain; version=0.0.4")


def _index_loaded() -> bool:
    if settings.use_mock:
        return False
    from app.agent import index_ready
    return index_ready()


# ---------------------------------------------------------------- chat

@app.post("/api/chat")
async def chat(req: ChatRequest, request: Request):
    """
    استریم پاسخ به قالب SSE. شکل رویدادها در app/contracts.py قفل شده.
    """
    started = time.perf_counter()

    if len(req.message) > settings.max_message_chars:
        return _error(
            "پیام بیش از حد طولانی است.", "message_too_long", 413,
            request.state.request_id,
        )

    acquired = settings.use_mock or await llm_capacity.acquire()
    if not acquired:
        metrics.failure("llm_capacity")
        return _error(
            "سرویس موقتاً شلوغ است. کمی بعد دوباره تلاش کنید.",
            "service_busy", 503, request.state.request_id,
        )

    if settings.use_mock:
        source = mock_chat_stream(req.message, req.session_id, req.client_id)
    else:
        from app.agent import chat_stream
        source = chat_stream(req.message, req.session_id, req.client_id)

    async def stream():
        try:
            async with asyncio.timeout(settings.request_timeout_seconds):
                async for event in source:
                    if await request.is_disconnected():
                        log.info("client disconnected session=%s", req.session_id)
                        return
                    if event.startswith("event: done"):
                        try:
                            data_line = next(
                                line[6:].strip() for line in event.splitlines()
                                if line.startswith("data:")
                            )
                            used = int(json.loads(data_line).get("tokens_used", 0))
                            metrics.token_usage("chat", used)
                        except (StopIteration, ValueError, TypeError, json.JSONDecodeError):
                            log.warning("invalid done usage event session=%s", req.session_id)
                    yield event
        except TimeoutError:
            metrics.failure("chat_timeout")
            log.warning("chat timed out session=%s", req.session_id)
            yield sse("error", ErrorEvent(
                message="پاسخ‌گویی بیش از حد طول کشید. لطفاً دوباره تلاش کنید.",
                code="timeout",
            ))
        except Exception:
            metrics.failure("chat_stream")
            log.exception("chat stream failed session=%s", req.session_id)
            yield sse("error", ErrorEvent(
                message="مشکلی در پردازش پیش آمد. لطفاً دوباره تلاش کنید.",
                code="internal_error",
            ))
        finally:
            if not settings.use_mock:
                llm_capacity.release()
            elapsed_seconds = time.perf_counter() - started
            metrics.observe_operation("chat", elapsed_seconds)
            elapsed = int(elapsed_seconds * 1000)
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
