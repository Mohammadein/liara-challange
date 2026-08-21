"""
قرارداد API — بین بک‌اند و فرانت.

⚠️ این فایل قفل است. فرانت‌اند علیه همین شکل ساخته می‌شود؛ تغییرش یعنی
شکستن UI. قبل از تغییر با تیم هماهنگ کنید.

    POST /api/chat  →  text/event-stream

    event: token        {"t": "سلام"}
    event: tool         {"name": "search_docs", "status": "running", "detail": "..."}
    event: flow         {"id","title","step","total","status","steps":[...]}
    event: sources      {"items": [{"title","section","url","service"}]}
    event: suggestions  {"items": [{"label","prompt","kind"}]}
    event: done         {"tokens_used": 1234, "cached": false, "latency_ms": 890}
    event: error        {"message": "...", "code": "rate_limited"}

`flow` و `suggestions` **افزوده** شده‌اند و اختیاری‌اند: کلاینتی که آن‌ها را
نمی‌شناسد نادیده‌شان می‌گیرد و رفتار قبلی‌اش عوض نمی‌شود.
"""

from __future__ import annotations

import json
from typing import Any, Literal

from pydantic import BaseModel, Field


# ---------- درخواست ----------

class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=4000)
    session_id: str = Field(min_length=1, max_length=64)
    # شناسه‌ی ناشناس مرورگر؛ اختیاری تا مصرف‌کننده‌های قدیمی API نشکنند.
    client_id: str | None = Field(default=None, min_length=16, max_length=64)


# ---------- رویدادهای پاسخ ----------

class TokenEvent(BaseModel):
    t: str


class ToolEvent(BaseModel):
    name: str
    status: Literal["running", "done", "error"]
    detail: str = ""


class Source(BaseModel):
    title: str            # عنوان صفحه
    section: str = ""     # عنوان بخش
    url: str              # لینک کامل با anchor
    service: str = ""     # برای نمایش برچسب سرویس


class SourcesEvent(BaseModel):
    items: list[Source]


class Suggestion(BaseModel):
    """یک «قدم بعدی» قابل کلیک.

    `label` چیزی است که روی چیپ نوشته می‌شود و `prompt` چیزی است که با کلیک
    به‌عنوان پیام بعدی ارسال می‌شود. این دو عمداً جدا هستند: برچسب باید کوتاه
    باشد ولی پیام باید کامل و بدون ابهام باشد تا بازیابی درست کار کند.
    """

    label: str = Field(max_length=80)
    prompt: str = Field(max_length=300)
    # ask  = یک سؤال معمولی
    # flow = شروع یک فرآیند چندمرحله‌ای
    # step = حرکت در فرآیند فعال (قدم بعد / تکرار / خروج)
    kind: Literal["ask", "flow", "step"] = "ask"


class SuggestionsEvent(BaseModel):
    items: list[Suggestion]


class FlowStepOut(BaseModel):
    index: int                                    # ۱-پایه
    title: str
    status: Literal["done", "current", "pending"]


class FlowEvent(BaseModel):
    """وضعیت فرآیند چندمرحله‌ای، برای رندر stepper در UI."""

    id: str
    title: str
    step: int                                     # قدم جاری، ۱-پایه (۰ = تمام‌شده)
    total: int
    status: Literal["started", "advanced", "completed", "exited"]
    steps: list[FlowStepOut] = []


class DoneEvent(BaseModel):
    tokens_used: int = 0
    cached: bool = False
    latency_ms: int = 0


class ErrorEvent(BaseModel):
    message: str
    code: str = "internal_error"


# ---------- کمکی ----------

EventName = Literal[
    "token", "tool", "flow", "sources", "suggestions", "done", "error"
]


def sse(event: EventName, data: BaseModel | dict[str, Any]) -> str:
    """یک رویداد را به قالب Server-Sent Events تبدیل می‌کند."""
    payload = data.model_dump() if isinstance(data, BaseModel) else data
    body = json.dumps(payload, ensure_ascii=False)
    return f"event: {event}\ndata: {body}\n\n"
