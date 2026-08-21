"""
بازیابی هیبریدی — جستجوی برداری + کلیدواژه‌ای، ترکیب‌شده با RRF.

چرا هر دو لازم‌اند (از نتایج خط پایه):
  «میرور npm»                       → BM25 عالی، وکتور ضعیف (اسم خاص)
  «به API‌م ریکوئست می‌زنم بلاک می‌شه» → BM25 صفر، وکتور باید نجات بدهد (CORS
                                       را کاربر به اسم نمی‌شناسد)

هیچ‌کدام به‌تنهایی کافی نیست. RRF به‌جای امتیاز خام، **رتبه‌ها** را ترکیب
می‌کند — چون امتیاز BM25 و شباهت کسینوسی اصلاً هم‌مقیاس نیستند و جمع کردن
مستقیمشان بی‌معنی است.
"""

from __future__ import annotations

import json
import logging
import pickle
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from app.settings import settings
from app.text_norm import normalize, tokenize

log = logging.getLogger("app.retrieval")

# ثابت RRF.
#
# ۶۰ مقدار استاندارد مقاله‌ی اصلی است، ولی برای k کوچک بد کار می‌کند:
# تفاوت 1/61 و 1/62 عملاً صفر است، پس سندی که در هر دو لیست متوسط است
# سندی را که در یکی اول است شکست می‌دهد. یعنی «توافق» بر «اطمینان»
# ترجیح داده می‌شود. عدد کوچک‌تر، رتبه‌های بالا را تیزتر می‌کند.
RRF_K = 5
# چقدر عمیق‌تر از k بازیابی کنیم قبل از ترکیب. عمق زیاد رقیق‌سازی می‌آورد.
POOL_FACTOR = 2
# وزن dense و bm25 — برابر.
#
# اول به dense وزن بیشتری داده بودم با این استدلال که سؤال کاربر توصیفی است.
# جاروب روی مجموعه‌ی طلایی خلافش را نشان داد: وزن ۰.۵ برای BM25 حدود ۵۸٪
# می‌داد و وزن ۱.۰ حدود ۶۸٪ — و این الگو در تمام ۱۲ ترکیب K/pool ثابت بود.
# منطقی هم هست: مستندات فنی پر از اسم خاص است (liara.json، npm، gunicorn).
WEIGHTS = (1.0, 1.0)

# تقویت (نه فیلتر) وقتی سرویس یا واریانت از context مکالمه معلوم است.
SERVICE_BOOST = 0.35
VARIANT_BOOST = 0.25


@dataclass
class Hit:
    """یک نتیجه‌ی بازیابی — همان فیلدهای Chunk به‌علاوه امتیاز."""
    id: str
    text: str
    url: str
    page_title: str
    section_title: str
    breadcrumb: str
    service: str
    variant: str | None
    has_code: bool
    score: float = 0.0

    @classmethod
    def from_dict(cls, d: dict, score: float = 0.0) -> "Hit":
        return cls(
            id=d["id"], text=d["text"], url=d["url"],
            page_title=d.get("page_title", ""),
            section_title=d.get("section_title", ""),
            breadcrumb=d.get("breadcrumb", ""),
            service=d.get("service", ""),
            variant=d.get("variant"),
            has_code=d.get("has_code", False),
            score=score,
        )


def _rrf(
    ranked_lists: list[list[int]],
    weights: list[float],
    rrf_k: int = RRF_K,
) -> dict[int, float]:
    """Reciprocal Rank Fusion — امتیاز هر سند = مجموع w/(K+rank)."""
    scores: dict[int, float] = {}
    for ranking, w in zip(ranked_lists, weights):
        for rank, idx in enumerate(ranking, start=1):
            scores[idx] = scores.get(idx, 0.0) + w / (rrf_k + rank)
    return scores


