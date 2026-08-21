"""
فرآیندهای چندمرحله‌ای (Runbook).

مسئله‌ای که حل می‌کند: بعضی کارها یک سؤال نیستند، یک **مسیر**اند. «می‌خوام
اپم رو بیارم بالا» یعنی ساخت برنامه، آماده‌سازی پروژه، liara.json، استقرار،
لاگ، متغیر محیطی — و هر کدام صفحه‌ی مستندات جداگانه‌ای دارد. یک پاسخ بلند که
همه را با هم بگوید خوانده نمی‌شود؛ کاربر وسطش گم می‌شود و نمی‌داند کجای کار
است.

طراحی: **اسکلت قطعی، محتوای بازیابی‌شده.**

  - ترتیب قدم‌ها، عنوان‌ها و صفحه‌ی مرجع هر قدم در همین فایل ثابت است. مدل
    اجازه ندارد قدم بسازد، حذف کند یا جابه‌جا کند — پس نه قدمی از قلم می‌افتد
    و نه لینکی توهم می‌شود.
  - متن هر قدم از مستندات همان قدم بازیابی و با LLM نوشته می‌شود — پس بیات
    نمی‌شود و با پروفایل کاربر (پلتفرم، روش استقرار، دیتابیس) تطبیق پیدا
    می‌کند.

`url_prefix` هر قدم یک فیلتر **نرم** است: اگر برای آن مسیر تکه‌ای پیدا نشد،
`agent` دوباره بدون فیلتر جستجو می‌کند. مسیرها ممکن است در مستندات عوض شوند و
یک فیلتر سخت، قدم را خالی تحویل می‌دهد.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from app.text_norm import normalize

DOCS = "https://docs.liara.ir"


# ------------------------------------------------------------ نگاشت دیتابیس
# اینجا نگه داشته می‌شوند نه در agent.py، چون هم مسیریابی سؤال و هم فرآیند
# «راه‌اندازی دیتابیس» به یک نگاشت نیاز دارند و دو نسخه‌ی جداگانه دیر یا زود
# از هم جدا می‌افتند.

DATABASE_ENGINE_ALIASES: dict[str, tuple[str, ...]] = {
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

PROFILE_DATABASE_NAMES: dict[str, str] = {
    "postgresql": "PostgreSQL", "mysql": "MySQL", "mariadb": "MariaDB",
    "mongodb": "MongoDB", "redis": "Redis", "mssql": "SQL Server",
    "elasticsearch": "Elasticsearch", "rabbitmq": "RabbitMQ", "sqlite": "SQLite",
}

DATABASE_URL_SLUGS: dict[str, str] = {
    "PostgreSQL": "postgresql", "MySQL": "mysql", "MariaDB": "mariadb",
    "MongoDB": "mongodb", "Redis": "redis", "SQL Server": "mssql",
    "Elasticsearch": "elastic-search", "RabbitMQ": "rabbitmq",
}

DEPLOY_METHOD_LABELS: dict[str, str] = {
    "cli": "Liara CLI",
    "console": "کنسول لیارا",
    "github": "GitHub",
}


# ------------------------------------------------------------ مدل داده

@dataclass(frozen=True)
class FlowStep:
    """یک قدم. رشته‌ها می‌توانند جای‌نگهدار `{platform}` و … داشته باشند."""

    key: str
    title: str
    query: str                     # کوئری بازیابی همین قدم
    goal: str                      # کاربر در پایان این قدم چه چیزی دارد
    url: str = ""                  # لینک مرجع، برای وقتی بازیابی چیزی نیاورد
    url_prefix: str | None = None  # فیلتر نرم مسیر
    service: str | None = None


@dataclass(frozen=True)
class Flow:
    id: str
    title: str
    summary: str
    topics: tuple[str, ...]        # واژه‌های موضوعی که این فرآیند را می‌سازند
    steps: tuple[FlowStep, ...]


@dataclass
class FlowState:
    """
    وضعیت فرآیند در یک گفتگو. در session ذخیره و از SQLite بازخوانی می‌شود.

    `hints` سیگنال‌هایی است که فرآیند با آن‌ها شروع شده — پلتفرم و موتور
    دیتابیس. بدون این، پیام «قدم بعد» هیچ اطلاعاتی ندارد و قدم دوم دوباره
    عمومی می‌شود: کاربری که در پیام اول گفته «داکر»، در قدم دوم راهنمای PHP
    و Go می‌گیرد. آنچه کاربر یک بار گفته نباید دوباره پرسیده یا فراموش شود.
    """

    id: str
    step: int = 0                              # ایندکس ۰-پایه‌ی قدم جاری
    done: list[str] = field(default_factory=list)
    hints: dict[str, str] = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "id": self.id, "step": self.step,
            "done": list(self.done), "hints": dict(self.hints),
        }

    @classmethod
    def from_dict(cls, data: dict | None) -> "FlowState | None":
        if not isinstance(data, dict) or not data.get("id"):
            return None
        if data["id"] not in FLOWS:
            return None
        raw_hints = data.get("hints")
        return cls(
            id=str(data["id"]),
            step=max(0, int(data.get("step") or 0)),
            done=[str(k) for k in (data.get("done") or [])],
            hints={
                str(k): str(v) for k, v in (raw_hints or {}).items() if v
            } if isinstance(raw_hints, dict) else {},
        )


# ------------------------------------------------------------ کاتالوگ

_DEPLOY = Flow(
    id="deploy_app",
    title="استقرار برنامه از صفر تا اجرا",
    summary="از ساختن برنامه در کنسول تا دیدن لاگ اجرای موفق.",
    topics=(
        "دیپلوی", "استقرار", "مستقر", "deploy", "بالا بیارم", "بالا بیاورم",
        "بیارم بالا", "بیاورم بالا", "اپم", "برنامه ام", "سایتم", "پروژه ام",
        "منتشر", "میزبانی", "داکر", "docker",
    ),
    steps=(
        FlowStep(
            key="create_app",
            title="ساخت برنامه در کنسول لیارا",
            query="ساخت برنامه جدید در کنسول لیارا و انتخاب پلتفرم و پلن",
            goal="یک برنامه با شناسه‌ی مشخص روی لیارا وجود داشته باشد.",
            url=f"{DOCS}/paas/about/",
            url_prefix="/paas/",
            service="paas",
        ),
        # کوئری‌های سه قدم زیر عمداً از هم دور نگه داشته شده‌اند: یک صفحه‌ی
        # مستندات هر سه را با هم پوشش می‌دهد و کوئری‌های نزدیک، متن یکسانی
        # برمی‌گردانند — آن‌وقت مدل قدم بعدی را می‌نویسد و شماره‌ی روی
        # stepper با متن یکی نمی‌شود.
        FlowStep(
            key="prepare_project",
            title="آماده‌سازی پروژه {platform_label} برای استقرار",
            query="وابستگی‌ها و دستور اجرای پروژه {platform_label} "
                  "قبل از استقرار در لیارا",
            goal="سورس پروژه آماده‌ی اجرا باشد: وابستگی‌ها اعلام‌شده و دستور "
                 "اجرا مشخص.",
            url="{deploy_url}",
            url_prefix="{platform_prefix}",
            service="paas",
        ),
        FlowStep(
            key="liara_json",
            title="نوشتن فایل liara.json",
            query="ساختار و فیلدهای فایل liara.json",
            goal="پیکربندی استقرار در ریپو باشد، نه در حافظه‌ی شما.",
            url=f"{DOCS}/paas/liarajson/",
            url_prefix="/paas/",
            service="paas",
        ),
        FlowStep(
            key="deploy",
            title="استقرار با {method_label}",
            query="اجرای دستور استقرار برنامه {platform_label} {method_phrase}",
            goal="اولین نسخه‌ی برنامه روی لیارا اجرا شده باشد.",
            url="{deploy_url}",
            url_prefix="{deploy_prefix}",
            service="paas",
        ),
        FlowStep(
            key="env",
            title="تنظیم متغیرهای محیطی",
            query="تنظیم متغیرهای محیطی برنامه در لیارا",
            goal="کلیدها و رشته‌های اتصال از کد بیرون باشند.",
            url=f"{DOCS}/paas/details/envs/",
            url_prefix="/paas/",
            service="paas",
        ),
        FlowStep(
            key="logs",
            title="بررسی لاگ و سلامت برنامه",
            query="مشاهده لاگ برنامه و بررسی وضعیت اجرا در لیارا",
            goal="بدانید برنامه بالا آمده و اگر نیامده، چرا.",
            url=f"{DOCS}/paas/details/logs/",
            url_prefix="/paas/",
            service="paas",
        ),
    ),
)

_DATABASE = Flow(
    id="database_setup",
    title="راه‌اندازی دیتابیس و اتصال برنامه به آن",
    summary="از ساخت دیتابیس تا وصل شدن برنامه و گرفتن بکاپ.",
    topics=(
        "دیتابیس", "پایگاه داده", "database", "postgres", "postgresql",
        "mysql", "mariadb", "mongo", "mongodb", "redis", "الاستیک",
    ),
    steps=(
        FlowStep(
            key="create_db",
            title="ساخت دیتابیس {engine_label}",
            query="راه‌اندازی سریع دیتابیس {engine_label} با کنسول لیارا",
            goal="دیتابیس ساخته و در حال اجرا باشد.",
            url="{db_url}",
            url_prefix="{db_prefix}",
            service="dbaas",
        ),
        FlowStep(
            key="credentials",
            title="گرفتن اطلاعات اتصال",
            query="اطلاعات اتصال و رشته اتصال دیتابیس {engine_label}",
            goal="رشته‌ی اتصال کامل را داشته باشید.",
            url="{db_url}",
            url_prefix="{db_prefix}",
            service="dbaas",
        ),
        FlowStep(
            key="wire_app",
            title="اتصال برنامه با متغیر محیطی",
            query="تنظیم رشته اتصال دیتابیس در متغیرهای محیطی برنامه",
            goal="برنامه بدون داشتن رمز داخل کد به دیتابیس وصل شود.",
            url=f"{DOCS}/paas/details/envs/",
            url_prefix="/paas/",
            service="paas",
        ),
        FlowStep(
            key="private_network",
            title="اتصال از شبکه خصوصی",
            query="شبکه خصوصی و اتصال داخلی برنامه به دیتابیس در لیارا",
            goal="ترافیک دیتابیس از اینترنت عمومی رد نشود.",
            url=f"{DOCS}/paas/details/private-network/",
            service="paas",
        ),
        FlowStep(
            key="backup",
            title="بکاپ و بازیابی",
            query="تهیه بکاپ و بازیابی دیتابیس {engine_label} در لیارا",
            goal="بدانید اگر داده از بین رفت چه کار می‌کنید.",
            url="{db_url}",
            url_prefix="/dbaas/",
            service="dbaas",
        ),
    ),
)

_DOMAIN = Flow(
    id="custom_domain",
    title="اتصال دامنه اختصاصی و فعال‌سازی HTTPS",
    summary="از افزودن دامنه تا سبز شدن قفل مرورگر.",
    topics=(
        "دامنه", "دامین", "domain", "dns", "ssl", "https", "گواهی",
        "subdomain", "زیردامنه",
    ),
    steps=(
        FlowStep(
            key="add_domain",
            title="افزودن دامنه به برنامه",
            query="افزودن دامنه اختصاصی به برنامه در کنسول لیارا",
            goal="دامنه در بخش دامنه‌های برنامه ثبت شده باشد.",
            url=f"{DOCS}/paas/domains/",
            url_prefix="/paas/",
            service="paas",
        ),
        FlowStep(
            key="dns_record",
            title="تنظیم رکورد DNS",
            query="تنظیم رکورد DNS برای اتصال دامنه به لیارا",
            goal="رکورد در پنل دامنه ثبت و منتشر شده باشد.",
            url=f"{DOCS}/dns-management-system/quick-setup/",
            url_prefix="/dns-management-system/",
            service="dns-management-system",
        ),
        FlowStep(
            key="ssl",
            title="فعال‌سازی گواهی SSL",
            query="فعال‌سازی گواهی SSL و HTTPS برای دامنه در لیارا",
            goal="سایت روی https بدون هشدار باز شود.",
            url=f"{DOCS}/paas/domains/",
            url_prefix="/paas/",
            service="paas",
        ),
        FlowStep(
            key="verify",
            title="بررسی نهایی و ریدایرکت",
            query="بررسی وضعیت دامنه و ریدایرکت www در لیارا",
            goal="همه‌ی شکل‌های آدرس به یک جای درست برسند.",
            url=f"{DOCS}/paas/domains/",
            url_prefix="/paas/",
            service="paas",
        ),
    ),
)

_TROUBLESHOOT = Flow(
    id="deploy_failure",
    title="عیب‌یابی استقرار ناموفق",
    summary="از خواندن لاگ تا استقرار موفق دوباره.",
    topics=(
        "خطا", "ارور", "error", "failed", "شکست", "بالا نمیاد",
        "کار نمی کند", "کار نمیکنه", "مشکل", "crash", "کرش", "بیلد",
    ),
    steps=(
        FlowStep(
            key="read_logs",
            title="پیدا کردن خط واقعی خطا در لاگ",
            query="مشاهده لاگ بیلد و لاگ اجرای برنامه در لیارا",
            goal="متن دقیق خطا را داشته باشید، نه حدس.",
            url=f"{DOCS}/paas/details/logs/",
            url_prefix="/paas/",
            service="paas",
        ),
        FlowStep(
            key="diagnose",
            title="تشخیص علت خطا",
            query="خطاهای رایج استقرار در لیارا و علت آن‌ها",
            goal="بدانید خطا مال نصب پکیج است، اجرا، یا پیکربندی.",
            url=f"{DOCS}/paas/",
            url_prefix="/paas/",
            service="paas",
        ),
        FlowStep(
            key="fix",
            title="رفع مشکل و اصلاح پیکربندی",
            query="رفع خطای نصب پکیج و تنظیم mirror در لیارا",
            goal="علت برطرف شده باشد، نه علامت.",
            url=f"{DOCS}/paas/",
            url_prefix="/paas/",
            service="paas",
        ),
        FlowStep(
            key="redeploy",
            title="استقرار دوباره و تأیید سلامت",
            query="استقرار دوباره برنامه و بررسی وضعیت اجرا در لیارا",
            goal="نسخه‌ی جدید بدون خطا بالا آمده باشد.",
            url=f"{DOCS}/paas/details/logs/",
            url_prefix="/paas/",
            service="paas",
        ),
    ),
)

_STORAGE = Flow(
    id="file_storage",
    title="راه‌اندازی فضای ذخیره‌سازی و آپلود فایل",
    summary="از ساخت باکت تا آپلود از داخل کد.",
    # «فایل» و «عکس» عمداً اینجا نیستند: آن‌قدر عمومی‌اند که هر پاسخی درباره‌ی
    # دیسک یا لاگ را هم به این فرآیند وصل می‌کردند.
    topics=(
        "باکت", "bucket", "object storage", "ذخیره سازی ابری", "s3",
        "اپلود فایل", "اپلود عکس", "فضای ذخیره سازی",
    ),
    steps=(
        FlowStep(
            key="create_bucket",
            title="ساخت باکت",
            query="ساخت باکت در فضای ذخیره‌سازی ابری لیارا",
            goal="یک باکت با سطح دسترسی مشخص داشته باشید.",
            url=f"{DOCS}/object-storage/quick-setup/",
            url_prefix="/object-storage/",
            service="object-storage",
        ),
        FlowStep(
            key="keys",
            title="ساخت کلید دسترسی",
            query="ساخت access key و secret key برای فضای ذخیره‌سازی لیارا",
            goal="کلید دسترسی داشته باشید و آن را در متغیر محیطی بگذارید.",
            url=f"{DOCS}/object-storage/",
            url_prefix="/object-storage/",
            service="object-storage",
        ),
        FlowStep(
            key="connect",
            title="اتصال از کد برنامه",
            query="اتصال به فضای ذخیره‌سازی لیارا از کد با endpoint سازگار S3",
            goal="برنامه بتواند فایل آپلود و دریافت کند.",
            url=f"{DOCS}/object-storage/",
            url_prefix="/object-storage/",
            service="object-storage",
        ),
        FlowStep(
            key="serve",
            title="دسترسی عمومی و دامنه باکت",
            query="دسترسی عمومی فایل‌ها و اتصال دامنه به باکت در لیارا",
            goal="فایل‌ها با آدرس پایدار در دسترس کاربران باشند.",
            url=f"{DOCS}/object-storage/",
            url_prefix="/object-storage/",
            service="object-storage",
        ),
    ),
)

FLOWS: dict[str, Flow] = {
    f.id: f for f in (_DEPLOY, _DATABASE, _DOMAIN, _TROUBLESHOOT, _STORAGE)
}


# ------------------------------------------------------------ تشخیص قصد

# فرآیند فقط وقتی شروع می‌شود که کاربر **صریحاً** مسیر بخواهد.
#
# اول با «موضوع تنهایی کافی است» نوشته شده بود و نتیجه‌اش بد بود: «نسخه
# پایتون رو کجا تعیین کنم؟» یک سؤال یک‌خطی است و پرت کردن کاربر به یک
# فرآیند شش‌قدمی، کمک نیست — مزاحمت است.
_STEPWISE_MARKERS = (
    "قدم به قدم", "مرحله به مرحله", "گام به گام", "از صفر", "از اول تا اخر",
    "راهنمای کامل", "کل فرایند", "کل مراحل", "همه مراحل", "چک لیست",
    "step by step", "راه اندازی کامل", "شروع کن", "فرایند",
)

_NEXT_INTENTS = (
    "قدم بعد", "مرحله بعد", "گام بعد", "بعدی", "برو بعدی", "ادامه",
    "انجام شد", "انجامش دادم", "تمام شد", "تموم شد", "اوکی شد", "done", "next",
)
_PREV_INTENTS = ("قدم قبل", "مرحله قبل", "گام قبل", "برگرد", "قبلی", "back")
_EXIT_INTENTS = (
    "خروج از فرایند", "لغو فرایند", "بستن فرایند", "توقف فرایند",
    "بی خیال فرایند", "متوقفش کن", "exit flow", "stop flow",
)
_RESTATE_INTENTS = ("دوباره بگو", "همین قدم", "بیشتر توضیح", "متوجه نشدم")


_FA_DIGITS = str.maketrans("0123456789", "۰۱۲۳۴۵۶۷۸۹")


def fa(value) -> str:
    """عدد با رقم فارسی — تا برچسب ابزار و stepper شبیه هم به نظر برسند."""
    return str(value).translate(_FA_DIGITS)


def _has(text: str, needles) -> bool:
    return any(n in text for n in needles)


def next_intent(text: str) -> bool:
    return _has(f" {normalize(text)} ", _NEXT_INTENTS)


def previous_intent(text: str) -> bool:
    return _has(f" {normalize(text)} ", _PREV_INTENTS)


def exit_intent(text: str) -> bool:
    return _has(f" {normalize(text)} ", _EXIT_INTENTS)


def restate_intent(text: str) -> bool:
    return _has(f" {normalize(text)} ", _RESTATE_INTENTS)


def match_flow(text: str) -> Flow | None:
    """
    متن کاربر → فرآیند، یا None.

    شرط دوگانه است: هم نشانه‌ی «مسیر می‌خواهم» و هم موضوع. اگر فقط یکی باشد،
    سؤال معمولی است و باید مثل قبل جواب داده شود.
    """
    normalized = f" {normalize(text)} "
    if not _has(normalized, _STEPWISE_MARKERS):
        return None

    best: tuple[int, Flow] | None = None
    for flow in FLOWS.values():
        score = sum(1 for topic in flow.topics if topic in normalized)
        if score and (best is None or score > best[0]):
            best = (score, flow)
    return best[1] if best else None


def flow_by_id(flow_id: str | None) -> Flow | None:
    return FLOWS.get(flow_id or "")


def suggestible_flow(text: str) -> Flow | None:
    """
    فرآیندی که ارزش دارد به‌عنوان چیپ پیشنهاد شود — بدون شروع خودکار.

    اینجا فقط موضوع کافی است، چون کاربر خودش باید روی چیپ کلیک کند. تفاوت با
    `match_flow` عمدی است: پیشنهاد ارزان است، شروع خودکار گران.
    """
    normalized = f" {normalize(text)} "
    best: tuple[int, Flow] | None = None
    for flow in FLOWS.values():
        score = sum(1 for topic in flow.topics if topic in normalized)
        if score and (best is None or score > best[0]):
            best = (score, flow)
    return best[1] if best else None


def start_prompt(flow: Flow) -> str:
    """پیامی که چیپ «شروع فرآیند» می‌فرستد — عمداً حاوی نشانه‌ی مسیر است."""
    return f"قدم به قدم راهنمایی کن: {flow.title}"


# ------------------------------------------------------------ جایگذاری

_PLATFORM_ALIASES: dict[str, tuple[str, ...]] = {
    "django": ("django", "جنگو"),
    "flask": ("flask", "فلسک"),
    "python": ("fastapi", "فست ای پی ای", "پایتون", "python"),
    # «نود» تنها عمداً اینجا نیست: در فارسی یعنی عدد ۹۰ و «نود درصد کاربرها»
    # را به پلتفرم Node.js تبدیل می‌کرد.
    "nodejs": ("nodejs", "node js", "نودجی اس", "نود جی اس"),
    "nextjs": ("nextjs", "next js", "نکست"),
    "react": ("react", "ری اکت"),
    "vue": ("vue", "ویو جی اس", "vuejs"),
    "angular": ("angular", "انگولار"),
    "laravel": ("laravel", "لاراول"),
    "php": ("php", "پی اچ پی"),
    "go": ("golang", "گولنگ"),
    "dotnet": ("dotnet", "net core", "دات نت"),
    "static": ("static site", "سایت استاتیک"),
    "docker": ("docker", "داکر"),
}


def detect_platform(text: str) -> str | None:
    normalized = f" {normalize(text)} "
    for platform, aliases in _PLATFORM_ALIASES.items():
        if any(alias in normalized for alias in aliases):
            return platform
    return None


def detect_engine(text: str) -> str | None:
    normalized = f" {normalize(text)} "
    for engine, aliases in DATABASE_ENGINE_ALIASES.items():
        if any(alias in normalized for alias in aliases):
            return engine
    return None


# نام‌های قدیمی، برای کدی که هنوز آن‌ها را صدا می‌زند.
_detect_platform = detect_platform
_detect_engine = detect_engine


def build_context(
    profile,
    text: str = "",
    hints: dict[str, str] | None = None,
) -> dict[str, str]:
    """
    جای‌نگهدارهای قدم‌ها را پر می‌کند.

    ترتیب اولویت: **متن همین پیام → پروفایل → hint**.

    متن اول است چون تازه‌ترین حرف صریح کاربر است؛ hint آخر است چون فقط
    حافظه‌ی چیزی است که قبلاً گفته شده. اما hint حیاتی است: پیام «قدم بعد»
    هیچ کلمه‌ای ندارد، و بدون حافظه، قدم دوم پلتفرمی را که کاربر در پیام اول
    گفته بود از دست می‌دهد.

    هر کلید همیشه مقدار دارد؛ یک `{platform}` پرنشده در URL یعنی لینک خراب
    در دموی جلوی داور.
    """
    from app.project import PLATFORMS

    hints = hints or {}

    platform = (
        detect_platform(text)
        or getattr(profile, "platform", None)
        or (hints.get("platform") or None)
    )
    profile_db = getattr(profile, "database", None)
    engine = (
        detect_engine(text)
        or PROFILE_DATABASE_NAMES.get(profile_db or "")
        or (hints.get("engine") or None)
    )
    method = getattr(profile, "deploy_method", None) or (hints.get("method") or None)
    slug = DATABASE_URL_SLUGS.get(engine or "", "")

    # لینک و فیلتر مسیر **اینجا** ساخته می‌شوند، نه با جای‌نگهدار داخل خود
    # مسیر. یک `{platform}` خالی، `/paas//how-tos/` می‌سازد که بعد از
    # مرتب‌سازی به یک آدرس ۴۰۴ تبدیل می‌شود — و لینک خراب در دمو، همان
    # امتیاز «ارائه منبع مناسب» را می‌گیرد.
    # برچسب فهرست فرم توضیح داخل پرانتز دارد («Docker (هر چیز دیگر)») که در
    # عنوان یک قدم و در کوئری جستجو فقط نویز است.
    label = re.sub(r"\s*\(.*?\)", "", PLATFORMS.get(platform or "", "")).strip()
    method_label = DEPLOY_METHOD_LABELS.get(method or "") or method or ""

    return {
        "platform": platform or "",
        "platform_label": label or "برنامه‌تان",
        "platform_prefix": f"/paas/{platform}/" if platform else "/paas/",
        "deploy_prefix": (
            f"/paas/{platform}/how-tos/deploy-app/" if platform else "/paas/"
        ),
        "deploy_url": (
            f"{DOCS}/paas/{platform}/how-tos/deploy-app/" if platform
            else f"{DOCS}/paas/about/"
        ),
        "engine_label": engine or "",
        "engine_slug": slug,
        "db_prefix": f"/dbaas/{slug}/" if slug else "/dbaas/",
        "db_url": f"{DOCS}/dbaas/{slug}/" if slug else f"{DOCS}/dbaas/about/",
        # `method` ممکن است کلید فرم («cli») یا برچسب واریانت بازیابی
        # («Liara CLI») باشد — هر دو پذیرفته می‌شوند.
        "method_label": method_label or "روش دلخواه شما",
        # وقتی روش معلوم نیست، «با روش دلخواه شما» در کوئری فقط نویز است؛
        # به‌جایش گزینه‌های واقعی جستجو می‌شوند.
        "method_phrase": (
            f"با {method_label}" if method_label
            else "با کنسول لیارا، Liara CLI یا GitHub"
        ),
        # سیگنال‌های خام، برای ذخیره در FlowState و استفاده در نوبت‌های بعدی.
        "_platform": platform or "",
        "_engine": engine or "",
        "_method": method or "",
    }


def hints_from(ctx: dict[str, str]) -> dict[str, str]:
    """سیگنال‌های قابل‌حمل را از ctx بیرون می‌کشد تا در session بمانند."""
    return {
        key: ctx.get(f"_{key}", "")
        for key in ("platform", "engine", "method")
        if ctx.get(f"_{key}")
    }


class _SafeDict(dict):
    def __missing__(self, key: str) -> str:  # pragma: no cover - دفاعی
        return ""


def _fill(template: str, ctx: dict[str, str]) -> str:
    out = template.format_map(_SafeDict(ctx))
    # جای‌نگهدار خالی («دیتابیس {engine_label}») نباید فاصله‌ی اضافه یا
    # `/dbaas//` بسازد.
    out = re.sub(r"(?<!:)//+", "/", out)
    return re.sub(r"\s{2,}", " ", out).strip()


@dataclass(frozen=True)
class ResolvedStep:
    index: int                     # ۱-پایه
    key: str
    title: str
    query: str
    goal: str
    url: str
    url_prefix: str | None
    service: str | None


def resolve(flow: Flow, ctx: dict[str, str]) -> list[ResolvedStep]:
    steps: list[ResolvedStep] = []
    for i, step in enumerate(flow.steps, start=1):
        prefix = _fill(step.url_prefix, ctx) if step.url_prefix else None
        # مسیر ناقص (پلتفرم نامعلوم) بدتر از نبودن فیلتر است: `/paas//` هیچ
        # تکه‌ای نمی‌گیرد و قدم خالی تحویل می‌شود.
        if prefix and prefix.count("/") < 2:
            prefix = None
        steps.append(ResolvedStep(
            index=i,
            key=step.key,
            title=_fill(step.title, ctx),
            query=_fill(step.query, ctx),
            goal=_fill(step.goal, ctx),
            url=_fill(step.url, ctx),
            url_prefix=prefix,
            service=step.service,
        ))
    return steps


def outline(steps: list[ResolvedStep], current: int) -> str:
    """فهرست قدم‌ها برای پرامپت، تا مدل بداند چه چیزی مال قدم‌های بعدی است."""
    lines = []
    for step in steps:
        marker = "→" if step.index == current else " "
        lines.append(f"{marker} {step.index}. {step.title} — {step.goal}")
    return "\n".join(lines)


def progress_payload(
    flow: Flow,
    steps: list[ResolvedStep],
    state: FlowState,
    status: str,
) -> dict:
    """داده‌ی رویداد `flow` برای UI."""
    current = state.step + 1 if status != "completed" else 0
    return {
        "id": flow.id,
        "title": flow.title,
        "step": current,
        "total": len(steps),
        "status": status,
        "steps": [
            {
                "index": step.index,
                "title": step.title,
                "status": (
                    "done" if step.key in state.done
                    else "current" if step.index == current
                    else "pending"
                ),
            }
            for step in steps
        ],
    }
