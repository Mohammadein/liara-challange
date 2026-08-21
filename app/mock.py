"""
پاسخ‌های ساختگی که دقیقاً قرارداد app/contracts.py را رعایت می‌کنند.

هدف: فرانت‌اند از همین حالا UI واقعی بسازد — با استریم واقعی، وضعیت ابزار
واقعی و کارت منابع واقعی — بدون اینکه منتظر آماده شدن بک‌اند بماند.
وقتی بک‌اند آماده شد فقط USE_MOCK=false می‌شود.

چهار سناریو دارد تا همه حالت‌های UI قابل ساخت باشند:
  ۱. پاسخ عادی با بلوک کد، منابع و چیپ‌های قدم بعدی
  ۲. سؤال تکمیلی (بدون منبع)  ← بنویسید: «جنگو دیپلوی کنم»
  ۳. تشخیص خطا با ابزار        ← بنویسید: «خطا ...»
  ۴. فرآیند چندمرحله‌ای         ← بنویسید: «قدم به قدم دیپلوی» و بعد «قدم بعد»
"""

from __future__ import annotations

import asyncio
import random
from collections.abc import AsyncIterator

from app import flows
from app.contracts import (
    DoneEvent,
    FlowEvent,
    Source,
    SourcesEvent,
    Suggestion,
    SuggestionsEvent,
    TokenEvent,
    ToolEvent,
    sse,
)
from app.flows import FlowState
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


_SUGGESTIONS_NORMAL = [
    Suggestion(
        label="تنظیم منطقه زمانی برنامه",
        prompt="منطقه زمانی برنامه را چطور روی Asia/Tehran تنظیم کنم؟",
    ),
    Suggestion(
        label="تغییر نسخه بعد از استقرار",
        prompt="اگر بعد از استقرار بخواهم نسخه پایتون را عوض کنم چه اتفاقی می‌افتد؟",
    ),
    Suggestion(
        label="راهنمای قدم‌به‌قدم: استقرار برنامه از صفر تا اجرا",
        prompt="قدم به قدم راهنمایی کن: استقرار برنامه از صفر تا اجرا",
        kind="flow",
    ),
]

# سؤال تکمیلی بدون گزینه‌ی قابل کلیک، بن‌بست است: کاربر باید همان چیزی را
# دوباره تایپ کند که ربات همین الان جلویش گذاشت.
_SUGGESTIONS_CLARIFY = [
    Suggestion(label="کنسول لیارا", prompt="با کنسول لیارا"),
    Suggestion(label="Liara CLI", prompt="با Liara CLI"),
    Suggestion(label="GitHub", prompt="با GitHub"),
]

_SUGGESTIONS_DIAGNOSE = [
    Suggestion(
        label="نصب پکیج بدون mirror",
        prompt="غیرفعال کردن mirror چه تأثیری روی زمان استقرار دارد؟",
    ),
    Suggestion(
        label="راهنمای قدم‌به‌قدم: عیب‌یابی استقرار ناموفق",
        prompt="قدم به قدم راهنمایی کن: عیب‌یابی استقرار ناموفق",
        kind="flow",
    ),
]

_FLOW_STEP_TEXT = """در این قدم برنامه‌ای می‌سازیم که کد قرار است داخلش اجرا شود.

۱. وارد کنسول لیارا شوید و از بخش **برنامه‌ها** گزینه ساخت برنامه جدید را بزنید.
۲. یک **شناسه** یکتا انتخاب کنید؛ همین شناسه در `liara.json` هم استفاده می‌شود.
۳. پلتفرم و پلن را انتخاب کنید.

اگر برنامه در فهرست با وضعیت «در انتظار استقرار» دیده شد، این قدم تمام است."""


def _pick_scenario(message: str) -> str:
    m = message.lower()
    if flows.match_flow(message) or flows.next_intent(message) \
            or flows.exit_intent(message):
        return "flow"
    if any(w in m for w in ("خطا", "ارور", "error", "failed", "traceback")):
        return "diagnose"
    if any(w in m for w in ("دیپلوی", "استقرار", "deploy", "مستقر")):
        return "clarify"
    return "normal"


def _mock_ctx(session, message: str, state) -> dict:
    """همان قاعده‌ی حافظه‌ی بک‌اند واقعی، تا mock هم درست به نظر برسد."""
    evidence = " ".join(
        turn["content"] for turn in session.turns[-8:] if turn["role"] == "user"
    )
    hints = dict(state.hints) if state else {}
    platform = flows.detect_platform(f"{evidence} {message}")
    if platform:
        hints.setdefault("platform", platform)
    return flows.build_context(None, message, hints=hints)