class Retriever:
    def __init__(self, chunks: list[dict], vectors: np.ndarray, bm25) -> None:
        self.chunks = chunks
        self.vectors = vectors
        self.bm25 = bm25

    # ---------------------------------------------------------- بارگذاری

    @classmethod
    def load(cls) -> "Retriever":
        # مسیرها اینجا تعریف می‌شوند نه از ingest.config — چون Dockerfile
        # فقط app/ و data/ را در ایمیج کپی می‌کند و ingest/ زمان اجرا نیست.
        data = Path(__file__).resolve().parent.parent / "data"

        chunks = json.loads((data / "chunks.json").read_text(encoding="utf-8"))
        vectors = np.load(data / "vectors.npy")
        with (data / "bm25.pkl").open("rb") as f:
            bm25 = pickle.load(f)

        # ترتیب سه فایل یک قرارداد ضمنی است؛ اگر به هم بخورد، پاسخ‌ها به
        # منابع کاملاً بی‌ربط ارجاع می‌دهند و دلیلش هم پیدا نمی‌شود.
        if len(chunks) != vectors.shape[0]:
            raise RuntimeError(
                f"ناهماهنگی ایندکس: {len(chunks)} تکه ولی {vectors.shape[0]} وکتور. "
                "python -m ingest.run_all را دوباره اجرا کنید."
            )

        log.info("index loaded: %d chunks, dim=%d", len(chunks), vectors.shape[1])
        return cls(chunks, vectors, bm25)

    # ---------------------------------------------------------- جستجو

    def _dense(
        self, query: str, n: int, allowed: list[int] | None = None,
    ) -> list[int]:
        from app.llm import embed_query_np

        sims = self.vectors @ embed_query_np(query)     # کل جستجو، همین یک خط
        if allowed is not None:
            candidates = np.asarray(allowed, dtype=np.int64)
            order = np.argsort(-sims[candidates])[:n]
            return candidates[order].tolist()
        return np.argsort(-sims)[:n].tolist()

    def _sparse(
        self, query: str, n: int, allowed: list[int] | None = None,
    ) -> list[int]:
        tokens = tokenize(query)
        if not tokens:
            return []
        scores = self.bm25.get_scores(tokens)
        if allowed is not None:
            candidates = np.asarray(allowed, dtype=np.int64)
            order = np.argsort(-scores[candidates])[:n]
            return candidates[order].tolist()
        return np.argsort(-scores)[:n].tolist()

    def search(
        self,
        query: str | list[str],
        k: int | None = None,
        *,
        mode: str = "hybrid",
        service: str | None = None,
        variant: str | None = None,
        url_prefix: str | None = None,
        rrf_k: int = RRF_K,
        pool_factor: int = POOL_FACTOR,
        weights: tuple[float, float] = WEIGHTS,
    ) -> list[Hit]:
        """
        mode: hybrid | dense | bm25
        service/variant: تقویت اختیاری — وقتی از context مکالمه می‌دانیم
        کاربر روی چه سرویسی یا با چه فریم‌ورکی کار می‌کند.
        url_prefix: فیلتر سخت فقط برای intent قطعی و قاعده‌محور؛ حدس مدل
        هرگز نباید به این پارامتر داده شود.
        بقیه پارامترها فقط برای جاروب در eval قابل تنظیم‌اند.
        """
        k = k or settings.top_k
        pool = max(k * pool_factor, 20)

        # چند کوئری (سؤال خام + بازنویسی‌شده) با هم ترکیب می‌شوند.
        #
        # بازنویسی گاهی اطلاعات را از دست می‌دهد: «میرور npm» به
        # «تنظیم mirror npm» تبدیل شد و ناگهان با anchorهای
        # liara-json-mirror در صفحات deploy-app تطابق پیدا کرد — سؤالی که
        # قبل از بازنویسی درست کار می‌کرد. جستجوی هر دو، بازنویسی را از
        # «جایگزینی» به «غنی‌سازی» تبدیل می‌کند.
        raw_queries = [query] if isinstance(query, str) else [q for q in query if q]
        queries: list[str] = []
        seen_queries: set[str] = set()
        for candidate in raw_queries:
            key = normalize(candidate).strip()
            if not key or key in seen_queries:
                continue
            seen_queries.add(key)
            queries.append(candidate)
        queries = queries or [""]
        allowed = (
            [i for i, chunk in enumerate(self.chunks)
             if url_prefix in chunk.get("url", "")]
            if url_prefix else None
        )

        ranked: list[list[int]] = []
        w: list[float] = []
        for i, q in enumerate(queries):
            # کوئری اول (سؤال خام کاربر) وزن کامل؛ بقیه کمی کمتر
            qw = 1.0 if i == 0 else 0.85
            if mode in ("dense", "hybrid"):
                ranked.append(self._dense(q, pool, allowed))
                w.append(weights[0] * qw)
            if mode in ("bm25", "hybrid"):
                ranked.append(self._sparse(q, pool, allowed))
                w.append(weights[1] * qw)

        scores = _rrf(ranked, w, rrf_k)

        # سرویس و واریانت **تقویت** می‌کنند، فیلتر نمی‌کنند.
        #
        # اول فیلتر سخت بود و اشتباه بود: سرویس را یک تماس ارزان مدل حدس
        # می‌زند و وقتی حدس غلط باشد، فیلتر پاسخ درست را کاملاً غیرقابل
        # دسترس می‌کند. نمونه‌ی واقعی: «چطور دامنه اختصاصی به باکت وصل کنم؟»
        # سرویس را dns-management-system حدس زد و صفحه‌ی
        # object-storage/add-domain را بیرون انداخت.
        # با تقویت، حدس درست کمک می‌کند و حدس غلط فقط بی‌اثر است.
        if service or variant:
            for idx in list(scores):
                c = self.chunks[idx]
                if service and c.get("service") == service:
                    scores[idx] *= 1 + SERVICE_BOOST
                if variant and c.get("variant") == variant:
                    scores[idx] *= 1 + VARIANT_BOOST

        top = sorted(scores, key=lambda i: -scores[i])[:k]
        return [Hit.from_dict(self.chunks[i], scores[i]) for i in top]

    def find(self, needle: str, limit: int = 5) -> list[dict]:
        """جستجوی زیررشته‌ای در تکه‌ها — برای پاسخ به «اصلاً این متن در ایندکس هست؟»"""
        low = needle.lower()
        out = [c for c in self.chunks if low in c["text"].lower()]
        return out[:limit]

    def variants_of(self, hits: list[Hit]) -> list[str]:
        """
        واریانت‌های موجود بین نتایج — پایه‌ی سؤال تکمیلی.

        اگر یک سؤال هم تکه‌ی «Liara CLI» برگرداند هم «Liara Console»، یعنی
        مستندات برای این کار چند روش دارد و ایجنت باید بپرسد کدام.
        """
        seen: list[str] = []
        for h in hits:
            if h.variant and h.variant not in seen:
                seen.append(h.variant)
        return seen


