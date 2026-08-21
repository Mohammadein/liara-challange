from __future__ import annotations

import unittest

from app.agent import (
    database_clarification,
    database_setup_route,
    flow_decision,
    liara_cli_route,
    python_version_route,
    rewrite,
)
from app.flows import FLOWS, FlowState, match_flow, start_prompt
from app.project import ProjectProfile
from app.session import Session


class DatabaseClarificationTests(unittest.TestCase):
    def test_generic_database_setup_requires_engine(self) -> None:
        session = Session(id="test")
        clarification = database_clarification(
            "چطوری دیتابیس رو مستقر کنم؟", session
        )
        self.assertIn("کدام دیتابیس", clarification or "")

    def test_specific_engine_does_not_require_clarification(self) -> None:
        session = Session(id="test")
        self.assertIsNone(database_clarification(
            "چطوری PostgreSQL رو مستقر کنم؟", session
        ))
        self.assertEqual(
            database_setup_route("چطوری PostgreSQL رو مستقر کنم؟", session),
            ("راه‌اندازی سریع دیتابیس PostgreSQL با کنسول لیارا", "dbaas",
             "/dbaas/postgresql/"),
        )

    def test_previous_user_message_can_supply_engine(self) -> None:
        session = Session(id="test")
        session.add("user", "برای پروژه‌ام MongoDB می‌خوام")
        session.add("assistant", "حتماً")
        self.assertIsNone(database_clarification(
            "چطوری دیتابیس رو راه‌اندازی کنم؟", session
        ))

    def test_project_profile_can_supply_engine(self) -> None:
        session = Session(
            id="test", profile=ProjectProfile(database="postgresql")
        )
        self.assertIsNone(database_clarification(
            "چطوری دیتابیس رو راه‌اندازی کنم؟", session
        ))

    def test_generic_migration_remains_searchable(self) -> None:
        session = Session(id="test")
        self.assertIsNone(database_clarification(
            "دیتابیسم رو از یک سرویس به سرویس دیگه منتقل کنم", session
        ))

class DatabaseRewriteTests(unittest.IsolatedAsyncioTestCase):
    async def test_generic_setup_short_circuits_before_search_and_llm(self) -> None:
        result = await rewrite("چطوری دیتابیس رو مستقر کنم؟", Session(id="test"))
        self.assertEqual(result["tokens"], 0)
        self.assertEqual(result["service"], "dbaas")
        self.assertIn("کدام دیتابیس", result["clarify"])

    async def test_engine_answer_routes_to_quick_setup(self) -> None:
        session = Session(id="test")
        session.add("user", "چطوری دیتابیس رو مستقر کنم؟")
        session.add("assistant", "کدام دیتابیس را می‌خواهید راه‌اندازی کنید؟")

        result = await rewrite("PostgreSQL", session)

        self.assertEqual(result["tokens"], 0)
        self.assertIsNone(result["clarify"])
        self.assertEqual(result["service"], "dbaas")
        self.assertTrue(result["canonical"])
        self.assertEqual(
            result["query"], "راه‌اندازی سریع دیتابیس PostgreSQL با کنسول لیارا"
        )


class PythonVersionRewriteTests(unittest.IsolatedAsyncioTestCase):
    async def test_python_version_after_deploy_is_not_ambiguous(self) -> None:
        question = "نسخه پایتون رو میتونم بعد از استقرار سرویس عوض کنم؟"
        self.assertEqual(
            python_version_route(question),
            ("تغییر نسخه پیش‌فرض Python در پلتفرم Python با فایل liara.json",
             "paas", "/paas/python/"),
        )

        result = await rewrite(question, Session(id="test"))

        self.assertEqual(result["tokens"], 0)
        self.assertIsNone(result["clarify"])
        self.assertEqual(result["service"], "paas")
        self.assertTrue(result["canonical"])
        self.assertEqual(result["url_prefix"], "/paas/python/")
        self.assertIn("liara.json", result["query"])

    async def test_framework_name_selects_its_own_version_docs(self) -> None:
        result = await rewrite(
            "نسخه پایتون Django رو چطور عوض کنم؟", Session(id="test")
        )
        self.assertIn("Django", result["query"])
        self.assertIsNone(result["clarify"])


class CliFollowupRewriteTests(unittest.IsolatedAsyncioTestCase):
    async def test_cli_install_routes_to_install_page(self) -> None:
        result = await rewrite("cli چجوری ستاپ کنم", Session(id="test"))
        self.assertEqual(result["url_prefix"], "/references/cli/install/")
        self.assertTrue(result["canonical"])

    async def test_capabilities_followup_keeps_cli_context(self) -> None:
        session = Session(id="test")
        session.add("user", "cli چجوری ستاپ کنم")
        session.add("assistant", "Liara CLI را نصب کنید.")

        self.assertEqual(
            liara_cli_route("چه امکاناتی داره؟", session),
            ("معرفی امکانات و دسته‌بندی دستورهای Liara CLI",
             "references", "/references/cli/about/"),
        )
        result = await rewrite("چه امکاناتی داره؟", session)
        self.assertEqual(result["tokens"], 0)
        self.assertEqual(result["url_prefix"], "/references/cli/about/")
        self.assertIn("Liara CLI", result["query"])

    async def test_generic_capabilities_without_cli_context_is_not_forced(self) -> None:
        session = Session(id="test")
        self.assertIsNone(liara_cli_route("چه امکاناتی داره؟", session))


class FlowRoutingTests(unittest.TestCase):
    def test_troubleshooting_title_beats_generic_deploy_topic(self) -> None:
        matched = match_flow(
            "قدم به قدم راهنمایی کن: عیب‌یابی استقرار ناموفق"
        )

        self.assertIsNotNone(matched)
        self.assertEqual(matched.id, "deploy_failure")

    def test_every_generated_start_prompt_round_trips_to_its_flow(self) -> None:
        for flow in FLOWS.values():
            with self.subTest(flow=flow.id):
                matched = match_flow(start_prompt(flow))
                self.assertIsNotNone(matched)
                self.assertEqual(matched.id, flow.id)

    def test_explicit_start_replaces_a_persisted_different_flow(self) -> None:
        session = Session(
            id="test", flow=FlowState(id="deploy_app", step=0)
        )

        decision = flow_decision(
            "قدم به قدم راهنمایی کن: عیب‌یابی استقرار ناموفق", session
        )

        self.assertIsNotNone(decision)
        action, flow, state = decision
        self.assertEqual(action, "start")
        self.assertEqual(flow.id, "deploy_failure")
        self.assertEqual(state.id, "deploy_failure")
        self.assertEqual(state.step, 0)


if __name__ == "__main__":
    unittest.main()
