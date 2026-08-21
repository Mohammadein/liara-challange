"""
حلقه‌ی پاسخ‌دهی.

    سؤال کاربر
        ↓  بازنویسی با مدل کوچک  (رفع ابهام + واژگان مستندات + تشخیص سرویس)
        ├─ اگر مبهم بود → سؤال تکمیلی، بدون هیچ بازیابی و بدون مدل بزرگ
        ↓
    بازیابی هیبریدی
        ↓
    پاسخ استریمی با مدل بزرگ
        ↓
    کارت منابع

چرا بازنویسی جدا و با مدل کوچک: کاربر علائم را با زبان روزمره توصیف می‌کند
ولی مستندات اصطلاح فنی دارد. «چند تا ورکر بذارم؟» هیچ کلمه‌ی مشترکی با
«gunicorn workers» ندارد و هیچ روش بازیابی‌ای این شکاف را پر نمی‌کند.
یک تماس ارزان قبل از جستجو، این را حل می‌کند.
"""

from __future__ import annotations

import json
import logging
import re
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass

from app import flows, suggest
from app.contracts import (
    DoneEvent,
    ErrorEvent,
    FlowEvent,
    Source,
    SourcesEvent,
    Suggestion,
    SuggestionsEvent,
    TokenEvent,
    ToolEvent,
    sse,
)
from app.flows import (
    DATABASE_ENGINE_ALIASES,
    DATABASE_URL_SLUGS,
    PROFILE_DATABASE_NAMES,
    FlowState,
)
from app.llm import LLMUnavailable, aclient
from app.prompts import (
    ANSWER_SYSTEM,
    FLOW_STEP_SYSTEM,
    PLAN_SYSTEM,
    REWRITE_SYSTEM,
    build_context,
)
from app.retrieval import Hit, Retriever
from app.tools import TOOL_SPECS, ToolBox
from app.session import Session, sessions
from app.settings import settings
from app.text_norm import normalize

log = logging.getLogger("app.agent")

# سقف دورهای ابزار. هر دور یک تماس LLM است.
MAX_TOOL_ROUNDS = 2

_retriever: Retriever | None = None

_DATABASE_TERMS = (
    "دیتابیس", "پایگاه داده", "database", " db ",
)
_DATABASE_ACTIONS = (
    "راه اندازی", "مستقر", "دیپلوی", "deploy", "نصب", "ایجاد", "ساخت",
    "بساز", "وصل", "اتصال", "connect", "بکاپ", "backup", "بازیابی", "restore",
)
# نگاشت‌های دیتابیس در app/flows.py زندگی می‌کنند: هم مسیریابی سؤال (همین
# فایل) و هم فرآیند «راه‌اندازی دیتابیس» به آن‌ها نیاز دارند و دو نسخه‌ی
# جداگانه دیر یا زود از هم جدا می‌افتند.
_DATABASE_ENGINE_ALIASES = DATABASE_ENGINE_ALIASES
_PROFILE_DATABASE_NAMES = PROFILE_DATABASE_NAMES
_DATABASE_URL_SLUGS = DATABASE_URL_SLUGS
# نام هر موتور دیتابیس، صاف‌شده — برای تست «این پیام درباره‌ی دیتابیس است؟»
DATABASE_ENGINE_ALIASES_FLAT = tuple(
    alias for aliases in DATABASE_ENGINE_ALIASES.values() for alias in aliases
)
_DATABASE_SETUP_ACTIONS = (
    "راه اندازی", "مستقر", "دیپلوی", "deploy", "نصب", "ایجاد", "ساخت", "بساز",
)


def _database_engine(text: str, session: Session) -> str | None:
    profile_database = getattr(session.profile, "database", None)
    if profile_database in _PROFILE_DATABASE_NAMES:
        return _PROFILE_DATABASE_NAMES[profile_database]
    for canonical, aliases in _DATABASE_ENGINE_ALIASES.items():
        if any(alias in text for alias in aliases):
            return canonical
    return None


def database_setup_route(
    question: str, session: Session,
) -> tuple[str, str, str] | None:
    """Return the canonical quick-setup query after the DB engine is known."""
    current = f" {normalize(question)} "
    recent_user_text = " ".join(
        turn["content"] for turn in session.turns[-8:] if turn["role"] == "user"
    )
    evidence = f" {normalize(recent_user_text + ' ' + question)} "
    engine = _database_engine(evidence, session)
    if not engine:
        return None

    last_assistant = next(
        (turn["content"] for turn in reversed(session.turns)
         if turn["role"] == "assistant"),
        "",
    )
    answers_engine_question = (
        "کدام دیتابیس" in last_assistant and _database_engine(current, session) is not None
    )
    setup_requested = any(action in current for action in _DATABASE_SETUP_ACTIONS)
    if not setup_requested and not answers_engine_question:
        return None

    if engine == "SQLite":
        return (
            "استفاده از دیتابیس SQLite با دیسک دائمی در برنامه لیارا",
            "paas", "/paas/",
        )
    slug = _DATABASE_URL_SLUGS[engine]
    return (
        f"راه‌اندازی سریع دیتابیس {engine} با کنسول لیارا",
        "dbaas", f"/dbaas/{slug}/",
    )


