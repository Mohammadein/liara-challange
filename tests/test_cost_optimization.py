from __future__ import annotations

import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from app.agent import AnswerResult, answer_once, chat_stream, rewrite
from app.cache import TTLCache, cache_key
from app.prompts import build_context
from app.retrieval import Hit
from app.session import Session
from app.suggest import next_steps


def hit(name: str, index: int = 1, text: str = "متن مستندات") -> Hit:
    return Hit(
        id=f"hit-{index}", text=text,
        url=f"https://docs.liara.ir/page-{index}",
        page_title=name, section_title=f"بخش {index}", breadcrumb="",
        service="paas", variant=None, has_code=False, score=1.0,
    )


class CacheTests(unittest.TestCase):
    def test_key_is_deterministic_and_does_not_contain_user_text(self) -> None:
        key1 = cache_key("answer", "متن خصوصی کاربر", {"platform": "django"})
        key2 = cache_key("answer", "متن خصوصی کاربر", {"platform": "django"})
        self.assertEqual(key1, key2)
        self.assertNotIn("متن خصوصی", key1)

    def test_cache_is_bounded_lru_and_returns_a_copy(self) -> None:
        cache = TTLCache(max_entries=2, ttl_seconds=60)
        cache.set("a", {"items": [1]})
        cache.set("b", {"items": [2]})
        found, value = cache.get("a")
        self.assertTrue(found)
        value["items"].append(99)
        cache.set("c", {"items": [3]})

        self.assertFalse(cache.get("b")[0])  # a اخیراً استفاده شد؛ b حذف می‌شود
        self.assertEqual(cache.get("a")[1], {"items": [1]})
        self.assertEqual(len(cache), 2)


class TokenBudgetTests(unittest.TestCase):
    def test_history_keeps_recent_pair_within_character_budget(self) -> None:
        session = Session(id="budget")
        session.add("user", "سؤال قدیمی")
        session.add("assistant", "پاسخ قدیمی")
        session.add("user", "شرح خطای اخیر " * 20)
        session.add("assistant", "پاسخ فنی طولانی " * 80)

        history = session.history(max_chars=300)

        self.assertLessEqual(sum(len(m["content"]) for m in history), 300)
        self.assertEqual(history[-1]["role"], "assistant")
        self.assertTrue(any(m["role"] == "user" for m in history))
        self.assertIn("حذف شد", history[-1]["content"])

    def test_document_context_respects_total_and_per_excerpt_budget(self) -> None:
        hits = [
            hit("صفحه اول", 1, "الف" * 1000),
            hit("صفحه دوم", 2, "ب" * 1000),
            hit("صفحه سوم", 3, "ج" * 1000),
        ]
        context = build_context(hits, max_chars=500, max_excerpt_chars=300)

        self.assertLessEqual(len(context), 500)
        self.assertIn("صفحه اول", context)
        self.assertIn("کنترل هزینه", context)


class _FakeCompletions:
    def __init__(self, content: str, tokens: int = 50) -> None:
        self.content = content
        self.tokens = tokens
        self.calls = 0

    async def create(self, **kwargs):
        self.calls += 1
        return SimpleNamespace(
            choices=[SimpleNamespace(
                message=SimpleNamespace(content=self.content)
            )],
            usage=SimpleNamespace(total_tokens=self.tokens),
        )


class _FakeAsyncClient:
    def __init__(self, content: str, tokens: int = 50) -> None:
        self.completions = _FakeCompletions(content, tokens)
        self.chat = SimpleNamespace(completions=self.completions)


class _FakeStreamCompletions:
    def __init__(self, answer: str, tokens: int = 70) -> None:
        self.answer = answer
        self.tokens = tokens
        self.calls = 0

    async def create(self, **kwargs):
        self.calls += 1

        async def parts():
            yield SimpleNamespace(
                usage=None,
                choices=[SimpleNamespace(delta=SimpleNamespace(
                    content=self.answer, tool_calls=None,
                ))],
            )
            yield SimpleNamespace(
                usage=SimpleNamespace(total_tokens=self.tokens), choices=[],
            )

        return parts()


class _FakeStreamClient:
    def __init__(self, answer: str, tokens: int = 70) -> None:
        self.completions = _FakeStreamCompletions(answer, tokens)
        self.chat = SimpleNamespace(completions=self.completions)


