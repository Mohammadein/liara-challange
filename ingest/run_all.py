"""
اجرای کل خط لوله ingest.

    python -m ingest.run_all              کامل
    python -m ingest.run_all --no-embed   فقط chunks.json و bm25 (بدون کلید API)

خروجی در data/:
    chunks.json    متن تکه‌ها + متادیتا  (بدون embed_text)
    vectors.npy    ماتریس نرمال‌شده
    bm25.pkl       ایندکس کلیدواژه

هر سه باید commit شوند تا داخل ایمیج داکر بروند.
"""

from __future__ import annotations

import json
import sys
import time

from ingest.build_bm25 import build_bm25
from ingest.chunk import build_chunks
from ingest.config import BM25_FILE, CHUNKS_FILE, DATA_DIR, VECTORS_FILE


def main() -> None:
    skip_embed = "--no-embed" in sys.argv
    started = time.time()
    DATA_DIR.mkdir(exist_ok=True)

    print("① ② پارس و تکه‌بندی…")
    chunks = build_chunks()
    print(f"   {len(chunks)} تکه")

    # ترتیب تکه‌ها قرارداد ضمنی بین سه فایل است: سطر i در vectors.npy و
    # سند i در bm25 باید همان chunks[i] باشد.
    CHUNKS_FILE.write_text(
        json.dumps([c.to_runtime_dict() for c in chunks], ensure_ascii=False),
        encoding="utf-8",
    )
    print(f"   {CHUNKS_FILE.name}  ({CHUNKS_FILE.stat().st_size / 1024 / 1024:.1f} MB)")

    print("③b ایندکس BM25…")
    build_bm25(chunks)
    print(f"   {BM25_FILE.name}  ({BM25_FILE.stat().st_size / 1024 / 1024:.1f} MB)")

    if skip_embed:
        print("\n③ امبدینگ رد شد (--no-embed)")
    else:
        print("③ امبدینگ…")
        from ingest.embed import build_vectors
        vectors = build_vectors(chunks)
        print(f"   {VECTORS_FILE.name}  {vectors.shape}  "
              f"({VECTORS_FILE.stat().st_size / 1024 / 1024:.1f} MB)")

    print(f"\nتمام شد در {time.time() - started:.0f} ثانیه")


if __name__ == "__main__":
    main()
