"""
سنجش کیفیت بازیابی روی مجموعه‌ی طلایی.

این تنها ابزاری است که به ما می‌گوید یک تغییر در chunking یا retrieval،
بازیابی را بهتر کرده یا بدتر. بدون آن فقط حدس می‌زنیم.

    python -m eval.run_eval              روش پیش‌فرض
    python -m eval.run_eval --bm25       فقط کلیدواژه
    python -m eval.run_eval --dense      فقط برداری
    python -m eval.run_eval --verbose    نمایش شکست‌ها

معیار: Recall@k — آیا سند درست در k نتیجه‌ی اول هست؟
سؤالات مبهم (expect=null) اینجا سنجیده نمی‌شوند؛ آن‌ها رفتار ایجنت را
می‌سنجند نه بازیابی را.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

GOLDEN = Path(__file__).parent / "golden.jsonl"
# نقاط گزارش. ۸ عمداً هست چون TOP_K محصول ۸ است — سنجیدن روی ۵ یعنی
# جریمه کردن سندی که در رتبه ۷ است و مدل واقعاً آن را می‌بیند.
KS = (1, 3, 8, 10)


def page_stem(url: str) -> str:
    """
    آخرین بخش مسیر، بدون anchor.

    مستندات لیارا برای هر پلتفرم یک نسخه از همان صفحه دارند
    (set-cron-job زیر python و nodejs و django). وقتی کاربر پلتفرمش را
    نگفته، هر کدام جواب درستی است — پس سنجش صفحه‌محور معیار منصفانه‌تری است.
    """
    return url.split("#")[0].rstrip("/").rsplit("/", 1)[-1]


def load_golden() -> list[dict]:
    rows = []
    with GOLDEN.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def rewrite_all(questions: list[str]) -> dict[str, str]:
    """
    همه‌ی سؤالات را از مرحله‌ی بازنویسی رد می‌کند.

    ⚠️ بدون این، eval فقط نصف خط لوله را می‌سنجد. محصول واقعی هرگز سؤال خام
    را جستجو نمی‌کند — اول با مدل کوچک به واژگان مستندات ترجمه‌اش می‌کند.
    «چند تا ورکر بذارم؟» در eval شکست می‌خورد ولی در محصول موفق است، چون
    آنجا به «تعداد worker های gunicorn» تبدیل شده.
    """
    import asyncio

    from app.agent import rewrite
    from app.session import Session

    async def run() -> dict[str, str]:
        out: dict[str, str] = {}
        for q in questions:
            try:
                plan = await rewrite(q, Session(id="_eval"))
                out[q] = plan["query"] or q
            except Exception as exc:
                print(f"  بازنویسی ناموفق برای {q!r}: {type(exc).__name__}")
                out[q] = q
        return out

    return asyncio.run(run())


def _load_retriever(mode: str):
    """
    بازیابی‌کننده را برمی‌گرداند: (query, k) -> list[Chunk]

    تا وقتی app/retrieval.py (فاز ۲) نوشته نشده، به BM25 برمی‌گردد تا
    همین حالا یک عدد پایه داشته باشیم.
    """
    try:
        from app.retrieval import Retriever          # type: ignore
        r = Retriever.load()
        return lambda q, k: r.search(q, k=k, mode=mode), f"hybrid/{mode}"
    except Exception:
        pass

    import pickle
    from ingest.chunk import build_chunks
    from ingest.config import BM25_FILE
    from ingest.text_norm import tokenize

    chunks = build_chunks()
    with BM25_FILE.open("rb") as f:
        bm25 = pickle.load(f)

    def search(q: str, k: int):
        scores = bm25.get_scores(tokenize(q))
        idx = sorted(range(len(scores)), key=lambda i: -scores[i])[:k]
        return [chunks[i] for i in idx]

    return search, "bm25 (پایه — app/retrieval.py هنوز نیست)"


def sweep() -> None:
    """
    جاروب پارامترهای فیوژن.

    امبدینگ سؤالات کش شده، پس کل جاروب فقط به تعداد سؤالات تماس API دارد
    نه به تعداد ترکیب‌ها.
    """
    from app.retrieval import Retriever

    r = Retriever.load()
    rows = [x for x in load_golden() if x.get("expect")]

    def score(**kw) -> tuple[int, int]:
        at5 = at1 = 0
        for row in rows:
            hits = r.search(row["q"], k=10, **kw)
            stem = page_stem(row["expect"])
            rank = next(
                (i + 1 for i, h in enumerate(hits) if page_stem(h.url) == stem), None
            )
            at5 += bool(rank and rank <= 5)
            at1 += bool(rank and rank == 1)
        return at5, at1

    n = len(rows)
    print(f"جاروب روی {n} سؤال (معیار: صفحه‌محور)\n")
    print(f"{'حالت':<28}{'@1':>8}{'@5':>10}")
    print("-" * 46)

    for mode in ("dense", "bm25"):
        a5, a1 = score(mode=mode)
        print(f"{mode:<28}{a1:>4}/{n}{a5:>7}/{n}  {a5 * 100 // n}%")

    print()
    best = (0, None)
    for rrf_k in (5, 12, 30, 60):
        for pool_factor in (2, 4, 8):
            for w in (0.5, 0.8, 1.0, 1.3):
                a5, a1 = score(
                    mode="hybrid", rrf_k=rrf_k,
                    pool_factor=pool_factor, weights=(1.0, w),
                )
                label = f"hybrid K={rrf_k} pool={pool_factor}x w={w}"
                mark = ""
                if a5 > best[0]:
                    best = (a5, label)
                    mark = "  ←"
                print(f"{label:<28}{a1:>4}/{n}{a5:>7}/{n}  {a5 * 100 // n}%{mark}")

    print(f"\nبهترین: {best[1]}  →  {best[0]}/{n}  ({best[0] * 100 // n}%)")


def main() -> None:
    mode = "dense" if "--dense" in sys.argv else "bm25" if "--bm25" in sys.argv else "hybrid"
    verbose = "--verbose" in sys.argv

    use_rewrite = "--rewrite" in sys.argv

    search, label = _load_retriever(mode)
    rows = [r for r in load_golden() if r.get("expect")]
    ambiguous = len(load_golden()) - len(rows)

    print(f"روش بازیابی : {label}")
    print(f"بازنویسی    : {'روشن (مثل محصول واقعی)' if use_rewrite else 'خاموش (سؤال خام)'}")
    print(f"سؤالات      : {len(rows)} سنجش‌پذیر + {ambiguous} مبهم (اینجا سنجیده نمی‌شوند)\n")

    rewritten: dict[str, str] = {}
    if use_rewrite:
        print("در حال بازنویسی سؤالات…")
        rewritten = rewrite_all([r["q"] for r in rows])
        print()

    strict = {k: 0 for k in KS}
    loose = {k: 0 for k in KS}
    by_type: dict[str, list[int]] = {}
    failures = []

    for row in rows:
        rq = rewritten.get(row["q"])
        # مثل محصول: هم سؤال خام هم بازنویسی‌شده
        query = [row["q"], rq] if rq and rq != row["q"] else row["q"]
        results = search(query, max(KS))
        urls = [c.url for c in results]

        # expect می‌تواند رشته یا لیست باشد: بعضی سؤالات بیش از یک پاسخ
        # درست دارند (مثلاً هم paas/disks/about هم paas/python/how-tos/use-disk)
        expects = row["expect"] if isinstance(row["expect"], list) else [row["expect"]]
        stems = {page_stem(e) for e in expects}

        r_strict = next(
            (i + 1 for i, u in enumerate(urls) if any(e in u for e in expects)), None
        )
        r_loose = next(
            (i + 1 for i, u in enumerate(urls) if page_stem(u) in stems), None
        )

        for k in KS:
            strict[k] += bool(r_strict and r_strict <= k)
            loose[k] += bool(r_loose and r_loose <= k)

        by_type.setdefault(row["type"], []).append(bool(r_loose and r_loose <= 5))

        if not r_loose or r_loose > 5:
            failures.append((row, urls[:3]))

    n = len(rows)
    print("Recall  (دقیق = همان مسیر | صفحه‌محور = همان صفحه، هر پلتفرمی)")
    print(f"{'':6}{'دقیق':>12}{'صفحه‌محور':>14}")
    for k in KS:
        bar = "█" * (loose[k] * 24 // max(n, 1))
        print(f"  @{k:<3}{strict[k]:>6}/{n} {strict[k] * 100 // n:>3}%"
              f"{loose[k]:>8}/{n} {loose[k] * 100 // n:>3}%  {bar}")

    print("\nبه تفکیک نوع سؤال (صفحه‌محور @5):")
    for t, vals in by_type.items():
        print(f"  {t:<10} {sum(vals)}/{len(vals)}  {sum(vals) * 100 // len(vals)}%")

    if failures:
        print(f"\nشکست‌ها ({len(failures)}):")
        for row, top in failures:
            print(f"\n  ✗ {row['q']}")
            rq = rewritten.get(row["q"])
            if rq and rq != row["q"]:
                print(f"    بازنویسی: {rq}")
            print(f"    انتظار: {row['expect']}")
            if verbose:
                for u in top:
                    print(f"    گرفت  : {u}")

    if not verbose and failures:
        print("\n(برای دیدن نتایج اشتباه: --verbose)")


if __name__ == "__main__":
    if "--sweep" in sys.argv:
        sweep()
    else:
        main()
