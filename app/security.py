"""
محدودسازی نرخ — سطل توکن درون‌حافظه‌ای.

بدون وابستگی به Redis نوشته شده چون معماری ما تک‌نمونه‌ای است و افزودن یک
سرویس جانبی برای این کار، یک نقطه شکست اضافه می‌آورد بدون سود عملی.
اگر بعداً چند نمونه شد، همین رابط با Redis جایگزین می‌شود.

هر تماس API یک تماس LLM پشتش دارد، یعنی نرخ کنترل‌نشده مستقیماً یعنی
هزینه‌ی کنترل‌نشده. این هم امنیت است هم بهینه‌سازی هزینه.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

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

    def check(self, key: str) -> tuple[bool, int]:
        """
        (اجازه دارد؟، ثانیه تا تلاش بعدی)
        """
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
            return True, 0

        return False, max(1, int((1 - b.tokens) / self.rate))

    def prune(self, max_keys: int = 10_000) -> None:
        """جلوگیری از رشد بی‌حد حافظه با ترافیک IPهای متنوع."""
        if len(self._buckets) <= max_keys:
            return
        now = time.monotonic()
        stale = [k for k, v in self._buckets.items() if now - v.last > 300]
        for k in stale:
            del self._buckets[k]


def client_ip(request: Request) -> str:
    """
    IP واقعی پشت پروکسی لیارا.

    ⚠️ X-Forwarded-For قابل جعل است. اینجا فقط برای سقف نرخ استفاده می‌شود
    نه برای تصمیم امنیتی، پس قابل قبول است — ولی هرگز نباید مبنای احراز
    هویت قرار بگیرد.
    """
    fwd = request.headers.get("x-forwarded-for")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.client.host if request.client else "unknown"
