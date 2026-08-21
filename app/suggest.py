"""
پیشنهاد قدم بعدی.

مسئله: کاربر بعد از خواندن پاسخ اغلب نمی‌داند **بعدش چه چیزی را باید بپرسد**.
جواب درست گرفتن ولی گیر کردن در قدم بعد، همان تیکت پشتیبانی را می‌سازد که
قرار بود حذف شود.

طراحی — سه لایه، به ترتیب ارزانی:

  ۱. اگر فرآیندی فعال است، پیشنهادها **قطعی**اند (قدم بعد / همین قدم / خروج).
     هیچ تماس مدلی لازم نیست؛ قدم‌ها از قبل معلوم‌اند.
  ۲. اگر موضوع پاسخ یکی از فرآیندهاست، یک چیپ «راهنمای قدم‌به‌قدم» اضافه
     می‌شود — باز هم بدون مدل.
  ۳. برای بقیه، یک تماس با **مدل کوچک** روی خلاصه‌ی پاسخ. سقف توکن پایین و
     ورودی بریده‌شده، چون این قابلیت نباید هزینه‌ی هر پاسخ را دوبرابر کند.

اگر لایه‌ی ۳ خطا داد یا چیز بی‌ربطی برگرداند، عناوین صفحات بازیابی‌شده به
پیشنهاد تبدیل می‌شوند. پیشنهاد نداشتن بهتر از پیشنهاد ساختگی است، ولی
پیشنهادِ برگرفته از منابع واقعی از هر دو بهتر است.
"""

from __future__ import annotations

import json
import logging

from app.contracts import Suggestion
from app.flows import Flow, ResolvedStep, start_prompt, suggestible_flow
from app.prompts import SUGGEST_SYSTEM
from app.settings import settings

log = logging.getLogger("app.suggest")

MAX_SUGGESTIONS = 3
# پاسخ‌های کوتاه‌تر از این معمولاً سؤال تکمیلی یا «پیدا نشد» هستند؛ پیشنهاد
# قدم بعدی روی آن‌ها بی‌معنی است و فقط توکن می‌سوزاند.
MIN_ANSWER_CHARS = 120
ANSWER_BUDGET = 700


def _clean(items: list[dict]) -> list[Suggestion]:
    out: list[Suggestion] = []
    seen: set[str] = set()
    for item in items:
        label = str(item.get("label") or "").strip()
        prompt = str(item.get("prompt") or "").strip() or label
        if not label or label in seen:
            continue
        seen.add(label)
        out.append(Suggestion(label=label[:80], prompt=prompt[:300], kind="ask"))
        if len(out) >= MAX_SUGGESTIONS:
            break
    return out


# ------------------------------------------------------------ فرآیند فعال

def for_flow(
    flow: Flow,
    steps: list[ResolvedStep],
    current_index: int,
) -> list[Suggestion]:
    """
    پیشنهادهای ناوبری فرآیند — قطعی، بدون تماس مدل.

    برچسب قدم بعد **عنوان واقعی همان قدم** است نه «بعدی»: کاربر باید قبل از
    کلیک بداند به کجا می‌رود.
    """
    items: list[Suggestion] = []
    nxt = next((s for s in steps if s.index == current_index + 1), None)
    if nxt:
        # بریدن اجباری است، نه احتیاط: عنوان قدم با پلتفرم پر می‌شود
        # («آماده‌سازی پروژه Python (FastAPI و سایر) …») و از سقف برچسب رد
        # می‌شود؛ آن‌وقت pydantic وسط استریم استثنا می‌دهد.
        items.append(Suggestion(
            label=f"قدم بعد: {nxt.title}"[:80],
            prompt="قدم بعد",
            kind="step",
        ))
    else:
        items.append(Suggestion(
            label="این قدم آخر است — جمع‌بندی کن",
            prompt="قدم بعد",
            kind="step",
        ))
    items.append(Suggestion(
        label="همین قدم را بیشتر توضیح بده",
        prompt="همین قدم را بیشتر توضیح بده",
        kind="step",
    ))
    items.append(Suggestion(
        label="خروج از فرآیند",
        prompt="خروج از فرآیند",
        kind="step",
    ))
    return items


# ------------------------------------------------------------ پاسخ عادی

def _from_hits(hits) -> list[Suggestion]:
    """پشتیبان قطعی: صفحه‌های مرتبطی که در بازیابی آمدند ولی جواب اصلی نبودند."""
    out: list[Suggestion] = []
    seen: set[str] = set()
    for hit in hits[1:]:
        page = (hit.page_title or "").strip()
        if not page or page in seen:
            continue
        seen.add(page)
        out.append(Suggestion(
            label=page[:80],
            prompt=f"درباره «{page}» توضیح بده"[:300],
            kind="ask",
        ))
        if len(out) >= 2:
            break
    return out


def _flow_chip(question: str, answer: str, active_flow: bool) -> list[Suggestion]:
    if active_flow:
        return []
    flow = suggestible_flow(f"{question} {answer[:400]}")
    if not flow:
        return []
    return [Suggestion(
        label=f"راهنمای قدم‌به‌قدم: {flow.title}"[:80],
        prompt=start_prompt(flow),
        kind="flow",
    )]


async def next_steps(
    question: str,
    answer: str,
    hits: list,
    *,
    active_flow: bool = False,
) -> tuple[list[Suggestion], int]:
    """
    (پیشنهادها، توکن مصرفی).

    هرگز استثنا پرتاب نمی‌کند: پیشنهاد قدم بعدی یک افزودنی است و نباید پاسخی
    را که کاربر همین الان گرفته خراب کند.
    """
    chips = _flow_chip(question, answer, active_flow)
    if len(answer.strip()) < MIN_ANSWER_CHARS:
        return chips, 0

    pages = []
    seen: set[str] = set()
    for hit in hits[:6]:
        title = (hit.page_title or "").strip()
        if title and title not in seen:
            seen.add(title)
            pages.append(f"- {title}")

    user = (
        f"سؤال کاربر:\n{question[:300]}\n\n"
        f"پاسخی که داده شد:\n{answer[:ANSWER_BUDGET]}\n\n"
        f"صفحات مستنداتی که استفاده شد:\n" + ("\n".join(pages) or "-")
    )

    try:
        from app.llm import aclient

        resp = await aclient().chat.completions.create(
            model=settings.model_fast,
            messages=[
                {"role": "system", "content": SUGGEST_SYSTEM},
                {"role": "user", "content": user},
            ],
            response_format={"type": "json_object"},
            temperature=0.3,
            max_tokens=220,
        )
        data = json.loads(resp.choices[0].message.content or "{}")
        tokens = resp.usage.total_tokens if resp.usage else 0
        items = _clean(data.get("items") or [])
    except Exception as exc:
        log.warning("suggestions failed (%s), falling back to hits",
                    type(exc).__name__)
        items, tokens = _from_hits(hits), 0

    if not items:
        items = _from_hits(hits)

    merged = chips + items
    return merged[:MAX_SUGGESTIONS + len(chips)], tokens
