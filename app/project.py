"""
پروفایل پروژه — «قبل از سؤال، بگو چه می‌سازی».

مسئله‌ای که حل می‌کند: کاربر vibe coder می‌داند **چه** می‌خواهد بسازد ولی
نمی‌داند روی لیارا **چطور** و با **کدام سرویس‌ها**. یک فرم کوتاه جای ده
سؤال تکمیلی را می‌گیرد.

دو خروجی دارد:
  ۱. نقشه‌ی استقرار — سرویس‌های لازم، ترتیب قدم‌ها، لینک هر قدم، liara.json
  ۲. پروفایل ماندگار در session — همه‌ی پاسخ‌های بعدی شخصی‌سازی می‌شوند

کاتالوگ پایین عمداً کوچک و دستی است: فقط نقاط ورود قطعی که مسیرشان در
مخزن مستندات تأیید شده. جزئیات از بازیابی می‌آید تا لینک‌ها بیات نشوند.
"""

from __future__ import annotations

from dataclasses import dataclass, field

DOCS = "https://docs.liara.ir"

# پلتفرم‌های PaaS لیارا. کلید = مقدار فیلد platform در liara.json
PLATFORMS: dict[str, str] = {
    "django": "Django",
    "flask": "Flask",
    "python": "Python (FastAPI و سایر)",
    "nodejs": "Node.js",
    "nextjs": "Next.js",
    "react": "React",
    "vue": "Vue",
    "angular": "Angular",
    "php": "PHP",
    "laravel": "Laravel",
    "go": "Go",
    "dotnet": ".NET",
    "static": "سایت استاتیک",
    "docker": "Docker (هر چیز دیگر)",
}

DATABASES: dict[str, str] = {
    "postgresql": "PostgreSQL",
    "mysql": "MySQL",
    "mariadb": "MariaDB",
    "mongodb": "MongoDB",
    "redis": "Redis",
    "mssql": "SQL Server",
    "elasticsearch": "Elasticsearch",
    "sqlite": "SQLite (فایل، نیاز به دیسک)",
    "none": "دیتابیس لازم ندارم",
}

# نیازمندی‌ها → سرویس یا قابلیت لیارا
NEEDS: dict[str, dict] = {
    "file_upload": {
        "label": "آپلود و نگهداری فایل یا تصویر",
        "service": "object-storage",
        "title": "فضای ذخیره‌سازی ابری",
        "url": f"{DOCS}/object-storage/quick-setup/",
        "why": "فایل‌های آپلودی نباید کنار کد بمانند؛ با هر استقرار پاک می‌شوند.",
    },
    "persistent_files": {
        "label": "فایل‌هایی که بعد از ری‌استارت باید بمانند",
        "service": "paas",
        "title": "دیسک",
        "url": f"{DOCS}/paas/disks/about/",
        "why": "فایل‌سیستم برنامه موقتی است؛ برای ماندگاری دیسک لازم است.",
    },
    "email": {
        "label": "ارسال ایمیل (تأیید حساب، اعلان)",
        "service": "email-server",
        "title": "سرور ایمیل",
        "url": f"{DOCS}/email-server/quick-setup/",
        "why": "ارسال ایمیل از سرور بدون تنظیمات درست، به اسپم می‌رود.",
    },
    "background_jobs": {
        "label": "کارهای زمان‌بندی‌شده یا پس‌زمینه",
        "service": "paas",
        "title": "کرون‌جاب",
        "url": f"{DOCS}/paas/details/cron/",
        "why": "کارهای دوره‌ای باید از طریق لیارا زمان‌بندی شوند.",
    },
    "ai": {
        "label": "استفاده از هوش مصنوعی و LLM",
        "service": "ai",
        "title": "سرویس هوش مصنوعی",
        "url": f"{DOCS}/ai/quick-start/",
        "why": "دسترسی به مدل‌های زبانی با API سازگار OpenAI.",
    },
    "custom_domain": {
        "label": "دامنه اختصاصی",
        "service": "dns-management-system",
        "title": "مدیریت DNS",
        "url": f"{DOCS}/dns-management-system/quick-setup/",
        "why": "برای وصل کردن دامنه‌ی خودت به برنامه.",
    },
    "websocket": {
        "label": "اتصال زنده (چت، نوتیفیکیشن لحظه‌ای)",
        "service": "paas",
        "title": "WebSocket",
        "url": f"{DOCS}/paas/details/websocket/",
        "why": "اتصال دائم نیاز به تنظیمات جداگانه دارد.",
    },
    "full_server": {
        "label": "دسترسی کامل به سرور (root)",
        "service": "iaas",
        "title": "سرور ابری",
        "url": f"{DOCS}/iaas/about/",
        "why": "وقتی PaaS کافی نیست و کنترل کامل لازم داری.",
    },
}

DEPLOY_METHODS: dict[str, str] = {
    "cli": "Liara CLI — با دستور از ترمینال",
    "console": "کنسول لیارا — آپلود از مرورگر",
    "github": "GitHub — استقرار خودکار با هر push",
}