def python_version_route(question: str) -> tuple[str, str, str] | None:
    """Route explicit Python-version questions away from one-click app pages."""
    text = f" {normalize(question)} "
    mentions_python = "python" in text or "پایتون" in text
    mentions_version = "نسخه" in text or "version" in text or "ورژن" in text
    if not (mentions_python and mentions_version):
        return None

    if "django" in text or "جنگو" in text:
        platform, slug = "Django", "django"
    elif "flask" in text or "فلسک" in text:
        platform, slug = "Flask", "flask"
    else:
        platform, slug = "Python", "python"
    return (
        f"تغییر نسخه پیش‌فرض Python در پلتفرم {platform} با فایل liara.json",
        "paas", f"/paas/{slug}/",
    )


def liara_cli_route(
    question: str, session: Session,
) -> tuple[str, str, str] | None:
    """Keep short CLI follow-ups anchored to the CLI documentation."""
    current = f" {normalize(question)} "
    recent_users = [
        normalize(turn["content"])
        for turn in session.turns[-8:] if turn["role"] == "user"
    ]
    previous = f" {' '.join(recent_users[-2:])} "
    mentions_cli = any(term in current for term in (" liara cli ", " cli ", "خط فرمان"))
    cli_context = mentions_cli or any(
        term in previous for term in ("liara cli", " cli ", "خط فرمان")
    )
    if not cli_context:
        return None

    install_terms = (
        "نصب", "راه اندازی", "ستاپ", "setup", "install", "اپدیت", "به روز",
    )
    capability_terms = (
        "امکانات", "قابلیت", "چه کار", "کارایی", "دستورات", "command",
    )
    if any(term in current for term in install_terms):
        return (
            "نصب و به‌روزرسانی Liara CLI",
            "references", "/references/cli/install/",
        )
    if any(term in current for term in capability_terms):
        return (
            "معرفی امکانات و دسته‌بندی دستورهای Liara CLI",
            "references", "/references/cli/about/",
        )
    return None


_DEPLOY_INTENTS = (
    "دیپلوی", "استقرار", "مستقر", "deploy", "بیارم بالا", "بالا بیارم",
    "بیاورم بالا", "بالا بیاورم", "راه اندازی", "اجرا کنم", "میزبانی",
    "هاست کنم", "منتشر کنم",
)

# اگر پیام درباره‌ی سرویس دیگری است، «استقرار برنامه» جواب آن نیست.
#
# بدون این تست یک رگرسیون واقعی داشتیم: کاربری با پروفایل Django که
# می‌پرسید «دیتابیس رو چطور راه بندازم؟» به صفحه‌ی استقرار Django هدایت
# می‌شد، چون هم پلتفرم معلوم بود و هم «راه اندازی» یک نیت استقرار است.
_NON_PAAS_TERMS = (
    "دیتابیس", "پایگاه داده", "database", " db ", "باکت", "bucket",
    "object storage", "ذخیره سازی", "دامنه", "dns", "ایمیل", "email",
    "کرون", "cron", "دیسک", "disk", "شبکه خصوصی",
)

# پلتفرم پروفایل فقط وقتی به کار می‌آید که پیام واقعاً درباره‌ی «خود برنامه»
# باشد، نه هر جمله‌ای که فعل راه‌اندازی دارد.
_APP_NOUNS = (
    "برنامه", "اپ", "اپلیکیشن", "سرویس", "سایت", "پروژه", "app",
)


def platform_deploy_route(
    question: str, session: Session,
) -> tuple[str, str, str] | None:
    """
    «می‌خوام X رو بیارم بالا» وقتی X یک پلتفرم شناخته‌شده است.

    این مسیر قاعده‌محور است چون کاربر **قبلاً جواب داده**. وقتی کسی می‌گوید
    «سرویس مبتنی بر داکر»، پلتفرم معلوم است و پرسیدن «چه نوع سرویسی؟» یعنی
    حرفش شنیده نشده. مدل بازنویسی گاهی این را ابهام می‌بیند؛ اینجا جلویش
    گرفته می‌شود.
    """
    from app.flows import _detect_platform
    from app.project import PLATFORMS

    current = f" {normalize(question)} "
    if any(term in current for term in _NON_PAAS_TERMS):
        return None
    if not any(term in current for term in _DEPLOY_INTENTS):
        return None
    if any(term in current for term in DATABASE_ENGINE_ALIASES_FLAT):
        return None

    # پلتفرمی که در همین پیام آمده، بر پروفایل مقدم است — کاربر ممکن است
    # درباره‌ی پروژه‌ی دومش بپرسد.
    platform = _detect_platform(current)
    if not platform and any(noun in current for noun in _APP_NOUNS):
        platform = getattr(session.profile, "platform", None)
    if not platform:
        return None

    # برچسب فهرست فرم توضیح داخل پرانتز دارد («Docker (هر چیز دیگر)») که در
    # یک کوئری جستجو فقط نویز است.
    label = re.sub(r"\s*\(.*?\)", "", PLATFORMS.get(platform, platform)).strip()
    return (
        f"استقرار برنامه {label} در لیارا",
        "paas",
        f"/paas/{platform}/",
    )


