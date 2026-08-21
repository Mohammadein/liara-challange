from __future__ import annotations

import unittest
import uuid
from pathlib import Path

from app.project import ProjectProfile
from app.session import (
    MAX_CONTEXT_TURNS,
    SessionAccessError,
    SessionNotFoundError,
    SessionStore,
)


class SessionStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        # A direct file under the workspace works in managed Windows sandboxes.
        self.path = Path.cwd() / "data" / f"session-test-{uuid.uuid4().hex}.db"
        self.store = SessionStore(self.path)

    def tearDown(self) -> None:
        for suffix in ("", "-wal", "-shm"):
            (Path(str(self.path) + suffix)).unlink(missing_ok=True)

    def test_persists_full_transcript_but_bounds_llm_context(self) -> None:
        session = self.store.create("browser-owner-1234")
        for i in range(MAX_CONTEXT_TURNS + 2):
            session.add("user", f"question {i}")
            session.add("assistant", f"answer {i}")

        restarted = SessionStore(self.path)
        loaded, messages = restarted.messages(session.id, "browser-owner-1234")

        self.assertEqual(len(messages), (MAX_CONTEXT_TURNS + 2) * 2)
        self.assertEqual(len(loaded.history()), MAX_CONTEXT_TURNS * 2)
        self.assertEqual(messages[0]["content"], "question 0")
        self.assertEqual(loaded.history()[0]["content"], "question 2")
        self.assertEqual(loaded.title, "question 0")

    def test_sources_profile_rename_delete_and_owner_isolation(self) -> None:
        session = self.store.create("browser-owner-1234")
        session.profile = ProjectProfile(platform="django", experience="beginner")
        session.variant = "Liara CLI"
        session.save()
        session.add("user", "چطور دیپلوی کنم؟")
        session.add("assistant", "پاسخ", sources=[{
            "title": "استقرار", "section": "CLI",
            "url": "https://docs.liara.ir/example", "service": "paas",
        }])

        with self.assertRaises(SessionAccessError):
            self.store.messages(session.id, "another-browser-1234")

        loaded, messages = SessionStore(self.path).messages(
            session.id, "browser-owner-1234"
        )
        self.assertEqual(loaded.profile.platform, "django")
        self.assertEqual(loaded.variant, "Liara CLI")
        self.assertEqual(messages[1]["sources"][0]["title"], "استقرار")

        self.store.rename(session.id, "browser-owner-1234", "گفتگوی استقرار")
        self.assertEqual(self.store.list("browser-owner-1234")[0]["title"],
                         "گفتگوی استقرار")
        self.store.delete(session.id, "browser-owner-1234")
        with self.assertRaises(SessionNotFoundError):
            self.store.get(session.id, "browser-owner-1234", create=False)

    def test_cleanup_removes_only_empty_sessions(self) -> None:
        filled = self.store.create("browser-owner-1234")
        filled.add("user", "سلام")
        empty = self.store.create("browser-owner-1234")
        same_empty = self.store.create("browser-owner-1234")

        self.assertEqual(same_empty.id, empty.id)

        self.assertEqual(self.store.delete_empty("browser-owner-1234"), 1)
        with self.assertRaises(SessionNotFoundError):
            self.store.get(empty.id, "browser-owner-1234", create=False)
        self.assertEqual(
            self.store.get(filled.id, "browser-owner-1234", create=False).id,
            filled.id,
        )


if __name__ == "__main__":
    unittest.main()
