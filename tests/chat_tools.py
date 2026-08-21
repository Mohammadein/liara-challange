"""
تست ابزارهای ایجنت روی /api/chat.

ابزارها فقط در مسیر چت فعال‌اند. api_v1/ask عمداً قطعی و بدون ابزار مانده
چون مصرف‌کننده‌اش ایجنت است و خودش می‌تواند چند بار صدا بزند؛ تأخیر برایش
مهم‌تر از خودمختاری ماست.

این اسکریپت SSE را پارس می‌کند و رویدادهای tool را جدا نشان می‌دهد.

    python -m tests.chat_tools
    python -m tests.chat_tools --only 3
    python -m tests.chat_tools --verbose
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request

CASES = [
    {
        "name": "بدون ابزار — سؤال مستقیم",
        "message": "آدرس میرور npm لیارا چیه؟",
        "expect": {
            "no_tools": ["list_variants", "diagnose_error"],
            "answer_any": ["package-mirror.liara.ir"],
        },
        "why": "پاسخ در نتایج جستجوی اول هست. ابزار زدن اینجا یعنی اتلاف "
               "وقت و توکن.",
    },
    {
        "name": "list_variants — پلتفرم مشخص نشده",
        "message": "می‌خوام برنامه‌م رو دیپلوی کنم، چیکار کنم؟",
        "expect": {"tools_any": ["list_variants"]},
        "why": "کاربر نگفته چه پلتفرمی و با چه روشی. باید گزینه‌ها را از "
               "مستندات بگیرد و بپرسد.",
    },
    {
        "name": "چند واریانت — باید اول بپرسد، نه اینکه همه را بریزد",
        "message": "چطور به object storage وصل بشم؟",
        "expect": {"max_code_blocks": 1, "max_answer_chars": 900},
        "why": "مستندات برای هر زبان نسخه جدا دارد. کاربر پایتون‌کار نباید "
               "مجبور باشد از کنار نمونه‌کد Go و dotNET رد شود تا به سؤال "
               "برسد. صدا زدن ابزار مهم نیست — مدل واریانت‌ها را از برچسب "
               "تکه‌ها می‌بیند؛ مهم این است که اول بپرسد.",
    },
    {
        "name": "diagnose_error — خطای نصب پکیج",
        "message": "دیپلوی روی لیارا خطا می‌ده:\n"
                   "ERROR: Could not find a version that satisfies the "
                   "requirement fastapi==0.115.6\n"
                   "ERROR: No matching distribution found for fastapi==0.115.6",
        "expect": {
            "tools_any": ["diagnose_error"],
            "answer_any": ["mirror", "میرور"],
        },
        "why": "باید mirror اختصاصی لیارا را به‌عنوان علت تشخیص بدهد، نه "
               "اینکه بگوید نسخه دیگری انتخاب کن.",
    },
    {
        "name": "تایم‌اوت ورکر — پاسخ درست، ابزار اختیاری",
        "message": "لاگ برنامه‌م پره از این:\n[CRITICAL] WORKER TIMEOUT (pid:42)",
        "expect": {"answer_any": ["GUNICORN_TIMEOUT", "timeout", "تایم‌اوت"]},
        "why": "اگر جستجوی اول جواب را دارد، صدا زدن ابزار فقط توکن و ثانیه "
               "هدر می‌دهد. ادعا روی درستی پاسخ است نه روی صدا زدن ابزار.",
    },
    {
        "name": "ماژول پیدا نشد — پاسخ درست",
        "message": "Traceback (most recent call last):\n"
                   "  File \"/app/main.py\", line 3, in <module>\n"
                   "    import psycopg2\n"
                   "ModuleNotFoundError: No module named 'psycopg2'",
        "expect": {"answer_any": ["requirements.txt"]},
        "why": "راه‌حل درست در لیارا افزودن ماژول به requirements.txt است.",
    },
    {
        "name": "ابهام — نباید ابزار بزند",
        "message": "کار نمی‌کنه",
        "expect": {"no_tools": ["list_variants", "diagnose_error"],
                   "max_tokens": 900},
        "why": "سؤال تکمیلی قبل از هر جستجویی می‌آید. ابزار زدن روی سؤال "
               "مبهم یعنی سوزاندن توکن بدون فایده.",
    },
]


def stream_chat(url: str, message: str, session: str, timeout: int = 120):
    """رویدادهای SSE را یکی‌یکی برمی‌گرداند."""
    req = urllib.request.Request(
        f"{url}/api/chat",
        data=json.dumps({"message": message, "session_id": session}).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        buf = ""
        for raw in resp:
            buf += raw.decode("utf-8", "replace")
            while "\n\n" in buf:
                block, buf = buf.split("\n\n", 1)
                event, data = "message", ""
                for line in block.split("\n"):
                    if line.startswith("event:"):
                        event = line[6:].strip()
                    elif line.startswith("data:"):
                        data += line[5:].strip()
                if data:
                    try:
                        yield event, json.loads(data)
                    except json.JSONDecodeError:
                        pass


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://127.0.0.1:8000")
    ap.add_argument("--only", type=int)
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    url = args.url.rstrip("/")
    try:
        with urllib.request.urlopen(f"{url}/health", timeout=15) as r:
            health = json.loads(r.read())
    except Exception as e:
        sys.exit(f"سرور در دسترس نیست: {e}")
    if health.get("mock"):
        sys.exit("سرور در حالت mock است.")

    cases = [CASES[args.only - 1]] if args.only else CASES
    passed = failed = 0
    total_tokens = 0

    for i, case in enumerate(cases, 1):
        print("=" * 72)
        print(f"{case['name']}")
        print(f"چرا  : {case['why']}")
        print(f"پیام : {case['message'][:110].replace(chr(10), ' ⏎ ')}")

        t0 = time.perf_counter()
        answer, tools, done = "", [], {}
        try:
            for event, data in stream_chat(url, case["message"], f"tools-{i}"):
                if event == "token":
                    answer += data.get("t", "")
                elif event == "tool":
                    tools.append((data.get("name"), data.get("status"),
                                  data.get("detail", "")))
                elif event == "done":
                    done = data
                elif event == "error":
                    print(f"  ❌ خطا: {data.get('message')}")
        except Exception as e:
            print(f"  ❌ {type(e).__name__}: {e}")
            failed += 1
            continue

        elapsed = time.perf_counter() - t0
        total_tokens += done.get("tokens_used", 0)
        called = {t[0] for t in tools}

        print(f"\nابزارها:")
        for name, status, detail in tools:
            if status == "done":
                print(f"  • {name} → {detail or '—'}")

        # بررسی ادعاها
        checks: list[tuple[bool, str]] = []
        exp = case["expect"]
        if "tools_any" in exp:
            ok = bool(called & set(exp["tools_any"]))
            checks.append((ok, f"یکی از {exp['tools_any']} صدا زده شد"))
        if "no_tools" in exp:
            bad = called & set(exp["no_tools"])
            checks.append((not bad, f"ابزار بی‌مورد نزد"
                                    + (f" (زد: {bad})" if bad else "")))
        if "answer_any" in exp:
            low = answer.lower()
            hit = [s for s in exp["answer_any"] if s.lower() in low]
            checks.append((bool(hit), f"پاسخ شامل یکی از {exp['answer_any']}"))
        if "max_tokens" in exp:
            t = done.get("tokens_used", 0)
            checks.append((t <= exp["max_tokens"], f"توکن {t} <= {exp['max_tokens']}"))
        if "max_code_blocks" in exp:
            n = answer.count("```") // 2
            checks.append((n <= exp["max_code_blocks"],
                           f"بلوک کد {n} <= {exp['max_code_blocks']}"))
        if "max_answer_chars" in exp:
            n = len(answer)
            checks.append((n <= exp["max_answer_chars"],
                           f"طول پاسخ {n} <= {exp['max_answer_chars']}"))

        ok = all(c[0] for c in checks)
        passed += ok
        failed += not ok

        for good, label in checks:
            print(f"  {'✓' if good else '✗'} {label}")

        print(f"\n{'✅' if ok else '❌'}  {done.get('tokens_used', 0)} توکن · "
              f"{elapsed:.1f}s")
        if args.verbose or not ok:
            print(f"\n{answer[:600]}")
        print()

    print("=" * 72)
    print(f"نتیجه: {passed} موفق، {failed} ناموفق از {len(cases)}")
    print(f"توکن: {total_tokens:,}")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
