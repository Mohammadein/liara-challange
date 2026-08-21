"""
تست جریان فرم پروژه.

ادعای اصلی این قابلیت این نیست که «نقشه تولید می‌شود» — این را با چشم هم
می‌شود دید. ادعای اصلی این است که **پروفایل، سؤالات بعدی را عوض می‌کند**.
پس تست، همان سؤال را قبل و بعد از ثبت پروفایل می‌پرسد و مقایسه می‌کند.

    python -m tests.project_flow
    python -m tests.project_flow --verbose
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
import uuid

URL = "http://127.0.0.1:8000"


def post(url: str, path: str, body: dict, timeout: int = 180):
    req = urllib.request.Request(
        url + path,
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read() or b"{}")
        except Exception:
            return e.code, {}


def chat(url: str, message: str, session: str) -> str:
    """پاسخ چت را جمع می‌کند (SSE)."""
    req = urllib.request.Request(
        url + "/api/chat",
        data=json.dumps({"message": message, "session_id": session}).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    answer = ""
    with urllib.request.urlopen(req, timeout=180) as resp:
        buf = ""
        for raw in resp:
            buf += raw.decode("utf-8", "replace")
            while "\n\n" in buf:
                block, buf = buf.split("\n\n", 1)
                ev, data = "", ""
                for line in block.split("\n"):
                    if line.startswith("event:"):
                        ev = line[6:].strip()
                    elif line.startswith("data:"):
                        data += line[5:].strip()
                if ev == "token" and data:
                    try:
                        answer += json.loads(data).get("t", "")
                    except json.JSONDecodeError:
                        pass
    return answer


PROFILE = {
    "description": "یه فروشگاه آنلاین با پنل ادمین که کاربرا بتونن عکس محصول "
                   "آپلود کنن و بعد از ثبت سفارش ایمیل تأیید بگیرن",
    "platform": "django",
    "database": "postgresql",
    "needs": ["file_upload", "email", "custom_domain"],
    "deploy_method": "cli",
    "experience": "beginner",
}

# سؤالی که بدون پروفایل مبهم است و با پروفایل باید مستقیم جواب بگیرد
PROBE = "چطور فایل‌های آپلودی رو نگه دارم؟"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default=URL)
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()
    url = args.url.rstrip("/")

    checks: list[tuple[bool, str]] = []

    # ---------- ۱. فرم از سرور توصیف می‌شود ----------
    print("=" * 72)
    print("۱. GET /api/v1/project/options")
    try:
        with urllib.request.urlopen(f"{url}/api/v1/project/options", timeout=20) as r:
            opts = json.loads(r.read())
    except Exception as e:
        sys.exit(f"ناموفق: {e}")

    names = [f["name"] for f in opts["fields"]]
    print(f"   فیلدها: {', '.join(names)}")
    for f in opts["fields"]:
        if f["options"]:
            print(f"   {f['name']}: {len(f['options'])} گزینه")
    checks.append(("description" in names, "فیلد description هست"))
    checks.append((len(opts["fields"]) >= 5, "حداقل ۵ فیلد"))

    # ---------- ۲. سؤال قبل از پروفایل ----------
    session = f"proj-{uuid.uuid4().hex[:8]}"
    print("\n" + "=" * 72)
    print(f"۲. سؤال «{PROBE}» — بدون پروفایل")
    before = chat(url, PROBE, session + "-cold")
    print(f"   {before[:300]}\n")

    # ---------- ۳. ثبت پروفایل ----------
    print("=" * 72)
    print("۳. POST /api/v1/project/plan")
    status, plan = post(url, "/api/v1/project/plan",
                        {"session_id": session, **PROFILE})
    if status != 200:
        sys.exit(f"ناموفق ({status}): {plan}")

    print(f"\n   سرویس‌های تشخیص‌داده‌شده:")
    for s in plan["services"]:
        print(f"     • {s['title']}  [{s['service']}]")
        print(f"       {s['why']}")

    titles = " ".join(s["title"] for s in plan["services"])
    checks.append(("Django" in titles, "پلتفرم Django تشخیص داده شد"))
    checks.append(("PostgreSQL" in titles, "دیتابیس PostgreSQL تشخیص داده شد"))
    checks.append(("ذخیره‌سازی" in titles, "Object Storage برای آپلود عکس"))
    checks.append(("ایمیل" in titles, "سرور ایمیل برای تأیید سفارش"))
    checks.append(("DNS" in titles, "مدیریت DNS برای دامنه اختصاصی"))

    lj = plan["liara_json"]
    print(f"\n   liara.json: {json.dumps(lj, ensure_ascii=False)}")
    checks.append((lj.get("platform") == "django", "liara.json پلتفرم درست"))

    print(f"\n   نقشه ({len(plan['plan'])} کاراکتر، "
          f"{plan['usage']['tokens']} توکن، {plan['usage']['latency_ms']}ms):")
    print("   " + plan["plan"][:900 if not args.verbose else 100000]
          .replace("\n", "\n   "))

    checks.append((len(plan["plan"]) > 300, "نقشه خالی نیست"))
    checks.append((len(plan["sources"]) > 0, "نقشه منبع دارد"))
    # کاربر CLI را انتخاب کرده، پس باید دستور ببیند
    checks.append(("liara " in plan["plan"], "دستور CLI در نقشه هست"))

    # قدم بی‌محتوا: «به مستندات مراجعه کنید» بدون گفتن کجا.
    # کاربر به‌جای مستندات آمده اینجا؛ ارجاع دادنش به مستندات یعنی هیچ.
    vague = plan["plan"].count("به مستندات مراجعه کنید")
    links = plan["plan"].count("](http")
    checks.append((vague <= 1, f"قدم‌های بی‌جزئیات ({vague}) حداکثر ۱"))
    checks.append((links >= 3, f"لینک مستقیم در نقشه ({links}) حداقل ۳"))

    # مدل نباید liara.json بنویسد — نسخه‌ی قطعی جدا برمی‌گردد و دوتایی
    # با هم تناقض پیدا می‌کنند.
    checks.append(('"platform"' not in plan["plan"]
                   and '"pythonVersion"' not in plan["plan"],
                   "نقشه liara.json موازی نمی‌سازد"))

    # مقدار نمونه را به‌جای پیشنهاد کپی نکند. یک بار timezone را روی
    # "Cuba" گذاشت — پیش‌فرض Asia/Tehran همان چیزی است که کاربر می‌خواهد.
    bad_tz = [tz for tz in ("Cuba", "America/", "Europe/", "US/")
              if tz in plan["plan"]]
    checks.append((not bad_tz, f"منطقه زمانی جعلی پیشنهاد نمی‌دهد {bad_tz or ''}"))
    checks.append(('"mirror": false' not in plan["plan"],
                   "mirror را بی‌دلیل غیرفعال نمی‌کند"))

    # ---------- ۴. همان سؤال، بعد از پروفایل ----------
    print("\n" + "=" * 72)
    print(f"۴. همان سؤال — با پروفایل")
    after = chat(url, PROBE, session)
    print(f"   {after[:400]}\n")

    # ادعای اصلی: حالا باید بداند جنگو است و نپرسد
    asks_platform = any(w in after for w in
                        ("کدام فریم", "کدام پلتفرم", "چه فریم", "چه پلتفرمی"))
    checks.append((not asks_platform, "دیگر پلتفرم را نمی‌پرسد"))
    checks.append((len(after) > 80, "پاسخ واقعی داد"))

    # ---------- نتیجه ----------
    print("=" * 72)
    ok = sum(1 for c, _ in checks if c)
    for good, label in checks:
        print(f"  {'✓' if good else '✗'} {label}")
    print(f"\nنتیجه: {ok}/{len(checks)}")
    sys.exit(0 if ok == len(checks) else 1)


if __name__ == "__main__":
    main()