def usable_clarification(clarify: str | None, options: list[str]) -> bool:
    """
    آیا این سؤال تکمیلی ارزش یک رفت‌وبرگشت را دارد؟

    معیار: باید گزینه داشته باشد. سؤال بی‌گزینه («چه نوع سرویسی می‌خواهید؟»)
    کار را به کاربر پس می‌دهد بدون اینکه بگوید جواب‌های ممکن چیست — و کاربر
    دقیقاً همان چیزی را دوباره می‌نویسد که بار اول نوشته بود.

    این تست اینجاست نه فقط در پرامپت: پرامپت یک درخواست است، این یک تضمین.
    """
    if not clarify or not clarify.strip():
        return False
    return len([o for o in options if str(o).strip()]) >= 2


def database_clarification(question: str, session: Session) -> str | None:
    """Ask for the engine before engine-specific database instructions.

    This is deterministic because searching first can mix quick-setup, restore,
    and connection pages from several database engines into an unsafe answer.
    """
    current = f" {normalize(question)} "
    recent_user_text = " ".join(
        turn["content"] for turn in session.turns[-8:] if turn["role"] == "user"
    )
    evidence = f" {normalize(recent_user_text + ' ' + question)} "

    engine_known = _database_engine(evidence, session) is not None
    database_topic = any(term in evidence for term in _DATABASE_TERMS)
    action_needs_engine = any(term in current for term in _DATABASE_ACTIONS)

    if database_topic and action_needs_engine and not engine_known:
        return (
            "کدام دیتابیس را می‌خواهید راه‌اندازی کنید؟ "
            "PostgreSQL، MySQL، MariaDB، MongoDB، Redis، SQL Server، "
            "Elasticsearch یا RabbitMQ؟"
        )
    return None


def get_retriever() -> Retriever:
    global _retriever
    if _retriever is None:
        _retriever = Retriever.load()
    return _retriever


def index_ready() -> bool:
    try:
        get_retriever()
        return True
    except Exception:
        return False


# ------------------------------------------------------------ بازنویسی

async def rewrite(question: str, session: Session) -> dict:
    """
    سؤال → {query, service, clarify, options}

    اگر مدل کوچک خطا داد یا JSON بی‌ربط برگرداند، به خود سؤال برمی‌گردیم.
    یک بازنویسی ناموفق نباید کل پاسخ را از بین ببرد.

    `options` گزینه‌های سؤال تکمیلی‌اند. سؤال تکمیلی بدون گزینه اصلاً منتشر
    نمی‌شود — به `usable_clarification` مراجعه کنید.
    """
    deterministic_clarification = database_clarification(question, session)
    if deterministic_clarification:
        return {
            "query": "راه‌اندازی دیتابیس در لیارا",
            "service": "dbaas",
            "clarify": deterministic_clarification,
            "options": [
                "PostgreSQL", "MySQL", "MariaDB", "MongoDB",
                "Redis", "SQL Server", "Elasticsearch", "RabbitMQ",
            ],
            "tokens": 0,
        }

    deterministic_setup = database_setup_route(question, session)
    if deterministic_setup:
        query, service, url_prefix = deterministic_setup
        return {
            "query": query, "service": service, "clarify": None,
            "tokens": 0, "canonical": True, "url_prefix": url_prefix,
        }

    deterministic_python_version = python_version_route(question)
    if deterministic_python_version:
        query, service, url_prefix = deterministic_python_version
        return {
            "query": query, "service": service, "clarify": None,
            "tokens": 0, "canonical": True, "url_prefix": url_prefix,
        }

    deterministic_cli = liara_cli_route(question, session)
    if deterministic_cli:
        query, service, url_prefix = deterministic_cli
        return {
            "query": query, "service": service, "clarify": None,
            "tokens": 0, "canonical": True, "url_prefix": url_prefix,
        }

    # آخرین مسیر قاعده‌محور، عمداً: عام‌ترین‌شان است و نباید جلوی مسیرهای
    # دقیق‌تر (دیتابیس، نسخه‌ی پایتون، CLI) را بگیرد.
    deterministic_platform = platform_deploy_route(question, session)
    if deterministic_platform:
        query, service, url_prefix = deterministic_platform
        return {
            "query": query, "service": service, "clarify": None,
            "tokens": 0, "canonical": True, "url_prefix": url_prefix,
        }

    history = session.transcript()
    user = f"مکالمه تا اینجا:\n{history}\n\nسؤال جدید: {question}" if history else question

    try:
        resp = await aclient().chat.completions.create(
            model=settings.model_fast,
            messages=[
                {"role": "system", "content": REWRITE_SYSTEM},
                {"role": "user", "content": user},
            ],
            response_format={"type": "json_object"},
            temperature=0,
            # ۳۰۰ نه ۲۰۰: حالا `options` هم در همین JSON می‌آید و بریده شدن
            # خروجی یعنی JSON ناقص و افتادن به مسیر پشتیبان.
            max_tokens=300,
        )
        data = json.loads(resp.choices[0].message.content or "{}")
        usage = resp.usage.total_tokens if resp.usage else 0
    except Exception as exc:
        log.warning("rewrite failed (%s), falling back to raw question", type(exc).__name__)
        return {"query": question, "service": None, "clarify": None, "tokens": 0}

    clarify = (data.get("clarify") or "").strip() or None
    raw_options = data.get("options")
    options = [str(o).strip() for o in raw_options if str(o).strip()] \
        if isinstance(raw_options, list) else []

    # سؤال تکمیلیِ بی‌گزینه انداخته می‌شود و به جستجوی عادی برمی‌گردیم.
    # یک پاسخ که چند حالت را پوشش می‌دهد، از یک سؤالِ بن‌بست بهتر است.
    if clarify and not usable_clarification(clarify, options):
        log.info("dropped option-less clarification: %r", clarify[:80])
        clarify, options = None, []

    return {
        "query": (data.get("query") or question).strip(),
        "service": data.get("service") or None,
        "clarify": clarify,
        "options": options,
        "tokens": usage,
    }


