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
from app.text_norm import tokenize

log = logging.getLogger("app.retrieval")

# ثابت RRF. عدد بزرگ‌تر یعنی تفاوت رتبه‌های بالا کم‌اهمیت‌تر.
# ۶۰ مقدار استاندارد مقاله‌ی اصلی است.
RRF_K = 60


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


def _rrf(ranked_lists: list[list[int]], weights: list[float]) -> dict[int, float]:
    """Reciprocal Rank Fusion — امتیاز هر سند = مجموع w/(K+rank)."""
    scores: dict[int, float] = {}
    for ranking, w in zip(ranked_lists, weights):
        for rank, idx in enumerate(ranking, start=1):
            scores[idx] = scores.get(idx, 0.0) + w / (RRF_K + rank)
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

    def _dense(self, query: str, n: int) -> list[int]:
        from app.llm import embed_query_np

        sims = self.vectors @ embed_query_np(query)     # کل جستجو، همین یک خط
        return np.argsort(-sims)[:n].tolist()

    def _sparse(self, query: str, n: int) -> list[int]:
        tokens = tokenize(query)
        if not tokens:
            return []
        scores = self.bm25.get_scores(tokens)
        return np.argsort(-scores)[:n].tolist()

    def search(
        self,
        query: str,
        k: int | None = None,
        *,
        mode: str = "hybrid",
        service: str | None = None,
        variant: str | None = None,
    ) -> list[Hit]:
        """
        mode: hybrid | dense | bm25
        service/variant: فیلتر اختیاری — وقتی از context مکالمه می‌دانیم
        کاربر روی چه سرویسی یا با چه فریم‌ورکی کار می‌کند.
        """
        k = k or settings.top_k
        pool = max(k * 6, 40)      # عمیق‌تر بازیابی کن، بعد ترکیب و برش بزن

        if mode == "dense":
            ranked, weights = [self._dense(query, pool)], [1.0]
        elif mode == "bm25":
            ranked, weights = [self._sparse(query, pool)], [1.0]
        else:
            ranked = [self._dense(query, pool), self._sparse(query, pool)]
            # وزن بیشتر به برداری: سؤالات کاربران معمولاً توصیفی‌اند و
            # کلیدواژه‌ی دقیق مستندات را ندارند.
            weights = [1.0, 0.7]

        scores = _rrf(ranked, weights)

        hits: list[Hit] = []
        for idx in sorted(scores, key=lambda i: -scores[i]):
            c = self.chunks[idx]
            if service and c.get("service") != service:
                continue
            if variant and c.get("variant") != variant:
                continue
            hits.append(Hit.from_dict(c, scores[idx]))
            if len(hits) >= k:
                break
        return hits

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
