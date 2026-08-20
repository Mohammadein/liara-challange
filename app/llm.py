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
from openai import OpenAI

from app.settings import settings

log = logging.getLogger("app.llm")

MAX_RETRIES = 3


class LLMUnavailable(RuntimeError):
    """سرویس مدل در دسترس نیست — به کاربر پیام قابل‌فهم داده می‌شود."""


@lru_cache(maxsize=1)
def client() -> OpenAI:
    if not settings.llm_configured:
        raise LLMUnavailable("کلید یا آدرس سرویس هوش مصنوعی تنظیم نشده است.")
    return OpenAI(
        base_url=settings.liara_ai_base_url,
        api_key=settings.liara_ai_api_key,
        timeout=60,
        max_retries=0,     # تلاش مجدد را خودمان مدیریت می‌کنیم
    )


def _retry(fn, what: str):
    """backoff نمایی. خطای گذرای شبکه نباید به کاربر خطا نشان بدهد."""
    last: Exception | None = None
    for attempt in range(MAX_RETRIES):
        try:
            return fn()
        except Exception as exc:
            last = exc
            if attempt < MAX_RETRIES - 1:
                wait = 2 ** attempt
                log.warning("%s failed (%s), retrying in %ss", what, type(exc).__name__, wait)
                time.sleep(wait)
    log.error("%s failed after %d attempts: %s", what, MAX_RETRIES, last)
    raise LLMUnavailable(f"{what} ناموفق بود.") from last


@lru_cache(maxsize=2048)
def embed_query(text: str) -> tuple[float, ...]:
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
        return resp.data[0].embedding

    vec = np.array(_retry(call, "امبدینگ سؤال"), dtype=np.float32)
    vec /= max(float(np.linalg.norm(vec)), 1e-9)
    return tuple(vec.tolist())


def embed_query_np(text: str) -> np.ndarray:
    return np.array(embed_query(text), dtype=np.float32)