EXPERIENCE: dict[str, str] = {
    "beginner": "تازه‌کارم، قدم‌به‌قدم توضیح بده",
    "intermediate": "با دیپلوی آشنام",
    "advanced": "حرفه‌ای‌ام، خلاصه بگو",
}


@dataclass
class ProjectProfile:
    """آنچه از کاربر می‌دانیم. همه‌ی فیلدها اختیاری‌اند جز توضیح."""

    description: str = ""
    platform: str | None = None
    database: str | None = None
    needs: list[str] = field(default_factory=list)
    deploy_method: str | None = None
    experience: str = "intermediate"

    def as_context(self) -> str:
        """
        پروفایل به‌صورت متن، برای تزریق در پرامپت.

        این همان چیزی است که پاسخ‌های بعدی را شخصی می‌کند: مدل دیگر لازم
        نیست بپرسد «کدام فریم‌ورک؟» چون از قبل می‌داند.
        """
        parts: list[str] = []
        if self.description:
            parts.append(f"پروژه‌ی کاربر: {self.description}")
        if self.platform:
            parts.append(f"پلتفرم: {PLATFORMS.get(self.platform, self.platform)}")
        if self.database and self.database != "none":
            parts.append(f"دیتابیس: {DATABASES.get(self.database, self.database)}")
        if self.needs:
            labels = [NEEDS[n]["label"] for n in self.needs if n in NEEDS]
            if labels:
                parts.append("نیازها: " + "، ".join(labels))
        if self.deploy_method:
            parts.append(f"روش استقرار ترجیحی: "
                         f"{DEPLOY_METHODS.get(self.deploy_method, self.deploy_method)}")
        parts.append(f"سطح تجربه: {EXPERIENCE.get(self.experience, self.experience)}")
        return "\n".join(parts)

    @property
    def variant_hint(self) -> str | None:
        """
        واریانت ترجیحی کاربر برای بازیابی.

        اگر گفته با CLI کار می‌کند، تکه‌های «Liara CLI» باید بالاتر بیایند.
        """
        return {
            "cli": "Liara CLI",
            "console": "Liara Console",
            "github": "Github",
        }.get(self.deploy_method or "")


# ------------------------------------------------------------ نقشه استقرار

def services_for(profile: ProjectProfile) -> list[dict]:
    """
    سرویس‌های لیارا که این پروژه لازم دارد.

    قاعده‌محور و قطعی — نه از مدل. اگر کاربر بگوید «آپلود عکس دارم»،
    نیاز به Object Storage یک واقعیت است نه یک نظر، پس نباید به یک تماس
    LLM سپرده شود که ممکن است از قلم بیندازدش.
    """
    out: list[dict] = []

    if profile.platform == "docker":
        out.append({
            "service": "paas", "title": "پلتفرم Docker",
            "url": f"{DOCS}/paas/docker/how-tos/deploy-app/",
            "why": "کنترل کامل روی ایمیج و دستور اجرا.",
        })
    elif profile.platform:
        out.append({
            "service": "paas",
            "title": f"پلتفرم {PLATFORMS.get(profile.platform, profile.platform)}",
            "url": f"{DOCS}/paas/{profile.platform}/how-tos/deploy-app/",
            "why": "میزبانی خود برنامه.",
        })

    if profile.database and profile.database not in ("none", "sqlite"):
        out.append({
            "service": "dbaas",
            "title": DATABASES.get(profile.database, profile.database),
            "url": f"{DOCS}/dbaas/about/",
            "why": "دیتابیس مدیریت‌شده با بکاپ خودکار.",
        })
    elif profile.database == "sqlite":
        out.append({
            "service": "paas", "title": "دیسک (برای SQLite)",
            "url": f"{DOCS}/paas/disks/about/",
            "why": "فایل SQLite بدون دیسک، با هر استقرار پاک می‌شود.",
        })

    seen = {s["title"] for s in out}
    for need in profile.needs:
        item = NEEDS.get(need)
        if item and item["title"] not in seen:
            seen.add(item["title"])
            out.append({k: item[k] for k in ("service", "title", "url", "why")})

    return out


def liara_json_for(profile: ProjectProfile) -> dict:
    """
    فایل liara.json پیشنهادی.

    فقط فیلدهای مستندشده. app را کاربر باید پر کند، پس جای‌نگهدار می‌گذاریم
    به‌جای اینکه نامی از خودمان بسازیم.
    """
    platform = profile.platform or "docker"
    config: dict = {"app": "<شناسه-برنامه>", "platform": platform}

    if platform in ("django", "flask", "python"):
        config[platform] = {"pythonVersion": "3.12"}
    if platform == "docker":
        config["port"] = 8000
    if "persistent_files" in profile.needs or profile.database == "sqlite":
        config["disks"] = [{"name": "data", "mountTo": "/app/data"}]

    return config
