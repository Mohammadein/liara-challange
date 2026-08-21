"""
محدودسازی نرخ — سطل توکن درون‌حافظه‌ای.

بدون وابستگی به Redis نوشته شده چون معماری ما تک‌نمونه‌ای است و افزودن یک
سرویس جانبی برای این کار، یک نقطه شکست اضافه می‌آورد بدون سود عملی.
اگر بعداً چند نمونه شد، همین رابط با Redis جایگزین می‌شود.

هر تماس API یک تماس LLM پشتش دارد، یعنی نرخ کنترل‌نشده مستقیماً یعنی
هزینه‌ی کنترل‌نشده. این هم امنیت است هم بهینه‌سازی هزینه.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from math import ceil
from threading import Lock

from fastapi import Request


@dataclass
class _Bucket:
    tokens: float
    last: float = field(default_factory=time.monotonic)


class RateLimiter:
    def __init__(self, per_minute: int, burst: int | None = None) -> None:
        self.rate = per_minute / 60.0          # توکن بر ثانیه
        self.capacity = burst or per_minute
        self._buckets: dict[str, _Bucket] = {}
        self._lock = Lock()

    def check(self, key: str) -> tuple[bool, int, int]:
        """
        (اجازه دارد؟، ثانیه تا تلاش بعدی، ظرفیت باقی‌مانده)
        """
        with self._lock:
            now = time.monotonic()
            b = self._buckets.get(key)
            if b is None:
                b = _Bucket(tokens=self.capacity)
                self._buckets[key] = b

            # پر شدن سطل به اندازه‌ی زمان گذشته
            b.tokens = min(self.capacity, b.tokens + (now - b.last) * self.rate)
            b.last = now

            if b.tokens >= 1:
                b.tokens -= 1
                return True, 0, max(0, int(b.tokens))

            retry_after = max(1, ceil((1 - b.tokens) / self.rate))
            return False, retry_after, 0

    def prune(self, max_keys: int = 10_000) -> None:
        """جلوگیری از رشد بی‌حد حافظه با ترافیک IPهای متنوع."""
        with self._lock:
            if len(self._buckets) <= max_keys:
                return
            now = time.monotonic()
            stale = [k for k, v in self._buckets.items() if now - v.last > 300]
            for k in stale:
                del self._buckets[k]

            # در حمله‌ای با IPهای یکتا، حتی کلیدهای تازه هم نباید حافظه را
            # بی‌حد رشد دهند. قدیمی‌ترین‌ها تا سقف حذف می‌شوند.
            overflow = len(self._buckets) - max_keys
            if overflow > 0:
                oldest = sorted(self._buckets, key=lambda k: self._buckets[k].last)
                for key in oldest[:overflow]:
                    del self._buckets[key]


class CapacityLimiter:
    """Bulkhead: خرابی/کندی LLM همه workerها را اشغال نمی‌کند."""

    def __init__(self, capacity: int) -> None:
        self._semaphore = asyncio.BoundedSemaphore(capacity)

    async def acquire(self, timeout: float = 0.25) -> bool:
        try:
            await asyncio.wait_for(self._semaphore.acquire(), timeout=timeout)
            return True
        except TimeoutError:
            return False

    def release(self) -> None:
        self._semaphore.release()


def client_ip(request: Request, *, trust_proxy: bool = False) -> str:
    """
    IP واقعی پشت پروکسی لیارا.

    X-Forwarded-For فقط با تنظیم صریح TRUST_PROXY_HEADERS پذیرفته می‌شود؛
    در حالت پیش‌فرض ورودی قابل جعل کاربر نادیده گرفته می‌شود.
    """
    fwd = request.headers.get("x-forwarded-for")
    if trust_proxy and fwd:
        return fwd.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


# یک bulkhead مشترک برای تمام مسیرهایی که LLM مصرف می‌کنند.
from app.settings import settings  # noqa: E402  (پس از تعریف کلاس‌ها)

llm_capacity = CapacityLimiter(settings.max_concurrent_llm_requests)