async def _mock_flow(message: str, session) -> AsyncIterator[str]:
    """
    یک فرآیند نمونه با وضعیت واقعی، تا stepper بدون LLM قابل ساخت باشد.

    وضعیت در همان session ذخیره می‌شود، پس «قدم بعد» واقعاً جلو می‌رود.
    """
    state = session.flow
    flow = flows.flow_by_id(state.id) if state else None

    if flow and flows.exit_intent(message):
        steps = flows.resolve(flow, _mock_ctx(session, message, state))
        session.flow = None
        session.save()
        yield sse("flow", FlowEvent(
            **flows.progress_payload(flow, steps, state, "exited")))
        text = f"از فرآیند «{flow.title}» خارج شدیم."
        async for ev in _stream_text(text):
            yield ev
        session.add("user", message)
        session.add("assistant", text)
        yield sse("done", DoneEvent(tokens_used=0, cached=False, latency_ms=350))
        return

    if flow and flows.next_intent(message):
        state.done.append(flow.steps[state.step].key)
        state.step += 1
        status = "advanced"
    else:
        flow = flows.match_flow(message) or flows.FLOWS["deploy_app"]
        state = FlowState(id=flow.id)
        status = "started"

    ctx = _mock_ctx(session, message, state)
    state.hints = {**state.hints, **flows.hints_from(ctx)}
    steps = flows.resolve(flow, ctx)

    if state.step >= len(steps):
        session.flow = None
        session.save()
        yield sse("flow", FlowEvent(
            **flows.progress_payload(flow, steps, state, "completed")))
        text = f"فرآیند «{flow.title}» تمام شد."
        async for ev in _stream_text(text):
            yield ev
        session.add("user", message)
        session.add("assistant", text)
        yield sse("done", DoneEvent(tokens_used=0, cached=False, latency_ms=400))
        return

    session.flow = state
    session.save()
    step = steps[state.step]

    yield sse("flow", FlowEvent(
        **flows.progress_payload(flow, steps, state, status)))
    yield sse("tool", ToolEvent(
        name="flow_step", status="done",
        detail=f"قدم {flows.fa(step.index)} از {flows.fa(len(steps))}"
               f" — {step.title}"))
    yield sse("tool", ToolEvent(
        name="search_docs", status="running", detail=step.query))
    await asyncio.sleep(0.6)
    yield sse("tool", ToolEvent(name="search_docs", status="done", detail="۳ نتیجه"))

    text = _FLOW_STEP_TEXT if step.index == 1 else (
        f"**{step.title}**\n\nمتن نمونه برای این قدم. هدف: {step.goal}"
    )
    async for ev in _stream_text(text):
        yield ev

    yield sse("sources", SourcesEvent(items=_SOURCES_NORMAL[:1]))
    items = [
        Suggestion(label=f"قدم بعد: {steps[state.step + 1].title}"[:80],
                   prompt="قدم بعد", kind="step")
        if state.step + 1 < len(steps) else
        Suggestion(label="این قدم آخر است — جمع‌بندی کن",
                   prompt="قدم بعد", kind="step"),
        Suggestion(label="همین قدم را بیشتر توضیح بده",
                   prompt="همین قدم را بیشتر توضیح بده", kind="step"),
        Suggestion(label="خروج از فرآیند", prompt="خروج از فرآیند", kind="step"),
    ]
    yield sse("suggestions", SuggestionsEvent(items=items))

    session.add("user", message)
    session.add(
        "assistant", text,
        sources=[s.model_dump() for s in _SOURCES_NORMAL[:1]],
        suggestions=[i.model_dump() for i in items],
    )
    yield sse("done", DoneEvent(tokens_used=0, cached=False, latency_ms=1800))


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

    # --- سناریوی فرآیند چندمرحله‌ای ---
    if scenario == "flow":
        async for event in _mock_flow(message, session):
            yield event
        return

    # --- سناریوی سؤال تکمیلی: هیچ ابزاری صدا زده نمی‌شود ---
    if scenario == "clarify":
        await asyncio.sleep(0.3)
        async for ev in _stream_text(_ANSWER_CLARIFY):
            yield ev
        yield sse("suggestions", SuggestionsEvent(items=_SUGGESTIONS_CLARIFY))
        session.add("user", message)
        session.add(
            "assistant", _ANSWER_CLARIFY,
            suggestions=[item.model_dump() for item in _SUGGESTIONS_CLARIFY],
        )
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
        yield sse("suggestions", SuggestionsEvent(items=_SUGGESTIONS_DIAGNOSE))
        session.add("user", message)
        session.add(
            "assistant", _ANSWER_DIAGNOSE,
            sources=[source.model_dump() for source in _SOURCES_DIAGNOSE],
            suggestions=[item.model_dump() for item in _SUGGESTIONS_DIAGNOSE],
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
    yield sse("suggestions", SuggestionsEvent(items=_SUGGESTIONS_NORMAL))
    session.add("user", message)
    session.add(
        "assistant", _ANSWER_NORMAL,
        sources=[source.model_dump() for source in _SOURCES_NORMAL],
        suggestions=[item.model_dump() for item in _SUGGESTIONS_NORMAL],
    )
    yield sse("done", DoneEvent(tokens_used=620, cached=False, latency_ms=2450))
