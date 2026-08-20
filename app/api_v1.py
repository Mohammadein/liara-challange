"""
API برنامه‌نویسی — برای مصرف توسط ایجنت‌های کدنویس، نه انسان.

سناریوی هدف: یک ایجنت (Cursor، Claude Code، Copilot…) موقع کار روی پروژه‌ای
که روی لیارا مستقر می‌شود به مشکل می‌خورد. به‌جای حدس زدن، همین‌جا می‌پرسد و
پاسخ مستند + متن خام مستندات + لینک دقیق می‌گیرد.

به همین دلیل پاسخ چند چیز را با هم برمی‌گرداند:
    answer      پاسخ آماده، اگر ایجنت بخواهد مستقیم به کاربر بدهد
    excerpts    متن خام مستندات، اگر بخواهد خودش استدلال کند
    sources     لینک دقیق با anchor، برای ارجاع
    confidence  اینکه چقدر به پاسخ اتکا کند
"""

from __future__ import annotations

import logging
import uuid

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from app.security import RateLimiter, client_ip
from app.settings import settings

log = logging.getLogger("app.api")

router = APIRouter(prefix="/api/v1", tags=["v1"])

# سقف نرخ مخصوص این API. هر تماس یک تماس LLM پشتش دارد.
limiter = RateLimiter(per_minute=settings.rate_limit_per_minute)


# ---------------------------------------------------------------- مدل‌ها

class AskRequest(BaseModel):
    question: str = Field(
        min_length=2, max_length=4000,
        description="سؤال، به فارسی یا انگلیسی.",
        examples=["چطور نسخه پایتون برنامه‌م رو تعیین کنم؟"],
    )
    platform: str | None = Field(
        default=None, max_length=40,
        description="راهنمای اختیاری پلتفرم: django | flask | nodejs | docker | …"
                    " مستندات لیارا برای هر پلتفرم نسخه‌ی جدا دارد؛ این کمک"
                    " می‌کند نسخه‌ی درست بالا بیاید.",
        examples=["django"],
    )
    session_id: str | None = Field(
        default=None, max_length=64,
        description="برای سؤال چندنوبتی. اگر خالی باشد، بدون حافظه اجرا می‌شود.",
    )
    include_excerpts: bool = Field(
        default=True,
        description="متن خام تکه‌های مستندات هم برگردد. برای ایجنت‌ها مفید است.",
    )
    max_sources: int = Field(default=5, ge=1, le=10)


class SourceOut(BaseModel):
    title: str
    section: str = ""
    url: str
    service: str = ""
    variant: str | None = None


class ExcerptOut(BaseModel):
    url: str
    page_title: str
    section_title: str = ""
    variant: str | None = None
    has_code: bool = False
    text: str


class Usage(BaseModel):
    tokens: int = 0
    latency_ms: int = 0


class AskResponse(BaseModel):
    request_id: str
    answer: str = Field(description="پاسخ به فارسی. اگر سؤال مبهم بوده، خالی است.")
    needs_clarification: bool = False
    clarification: str | None = Field(
        default=None,
        description="اگر سؤال برای جستجو خیلی مبهم بوده، سؤال تکمیلی اینجاست.",
    )
    sources: list[SourceOut] = []
    excerpts: list[ExcerptOut] = []
    query_used: str = Field(description="کوئری بازنویسی‌شده‌ای که واقعاً جستجو شد.")
    service: str | None = None
    confidence: str = Field(
        description="none | low | medium | high — اکتشافی بر پایه توافق نتایج "
                    "بازیابی. احتمال کالیبره‌شده نیست."
    )
    usage: Usage


# ---------------------------------------------------------------- روت

@router.post(
    "/ask",
    response_model=AskResponse,
    summary="پرسش از مستندات لیارا",
    description="سؤال را می‌گیرد، مستندات لیارا را جستجو می‌کند و پاسخ مستند "
                "به‌همراه لینک منابع و متن خام برمی‌گرداند.",
)
async def ask(req: AskRequest, request: Request):
    request_id = uuid.uuid4().hex[:12]

    allowed, retry_after = limiter.check(client_ip(request))
    if not allowed:
        limiter.prune()
        log.warning("rate limited ip=%s rid=%s", client_ip(request), request_id)
        return JSONResponse(
            status_code=429,
            content={"code": "rate_limited", "request_id": request_id,
                     "message": f"تعداد درخواست بیش از حد. {retry_after} ثانیه صبر کنید."},
            headers={"Retry-After": str(retry_after)},
        )

    if settings.use_mock:
        return JSONResponse(
            status_code=503,
            content={"code": "mock_mode", "request_id": request_id,
                     "message": "سرور در حالت mock است. USE_MOCK=false را تنظیم کنید."},
        )

    from app.agent import answer_once
    from app.llm import LLMUnavailable

    try:
        result = await answer_once(
            req.question,
            session_id=req.session_id,
            platform=req.platform,
            k=req.max_sources,
        )
    except LLMUnavailable as exc:
        log.error("llm unavailable rid=%s: %s", request_id, exc)
        return JSONResponse(
            status_code=503,
            content={"code": "llm_unavailable", "request_id": request_id,
                     "message": "سرویس هوش مصنوعی در دسترس نیست."},
        )
    except Exception:
        log.exception("ask failed rid=%s", request_id)
        return JSONResponse(
            status_code=500,
            content={"code": "internal_error", "request_id": request_id,
                     "message": "خطای داخلی."},
        )

    # یک منبع به ازای هر صفحه؛ چند تکه از یک صفحه یک لینک است.
    sources: list[SourceOut] = []
    seen: set[str] = set()
    for h in result.hits:
        if h.url in seen:
            continue
        seen.add(h.url)
        sources.append(SourceOut(
            title=h.page_title, section=h.section_title,
            url=h.url, service=h.service, variant=h.variant,
        ))

    log.info(
        "ask rid=%s conf=%s hits=%d tokens=%d ms=%d q=%r",
        request_id, result.confidence, len(result.hits),
        result.tokens, result.latency_ms, result.query_used,
    )

    return AskResponse(
        request_id=request_id,
        answer=result.answer,
        needs_clarification=bool(result.clarification),
        clarification=result.clarification,
        sources=sources,
        excerpts=[
            ExcerptOut(
                url=h.url, page_title=h.page_title, section_title=h.section_title,
                variant=h.variant, has_code=h.has_code, text=h.text,
            )
            for h in (result.hits if req.include_excerpts else [])
        ],
        query_used=result.query_used,
        service=result.service,
        confidence=result.confidence,
        usage=Usage(tokens=result.tokens, latency_ms=result.latency_ms),
    )