# ------------------------------------------------------------ منابع

def _sources(hits: list[Hit]) -> list[Source]:
    """
    یک منبع به ازای هر صفحه — نه هر تکه.

    چند تکه از یک صفحه، یک لینک است. نمایش پنج کارت که همه به یک صفحه
    اشاره می‌کنند، به کاربر حس پوشش کاذب می‌دهد.
    """
    out: list[Source] = []
    seen: set[str] = set()
    for h in hits:
        if h.url in seen:
            continue
        seen.add(h.url)
        out.append(Source(
            title=h.page_title,
            section=h.section_title,
            url=h.url,
            service=h.service,
        ))
    return out


def confidence_of(hits: list[Hit]) -> str:
    """
    سیگنال اطمینان — اکتشافی و عمداً ساده، تا قابل توضیح باشد.

    مبنا: توافق بین تکه‌های بازیابی‌شده. اگر چند تکه از یک صفحه بالا آمده
    باشند، یعنی مستندات صفحه‌ی مشخصی برای این موضوع دارد. اگر نتایج
    پراکنده باشند، احتمالاً بازیابی مطمئن نبوده.

    این عدد کالیبره‌شده نیست و نباید به‌عنوان احتمال درستی خوانده شود؛
    برای ایجنت مصرف‌کننده یک راهنماست که کی به پاسخ اتکا کند و کی خودش
    excerpts را بخواند.
    """
    if not hits:
        return "none"
    top_url = hits[0].url.split("#")[0]
    same_page = sum(1 for h in hits if h.url.split("#")[0] == top_url)
    if same_page >= 2:
        return "high"
    if len(hits) >= 3:
        return "medium"
    return "low"


@dataclass
class AnswerResult:
    answer: str
    hits: list[Hit]
    query_used: str
    service: str | None
    clarification: str | None
    confidence: str
    tokens: int
    latency_ms: int


async def answer_once(
    question: str,
    *,
    session_id: str | None = None,
    platform: str | None = None,
    k: int | None = None,
) -> AnswerResult:
    """
    همان مسیر chat_stream ولی یکجا — برای API برنامه‌نویسی.

    نکته‌ی طراحی: پاسخِ ایجنت‌ها با پاسخِ انسان فرق دارد. یک ایجنت کدنویس
    به متن خام مستندات و سیگنال اطمینان نیاز دارد تا خودش قضاوت کند، نه
    فقط یک پاراگراف روان. هر دو برگردانده می‌شوند.
    """
    started = time.perf_counter()
    session = sessions.get(session_id) if session_id else Session(id="_stateless")
    tokens = 0

    plan = await rewrite(question, session)
    tokens += plan["tokens"]

    if plan["clarify"]:
        if session_id:
            if plan["service"]:
                session.service = plan["service"]
            session.add("user", question)
            session.add("assistant", plan["clarify"])
        return AnswerResult(
            answer="", hits=[], query_used=plan["query"],
            service=plan["service"], clarification=plan["clarify"],
            confidence="none", tokens=tokens,
            latency_ms=int((time.perf_counter() - started) * 1000),
        )

    # راهنمای پلتفرم از سمت فراخواننده به کوئری اضافه می‌شود تا نسخه‌ی
    # درست صفحه (django/flask/nodejs) بالا بیاید.
    query = f"{plan['query']} {platform}" if platform else plan["query"]
    service = plan["service"] or session.service

    retriever = get_retriever()
    search_queries = [query] if plan.get("canonical") else [question, query]
    hits = retriever.search(
        search_queries, k=k or settings.top_k, service=service,
        url_prefix=plan.get("url_prefix"),
    )
    if not hits and plan.get("url_prefix"):
        hits = retriever.search(
            search_queries, k=k or settings.top_k, service=service,
        )

    resp = await aclient().chat.completions.create(
        model=settings.model_answer,
        messages=[
            {"role": "system", "content": ANSWER_SYSTEM},
            *session.history(),
            {"role": "user", "content":
                f"# متن مستندات\n\n{build_context(hits)}\n\n# سؤال کاربر\n\n{question}"},
        ],
        temperature=0.2,
        max_tokens=900,
    )
    answer = resp.choices[0].message.content or ""
    tokens += resp.usage.total_tokens if resp.usage else 0

    if session_id:
        session.add("user", question)
        session.add("assistant", answer, sources=[s.model_dump() for s in _sources(hits)])
        if service:
            session.service = service
        session.save()

    return AnswerResult(
        answer=answer, hits=hits, query_used=query, service=service,
        clarification=None, confidence=confidence_of(hits), tokens=tokens,
        latency_ms=int((time.perf_counter() - started) * 1000),
    )