class ModelCallReductionTests(unittest.IsolatedAsyncioTestCase):
    async def test_rewrite_cache_avoids_second_model_call(self) -> None:
        fake = _FakeAsyncClient(
            '{"query":"محاسبه هزینه و صورتحساب","service":null,'
            '"clarify":null,"options":[]}',
            tokens=41,
        )
        fresh_cache = TTLCache(max_entries=10, ttl_seconds=60)
        with (
            patch("app.agent.aclient", return_value=fake),
            patch("app.agent._rewrite_cache", fresh_cache),
        ):
            first = await rewrite("آخر ماه هزینه چطور حساب می‌شود؟", Session(id="a"))
            second = await rewrite("آخر ماه هزینه چطور حساب می‌شود؟", Session(id="b"))

        self.assertEqual(first["tokens"], 41)
        self.assertEqual(second["tokens"], 0)
        self.assertTrue(second["cached"])
        self.assertEqual(fake.completions.calls, 1)

    async def test_stateless_answer_cache_reports_zero_tokens_on_hit(self) -> None:
        fake = _FakeAsyncClient("پاسخ مستند و کوتاه", tokens=90)
        retriever = SimpleNamespace(search=lambda *args, **kwargs: [hit("راهنما")])
        plan = {
            "query": "راهنمای هزینه", "service": None, "clarify": None,
            "tokens": 10, "canonical": True,
        }
        fresh_cache = TTLCache(max_entries=10, ttl_seconds=60)
        mocked_rewrite = AsyncMock(return_value=plan)

        with (
            patch("app.agent.aclient", return_value=fake),
            patch("app.agent.get_retriever", return_value=retriever),
            patch("app.agent.rewrite", mocked_rewrite),
            patch("app.agent._stateless_answer_cache", fresh_cache),
        ):
            first: AnswerResult = await answer_once("هزینه را توضیح بده", k=3)
            second: AnswerResult = await answer_once("هزینه را توضیح بده", k=3)

        self.assertEqual(first.tokens, 100)
        self.assertFalse(first.cached)
        self.assertEqual(second.tokens, 0)
        self.assertTrue(second.cached)
        self.assertEqual(fake.completions.calls, 1)
        self.assertEqual(mocked_rewrite.await_count, 1)

    async def test_grounded_suggestions_bypass_suggestion_model(self) -> None:
        items, tokens = await next_steps(
            "چطور برنامه را مستقر کنم؟",
            "برای استقرار این مراحل مستند را انجام بده. " * 8,
            [hit("استقرار", 1), hit("تنظیم دامنه", 2), hit("مشاهده لاگ", 3)],
        )

        self.assertEqual(tokens, 0)
        self.assertTrue(items)
        self.assertTrue(any("تنظیم دامنه" in item.label for item in items))

    async def test_chat_answer_cache_replays_stream_without_second_llm_call(self) -> None:
        fake = _FakeStreamClient("این پاسخ از مستندات رسمی ساخته شده است.")
        retriever = SimpleNamespace(search=lambda *args, **kwargs: [hit("راهنما")])
        plan = {
            "query": "راهنمای هزینه", "service": None, "clarify": None,
            "tokens": 0, "canonical": True,
        }
        fresh_cache = TTLCache(max_entries=10, ttl_seconds=60)
        fake_sessions = SimpleNamespace(
            get=lambda session_id, client_id=None: Session(id=session_id)
        )

        with (
            patch("app.agent.aclient", return_value=fake),
            patch("app.agent.get_retriever", return_value=retriever),
            patch("app.agent.rewrite", AsyncMock(return_value=plan)),
            patch("app.agent.sessions", fake_sessions),
            patch("app.agent._chat_answer_cache", fresh_cache),
        ):
            first = [event async for event in chat_stream("هزینه را بگو", "one")]
            second = [event async for event in chat_stream("هزینه را بگو", "two")]

        def done(events: list[str]) -> dict:
            block = next(event for event in events if event.startswith("event: done"))
            data = next(line[6:].strip() for line in block.splitlines()
                        if line.startswith("data:"))
            return json.loads(data)

        self.assertFalse(done(first)["cached"])
        self.assertTrue(done(second)["cached"])
        self.assertEqual(done(second)["tokens_used"], 0)
        self.assertEqual(fake.completions.calls, 1)


class EmbeddingCacheTests(unittest.TestCase):
    def test_equivalent_persian_forms_share_one_embedding_call(self) -> None:
        from app import llm

        calls = 0

        def create(**kwargs):
            nonlocal calls
            calls += 1
            return SimpleNamespace(
                data=[SimpleNamespace(embedding=[3.0, 4.0])],
                usage=SimpleNamespace(total_tokens=2),
            )

        fake_client = SimpleNamespace(embeddings=SimpleNamespace(create=create))
        llm._embed_query_cached.cache_clear()
        try:
            with patch("app.llm.client", return_value=fake_client):
                first = llm.embed_query("كتاب")
                second = llm.embed_query("کتاب")
        finally:
            llm._embed_query_cached.cache_clear()

        self.assertEqual(first, second)
        self.assertEqual(calls, 1)


if __name__ == "__main__":
    unittest.main()
