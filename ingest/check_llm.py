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

# از پیام خطای خود گیت‌وی درآمده؛ /v1/models همه‌شان را برنمی‌گرداند.
EMBEDDING_CANDIDATES = [
    "openai/text-embedding-3-small",
    "openai/text-embedding-3-large",
    "openai/text-embedding-ada-002",
    "google/gemini-embedding-001",
    "google/gemini-embedding-2",
    "intfloat/multilingual-e5-large",
]


def main() -> None:
    print(f"base_url : {settings.liara_ai_base_url}")
    # SecretStr همیشه truthy است و str() آن '**********' می‌دهد؛ هر دو
    # اشتباه قبلی باعث می‌شد این اسکریپت با کلید ماسک‌شده تماس بگیرد.
    print(f"کلید     : {'تنظیم شده' if settings.llm_configured else '❌ خالی'}")
    if not settings.llm_configured:
        sys.exit("\nLIARA_AI_API_KEY یا LIARA_AI_BASE_URL در .env تنظیم نشده.")

    client = OpenAI(
        base_url=settings.liara_ai_base_url,
        api_key=settings.liara_ai_api_key_value,
        timeout=30,
    )

    # --- مدل‌های در دسترس ---
    print("\n── مدل‌های در دسترس ──")
    embed: list[str] = []
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
    #
    # هر مدلی که در allowlist هست لزوماً واقعاً سرو نمی‌شود: گیت‌وی لیارا
    # اسم را قبول می‌کند ولی بک‌اند ممکن است «Unsupported embedding model»
    # بدهد. تنها راه مطمئن، تماس واقعی با تک‌تکشان است.
    candidates = list(dict.fromkeys(
        [settings.model_embedding, *EMBEDDING_CANDIDATES, *embed]
    ))
    print("\n── تست واقعی مدل‌های امبدینگ ──")
    working: list[tuple[str, int]] = []
    for name in candidates:
        try:
            # صریح float: SDK وقتی numpy نصب باشد پیش‌فرض base64 می‌فرستد و
            # بک‌اند Google آن را رد می‌کند.
            r = client.embeddings.create(
                model=name, input=["استقرار برنامه جنگو در لیارا"],
                encoding_format="float",
            )
            dim = len(r.data[0].embedding)
            working.append((name, dim))
            print(f"  ✓ {name}  →  dim={dim}")
        except Exception as exc:
            reason = getattr(getattr(exc, "response", None), "text", "") or str(exc)
            print(f"  ❌ {name}  →  {type(exc).__name__}: {reason[:160]}")

    if working:
        print("\nقابل استفاده:")
        for name, dim in working:
            size_mb = 3995 * dim * 4 / 1024 / 1024
            print(f"  MODEL_EMBEDDING={name}   (dim={dim}, vectors.npy ≈ {size_mb:.0f} MB)")
    else:
        print("\n❌ هیچ مدل امبدینگی کار نکرد — با پشتیبانی لیارا تماس بگیرید.")

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
