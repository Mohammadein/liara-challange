from __future__ import annotations

import logging
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

from fastapi.testclient import TestClient
from pydantic import SecretStr, ValidationError

from app.main import app
from app.observability import JsonFormatter
from app.security import RateLimiter
from app.settings import Settings, settings


class RateLimiterTests(unittest.TestCase):
    def test_capacity_is_enforced_under_concurrency(self) -> None:
        limiter = RateLimiter(per_minute=10)
        with ThreadPoolExecutor(max_workers=20) as pool:
            results = list(pool.map(lambda _: limiter.check("same-client"), range(100)))

        self.assertEqual(sum(1 for allowed, _, _ in results if allowed), 10)
        rejected = [result for result in results if not result[0]]
        self.assertTrue(rejected)
        self.assertTrue(all(retry_after >= 1 for _, retry_after, _ in rejected))


class SecretSettingsTests(unittest.TestCase):
    def test_secret_is_masked_and_redacted_from_json_log(self) -> None:
        secret = "super-secret-value"
        config = Settings(
            _env_file=None,
            use_mock=True,
            liara_ai_api_key=secret,
        )
        self.assertNotIn(secret, repr(config))

        record = logging.LogRecord(
            "test", logging.ERROR, __file__, 1, "failed key=%s", (secret,), None
        )
        rendered = JsonFormatter((secret,)).format(record)
        self.assertNotIn(secret, rendered)
        self.assertIn("[REDACTED]", rendered)

    def test_production_fails_fast_without_key(self) -> None:
        with self.assertRaises(ValidationError):
            Settings(
                _env_file=None,
                use_mock=False,
                liara_ai_base_url="https://ai.liara.ir/api/v1/example",
                liara_ai_api_key="",
            )


class OperationsApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.mock_mode = patch.object(settings, "use_mock", True)
        self.mock_mode.start()

    def tearDown(self) -> None:
        self.mock_mode.stop()

    def test_request_id_validation_errors_and_security_headers(self) -> None:
        request_id = "judge-request-1234"
        with TestClient(app) as client:
            response = client.post(
                "/api/chat", json={}, headers={"X-Request-ID": request_id}
            )

        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json()["code"], "validation_error")
        self.assertEqual(response.json()["request_id"], request_id)
        self.assertEqual(response.headers["X-Request-ID"], request_id)
        self.assertEqual(response.headers["X-Content-Type-Options"], "nosniff")
        self.assertNotIn("input", response.text)

    def test_expensive_endpoint_is_rate_limited_with_retry_header(self) -> None:
        with (
            patch("app.main._expensive_limiter", RateLimiter(per_minute=1)),
            TestClient(app) as client,
        ):
            first = client.post("/api/chat", json={})
            second = client.post("/api/chat", json={})

        self.assertEqual(first.status_code, 422)
        self.assertEqual(second.status_code, 429)
        self.assertEqual(second.json()["code"], "rate_limited")
        self.assertGreaterEqual(int(second.headers["Retry-After"]), 1)
        self.assertEqual(second.headers["X-RateLimit-Remaining"], "0")

    def test_body_size_limit_is_checked_before_parsing(self) -> None:
        with (
            patch.object(settings, "max_request_body_bytes", 10),
            TestClient(app) as client,
        ):
            response = client.post("/api/chat", content=b"x" * 100)

        self.assertEqual(response.status_code, 413)
        self.assertEqual(response.json()["code"], "request_too_large")

    def test_chunked_body_cannot_bypass_size_limit(self) -> None:
        def chunks():
            yield b'{' + b'"message":"' + b"x" * 30
            yield b'","session_id":"example"}'

        with (
            patch.object(settings, "max_request_body_bytes", 20),
            TestClient(app) as client,
        ):
            response = client.post(
                "/api/chat", content=chunks(), headers={"Transfer-Encoding": "chunked"}
            )

        self.assertEqual(response.status_code, 413)
        self.assertEqual(response.json()["code"], "request_too_large")

    def test_health_readiness_and_protected_metrics(self) -> None:
        with (
            patch.object(settings, "metrics_token", SecretStr("metrics-secret")),
            TestClient(app) as client,
        ):
            health = client.get("/health")
            ready = client.get("/ready")
            denied = client.get("/metrics")
            allowed = client.get(
                "/metrics", headers={"Authorization": "Bearer metrics-secret"}
            )

        self.assertEqual(health.status_code, 200)
        self.assertEqual(ready.status_code, 200)
        self.assertEqual(denied.status_code, 401)
        self.assertEqual(denied.headers["WWW-Authenticate"], "Bearer")
        self.assertEqual(allowed.status_code, 200)
        self.assertIn("liara_http_requests_total", allowed.text)
        self.assertIn("liara_failures_total", allowed.text)
        self.assertIn("liara_llm_tokens_total", allowed.text)


if __name__ == "__main__":
    unittest.main()
