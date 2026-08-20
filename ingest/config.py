"""
مسیرها، نگاشت مسیر→URL و برچسب‌های فارسی.

نگاشت مسیر→URL بحرانی‌ترین قطعه‌ی کل پروژه است: ارجاع به منبع مستقیماً
به آن وابسته است و لینک ۴۰۴ در دمو یعنی از دست دادن امتیاز.
برای تست: python -m ingest.config
"""

from __future__ import annotations

from pathlib import Path

# --- مسیرها ---
ROOT = Path(__file__).resolve().parent.parent
DOCS_PAGES = ROOT / "docs" / "src" / "pages"
DATA_DIR = ROOT / "data"
CACHE_DIR = ROOT / ".cache"

CHUNKS_FILE = DATA_DIR / "chunks.json"
VECTORS_FILE = DATA_DIR / "vectors.npy"
BM25_FILE = DATA_DIR / "bm25.pkl"

DOCS_BASE_URL = "https://docs.liara.ir"

# --- تکه‌بندی ---
TARGET_CHUNK_CHARS = 1800   # ~۴۵۰ توکن فارسی
MIN_CHUNK_CHARS = 250       # کوتاه‌تر از این مشکلی ندارد؛ سرآیند زمینه جبرانش می‌کند
MAX_CHUNK_CHARS = 3500      # بیشتر از این شکسته می‌شود

# سقف طول یک بلوک کد. مستندات AI SDK نمونه‌های چندده‌کیلوبایتی دارند که
# نه کسی درباره‌شان سؤال می‌پرسد، نه در context مدل جا می‌شوند.
MAX_CODE_BLOCK_CHARS = 2000

# --- سرویس‌ها (۱۱ بخش سطح‌اول مستندات) ---
SERVICE_LABELS: dict[str, str] = {
    "ai": "هوش مصنوعی",
    "dbaas": "دیتابیس",
    "dns-management-system": "مدیریت DNS",
    "email-server": "سرور ایمیل",
    "iaas": "سرور ابری",
    "mirrors": "میرورها",
    "object-storage": "فضای ذخیره‌سازی ابری",
    "one-click-apps": "نصب یک‌کلیکی",
    "overview": "معرفی کلی",
    "paas": "پلتفرم (PaaS)",
    "references": "مرجع",
}

# برچسب بخش‌های میانی مسیر. هرچه نباشد، خودِ نام مسیر استفاده می‌شود.
SEGMENT_LABELS: dict[str, str] = {
    "how-tos": "راهنماها",
    "details": "جزئیات",
    "cli": "خط فرمان",
    "console": "کنسول",
    "team": "تیم‌ها",
    "api": "API",
    "faq": "سؤالات متداول",
    "cookbook": "کوک‌بوک",
    "foundations": "مفاهیم پایه",
    "quick-start": "شروع سریع",
    "connect-to-service": "اتصال به سرویس",
}


def path_to_url(mdx_path: Path | str, anchor: str | None = None) -> str:
    """
    مسیر فایل mdx را به URL مستندات تبدیل می‌کند.

    >>> path_to_url("docs/src/pages/paas/django/how-tos/deploy-app.mdx")
    'https://docs.liara.ir/paas/django/how-tos/deploy-app/'
    >>> path_to_url("docs/src/pages/ai/about.mdx", anchor="liara-provided-ais")
    'https://docs.liara.ir/ai/about/#liara-provided-ais'
    """
    rel = _relative_slug(mdx_path)
    url = f"{DOCS_BASE_URL}/{rel}/"
    if anchor:
        url += f"#{anchor}"
    return url


def path_to_service(mdx_path: Path | str) -> str:
    """اولین بخش مسیر = سرویس. برای فیلتر بازیابی استفاده می‌شود."""
    rel = _relative_slug(mdx_path)
    return rel.split("/", 1)[0] if rel else ""


def path_to_breadcrumb(mdx_path: Path | str) -> str:
    """
    مسیر را به رشته‌ی خوانا تبدیل می‌کند؛ نام فایل حذف می‌شود چون
    عنوان صفحه جداگانه نگه داشته می‌شود.

    >>> path_to_breadcrumb("docs/src/pages/paas/django/how-tos/deploy-app.mdx")
    'پلتفرم (PaaS) › django › راهنماها'
    """
    parts = _relative_slug(mdx_path).split("/")[:-1]
    if not parts:
        return ""
    labels = [SERVICE_LABELS.get(parts[0], parts[0])]
    labels += [SEGMENT_LABELS.get(p, p) for p in parts[1:]]
    return " › ".join(labels)


def _relative_slug(mdx_path: Path | str) -> str:
    """
    مسیر را به اسلاگ نسبی تبدیل می‌کند: 'paas/django/how-tos/deploy-app'

    با مسیر مطلق ویندوز، مسیر نسبی و جداکننده‌ی هر دو سیستم‌عامل کار می‌کند.
    """
    parts = Path(str(mdx_path).replace("\\", "/")).parts

    # پیدا کردن 'pages' و برداشتن هرچه بعدش می‌آید
    if "pages" in parts:
        parts = parts[parts.index("pages") + 1:]

    if not parts:
        return ""

    *dirs, filename = parts
    stem = filename[:-4] if filename.endswith(".mdx") else filename
    return "/".join([*dirs, stem])


if __name__ == "__main__":
    cases = [
        "docs/src/pages/paas/django/how-tos/deploy-app.mdx",
        r"E:\Hackaton\liara-challange\docs\src\pages\ai\about.mdx",
        "docs/src/pages/references/cli/deploy-app.mdx",
        "docs/src/pages/mirrors/npm.mdx",
    ]
    for c in cases:
        print(f"{c}\n  → {path_to_url(c)}\n  service: {path_to_service(c)}"
              f"\n  breadcrumb: {path_to_breadcrumb(c)}\n")

    n = len(list(DOCS_PAGES.rglob("*.mdx"))) if DOCS_PAGES.exists() else 0
    print(f"فایل‌های mdx پیدا شده: {n}")
