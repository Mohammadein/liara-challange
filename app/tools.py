"""
ابزارهای ایجنت.

فقط دو ابزار، عمداً. هر ابزار اضافه یعنی یک دور LLM بیشتر، تأخیر بیشتر و
ریسک رگرسیون روی تست‌هایی که الان سبزند. این دو چیزی می‌کنند که بازیابیِ
تنها نمی‌تواند:

  list_variants   گزینه‌های سؤال تکمیلی را از خودِ داده می‌گیرد، نه از حدس
                  مدل. یعنی هرگز گزینه‌ای پیشنهاد نمی‌شود که مستنداتش نیست.

  diagnose_error  امضای خطا را از لاگ جدا می‌کند. جستجو با کل لاگ شکست
                  می‌خورد چون نویز (نام فایل، نسخه، مسیر) بر عبارت واقعی
                  خطا غلبه می‌کند.

هر دو داخل خودشان جستجو می‌کنند، پس به search_docs جداگانه نیاز نیست.
"""

from __future__ import annotations

import json
import logging
import re

from app.retrieval import Hit, Retriever

log = logging.getLogger("app.tools")

MAX_EXCERPT_CHARS = 700

TOOL_LABELS = {
    "list_variants": "بررسی روش‌های موجود",
    "diagnose_error": "تحلیل خطا",
    # ابزار نیست، ولی از همین کانال نمایش داده می‌شود تا کاربر ببیند ایجنت
    # کجای فرآیند ایستاده.
    "flow_step": "قدم فرآیند",
}

TOOL_SPECS = [
    {
        "type": "function",
        "function": {
            "name": "list_variants",
            "description": (
                "Find which platform/method/language variants Liara's docs "
                "cover for a topic, and optionally fetch the excerpts for one "
                "of them.\n\n"
                "Call this WITHOUT a variant when the user did not say which "
                "platform, framework, language, OS or deployment method they "
                "use AND the steps differ between them. Present the returned "
                "options to the user and ask which one — never invent options.\n\n"
                "Call it WITH a variant once the user has chosen, to get the "
                "excerpts for just that variant."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "topic": {
                        "type": "string",
                        "description": "The topic in Persian, e.g. "
                                       "'استقرار برنامه' or 'اتصال به object storage'",
                    },
                    "variant": {
                        "type": "string",
                        "description": "Optional. One of the variants previously "
                                       "returned, e.g. 'Liara CLI' or 'Python'.",
                    },
                },
                "required": ["topic"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "diagnose_error",
            "description": (
                "Diagnose a build, deployment or runtime error from a log. "
                "Call this whenever the user pastes an error message, stack "
                "trace or build log. It extracts the actual error signature "
                "and searches the docs for that specific failure, which works "
                "far better than searching the whole log."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "log": {
                        "type": "string",
                        "description": "The error output, as pasted by the user.",
                    },
                    "platform": {
                        "type": "string",
                        "description": "Optional: django, flask, nodejs, docker, …",
                    },
                },
                "required": ["log"],
            },
        },
    },
]


# ------------------------------------------------------------ امضای خطا

# به ترتیب اهمیت. اولین الگویی که بگیرد، همان امضای خطاست.
_ERROR_PATTERNS = [
    r"Could not find a version that satisfies the requirement \S+",
    r"ModuleNotFoundError: No module named '[^']+'",
    r"ImportError: [^\n]+",
    r"npm ERR! [^\n]+",
    r"ERROR: [^\n]+",
    r"FATAL: [^\n]+",
    r"[A-Za-z_.]*Error: [^\n]+",
    r"[A-Za-z_.]*Exception: [^\n]+",
    r"WORKER TIMEOUT[^\n]*",
    r"exited with code \d+",
    r"permission denied[^\n]*",
    r"connection refused[^\n]*",
    r"[45]\d\d [A-Za-z ]+",
]

# نویزی که بین لاگ‌ها فرق می‌کند و جستجو را خراب می‌کند
_NOISE = [
    (r"/[\w./-]{8,}", " "),            # مسیر فایل
    (r"\b[0-9a-f]{8,}\b", " "),        # هش
    (r"\b\d{1,3}(\.\d{1,3}){3}\b", " "),  # IP
    (r"line \d+", " "),
]


def error_signature(log: str) -> str:
    """
    خط یا خطوطی از لاگ که واقعاً خطا را توصیف می‌کنند.

    جستجو با کل لاگ شکست می‌خورد: در یک تست واقعی، عبارت
    `fastapi==0.115.6` باعث شد صفحات FastAPI بالا بیایند به‌جای صفحه‌ی
    mirror که راه‌حل واقعی بود.
    """
    text = log.strip()
    found: list[str] = []
    for pattern in _ERROR_PATTERNS:
        for m in re.finditer(pattern, text, flags=re.IGNORECASE):
            line = m.group(0).strip()
            if line not in found:
                found.append(line)
        if found:
            break

    if not found:
        # الگویی نگرفت: چند خط آخر معمولاً حاوی خطا هستند
        lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
        found = lines[-3:]

    sig = " ".join(found[:3])
    for pattern, repl in _NOISE:
        sig = re.sub(pattern, repl, sig)
    return re.sub(r"\s+", " ", sig).strip()[:300]


