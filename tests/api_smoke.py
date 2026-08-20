"""
تست دودی API — خروجی‌اش را یکجا کپی کن و بفرست.

    python -m tests.api_smoke                          روی لوکال
    python -m tests.api_smoke --url https://liara-docs-assistant.liara.run
    python -m tests.api_smoke --full                   متن کامل پاسخ‌ها

هر سناریو یک رفتار متفاوت را می‌سنجد، نه صرفاً «کار می‌کند یا نه».
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request

CASES: list[dict] = [
    {
        "name": "۱. ساده — تطابق مستقیم",
        "checks": "پاسخ باید بلوک کد JSON با pythonVersion داشته باشد؛ "
                  "منبع باید anchor داشته باشد؛ confidence بالا",
        "body": {"question": "چطور نسخه پایتون برنامه‌م رو تعیین کنم؟"},
    },
    {
        "name": "۲. بازنویسی کوئری — واژگان کاربر با مستندات فرق دارد",
        "checks": "query_used باید کلمه gunicorn/worker داشته باشد، "
                  "نه صرفاً تکرار سؤال کاربر",
        "body": {"question": "چند تا ورکر برای برنامه‌م بذارم؟"},
    },
    {
        "name": "۳. بازنویسی کوئری — کاربر اصطلاح فنی را نمی‌داند",
        "checks": "query_used باید به CORS اشاره کند",
        "body": {"question": "از مرورگر به API‌م ریکوئست می‌زنم ولی بلاک می‌شه"},
    },
    {
        "name": "۴. راهنمای پلتفرم",
        "checks": "منابع باید مربوط به django باشند نه پلتفرم دیگر",
        "body": {"question": "چطور متغیر محیطی اضافه کنم؟", "platform": "django"},
    },
    {
        "name": "۵. ابهام — باید سؤال تکمیلی بپرسد",
        "checks": "needs_clarification=true، answer خالی، "
                  "توکن کم (مدل بزرگ نباید صدا زده شود)",
        "body": {"question": "کار نمی‌کنه"},
    },
    {
        "name": "۶. ابهام — بدون زمینه",
        "checks": "needs_clarification=true",
        "body": {"question": "قیمتش چنده؟"},
    },
    {
        "name": "۷. سناریوی ایجنت — تشخیص خطای دیپلوی",
        "checks": "باید mirror لیارا را تشخیص بدهد و راه‌حل liara.json بدهد",
        "body": {
            "question": "دیپلوی روی لیارا خطا می‌ده: ERROR: Could not find a "
                        "version that satisfies the requirement fastapi==0.115.6",
            "platform": "python",
        },
    },
    {
        "name": "۸. ضدتوهم — چیزی که در مستندات نیست",
        "checks": "باید صریح بگوید در مستندات نیست. نباید فیلد یا پلن جعلی بسازد",
        "body": {"question": "فیلد quantumBoost توی liara.json چیکار می‌کنه؟"},
    },
    {
        "name": "۹. چندنوبتی — نوبت اول",
        "checks": "پاسخ درباره object storage",
        "body": {"question": "چطور باکت object storage بسازم؟",
                 "session_id": "smoke-multiturn"},
    },
    {
        "name": "۱۰. چندنوبتی — نوبت دوم، ارجاع ضمنی",
        "checks": "باید بفهمد «بهش» یعنی همان باکت و درباره دامنه اختصاصی "
                  "پاسخ بدهد، نه چیز دیگر",
        "body": {"question": "چطور یه دامنه اختصاصی بهش وصل کنم؟",
                 "session_id": "smoke-multiturn"},
    },
    {
        "name": "۱۱. انگلیسی ورودی — خروجی باید فارسی باشد",
        "checks": "answer باید فارسی باشد حتی وقتی سؤال انگلیسی است",
        "body": {"question": "How do I deploy a Django app to Liara using the CLI?"},
    },
    {
        "name": "۱۲. اسم خاص — نقطه قوت BM25",
        "checks": "باید صفحه میرور npm را پیدا کند",
        "body": {"question": "آدرس میرور npm لیارا چیه؟"},
    },
]


def call(url: str, body: dict, timeout: int = 90) -> tuple[int, dict, float]:
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        f"{url}/api/v1/ask",
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    started = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read()), time.perf_counter() - started
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}"), time.perf_counter() - started
    except Exception as e:
        return 0, {"error": f"{type(e).__name__}: {e}"}, time.perf_counter() - started


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://127.0.0.1:8000")
    ap.add_argument("--full", action="store_true", help="متن کامل پاسخ‌ها")
    ap.add_argument("--only", type=int, help="فقط یک سناریو (شماره)")
    args = ap.parse_args()

    url = args.url.rstrip("/")
    cases = [CASES[args.only - 1]] if args.only else CASES

    # سلامت سرور
    try:
        with urllib.request.urlopen(f"{url}/health", timeout=15) as r:
            health = json.loads(r.read())
    except Exception as e:
        sys.exit(f"سرور در دسترس نیست: {e}")

    print(f"URL     : {url}")
    print(f"health  : {json.dumps(health, ensure_ascii=False)}")
    if health.get("mock"):
        sys.exit("\n⚠️ سرور در حالت mock است. USE_MOCK=false و ری‌استارت.")
    if not health.get("index_loaded"):
        sys.exit("\n⚠️ ایندکس لود نشده. python -m ingest.run_all")

    total_tokens = 0
    print()

    for case in cases:
        print("=" * 72)
        print(case["name"])
        print(f"سؤال    : {case['body']['question']}")
        if case["body"].get("platform"):
            print(f"platform: {case['body']['platform']}")
        print(f"انتظار  : {case['checks']}")
        print("-" * 72)

        status, data, elapsed = call(url, case["body"])

        if status != 200:
            print(f"❌ HTTP {status}: {json.dumps(data, ensure_ascii=False)}")
            print()
            continue

        total_tokens += data.get("usage", {}).get("tokens", 0)
        limit = 100_000 if args.full else 600

        print(f"query_used : {data.get('query_used')}")
        print(f"service    : {data.get('service')}")
        print(f"confidence : {data.get('confidence')}")
        print(f"clarify    : {data.get('needs_clarification')} "
              f"{data.get('clarification') or ''}")
        print(f"usage      : {data.get('usage', {}).get('tokens')} توکن، "
              f"{data.get('usage', {}).get('latency_ms')} ms "
              f"(کل رفت‌وبرگشت {elapsed:.1f}s)")

        answer = data.get("answer") or ""
        print(f"\nپاسخ ({len(answer)} کاراکتر):")
        print(answer[:limit] + ("\n…[بریده شد]" if len(answer) > limit else ""))

        srcs = data.get("sources", [])
        print(f"\nمنابع ({len(srcs)}):")
        for s in srcs:
            v = f"  [{s['variant']}]" if s.get("variant") else ""
            print(f"  - {s['title']}{' › ' + s['section'] if s.get('section') else ''}{v}")
            print(f"    {s['url']}")

        exc = data.get("excerpts", [])
        print(f"\nمتن خام: {len(exc)} تکه"
              f" ({sum(len(e['text']) for e in exc)} کاراکتر)")
        print()

    print("=" * 72)
    print(f"مجموع توکن مصرفی: {total_tokens}")


if __name__ == "__main__":
    main()
