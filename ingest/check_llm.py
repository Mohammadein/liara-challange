"""
تست اتصال به سرویس LLM — قبل از اجرای امبدینگ.

بهتر است اسم مدل‌ها و ابعاد وکتور را با یک تماس کوچک بفهمیم تا اینکه
وسط یک ران یک‌میلیون‌توکنی به خطا بخوریم.

    python -m ingest.check_llm
"""

from __future__ import annotations

import sys

from openai import OpenAI

from app.settings import settings


def main() -> None:
    print(f"base_url : {settings.liara_ai_base_url}")
    print(f"کلید     : {'تنظیم شده' if settings.liara_ai_api_key else '❌ خالی'}")
    if not settings.liara_ai_api_key:
        sys.exit("\nکلید در .env تنظیم نشده.")

    client = OpenAI(
        base_url=settings.liara_ai_base_url,
        api_key=settings.liara_ai_api_key,
        timeout=30,
    )

    # --- مدل‌های در دسترس ---
    print("\n── مدل‌های در دسترس ──")
    try:
        models = sorted(m.id for m in client.models.list().data)
        print(f"مجموعاً {len(models)} مدل")
        embed = [m for m in models if "embed" in m.lower()]
        print(f"\nمدل‌های امبدینگ ({len(embed)}):")
        for m in embed:
            print(f"  {m}")
        print("\nنمونه‌ای از مدل‌های چت:")
        for m in [x for x in models if "embed" not in x.lower()][:20]:
            print(f"  {m}")
    except Exception as exc:
        print(f"لیست مدل‌ها در دسترس نیست ({type(exc).__name__}) — مهم نیست.")

    # --- تست امبدینگ ---
    print(f"\n── تست امبدینگ: {settings.model_embedding} ──")
    try:
        r = client.embeddings.create(
            model=settings.model_embedding,
            input=["استقرار برنامه جنگو در لیارا"],
        )
        dim = len(r.data[0].embedding)
        print(f"✓ کار می‌کند — ابعاد وکتور: {dim}")
        print(f"  حجم تخمینی vectors.npy: {3341 * dim * 4 / 1024 / 1024:.0f} MB")
    except Exception as exc:
        print(f"❌ {type(exc).__name__}: {exc}")
        print("   MODEL_EMBEDDING را در .env با یکی از مدل‌های بالا عوض کنید.")

    # --- تست چت ---
    print(f"\n── تست چت: {settings.model_answer} ──")
    try:
        r = client.chat.completions.create(
            model=settings.model_answer,
            messages=[{"role": "user", "content": "فقط بنویس: سلام"}],
            max_tokens=20,
        )
        print(f"✓ کار می‌کند — پاسخ: {r.choices[0].message.content!r}")
        if r.usage:
            print(f"  توکن: {r.usage.total_tokens}")
    except Exception as exc:
        print(f"❌ {type(exc).__name__}: {exc}")

    # --- تست tool calling (لازمِ فاز ۳) ---
    print("\n── تست tool calling ──")
    try:
        r = client.chat.completions.create(
            model=settings.model_answer,
            messages=[{"role": "user", "content": "هوای تهران چطوره؟"}],
            tools=[{
                "type": "function",
                "function": {
                    "name": "get_weather",
                    "description": "دمای یک شهر",
                    "parameters": {
                        "type": "object",
                        "properties": {"city": {"type": "string"}},
                        "required": ["city"],
                    },
                },
            }],
            max_tokens=80,
        )
        calls = r.choices[0].message.tool_calls
        print(f"✓ پشتیبانی می‌شود — {calls[0].function.name if calls else 'بدون فراخوانی'}")
    except Exception as exc:
        print(f"❌ {type(exc).__name__}: {exc}")
        print("   بدون tool calling، قابلیت‌های Agentic (۵۰ امتیاز) محدود می‌شود.")


if __name__ == "__main__":
    main()