async def build_plan(profile, services: list[dict]) -> dict:
    """
    پروفایل پروژه → نقشه‌ی استقرار.

    برای هر سرویس لازم یک بازیابی جدا انجام می‌شود، نه یک جستجوی کلی.
    دلیلش: «دیتابیس postgres» و «آپلود فایل» و «کرون‌جاب» سه موضوع کاملاً
    متفاوت‌اند و یک کوئری ترکیبی برای هیچ‌کدام تکه‌ی خوبی نمی‌آورد.
    """
    started = time.perf_counter()
    retriever = get_retriever()

    # بازیابی هدفمند به ازای هر سرویس.
    #
    # سهم را با تعداد سرویس‌ها تقسیم نمی‌کنیم: با ۵ سرویس، هرکدام ۲ تکه
    # می‌گرفت و مدل چیزی برای گفتن نداشت، پس چهار قدم پشت سر هم می‌نوشت
    # «به مستندات مراجعه کنید». نقشه‌ای که جزئیات ندارد، نقشه نیست.
    per_service = 4
    hits: list[Hit] = []
    seen: set[str] = set()

    queries = [f"{s['title']} در لیارا" for s in services]
    if profile.platform:
        queries.append(f"استقرار برنامه {profile.platform}")

    for q in queries:
        for h in retriever.search(q, k=per_service, variant=profile.variant_hint):
            if h.id not in seen:
                seen.add(h.id)
                hits.append(h)

    # لینک هر سرویس به مدل داده می‌شود تا قدم‌های بی‌جزئیات دست‌کم لینک
    # درست داشته باشند، نه «به مستندات مراجعه کنید» بدون گفتن کجا.
    service_list = "\n".join(
        f"- {s['title']} ({s['why']}) — {s['url']}" for s in services
    )
    user_msg = (
        f"# پروفایل پروژه\n{profile.as_context()}\n\n"
        f"# سرویس‌های لازم (قطعی، تغییرشان نده)\n{service_list}\n\n"
        f"# متن مستندات\n{build_context(hits)}"
    )

    resp = await aclient().chat.completions.create(
        model=settings.model_answer,
        messages=[
            {"role": "system", "content": PLAN_SYSTEM},
            {"role": "user", "content": user_msg},
        ],
        temperature=0.3,
        max_tokens=1600,
    )

    return {
        "plan": resp.choices[0].message.content or "",
        "sources": [s.model_dump() for s in _sources(hits)],
        "tokens": resp.usage.total_tokens if resp.usage else 0,
        "latency_ms": int((time.perf_counter() - started) * 1000),
    }


def _tool_detail(name: str, raw_result: str) -> str:
    """
    خلاصه‌ی نتیجه‌ی ابزار برای نمایش در UI.

    قابلیت Agentic که دیده نشود امتیاز نمی‌گیرد. داور باید روی صفحه ببیند
    «بررسی روش‌های موجود — Liara CLI، Console، Github» نه یک اسپینر خالی.
    """
    try:
        data = json.loads(raw_result)
    except Exception:
        return ""

    if data.get("error"):
        return "ناموفق"
    if name == "list_variants":
        variants = data.get("variants")
        if variants:
            return "، ".join(variants[:4])
        if data.get("variant"):
            return str(data["variant"])
        return "بدون تفکیک"
    if name == "diagnose_error":
        return str(data.get("error_signature", ""))[:80]
    return ""


# ------------------------------------------------------------ فرآیند چندمرحله‌ای

def flow_decision(
    question: str, session: Session,
) -> tuple[str, flows.Flow, FlowState] | None:
    """
    آیا این نوبت مکالمه، یک حرکت در فرآیند است؟

    خروجی: (کنش، فرآیند، وضعیت) یا None برای مسیر عادی پاسخ‌دهی.

    ترتیب بررسی مهم است: «خروج» قبل از «بعدی» می‌آید چون «بی‌خیال فرآیند»
    هم واژه‌ی فرآیند دارد هم می‌تواند با ادامه اشتباه گرفته شود.
    """
    state = session.flow
    flow = flows.flow_by_id(state.id) if state else None

    if state and flow:
        if flows.exit_intent(question):
            return "exit", flow, state
        if flows.next_intent(question):
            return "next", flow, state
        if flows.previous_intent(question):
            return "prev", flow, state
        if flows.restate_intent(question):
            return "stay", flow, state
        # سؤال آزاد وسط فرآیند: مسیر عادی جواب می‌دهد، ولی stepper باقی
        # می‌ماند. پرت کردن کاربر از سؤالش به قدم بعدی، کمک نیست.
        return None

    matched = flows.match_flow(question)
    if matched:
        return "start", matched, FlowState(id=matched.id)
    return None


