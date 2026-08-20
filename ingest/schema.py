"""
قرارداد داده — شکل یک «تکه» (chunk).

⚠️ این فایل قفل است. هر تغییر در فیلدها یعنی اجرای دوباره کل خط لوله ingest
روی ۱۱۴۲ فایل. قبل از تغییر با تیم هماهنگ کنید.
"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Any


@dataclass
class Chunk:
    """یک تکه‌ی قابل بازیابی از مستندات لیارا."""

    # --- شناسه ---
    # قالب: {slug-صفحه}#{anchor-بخش}#{روش}
    # مثال: paas-django-deploy-app#liara-json#cli
    id: str

    # --- متن ---
    # متنی که به LLM داده می‌شود (بدون سرآیند زمینه)
    text: str

    # متنی که وکتور می‌شود (با سرآیند زمینه).
    # فقط build time لازم است و در chunks.json ذخیره نمی‌شود تا فایل نصف شود.
    embed_text: str = field(repr=False, default="")

    # --- منبع ---
    # لینک کامل با anchor بخش. این چیزی است که به کاربر نشان داده می‌شود.
    url: str = ""

    # --- زمینه ---
    page_title: str = ""       # عنوان H1 صفحه
    section_title: str = ""    # عنوان <Section title="...">
    breadcrumb: str = ""       # مثال: "PaaS › Django › راهنماها"

    # --- متادیتا برای فیلتر و شخصی‌سازی ---
    service: str = ""           # اولین بخش مسیر: paas | dbaas | ai | object-storage | ...
    variant: str | None = None  # برچسب <Tabs>؛ ۷۷ مقدار مختلف در مستندات دیده شد
    has_code: bool = False      # آیا تکه بلوک کد دارد

    # --- ردیابی ---
    source_path: str = ""      # مسیر فایل اصلی، برای دیباگ

    def to_runtime_dict(self) -> dict[str, Any]:
        """
        دیکشنری برای chunks.json (زمان اجرا).

        embed_text عمداً حذف می‌شود: در زمان اجرا هرگز استفاده نمی‌شود و
        نگه‌داشتنش حجم فایل را تقریباً دو برابر می‌کند.
        """
        d = asdict(self)
        d.pop("embed_text", None)
        return d

    @classmethod
    def from_runtime_dict(cls, d: dict[str, Any]) -> "Chunk":
        return cls(**{**d, "embed_text": ""})


# نمونه مرجع — تست‌ها و mock باید دقیقاً همین شکل را تولید کنند.
EXAMPLE_CHUNK = Chunk(
    id="paas-django-deploy-app#liara-json#cli",
    text='در ادامه، در مسیر اصلی پروژه، یک فایل به نام liara.json ایجاد کنید...',
    embed_text="مسیر: PaaS › Django › راهنماها\nصفحه: استقرار برنامه Django در لیارا\n"
               "بخش: فایل liara.json\n---\nدر ادامه، ...",
    url="https://docs.liara.ir/paas/django/how-tos/deploy-app/#liara-json",
    page_title="استقرار برنامه Django در لیارا",
    section_title="فایل liara.json",
    breadcrumb="PaaS › Django › راهنماها",
    service="paas",
    variant="Liara CLI",
    has_code=True,
    source_path="src/pages/paas/django/how-tos/deploy-app.mdx",
)
