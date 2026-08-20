"""
خروجی کنسول را روی UTF-8 تنظیم می‌کند.

ویندوز به‌صورت پیش‌فرض cp1252 را برای stdout انتخاب می‌کند و هر print فارسی
با UnicodeEncodeError کرش می‌کند — مخصوصاً وقتی خروجی هدایت شود (`> file`).
این باگ بی‌سروصداست: اسکریپت وسط کار می‌میرد ولی بقیه‌ی خط لوله ظاهراً
اجرا می‌شود و آدم فکر می‌کند نتیجه معتبر است.
"""

import sys

for _stream in (sys.stdout, sys.stderr):
    if _stream and getattr(_stream, "encoding", "").lower() not in ("utf-8", "utf8"):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