def _flow_search(
    step: flows.ResolvedStep, session: Session, question: str, extra: bool,
) -> list[Hit]:
    """
    بازیابی برای یک قدم — با فیلتر مسیر، و بدون آن اگر چیزی نگرفت.

    فیلتر مسیر **نرم** است چون مسیرهای مستندات عوض می‌شوند. یک قدم خالی در
    وسط فرآیند بدتر از یک قدم با منبع کمی عام‌تر است.
    """
    retriever = get_retriever()
    profile = session.profile
    queries = [step.query]
    if extra:
        queries.append(question)
    variant = session.variant or (profile.variant_hint if profile else None)

    hits = retriever.search(
        queries, k=settings.top_k, service=step.service,
        variant=variant, url_prefix=step.url_prefix,
    )
    if not hits and step.url_prefix:
        hits = retriever.search(
            queries, k=settings.top_k, service=step.service, variant=variant,
        )
    return hits


def _advance(state: FlowState, steps: list[flows.ResolvedStep], action: str) -> str:
    """وضعیت را جابه‌جا می‌کند و برچسب رویداد را برمی‌گرداند."""
    if action == "start":
        state.step = 0
        state.done = []
        return "started"

    if action == "next":
        if state.step < len(steps):
            key = steps[state.step].key
            if key not in state.done:
                state.done.append(key)
        state.step += 1
        return "advanced"

    if action == "prev":
        state.step = max(0, state.step - 1)
        # قدم‌هایی که به آن‌ها برگشته‌ایم دیگر «انجام‌شده» نیستند، وگرنه
        # stepper چیزی نشان می‌دهد که با جایی که کاربر ایستاده جور نیست.
        keys_ahead = {s.key for s in steps if s.index > state.step}
        state.done = [k for k in state.done if k not in keys_ahead]
        return "advanced"

    return "advanced"


async def flow_stream(
    question: str,
    session: Session,
    action: str,
    flow: flows.Flow,
    state: FlowState,
    counter: dict[str, int],
) -> AsyncIterator[str]:
    """یک نوبت از فرآیند: یک قدم، با متن بازیابی‌شده‌ی همان قدم."""
    ctx = flows.build_context(session.profile, question)
    steps = flows.resolve(flow, ctx)

    # --- خروج ---
    if action == "exit":
        session.flow = None
        session.save()
        yield sse("flow", FlowEvent(
            **flows.progress_payload(flow, steps, state, "exited")))
        message = (
            f"از فرآیند «{flow.title}» خارج شدیم. "
            "هر وقت خواستی از همان قدم ادامه بدهیم، بگو."
        )
        for word in message.split(" "):
            yield sse("token", TokenEvent(t=word + " "))
        session.add("user", question)
        session.add("assistant", message)
        return

    status = _advance(state, steps, action)

    # --- پایان فرآیند ---
    if state.step >= len(steps):
        session.flow = None
        session.save()
        yield sse("flow", FlowEvent(
            **flows.progress_payload(flow, steps, state, "completed")))
        checklist = "\n".join(f"- {s.title}" for s in steps)
        message = (
            f"فرآیند «{flow.title}» تمام شد. چیزی که پشت سر گذاشتی:\n\n"
            f"{checklist}\n\n"
            "اگر جایی از این مسیر گیر کردی، همان قدم را اسم ببر تا برگردیم."
        )
        for word in message.split(" "):
            yield sse("token", TokenEvent(t=word + " "))
        session.add("user", question)
        session.add("assistant", message)
        yield sse("suggestions", SuggestionsEvent(items=[]))
        return

    step = steps[state.step]
    session.flow = state
    if step.service:
        session.service = step.service
    session.save()

    yield sse("flow", FlowEvent(
        **flows.progress_payload(flow, steps, state, status)))
    yield sse("tool", ToolEvent(
        name="flow_step", status="done",
        detail=f"قدم {step.index} از {len(steps)} — {step.title}",
    ))
    yield sse("tool", ToolEvent(
        name="search_docs", status="running", detail=step.query))

    hits = _flow_search(step, session, question, extra=(action == "stay"))
    yield sse("tool", ToolEvent(
        name="search_docs", status="done", detail=f"{len(hits)} نتیجه"))

    system = (
        FLOW_STEP_SYSTEM
        + f"\n\n## Procedure\n«{flow.title}» — {flow.summary}\n\n"
        + "## Outline (fixed, do not change)\n"
        + flows.outline(steps, step.index)
        + f"\n\n## Current step\n{step.index} از {len(steps)}: {step.title}\n"
        + f"هدف این قدم: {step.goal}\n"
        + (f"صفحه مرجع این قدم: {step.url}\n" if step.url else "")
    )
    if state.done:
        titles = [s.title for s in steps if s.key in state.done]
        system += "\n## Already done\n" + "، ".join(titles) + "\n"
    if session.profile:
        system += "\n## This user's project\n" + session.profile.as_context()

    user = (
        f"# متن مستندات\n\n{build_context(hits)}\n\n"
        f"# پیام کاربر\n\n{question}"
    )

    answer = ""
    stream = await aclient().chat.completions.create(
        model=settings.model_answer,
        messages=[
            {"role": "system", "content": system},
            *session.history(),
            {"role": "user", "content": user},
        ],
        temperature=0.2,
        max_tokens=900,
        stream=True,
        stream_options={"include_usage": True},
    )
    async for part in stream:
        if part.usage:
            counter["tokens"] += part.usage.total_tokens
        if not part.choices:
            continue
        delta = part.choices[0].delta
        if delta.content:
            answer += delta.content
            yield sse("token", TokenEvent(t=delta.content))

    stored_sources = _sources(hits)
    if stored_sources:
        yield sse("sources", SourcesEvent(items=stored_sources))

    items = suggest.for_flow(flow, steps, step.index)
    yield sse("suggestions", SuggestionsEvent(items=items))

    session.add("user", question)
    session.add(
        "assistant", answer,
        sources=[source.model_dump() for source in stored_sources],
        suggestions=[item.model_dump() for item in items],
    )
    session.save()


