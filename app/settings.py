"""تنظیمات از متغیرهای محیطی. هیچ راز و کلیدی در کد نیست."""

from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- سرویس هوش مصنوعی لیارا (سازگار با OpenAI) ---
    liara_ai_base_url: str = "https://ai.liara.ir/api/v1/CHANGE_ME"
    liara_ai_api_key: str = ""

    # --- مدل‌ها ---
    # ⚠️ باید با مدل‌های فعال در پلن لیارا تطبیق داده شوند.
    model_answer: str = "openai/gpt-4o-mini"      # پاسخ نهایی
    model_fast: str = "openai/gpt-4o-mini"        # بازنویسی کوئری، مسیریابی
    model_embedding: str = "openai/text-embedding-3-small"

    # --- حالت اجرا ---
    # true = پاسخ ساختگی بدون تماس با LLM؛ برای کار موازی روی UI
    use_mock: bool = True

    # --- بازیابی ---
    # ۸ نه ۵: روی مجموعه‌ی طلایی Recall@5 برابر ۸۶٪ و Recall@10 برابر ۹۳٪ بود.
    # آن ۷ درصد یعنی سؤالاتی که سند درست را پیدا می‌کنیم ولی لب مرز جا
    # می‌ماند و مدل می‌گوید «در مستندات نیست» — بدترین حالت شکست.
    # هزینه‌اش حدود ۴۰٪ context بیشتر است که در برابر ۸۰ امتیاز کیفیت
    # پاسخ در مقابل ۲۵ امتیاز هزینه، معامله‌ی درستی است.
    top_k: int = 8

    # --- امنیت ---
    max_message_chars: int = 2000
    rate_limit_per_minute: int = 20

    # --- عملیات ---
    log_level: str = "INFO"
    # روی لیارا برای ماندگاری بین deployها این مسیر را روی Disk قرار دهید.
    session_db_path: str = "data/sessions.db"

    @property
    def llm_configured(self) -> bool:
        return bool(self.liara_ai_api_key) and "CHANGE_ME" not in self.liara_ai_base_url


settings = Settings()
