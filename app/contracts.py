"""
قرارداد API — بین بک‌اند و فرانت.

⚠️ این فایل قفل است. فرانت‌اند علیه همین شکل ساخته می‌شود؛ تغییرش یعنی
شکستن UI. قبل از تغییر با تیم هماهنگ کنید.

    POST /api/chat  →  text/event-stream

    event: token    {"t": "سلام"}
    event: tool     {"name": "search_docs", "status": "running", "detail": "..."}
    event: sources  {"items": [{"title","section","url","service"}]}
    event: done     {"tokens_used": 1234, "cached": false, "latency_ms": 890}
    event: error    {"message": "...", "code": "rate_limited"}
"""

from __future__ import annotations

import json
from typing import Any, Literal

from pydantic import BaseModel, Field


# ---------- درخواست ----------

class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=4000)
    session_id: str = Field(min_length=1, max_length=64)


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


class DoneEvent(BaseModel):
    tokens_used: int = 0
    cached: bool = False
    latency_ms: int = 0


class ErrorEvent(BaseModel):
    message: str
    code: str = "internal_error"


# ---------- کمکی ----------

EventName = Literal["token", "tool", "sources", "done", "error"]


def sse(event: EventName, data: BaseModel | dict[str, Any]) -> str:
    """یک رویداد را به قالب Server-Sent Events تبدیل می‌کند."""
    payload = data.model_dump() if isinstance(data, BaseModel) else data
    body = json.dumps(payload, ensure_ascii=False)
    return f"event: {event}\ndata: {body}\n\n"
