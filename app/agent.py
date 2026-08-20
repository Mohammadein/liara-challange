"""
حلقه‌ی پاسخ‌دهی.

    سؤال کاربر
        ↓  بازنویسی با مدل کوچک  (رفع ابهام + واژگان مستندات + تشخیص سرویس)
        ├─ اگر مبهم بود → سؤال تکمیلی، بدون هیچ بازیابی و بدون مدل بزرگ
        ↓
    بازیابی هیبریدی
        ↓
    پاسخ استریمی با مدل بزرگ
        ↓
    کارت منابع

چرا بازنویسی جدا و با مدل کوچک: کاربر علائم را با زبان روزمره توصیف می‌کند
ولی مستندات اصطلاح فنی دارد. «چند تا ورکر بذارم؟» هیچ کلمه‌ی مشترکی با
«gunicorn workers» ندارد و هیچ روش بازیابی‌ای این شکاف را پر نمی‌کند.
یک تماس ارزان قبل از جستجو، این را حل می‌کند.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass

from app.contracts import (
    DoneEvent,
    ErrorEvent,
    Source,
    SourcesEvent,
    TokenEvent,
    ToolEvent,
    sse,
)
from app.llm import LLMUnavailable, aclient
from app.prompts import ANSWER_SYSTEM, REWRITE_SYSTEM, build_context
from app.retrieval import Hit, Retriever
from app.session import Session, sessions
from app.settings import settings

log = logging.getLogger("app.agent")

_retriever: Retriever | None = None


def get_retriever() -> Retriever:
    global _retriever
    if _retriever is None:
        _retriever = Retriever.load()
    return _retriever


def index_ready() -> bool:
    try:
        get_retriever()
        return True
    except Exception:
        return False


# ------------------------------------------------------------ بازنویسی

async def rewrite(question: str, session: Session) -> dict:
    """
    سؤال → {query, service, clarify}

    اگر مدل کوچک خطا داد یا JSON بی‌ربط برگرداند، به خود سؤال برمی‌گردیم.
    یک بازنویسی ناموفق نباید کل پاسخ را از بین ببرد.
    """
    history = session.transcript()
    user = f"مکالمه تا اینجا:\n{history}\n\nسؤال جدید: {question}" if history else question

    try:
        resp = await aclient().chat.completions.create(
            model=settings.model_fast,
            messages=[
                {"role": "system", "content": REWRITE_SYSTEM},
                {"role": "user", "content": user},
            ],
            response_format={"type": "json_object"},
            temperature=0,
            max_tokens=200,
        )
        data = json.loads(resp.choices[0].message.content or "{}")
        usage = resp.usage.total_tokens if resp.usage else 0
    except Exception as exc:
        log.warning("rewrite failed (%s), falling back to raw question", type(exc).__name__)
        return {"query": question, "service": None, "clarify": None, "tokens": 0}

    return {
        "query": (data.get("query") or question).strip(),
        "service": data.get("service") or None,
        "clarify": (data.get("clarify") or None),
        "tokens": usage,
    }


# ------------------------------------------------------------ منابع

def _sources(hits: list[Hit]) -> list[Source]:
    """
    یک منبع به ازای هر صفحه — نه هر تکه.

    چند تکه از یک صفحه، یک لینک است. نمایش پنج کارت که همه به یک صفحه
    اشاره می‌کنند، به کاربر حس پوشش کاذب می‌دهد.
    """
    out: list[Source] = []
    seen: set[str] = set()
    for h in hits:
        if h.url in seen:
            continue
        seen.add(h.url)
        out.append(Source(
            title=h.page_title,
            section=h.section_title,
            url=h.url,
            service=h.service,
        ))
    return out


def confidence_of(hits: list[Hit]) -> str:
    """
    سیگنال اطمینان — اکتشافی و عمداً ساده، تا قابل توضیح باشد.

    مبنا: توافق بین تکه‌های بازیابی‌شده. اگر چند تکه از یک صفحه بالا آمده
    باشند، یعنی مستندات صفحه‌ی مشخصی برای این موضوع دارد. اگر نتایج
    پراکنده باشند، احتمالاً بازیابی مطمئن نبوده.

    این عدد کالیبره‌شده نیست و نباید به‌عنوان احتمال درستی خوانده شود؛
    برای ایجنت مصرف‌کننده یک راهنماست که کی به پاسخ اتکا کند و کی خودش
    excerpts را بخواند.
    """
    if not hits:
        return "none"
    top_url = hits[0].url.split("#")[0]
    same_page = sum(1 for h in hits if h.url.split("#")[0] == top_url)
    if same_page >= 2:
        return "high"
    if len(hits) >= 3:
        return "medium"
    return "low"


@dataclass
class AnswerResult:
    answer: str
    hits: list[Hit]
    query_used: str
    service: str | None
    clarification: str | None
    confidence: str
    tokens: int
    latency_ms: int


async def answer_once(
    question: str,
    *,
    session_id: str | None = None,
    platform: str | None = None,
    k: int | None = None,
) -> AnswerResult:
    """
    همان مسیر chat_stream ولی یکجا — برای API برنامه‌نویسی.

    نکته‌ی طراحی: پاسخِ ایجنت‌ها با پاسخِ انسان فرق دارد. یک ایجنت کدنویس
    به متن خام مستندات و سیگنال اطمینان نیاز دارد تا خودش قضاوت کند، نه
    فقط یک پاراگراف روان. هر دو برگردانده می‌شوند.
    """
    started = time.perf_counter()
    session = sessions.get(session_id) if session_id else Session(id="_stateless")
    tokens = 0

    plan = await rewrite(question, session)
    tokens += plan["tokens"]

    if plan["clarify"]:
        if session_id:
            session.add("user", question)
            session.add("assistant", plan["clarify"])
        return AnswerResult(
            answer="", hits=[], query_used=plan["query"],
            service=plan["service"], clarification=plan["clarify"],
            confidence="none", tokens=tokens,
            latency_ms=int((time.perf_counter() - started) * 1000),
        )

    # راهنمای پلتفرم از سمت فراخواننده به کوئری اضافه می‌شود تا نسخه‌ی
    # درست صفحه (django/flask/nodejs) بالا بیاید.
    query = f"{plan['query']} {platform}" if platform else plan["query"]
    service = plan["service"] or session.service

    retriever = get_retriever()
    hits = retriever.search(query, k=k or settings.top_k, service=service)
    if not hits and service:
        hits = retriever.search(query, k=k or settings.top_k)

    resp = await aclient().chat.completions.create(
        model=settings.model_answer,
        messages=[
            {"role": "system", "content": ANSWER_SYSTEM},
            *session.history(),
            {"role": "user", "content":
                f"# متن مستندات\n\n{build_context(hits)}\n\n# سؤال کاربر\n\n{question}"},
        ],
        temperature=0.2,
        max_tokens=900,
    )
    answer = resp.choices[0].message.content or ""
    tokens += resp.usage.total_tokens if resp.usage else 0

    if session_id:
        session.add("user", question)
        session.add("assistant", answer)
        if service:
            session.service = service

    return AnswerResult(
        answer=answer, hits=hits, query_used=query, service=service,
        clarification=None, confidence=confidence_of(hits), tokens=tokens,
        latency_ms=int((time.perf_counter() - started) * 1000),
    )


# ------------------------------------------------------------ حلقه اصلی

async def chat_stream(question: str, session_id: str) -> AsyncIterator[str]:
    started = time.perf_counter()
    session = sessions.get(session_id)
    tokens_used = 0

    try:
        # --- ۱. بازنویسی ---
        yield sse("tool", ToolEvent(
            name="understand", status="running", detail="در حال درک سؤال"
        ))
        plan = await rewrite(question, session)
        tokens_used += plan["tokens"]

        # --- ۲. سؤال تکمیلی: نه بازیابی، نه مدل بزرگ ---
        if plan["clarify"]:
            yield sse("tool", ToolEvent(name="understand", status="done",
                                        detail="نیاز به توضیح بیشتر"))
            for word in plan["clarify"].split(" "):
                yield sse("token", TokenEvent(t=word + " "))
            session.add("user", question)
            session.add("assistant", plan["clarify"])
            yield sse("done", DoneEvent(
                tokens_used=tokens_used, cached=False,
                latency_ms=int((time.perf_counter() - started) * 1000),
            ))
            return

        yield sse("tool", ToolEvent(name="understand", status="done",
                                    detail=plan["query"]))

        # --- ۳. بازیابی ---
        yield sse("tool", ToolEvent(name="search_docs", status="running",
                                    detail="جستجوی مستندات"))
        service = plan["service"] or session.service
        hits = get_retriever().search(plan["query"], k=settings.top_k, service=service)
        if not hits and service:
            hits = get_retriever().search(plan["query"], k=settings.top_k)

        if service:
            session.service = service
        yield sse("tool", ToolEvent(name="search_docs", status="done",
                                    detail=f"{len(hits)} نتیجه"))

        # --- ۴. پاسخ ---
        context = build_context(hits)
        messages = [
            {"role": "system", "content": ANSWER_SYSTEM},
            *session.history(),
            {"role": "user", "content":
                f"# متن مستندات\n\n{context}\n\n# سؤال کاربر\n\n{question}"},
        ]

        answer = ""
        stream = await aclient().chat.completions.create(
            model=settings.model_answer,
            messages=messages,
            temperature=0.2,
            max_tokens=900,
            stream=True,
            stream_options={"include_usage": True},
        )
        async for part in stream:
            if part.usage:
                tokens_used += part.usage.total_tokens
            if not part.choices:
                continue
            delta = part.choices[0].delta.content
            if delta:
                answer += delta
                yield sse("token", TokenEvent(t=delta))

        # --- ۵. منابع ---
        if hits:
            yield sse("sources", SourcesEvent(items=_sources(hits)))

        session.add("user", question)
        session.add("assistant", answer)

        yield sse("done", DoneEvent(
            tokens_used=tokens_used, cached=False,
            latency_ms=int((time.perf_counter() - started) * 1000),
        ))

    except LLMUnavailable as exc:
        log.error("llm unavailable: %s", exc)
        yield sse("error", ErrorEvent(
            message="سرویس هوش مصنوعی در دسترس نیست. لطفاً کمی بعد دوباره تلاش کنید.",
            code="llm_unavailable",
        ))
    except Exception:
        log.exception("chat failed session=%s", session_id)
        yield sse("error", ErrorEvent(
            message="مشکلی در پردازش پیش آمد. لطفاً دوباره تلاش کنید.",
            code="internal_error",
        ))
