"""
③ Embedding — تبدیل متن تکه‌ها به وکتور.

سه نکته که اگر رعایت نشوند بی‌سروصدا خراب می‌کنند:

  ۱. کش — این اسکریپت چند بار اجرا می‌شود (چون chunking را تنظیم می‌کنیم).
     بدون کش، هر بار یک میلیون توکن دوباره خرج می‌شود.
  ۲. نرمال‌سازی موقع build — تا زمان اجرا فقط یک ضرب داخلی باشد.
  ۳. تقارن با زمان کوئری — سؤال کاربر باید با همین مدل و همین پیشوند امبد
     شود. تابع embed_query() پایین تنها راه درست انجام این کار است؛
     زمان اجرا هم از همین استفاده می‌شود.

اجرا:
    python -m ingest.embed
"""

from __future__ import annotations

import hashlib
import json
import sys
import time

import numpy as np
from openai import OpenAI

from app.settings import settings
from ingest.config import CACHE_DIR, VECTORS_FILE
from ingest.schema import Chunk

BATCH_SIZE = 96
MAX_RETRIES = 4

# پیشوند مدل‌های خانواده e5 / bge. برای مدل‌های OpenAI بی‌اثر است.
DOC_PREFIX = ""
QUERY_PREFIX = ""


def _client() -> OpenAI:
    if not settings.llm_configured:
        sys.exit(
            "LIARA_AI_API_KEY یا LIARA_AI_BASE_URL تنظیم نشده.\n"
            "فایل .env را بسازید و مقادیر را از کنسول لیارا > هوش مصنوعی بردارید."
        )
    return OpenAI(
        base_url=settings.liara_ai_base_url,
        api_key=settings.liara_ai_api_key_value,
    )


def _cache_path():
    CACHE_DIR.mkdir(exist_ok=True)
    model = settings.model_embedding.replace("/", "_")
    return CACHE_DIR / f"embeddings-{model}.jsonl"


def _load_cache() -> dict[str, list[float]]:
    path = _cache_path()
    if not path.exists():
        return {}
    cache: dict[str, list[float]] = {}
    with path.open(encoding="utf-8") as f:
        for line in f:
            try:
                rec = json.loads(line)
                cache[rec["h"]] = rec["v"]
            except Exception:
                continue
    return cache


def _key(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:24]


def _embed_batch(client: OpenAI, texts: list[str]) -> list[list[float]]:
    """با backoff نمایی تلاش مجدد می‌کند؛ خطای گذرای شبکه نباید کل ران را بسوزاند."""
    for attempt in range(MAX_RETRIES):
        try:
            resp = client.embeddings.create(model=settings.model_embedding, input=texts)
            return [d.embedding for d in resp.data]
        except Exception as exc:
            if attempt == MAX_RETRIES - 1:
                raise
            wait = 2 ** attempt
            print(f"  خطا ({type(exc).__name__})، {wait} ثانیه صبر و تلاش مجدد…")
            time.sleep(wait)
    return []


def embed_texts(texts: list[str], *, use_cache: bool = True) -> np.ndarray:
    """
    لیست متن → ماتریس نرمال‌شده (n, dim).
    فقط متن‌هایی که در کش نیستند به API فرستاده می‌شوند.
    """
    client = _client()
    cache = _load_cache() if use_cache else {}

    keys = [_key(t) for t in texts]
    todo = [(k, t) for k, t in zip(keys, texts) if k not in cache]
    # حذف تکراری‌های داخل خودِ ورودی
    todo = list({k: (k, t) for k, t in todo}.values())

    if todo:
        print(f"  {len(todo)} تکه جدید برای امبدینگ ({len(texts) - len(todo)} از کش)")
        cache_file = _cache_path().open("a", encoding="utf-8")
        try:
            for i in range(0, len(todo), BATCH_SIZE):
                batch = todo[i:i + BATCH_SIZE]
                vecs = _embed_batch(client, [DOC_PREFIX + t for _, t in batch])
                for (k, _), v in zip(batch, vecs):
                    cache[k] = v
                    cache_file.write(json.dumps({"h": k, "v": v}) + "\n")
                cache_file.flush()
                done = min(i + BATCH_SIZE, len(todo))
                print(f"  {done}/{len(todo)}", end="\r", flush=True)
        finally:
            cache_file.close()
        print()
    else:
        print("  همه‌ی تکه‌ها از کش خوانده شدند")

    mat = np.array([cache[k] for k in keys], dtype=np.float32)
    norms = np.linalg.norm(mat, axis=1, keepdims=True)
    return mat / np.maximum(norms, 1e-9)


def embed_query(text: str) -> np.ndarray:
    """
    امبدینگ سؤال کاربر — زمان اجرا.

    باید دقیقاً همان مدل و همان پیشوند اسناد باشد، وگرنه بازیابی بی‌سروصدا
    خراب می‌شود و دلیلش پیدا نمی‌شود.
    """
    client = _client()
    resp = client.embeddings.create(
        model=settings.model_embedding, input=[QUERY_PREFIX + text]
    )
    v = np.array(resp.data[0].embedding, dtype=np.float32)
    return v / max(float(np.linalg.norm(v)), 1e-9)


def build_vectors(chunks: list[Chunk]) -> np.ndarray:
    vectors = embed_texts([c.embed_text for c in chunks])
    VECTORS_FILE.parent.mkdir(exist_ok=True)
    np.save(VECTORS_FILE, vectors)
    return vectors


if __name__ == "__main__":
    from ingest.chunk import build_chunks

    print("ساخت تکه‌ها…")
    chunks = build_chunks()
    print(f"  {len(chunks)} تکه")

    print("امبدینگ…")
    started = time.time()
    vectors = build_vectors(chunks)
    print(f"\nماتریس: {vectors.shape}  →  {VECTORS_FILE}")
    print(f"زمان: {time.time() - started:.0f} ثانیه")
