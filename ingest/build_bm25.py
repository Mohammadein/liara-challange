"""
③b BM25 — ایندکس کلیدواژه‌ای.

چرا کنار جستجوی برداری لازم است: جستجوی معنایی روی اسم‌های خاص ضعیف است.
کاربری که می‌پرسد «قیمت لیارا دیسک چنده؟» با تطابق دقیق کلیدواژه‌ی
«لیارا دیسک» خیلی بهتر پیدا می‌شود تا با شباهت معنایی. نتایج دو روش
زمان اجرا با RRF ترکیب می‌شوند.

اجرا:
    python -m ingest.build_bm25
"""

from __future__ import annotations

import os
import pickle
import re

from rank_bm25 import BM25Okapi

from ingest.config import BM25_FILE
from ingest.schema import Chunk
from ingest.text_norm import tokenize

_FENCE = re.compile(r"```.*?```", re.DOTALL)

# عنوان چند بار تکرار می‌شود تا وزن بگیرد. BM25 وزن میدانی ندارد، پس
# تکرار تنها راه گفتن «تطابق در عنوان مهم‌تر از تطابق در بدنه است».
#
# ⚠️ این عدد دو طرفه است. با ۳، «میرور npm» درست شد ولی «ساخت باکت» خراب:
# صفحه‌ی «حذف باکت» (کلمه در عنوان) صفحه‌ای را که واقعاً ساخت باکت را یاد
# می‌دهد (کلمه فقط در بدنه) شکست می‌داد. با LIARA_TITLE_BOOST قابل تنظیم
# است تا با مجموعه‌ی طلایی اندازه‌گیری شود، نه حدس.
TITLE_BOOST = int(os.environ.get("LIARA_TITLE_BOOST", "2"))


def bm25_text(chunk: Chunk) -> str:
    """
    متنی که وارد ایندکس کلیدواژه می‌شود — عمداً با text و embed_text فرق دارد.

    دو تصمیم:
      ۱. بلوک‌های کد حذف می‌شوند. یک نمونه‌کد ممکن است ۲۰ بار `liara.json`
         داشته باشد و امتیاز را از صفحه‌ای بدزدد که واقعاً موضوعش همان است.
         کد برای جستجوی معنایی و نمایش به کاربر سر جایش می‌ماند.
      ۲. عنوان صفحه/بخش و اسلاگ مسیر تکرار می‌شوند تا وزن بگیرند.
    """
    body = _FENCE.sub(" ", chunk.text)
    slug = chunk.source_path.rsplit("/", 1)[-1].removesuffix(".mdx").replace("-", " ")

    titles = " ".join(
        filter(None, [chunk.page_title, chunk.section_title, chunk.variant, slug])
    )
    return " ".join([titles] * TITLE_BOOST) + " " + chunk.breadcrumb + " " + body


def build_bm25(chunks: list[Chunk]) -> BM25Okapi:
    corpus = [tokenize(bm25_text(c)) for c in chunks]
    bm25 = BM25Okapi(corpus)

    BM25_FILE.parent.mkdir(exist_ok=True)
    with BM25_FILE.open("wb") as f:
        pickle.dump(bm25, f, protocol=pickle.HIGHEST_PROTOCOL)
    return bm25


if __name__ == "__main__":
    from ingest.chunk import build_chunks
    from ingest.text_norm import normalize

    chunks = build_chunks()
    print(f"ساخت ایندکس BM25 روی {len(chunks)} تکه…")
    bm25 = build_bm25(chunks)
    size_mb = BM25_FILE.stat().st_size / 1024 / 1024
    print(f"ذخیره شد: {BM25_FILE}  ({size_mb:.1f} MB)")

    # تست سریع نرمال‌سازی
    print("\nتست نرمال‌سازی فارسی:")
    for s in ["فايل liara.json", "فایل liara.json", "چطور دیتابيس بسازم؟"]:
        print(f"  {s!r:<32} → {normalize(s)!r}")

    # تست سریع جستجو
    print("\nتست جستجوی کلیدواژه‌ای:")
    for q in ["فایل liara.json چیست", "نسخه پایتون", "میرور npm"]:
        scores = bm25.get_scores(tokenize(q))
        top = sorted(range(len(scores)), key=lambda i: -scores[i])[:3]
        print(f"\n  «{q}»")
        for i in top:
            print(f"    {scores[i]:6.2f}  {chunks[i].page_title} › "
                  f"{chunks[i].section_title or '—'}")
