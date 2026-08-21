"""
پاسخ‌های ساختگی که دقیقاً قرارداد app/contracts.py را رعایت می‌کنند.

هدف: فرانت‌اند از همین حالا UI واقعی بسازد — با استریم واقعی، وضعیت ابزار
واقعی و کارت منابع واقعی — بدون اینکه منتظر آماده شدن بک‌اند بماند.
وقتی بک‌اند آماده شد فقط USE_MOCK=false می‌شود.

سه سناریو دارد تا همه حالت‌های UI قابل ساخت باشند:
  ۱. پاسخ عادی با بلوک کد و منابع
  ۲. سؤال تکمیلی (بدون منبع)  ← بنویسید: «جنگو دیپلوی کنم»
  ۳. تشخیص خطا با ابزار        ← بنویسید: «خطا ...»
"""

from __future__ import annotations

import asyncio
import random
from collections.abc import AsyncIterator

from app.contracts import (
    DoneEvent,
    Source,
    SourcesEvent,
    TokenEvent,
    ToolEvent,
    sse,
)
from app.session import sessions

TOKEN_DELAY = (0.012, 0.035)  # تأخیر مصنوعی تا حس واقعی استریم بدهد


_ANSWER_NORMAL = """برای تعیین نسخه پایتون در لیارا، فیلد `pythonVersion` را در فایل `liara.json` قرار بدهید:

```json
{
  "app": "my-web-app",
  "platform": "django",
  "django": {
    "pythonVersion": "3.12"
  }
}
```

اگر این فیلد را نگذارید، لیارا نسخه پیش‌فرض را انتخاب می‌کند.

**قدم بعدی:** می‌خواهید منطقه زمانی برنامه را هم تنظیم کنیم؟"""

_ANSWER_CLARIFY = """قبل از اینکه راهنمایی دقیق بدهم، یک نکته را روشن کنیم.

استقرار برنامه Django در لیارا سه روش دارد و مراحل هرکدام متفاوت است:

- **کنسول لیارا** — آپلود فایل zip از طریق مرورگر
- **Liara CLI** — استقرار با دستور `liara deploy` از ترمینال
- **GitHub** — اتصال ریپازیتوری و استقرار خودکار در هر push

با کدام روش می‌خواهید پیش بروید؟"""

_ANSWER_DIAGNOSE = """این خطا یعنی لیارا نتوانسته پکیج‌های `requirements.txt` را نصب کند.

دلیل رایجش این است که لیارا برای نصب پکیج‌ها از **mirror اختصاصی** خودش استفاده می‌کند و بعضی پکیج‌های جدید هنوز روی آن نیستند.

راه حل — mirror را در `liara.json` غیرفعال کنید:

```json
{
  "django": {
    "mirror": false
  }
}
```

بعد دوباره `liara deploy` بزنید. استقرار کمی کندتر می‌شود ولی پکیج‌ها مستقیم از PyPI نصب می‌شوند."""


_SOURCES_NORMAL = [
    Source(
        title="استقرار برنامه Django در لیارا",
        section="تعیین نسخه",
        url="https://docs.liara.ir/paas/django/how-tos/deploy-app/#liara-json-version",
        service="paas",
    ),
    Source(
        title="آشنایی با فایل liara.json",
        section="",
        url="https://docs.liara.ir/paas/liarajson/",
        service="paas",
    ),
    Source(
        title="نسخه‌های قابل ارائه Python در لیارا",
        section="",
        url="https://docs.liara.ir/paas/django/how-tos/choose-version/",
        service="paas",
    ),
]

_SOURCES_DIAGNOSE = [
    Source(
        title="استقرار برنامه Django در لیارا",
        section="mirror لیارا",
        url="https://docs.liara.ir/paas/django/how-tos/deploy-app/#liara-json-mirror",
        service="paas",
    ),
]


def _pick_scenario(message: str) -> str:
    m = message.lower()
    if any(w in m for w in ("خطا", "ارور", "error", "failed", "traceback")):
        return "diagnose"
    if any(w in m for w in ("دیپلوی", "استقرار", "deploy", "مستقر")):
        return "clarify"
    return "normal"


async def _stream_text(text: str) -> AsyncIterator[str]:
    """کلمه‌به‌کلمه استریم می‌کند و فاصله‌ها را حفظ می‌کند."""
    for word in text.split(" "):
        yield sse("token", TokenEvent(t=word + " "))
        await asyncio.sleep(random.uniform(*TOKEN_DELAY))


async def mock_chat_stream(
    message: str,
    session_id: str,
    client_id: str | None = None,
) -> AsyncIterator[str]:
    session = sessions.get(session_id, client_id)
    scenario = _pick_scenario(message)

    # --- سناریوی سؤال تکمیلی: هیچ ابزاری صدا زده نمی‌شود ---
    if scenario == "clarify":
        await asyncio.sleep(0.3)
        async for ev in _stream_text(_ANSWER_CLARIFY):
            yield ev
        session.add("user", message)
        session.add("assistant", _ANSWER_CLARIFY)
        yield sse("done", DoneEvent(tokens_used=180, cached=False, latency_ms=1400))
        return

    # --- سناریوی تشخیص خطا: دو ابزار پشت سر هم ---
    if scenario == "diagnose":
        yield sse("tool", ToolEvent(name="diagnose_error", status="running",
                                    detail="در حال تحلیل لاگ خطا"))
        await asyncio.sleep(0.8)
        yield sse("tool", ToolEvent(name="diagnose_error", status="done"))

        yield sse("tool", ToolEvent(name="search_docs", status="running",
                                    detail="جستجوی «mirror پکیج»"))
        await asyncio.sleep(0.7)
        yield sse("tool", ToolEvent(name="search_docs", status="done",
                                    detail="۱ نتیجه"))

        async for ev in _stream_text(_ANSWER_DIAGNOSE):
            yield ev
        yield sse("sources", SourcesEvent(items=_SOURCES_DIAGNOSE))
        session.add("user", message)
        session.add(
            "assistant", _ANSWER_DIAGNOSE,
            sources=[source.model_dump() for source in _SOURCES_DIAGNOSE],
        )
        yield sse("done", DoneEvent(tokens_used=760, cached=False, latency_ms=3100))
        return

    # --- سناریوی عادی ---
    yield sse("tool", ToolEvent(name="search_docs", status="running",
                                detail="در حال جستجوی مستندات"))
    await asyncio.sleep(0.9)
    yield sse("tool", ToolEvent(name="search_docs", status="done",
                                detail="۳ نتیجه مرتبط"))

    async for ev in _stream_text(_ANSWER_NORMAL):
        yield ev

    yield sse("sources", SourcesEvent(items=_SOURCES_NORMAL))
    session.add("user", message)
    session.add(
        "assistant", _ANSWER_NORMAL,
        sources=[source.model_dump() for source in _SOURCES_NORMAL],
    )
    yield sse("done", DoneEvent(tokens_used=620, cached=False, latency_ms=2450))