async def _symptom_query(text: str) -> str:
    """
    خطای انگلیسی → توصیف فارسی همان مشکل، به زبان مستندات.

    یک تماس ارزان با مدل کوچک. اگر شکست بخورد، رشته‌ی خالی برمی‌گردد و
    جستجو با امضای خام ادامه می‌یابد — ترجمه‌ی ناموفق نباید ابزار را بکشد.
    """
    if not text.strip():
        return ""
    try:
        from app.llm import aclient
        from app.prompts import SYMPTOM_SYSTEM
        from app.settings import settings

        resp = await aclient().chat.completions.create(
            model=settings.model_fast,
            messages=[
                {"role": "system", "content": SYMPTOM_SYSTEM},
                {"role": "user", "content": text[:600]},
            ],
            temperature=0,
            max_tokens=60,
        )
        return (resp.choices[0].message.content or "").strip().strip('"')
    except Exception as exc:
        log.warning("symptom translation failed: %s", type(exc).__name__)
        return ""


# ------------------------------------------------------------ جعبه ابزار

class ToolBox:
    """
    اجرای ابزارها و جمع‌آوری تکه‌های بازیابی‌شده.

    تکه‌ها جمع می‌شوند تا کارت منابع در پایان شامل هرچه ایجنت واقعاً دیده
    باشد، نه فقط نتایج جستجوی اول.
    """

    def __init__(self, retriever: Retriever, k: int = 8) -> None:
        self.r = retriever
        self.k = k
        self.collected: list[Hit] = []

    def _collect(self, hits: list[Hit]) -> None:
        seen = {h.id for h in self.collected}
        self.collected.extend(h for h in hits if h.id not in seen)

    def _excerpts(self, hits: list[Hit]) -> list[dict]:
        return [
            {
                "page": h.page_title,
                "section": h.section_title or None,
                "variant": h.variant,
                "url": h.url,
                "text": h.text[:MAX_EXCERPT_CHARS],
            }
            for h in hits
        ]

    # ---------------------------------------------------------- ابزارها

    def list_variants(self, topic: str, variant: str | None = None) -> dict:
        if variant:
            hits = self.r.search(topic, k=self.k, variant=variant)
            self._collect(hits)
            return {
                "topic": topic,
                "variant": variant,
                "excerpts": self._excerpts(hits[:5]),
            }

        # جستجوی عمیق‌تر از حد معمول: دنبال تنوع می‌گردیم نه بهترین تطابق
        hits = self.r.search(topic, k=max(self.k * 3, 24))
        variants = self.r.variants_of(hits)
        self._collect(hits[:self.k])

        return {
            "topic": topic,
            "variants": variants,
            "note": (
                "این گزینه‌ها از خود مستندات آمده‌اند. فقط از همین‌ها به کاربر "
                "پیشنهاد بده و گزینه‌ی دیگری اضافه نکن."
                if variants else
                "مستندات برای این موضوع نسخه‌های متفاوتی ندارد؛ نیازی به "
                "پرسیدن از کاربر نیست."
            ),
            "excerpts": self._excerpts(hits[:4]),
        }

    async def diagnose_error(self, log: str, platform: str | None = None) -> dict:
        sig = error_signature(log)
        symptom = await _symptom_query(sig or log[:300])

        # سه کوئری با هم: امضای خطا، ترجمه‌ی فارسی علامت، و لاگ خام.
        #
        # امضا معمولاً انگلیسی است ولی مستندات لیارا همان خطا را فارسی
        # توصیف می‌کند، پس تطابق واژگانی صفر است. نمونه‌ی واقعی:
        # «Could not find a version that satisfies...» هیچ‌وقت صفحه‌ی
        # mirror را پیدا نمی‌کرد و پاسخ می‌شد «نسخه دیگری امتحان کن» —
        # که راه‌حل واقعی لیارا نیست.
        queries = [q for q in (symptom, sig, log[:300]) if q]
        if platform:
            queries.insert(0, f"{symptom or sig} {platform}")

        hits = self.r.search(queries, k=self.k)
        self._collect(hits)

        return {
            "error_signature": sig or "(الگوی مشخصی پیدا نشد)",
            "symptom_query": symptom,
            "excerpts": self._excerpts(hits[:6]),
            "note": (
                "اگر این متن‌ها علت خطا را پوشش نمی‌دهند، صادقانه بگو که در "
                "مستندات لیارا موردی پیدا نشد. راه‌حل از خودت نساز."
            ),
        }

    # ---------------------------------------------------------- اجرا

    async def run(self, name: str, args: dict) -> str:
        try:
            if name == "list_variants":
                result = self.list_variants(
                    str(args.get("topic", "")), args.get("variant")
                )
            elif name == "diagnose_error":
                result = await self.diagnose_error(
                    str(args.get("log", "")), args.get("platform")
                )
            else:
                result = {"error": f"ابزار ناشناخته: {name}"}
        except Exception as exc:
            log.exception("tool %s failed", name)
            # خطای ابزار نباید کل پاسخ را از بین ببرد؛ مدل باید بتواند
            # بدون آن ادامه بدهد.
            result = {"error": f"{type(exc).__name__}", "recoverable": True}

        return json.dumps(result, ensure_ascii=False)
