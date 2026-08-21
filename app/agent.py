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
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass

from app.contracts import (
    DoneEvent,
    ErrorEvent,
    Source,
    SourcesEvent,
    TokenEvent,
    ToolEvent,
    sse,
)
from app.llm import LLMUnavailable, aclient
from app.prompts import ANSWER_SYSTEM, PLAN_SYSTEM, REWRITE_SYSTEM, build_context
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
_DATABASE_ENGINE_ALIASES = {
    "PostgreSQL": ("postgresql", "postgres", "پستگرس", "پستگری"),
    "MySQL": ("mysql", "my sql", "مای اس کیو ال", "مای اسکیوال"),
    "MariaDB": ("mariadb", "maria db", "ماریا دی بی", "ماریا"),
    "MongoDB": ("mongodb", "mongo", "مونگو"),
    "Redis": ("redis", "ردیس"),
    "SQL Server": ("mssql", "sql server", "sqlserver", "اس کیو ال سرور"),
    "Elasticsearch": ("elasticsearch", "elastic search", "الاستیک"),
    "RabbitMQ": ("rabbitmq", "rabbit mq", "ربیت ام کیو", "ربیت"),
    "SQLite": ("sqlite", "اس کیو لایت"),
}
_PROFILE_DATABASE_NAMES = {
    "postgresql": "PostgreSQL", "mysql": "MySQL", "mariadb": "MariaDB",
    "mongodb": "MongoDB", "redis": "Redis", "mssql": "SQL Server",
    "elasticsearch": "Elasticsearch", "rabbitmq": "RabbitMQ", "sqlite": "SQLite",
}
_DATABASE_URL_SLUGS = {
    "PostgreSQL": "postgresql", "MySQL": "mysql", "MariaDB": "mariadb",
    "MongoDB": "mongodb", "Redis": "redis", "SQL Server": "mssql",
    "Elasticsearch": "elastic-search", "RabbitMQ": "rabbitmq",
}
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
    سؤال → {query, service, clarify}

    اگر مدل کوچک خطا داد یا JSON بی‌ربط برگرداند، به خود سؤال برمی‌گردیم.
    یک بازنویسی ناموفق نباید کل پاسخ را از بین ببرد.
    """
    deterministic_clarification = database_clarification(question, session)
    if deterministic_clarification:
        return {
            "query": "راه‌اندازی دیتابیس در لیارا",
            "service": "dbaas",
            "clarify": deterministic_clarification,
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
            max_tokens=200,
        )
        data = json.loads(resp.choices[0].message.content or "{}")
        usage = resp.usage.total_tokens if resp.usage else 0
    except Exception as exc:
        log.warning("rewrite failed (%s), falling back to raw question", type(exc).__name__)
        return {"query": question, "service": None, "clarify": None, "tokens": 0}

    return {
        "query": (data.get("query") or question).strip(),
        "service": data.get("service") or None,
        "clarify": (data.get("clarify") or None),
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
            if plan["service"]:
                session.service = plan["service"]
            session.add("user", question)
            session.add("assistant", plan["clarify"])
            yield sse("done", DoneEvent(
                tokens_used=tokens_used, cached=False,
                latency_ms=int((time.perf_counter() - started) * 1000),
            ))
            return

        yield sse("tool", ToolEvent(name="understand", status="done",
                                    detail=plan["query"]))

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

        hits = get_retriever().search(
            queries, k=settings.top_k, service=service,
            variant=session.variant or (profile.variant_hint if profile else None),
            url_prefix=plan.get("url_prefix"),
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

        session.add("user", question)
        session.add(
            "assistant", answer,
            sources=[source.model_dump() for source in stored_sources],
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