# ------------------------------------------------------------------ دیباگ

if __name__ == "__main__":
    import sys

    for _s in (sys.stdout, sys.stderr):
        if _s and getattr(_s, "encoding", "").lower() not in ("utf-8", "utf8"):
            try:
                _s.reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass

    r = Retriever.load()
    args = sys.argv[1:]

    if not args:
        print("python -m app.retrieval \"کوئری\"          نتایج بازیابی")
        print("python -m app.retrieval --find \"متن\"      آیا این متن در ایندکس هست؟")
        sys.exit(0)

    if args[0] == "--find":
        found = r.find(" ".join(args[1:]))
        print(f"{len(found)} تکه پیدا شد\n")
        for c in found:
            print(f"id   : {c['id']}")
            print(f"url  : {c['url']}")
            print(f"صفحه : {c['page_title']} › {c['section_title'] or '—'}")
            print(f"متن  : {c['text'][:400]}\n")
        sys.exit(0)

    query = " ".join(args)
    for mode in ("hybrid", "dense", "bm25"):
        print(f"\n──── {mode} ────")
        for i, h in enumerate(r.search(query, k=5, mode=mode), 1):
            print(f"{i}. [{h.score:.4f}] {h.page_title} › {h.section_title or '—'}")
            print(f"   {h.url}")
