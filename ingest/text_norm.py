"""
منتقل شد به app/text_norm.py

دلیل: زمان اجرا فقط app/ در ایمیج داکر هست، و توکن‌سازی ایندکس و توکن‌سازی
سؤال کاربر باید حتماً از یک کد بیایند. این فایل فقط برای سازگاری واردات
باقی مانده است.
"""

from app.text_norm import STOPWORDS, normalize, tokenize  # noqa: F401
