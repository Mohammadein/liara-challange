"""Logging ساختاریافته و metricهای کم‌هزینه و بدون وابستگی خارجی."""

from __future__ import annotations

import json
import logging
import re
import threading
from collections import Counter
from contextvars import ContextVar
from datetime import datetime, timezone
from typing import Iterable


request_id_var: ContextVar[str] = ContextVar("request_id", default="-")


class JsonFormatter(logging.Formatter):
    """هر خط یک JSON معتبر؛ مناسب logهای لیارا و ابزارهای جمع‌آوری log."""

    def __init__(self, secrets: Iterable[str] = ()) -> None:
        super().__init__()
        self._secrets = tuple(value for value in secrets if len(value) >= 6)

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, object] = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "request_id": request_id_var.get(),
        }
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        rendered = json.dumps(payload, ensure_ascii=False)
        for secret in self._secrets:
            rendered = rendered.replace(secret, "[REDACTED]")
        return rendered


class SecretRedactionFilter(logging.Filter):
    """آخرین حفاظ: Secret شناخته‌شده را حتی در متن exception حذف می‌کند."""

    def __init__(self, secrets: Iterable[str]) -> None:
        super().__init__()
        self._secrets = tuple(value for value in secrets if len(value) >= 6)

    def filter(self, record: logging.LogRecord) -> bool:
        if not self._secrets:
            return True
        message = record.getMessage()
        for secret in self._secrets:
            message = message.replace(secret, "[REDACTED]")
        record.msg = message
        record.args = ()
        return True


def configure_logging(level: str, secrets: Iterable[str] = ()) -> None:
    secret_values = tuple(secrets)
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter(secret_values))
    handler.addFilter(SecretRedactionFilter(secret_values))
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level)


# شناسه‌های session نباید label متریک شوند؛ cardinality بی‌حد می‌سازد.
_SESSION_PATH = re.compile(r"^/api/sessions/[^/]+$")


def route_label(path: str) -> str:
    if _SESSION_PATH.match(path):
        return "/api/sessions/{id}"
    if path.startswith("/api/") or path in {"/health", "/ready", "/metrics"}:
        return path
    return "/static"


class Metrics:
    _duration_buckets = (0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 15.0, 60.0)

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.requests: Counter[tuple[str, str, int]] = Counter()
        self.duration: Counter[tuple[str, float]] = Counter()
        self.duration_sum: Counter[str] = Counter()
        self.in_flight = 0
        self.rate_limited: Counter[str] = Counter()
        self.failures: Counter[str] = Counter()
        self.tokens: Counter[str] = Counter()
        self.operation_duration: Counter[tuple[str, float]] = Counter()
        self.operation_duration_sum: Counter[str] = Counter()
        self.operations: Counter[str] = Counter()

    def start_request(self) -> None:
        with self._lock:
            self.in_flight += 1

    def finish_request(
        self, method: str, route: str, status: int, elapsed_seconds: float,
    ) -> None:
        with self._lock:
            self.in_flight = max(0, self.in_flight - 1)
            self.requests[(method, route, status)] += 1
            self.duration_sum[route] += elapsed_seconds
            for upper in self._duration_buckets:
                if elapsed_seconds <= upper:
                    self.duration[(route, upper)] += 1

    def rate_limit(self, scope: str) -> None:
        with self._lock:
            self.rate_limited[scope] += 1

    def failure(self, kind: str) -> None:
        with self._lock:
            self.failures[kind] += 1

    def token_usage(self, operation: str, amount: int) -> None:
        if amount <= 0:
            return
        with self._lock:
            self.tokens[operation] += amount

    def observe_operation(self, operation: str, elapsed_seconds: float) -> None:
        with self._lock:
            self.operations[operation] += 1
            self.operation_duration_sum[operation] += elapsed_seconds
            for upper in self._duration_buckets:
                if elapsed_seconds <= upper:
                    self.operation_duration[(operation, upper)] += 1

    def render(self) -> str:
        """فرمت exposition استاندارد Prometheus."""
        with self._lock:
            lines = [
                "# HELP liara_http_requests_total Total HTTP requests.",
                "# TYPE liara_http_requests_total counter",
            ]
            for (method, route, status), value in sorted(self.requests.items()):
                lines.append(
                    f'liara_http_requests_total{{method="{method}",route="{route}",'
                    f'status="{status}"}} {value}'
                )
            lines += [
                "# HELP liara_http_requests_in_flight Current HTTP requests.",
                "# TYPE liara_http_requests_in_flight gauge",
                f"liara_http_requests_in_flight {self.in_flight}",
                "# HELP liara_http_request_duration_seconds Request latency.",
                "# TYPE liara_http_request_duration_seconds histogram",
            ]
            routes = sorted({route for route, _ in self.duration})
            for route in routes:
                for upper in self._duration_buckets:
                    value = self.duration[(route, upper)]
                    lines.append(
                        f'liara_http_request_duration_seconds_bucket{{route="{route}",'
                        f'le="{upper}"}} {value}'
                    )
                count = sum(
                    value for (method, r, status), value in self.requests.items()
                    if r == route
                )
                lines.append(
                    f'liara_http_request_duration_seconds_bucket{{route="{route}",'
                    f'le="+Inf"}} {count}'
                )
                lines.append(
                    f'liara_http_request_duration_seconds_count{{route="{route}"}} {count}'
                )
                lines.append(
                    f'liara_http_request_duration_seconds_sum{{route="{route}"}} '
                    f'{self.duration_sum[route]:.6f}'
                )
            lines += [
                "# HELP liara_rate_limited_total Rejected requests.",
                "# TYPE liara_rate_limited_total counter",
            ]
            for scope, value in sorted(self.rate_limited.items()):
                lines.append(f'liara_rate_limited_total{{scope="{scope}"}} {value}')
            lines += [
                "# HELP liara_failures_total Application failures by kind.",
                "# TYPE liara_failures_total counter",
            ]
            for kind, value in sorted(self.failures.items()):
                lines.append(f'liara_failures_total{{kind="{kind}"}} {value}')
            lines += [
                "# HELP liara_llm_tokens_total LLM tokens reported by provider.",
                "# TYPE liara_llm_tokens_total counter",
            ]
            for operation, value in sorted(self.tokens.items()):
                lines.append(
                    f'liara_llm_tokens_total{{operation="{operation}"}} {value}'
                )
            lines += [
                "# HELP liara_llm_operation_duration_seconds End-to-end LLM operation latency.",
                "# TYPE liara_llm_operation_duration_seconds histogram",
            ]
            for operation in sorted(self.operations):
                for upper in self._duration_buckets:
                    value = self.operation_duration[(operation, upper)]
                    lines.append(
                        f'liara_llm_operation_duration_seconds_bucket{{operation="{operation}",'
                        f'le="{upper}"}} {value}'
                    )
                count = self.operations[operation]
                lines.append(
                    f'liara_llm_operation_duration_seconds_bucket{{operation="{operation}",'
                    f'le="+Inf"}} {count}'
                )
                lines.append(
                    f'liara_llm_operation_duration_seconds_count{{operation="{operation}"}} '
                    f'{count}'
                )
                lines.append(
                    f'liara_llm_operation_duration_seconds_sum{{operation="{operation}"}} '
                    f'{self.operation_duration_sum[operation]:.6f}'
                )
            return "\n".join(lines) + "\n"


metrics = Metrics()