# ------------------------------------------------------------ حلقه اصلی

async def chat_stream(
    question: str,
    session_id: str,
    client_id: str | None = None,
) -> AsyncIterator[str]:
    started = time.perf_counter()
    session = sessions.get(session_id, client_id)
    tokens_used = 0

    try:
        # --- ۰. فرآیند چندمرحله‌ای ---
        #
        # قبل از بازنویسی، چون «قدم بعد» سؤال نیست و نه بازنویسی لازم دارد
        # نه جستجوی آزاد؛ قدم بعدی از قبل معلوم است.
        decision = flow_decision(question, session)
        if decision:
            action, flow, state = decision
            counter = {"tokens": 0}
            async for event in flow_stream(
                question, session, action, flow, state, counter
            ):
                yield event
            log.info(
                "flow session=%s id=%s action=%s step=%d tokens=%d",
                session_id, flow.id, action, state.step, counter["tokens"],
            )
            yield sse("done", DoneEvent(
                tokens_used=counter["tokens"], cached=False,
                latency_ms=int((time.perf_counter() - started) * 1000),
            ))
            return

        # --- ۱. بازنویسی ---
        yield sse("tool", ToolEvent(
            name="understand", status="running", detail="در حال درک سؤال"
        ))
        plan = await rewrite(question, session)
        tokens_used += plan["tokens"]

        # --- ۲. سؤال تکمیلی: نه بازیابی، نه مدل بزرگ ---
        if plan["clarify"]:
            yield sse("tool", ToolEvent(name="understand", status="done",
                                        detail="نیاز به توضیح بیشتر"))
            for word in plan["clarify"].split(" "):
                yield sse("token", TokenEvent(t=word + " "))

            # گزینه‌ها به چیپ تبدیل می‌شوند تا سؤال تکمیلی بن‌بست نباشد:
            # کاربر یک کلیک جواب می‌دهد، نه یک تایپ دوباره.
            items = [
                Suggestion(label=option[:80], prompt=option[:300], kind="ask")
                for option in plan.get("options", [])
            ]
            if items:
                yield sse("suggestions", SuggestionsEvent(items=items))

            if plan["service"]:
                session.service = plan["service"]
            session.add("user", question)
            session.add(
                "assistant", plan["clarify"],
                suggestions=[item.model_dump() for item in items],
            )
            yield sse("done", DoneEvent(
                tokens_used=tokens_used, cached=False,
                latency_ms=int((time.perf_counter() - started) * 1000),
            ))
            return

        yield sse("tool", ToolEvent(name="understand", status="done",
                                    detail=plan["query"]))

        # سؤال آزاد وسط یک فرآیند فعال: به سؤالش جواب می‌دهیم، ولی stepper
        # باید سر جایش بماند وگرنه کاربر فکر می‌کند فرآیند لغو شده.
        active_flow = flows.flow_by_id(session.flow.id) if session.flow else None
        active_steps: list[flows.ResolvedStep] = []
        if active_flow and session.flow:
            active_steps = flows.resolve(
                active_flow, flows.build_context(session.profile, question)
            )
            yield sse("flow", FlowEvent(**flows.progress_payload(
                active_flow, active_steps, session.flow, "advanced")))

        # --- ۳. بازیابی ---
        yield sse("tool", ToolEvent(name="search_docs", status="running",
                                    detail="جستجوی مستندات"))
        service = plan["service"] or session.service
        profile = session.profile

        # پلتفرم پروفایل به کوئری اضافه می‌شود تا نسخه‌ی درست صفحه بالا
        # بیاید: کاربر جنگویی نباید مستندات nodejs بگیرد.
        queries = [plan["query"]] if plan.get("canonical") else [question, plan["query"]]
        if profile and profile.platform:
            queries.append(f"{plan['query']} {profile.platform}")

        variant = session.variant or (profile.variant_hint if profile else None)
        hits = get_retriever().search(
            queries, k=settings.top_k, service=service, variant=variant,
            url_prefix=plan.get("url_prefix"),
        )
        # فیلتر مسیر سخت است و اگر مسیری در مستندات عوض شده باشد صفر نتیجه
        # می‌دهد — و کاربر «در مستندات پیدا نشد» می‌گیرد در حالی که مطلب
        # هست. یک بار بدون فیلتر دوباره می‌گردیم.
        if not hits and plan.get("url_prefix"):
            log.info("url_prefix %s matched nothing, retrying unfiltered",
                     plan["url_prefix"])
            hits = get_retriever().search(
                queries, k=settings.top_k, service=service, variant=variant,
            )

        if service:
            session.service = service
        yield sse("tool", ToolEvent(name="search_docs", status="done",
                                    detail=f"{len(hits)} نتیجه"))

        # --- ۴. پاسخ ---
        context = build_context(hits)
        system = ANSWER_SYSTEM
        if profile:
            # پروفایل در پیام سیستم می‌آید نه در پیام کاربر: باید در تمام
            # نوبت‌های مکالمه معتبر بماند، نه فقط همین یکی.
            system += (
                "\n\n## This user's project\n"
                "They already told you about their project. Use it: answer for "
                "their platform without asking, and do not ask questions the "
                "profile already answers.\n\n"
                + profile.as_context()
            )

        messages = [
            {"role": "system", "content": system},
            *session.history(),
            {"role": "user", "content":
                f"# متن مستندات\n\n{context}\n\n# سؤال کاربر\n\n{question}"},
        ]

        box = ToolBox(get_retriever(), k=settings.top_k)
        box.collected = list(hits)
        answer = ""

        # حلقه‌ی ابزار با سقف. هر دور یک تماس LLM است — بدون سقف، مدل
        # می‌تواند بی‌پایان جستجو کند. در دور آخر ابزارها برداشته می‌شوند
        # تا مدل مجبور شود پاسخ بدهد، نه اینکه باز هم ابزار بخواهد.
        for round_no in range(MAX_TOOL_ROUNDS + 1):
            last_round = round_no == MAX_TOOL_ROUNDS
            stream = await aclient().chat.completions.create(
                model=settings.model_answer,
                messages=messages,
                temperature=0.2,
                max_tokens=900,
                stream=True,
                stream_options={"include_usage": True},
                **({} if last_round else {"tools": TOOL_SPECS, "tool_choice": "auto"}),
            )

            calls: dict[int, dict] = {}
            async for part in stream:
                if part.usage:
                    tokens_used += part.usage.total_tokens
                if not part.choices:
                    continue
                delta = part.choices[0].delta

                if delta.content:
                    answer += delta.content
                    yield sse("token", TokenEvent(t=delta.content))

                # فراخوانی ابزار تکه‌تکه می‌آید و باید سرهم شود
                for tc in (delta.tool_calls or []):
                    slot = calls.setdefault(tc.index, {"id": "", "name": "", "args": ""})
                    if tc.id:
                        slot["id"] = tc.id
                    if tc.function and tc.function.name:
                        slot["name"] = tc.function.name
                    if tc.function and tc.function.arguments:
                        slot["args"] += tc.function.arguments

            if not calls:
                break

            messages.append({
                "role": "assistant",
                "content": answer or None,
                "tool_calls": [
                    {"id": c["id"], "type": "function",
                     "function": {"name": c["name"], "arguments": c["args"] or "{}"}}
                    for c in calls.values()
                ],
            })

            for c in calls.values():
                try:
                    args = json.loads(c["args"] or "{}")
                except json.JSONDecodeError:
                    args = {}

                detail = args.get("topic") or args.get("variant") or ""
                yield sse("tool", ToolEvent(name=c["name"], status="running",
                                            detail=detail))
                result = await box.run(c["name"], args)
                yield sse("tool", ToolEvent(
                    name=c["name"], status="done",
                    detail=_tool_detail(c["name"], result),
                ))

                messages.append({
                    "role": "tool",
                    "tool_call_id": c["id"],
                    "content": result,
                })

        # --- ۵. منابع ---
        stored_sources: list[Source] = []
        if box.collected:
            stored_sources = _sources(box.collected)
            yield sse("sources", SourcesEvent(items=stored_sources))

        # --- ۶. قدم بعدی ---
        #
        # بعد از منابع فرستاده می‌شود نه قبل از پاسخ: کاربر باید اول جوابش را
        # ببیند. اگر این تماس شکست بخورد، `next_steps` خودش پشتیبان قطعی
        # برمی‌گرداند و هیچ‌وقت استثنا پرتاب نمی‌کند.
        if active_flow and session.flow:
            items = suggest.for_flow(
                active_flow, active_steps, session.flow.step + 1)
        else:
            items, suggest_tokens = await suggest.next_steps(
                question, answer, box.collected, active_flow=False,
            )
            tokens_used += suggest_tokens
        if items:
            yield sse("suggestions", SuggestionsEvent(items=items))

        session.add("user", question)
        session.add(
            "assistant", answer,
            sources=[source.model_dump() for source in stored_sources],
            suggestions=[item.model_dump() for item in items],
        )
        session.save()

        yield sse("done", DoneEvent(
            tokens_used=tokens_used, cached=False,
            latency_ms=int((time.perf_counter() - started) * 1000),
        ))

    except LLMUnavailable as exc:
        log.error("llm unavailable: %s", exc)
        yield sse("error", ErrorEvent(
            message="سرویس هوش مصنوعی در دسترس نیست. لطفاً کمی بعد دوباره تلاش کنید.",
            code="llm_unavailable",
        ))
    except Exception:
        log.exception("chat failed session=%s", session_id)
        yield sse("error", ErrorEvent(
            message="مشکلی در پردازش پیش آمد. لطفاً دوباره تلاش کنید.",
            code="internal_error",
        ))
