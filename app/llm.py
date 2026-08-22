"""
کلاینت سرویس LLM — سازگار با OpenAI.

تنها نقطه‌ی تماس با مدل. همه‌چیز از env می‌آید، پس سوییچ بین ارائه‌دهنده‌ها
(مثلاً به ai.liara.ir) فقط تغییر دو متغیر است، بدون دست زدن به کد.
"""

from __future__ import annotations

import logging
import time
from functools import lru_cache

import numpy as np
from openai import (
    APIConnectionError,
    APITimeoutError,
    AsyncOpenAI,
    InternalServerError,
    OpenAI,
    RateLimitError,
)

from app.observability import metrics
from app.settings import settings
from app.text_norm import normalize

log = logging.getLogger("app.llm")

MAX_RETRIES = 3
RETRYABLE_ERRORS = (
    APIConnectionError,
    APITimeoutError,
    InternalServerError,
    RateLimitError,
)


class LLMUnavailable(RuntimeError):
    """سرویس مدل در دسترس نیست — به کاربر پیام قابل‌فهم داده می‌شود."""


@lru_cache(maxsize=1)
def client() -> OpenAI:
    if not settings.llm_configured:
        raise LLMUnavailable("کلید یا آدرس سرویس هوش مصنوعی تنظیم نشده است.")
    return OpenAI(
        base_url=settings.liara_ai_base_url,
        api_key=settings.liara_ai_api_key_value,
        timeout=settings.llm_timeout_seconds,
        max_retries=0,     # تلاش مجدد را خودمان مدیریت می‌کنیم
    )


@lru_cache(maxsize=1)
def aclient() -> AsyncOpenAI:
    """
    کلاینت ناهمگام — برای استریم چت.

    کلاینت همگام در یک اندپوینت async کل event loop را بلاک می‌کند و
    درخواست‌های همزمان بقیه کاربران را متوقف می‌کند.
    """
    if not settings.llm_configured:
        raise LLMUnavailable("کلید یا آدرس سرویس هوش مصنوعی تنظیم نشده است.")
    return AsyncOpenAI(
        base_url=settings.liara_ai_base_url,
        api_key=settings.liara_ai_api_key_value,
        timeout=settings.llm_timeout_seconds,
        max_retries=1,
    )


def _retry(fn, what: str):
    """backoff نمایی. خطای گذرای شبکه نباید به کاربر خطا نشان بدهد."""
    last: Exception | None = None
    for attempt in range(MAX_RETRIES):
        try:
            return fn()
        except RETRYABLE_ERRORS as exc:
            last = exc
            if attempt < MAX_RETRIES - 1:
                wait = 2 ** attempt
                log.warning("%s failed (%s), retrying in %ss", what, type(exc).__name__, wait)
                time.sleep(wait)
        except Exception as exc:
            # 4xxهایی مثل کلید اشتباه یا ورودی بد با retry درست نمی‌شوند.
            # پیام provider هم لاگ می‌شود وگرنه «۴۰۰ Bad Request» بدون علت
            # می‌ماند؛ Secretها را SecretRedactionFilter پاک می‌کند.
            log.error(
                "%s failed permanently (%s): %s",
                what, type(exc).__name__, str(exc)[:300],
            )
            raise LLMUnavailable(f"{what} ناموفق بود.") from exc
    log.error("%s failed after %d attempts: %s", what, MAX_RETRIES, last)
    raise LLMUnavailable(f"{what} ناموفق بود.") from last


@lru_cache(maxsize=settings.cache_max_entries)
def _embed_query_cached(text: str) -> tuple[float, ...]:
    """
    امبدینگ سؤال کاربر.

    ⚠️ باید دقیقاً همان مدلی باشد که ایندکس با آن ساخته شده، وگرنه بازیابی
    بی‌سروصدا خراب می‌شود و دلیلش پیدا نمی‌شود.

    کش شده چون سؤالات تکراری زیاد است — هم سریع‌تر، هم ارزان‌تر.
    خروجی tuple است تا قابل کش باشد؛ نرمال‌شده برمی‌گردد.
    """
    def call():
        resp = client().embeddings.create(
            model=settings.model_embedding, input=[text]
        )
        if resp.usage:
            metrics.token_usage("embedding", resp.usage.total_tokens)
        return resp.data[0].embedding

    vec = np.array(_retry(call, "امبدینگ سؤال"), dtype=np.float32)
    vec /= max(float(np.linalg.norm(vec)), 1e-9)
    return tuple(vec.tolist())


def embed_query(text: str) -> tuple[float, ...]:
    """ورودی نرمال‌شده باعث hit برای شکل‌های معادل فارسی نیز می‌شود."""
    normalized = normalize(text).strip()
    before = _embed_query_cached.cache_info()
    result = _embed_query_cached(normalized)
    after = _embed_query_cached.cache_info()
    metrics.cache_event(
        "embedding", "hit" if after.hits > before.hits else "miss"
    )
    return result


def embed_query_np(text: str) -> np.ndarray:
    return np.array(embed_query(text), dtype=np.float32)
