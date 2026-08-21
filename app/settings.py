"""تنظیمات از متغیرهای محیطی؛ Secretها هرگز در repr یا log دیده نمی‌شوند."""

from __future__ import annotations

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- سرویس هوش مصنوعی لیارا (سازگار با OpenAI) ---
    liara_ai_base_url: str = "https://ai.liara.ir/api/v1/CHANGE_ME"
    liara_ai_api_key: SecretStr = SecretStr("")

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
    max_message_chars: int = Field(default=2000, ge=100, le=20_000)
    max_request_body_bytes: int = Field(default=65_536, ge=1024, le=1_048_576)
    # مسیرهای دارای LLM سقف پایین‌تر و باقی API سقف بالاتری دارند.
    rate_limit_per_minute: int = Field(default=20, ge=1, le=10_000)
    api_rate_limit_per_minute: int = Field(default=120, ge=1, le=100_000)
    max_concurrent_llm_requests: int = Field(default=8, ge=1, le=100)

    # timeout کل یک درخواست مدل، مستقل از timeout اتصال کلاینت OpenAI.
    llm_timeout_seconds: float = Field(default=75.0, ge=5.0, le=300.0)
    request_timeout_seconds: float = Field(default=120.0, ge=10.0, le=600.0)

    # فقط وقتی برنامه واقعاً پشت reverse proxy قابل اعتماد لیارا اجرا می‌شود
    # فعال شود؛ در غیر این صورت X-Forwarded-For ورودی کاربر قابل جعل است.
    trust_proxy_headers: bool = False

    # اگر تنظیم شود، /metrics فقط با Bearer token قابل خواندن است.
    metrics_token: SecretStr = SecretStr("")

    # --- عملیات ---
    log_level: str = "INFO"
    # روی لیارا برای ماندگاری بین deployها این مسیر را روی Disk قرار دهید.
    session_db_path: str = "data/sessions.db"

    @field_validator("log_level")
    @classmethod
    def validate_log_level(cls, value: str) -> str:
        level = value.upper()
        if level not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
            raise ValueError("LOG_LEVEL نامعتبر است")
        return level

    @model_validator(mode="after")
    def validate_production_secrets(self) -> "Settings":
        # fail-fast در production؛ اجرای mock عمداً بدون کلید مجاز است.
        if not self.use_mock and not self.llm_configured:
            raise ValueError(
                "وقتی USE_MOCK=false است، LIARA_AI_BASE_URL و "
                "LIARA_AI_API_KEY باید تنظیم شوند."
            )
        return self

    @property
    def llm_configured(self) -> bool:
        return bool(self.liara_ai_api_key.get_secret_value()) and (
            "CHANGE_ME" not in self.liara_ai_base_url
        )

    @property
    def liara_ai_api_key_value(self) -> str:
        """تنها مسیر مجاز تبدیل SecretStr به مقدار واقعی برای کلاینت LLM."""
        return self.liara_ai_api_key.get_secret_value()

    @property
    def metrics_token_value(self) -> str:
        return self.metrics_token.get_secret_value()


settings = Settings()
