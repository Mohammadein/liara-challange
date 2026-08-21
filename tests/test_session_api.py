from __future__ import annotations

import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.main import app
from app.session import SessionStore
from app.settings import settings


class SessionApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.path = Path.cwd() / "data" / f"session-api-test-{uuid.uuid4().hex}.db"
        self.store = SessionStore(self.path)
        self.owner = "browser-test-owner-1234"

    def tearDown(self) -> None:
        for suffix in ("", "-wal", "-shm"):
            Path(str(self.path) + suffix).unlink(missing_ok=True)

    def test_create_chat_reopen_rename_isolate_and_delete(self) -> None:
        with (
            patch("app.api_sessions.sessions", self.store),
            patch("app.mock.sessions", self.store),
            patch.object(settings, "use_mock", True),
            TestClient(app) as client,
        ):
            page = client.get("/")
            self.assertEqual(page.status_code, 200)
            self.assertIn('id="sessions"', page.text)

            created = client.post("/api/sessions", json={"client_id": self.owner})
            self.assertEqual(created.status_code, 201)
            session_id = created.json()["id"]

            response = client.post("/api/chat", json={
                "message": "نسخه پایتون رو کجا تعیین کنم؟",
                "session_id": session_id,
                "client_id": self.owner,
            })
            self.assertEqual(response.status_code, 200)
            self.assertIn("event: done", response.text)

            detail = client.get(
                f"/api/sessions/{session_id}", params={"client_id": self.owner}
            )
            self.assertEqual(detail.status_code, 200)
            messages = detail.json()["messages"]
            self.assertEqual(len(messages), 2)
            self.assertTrue(messages[1]["sources"])

            forbidden = client.get(
                f"/api/sessions/{session_id}",
                params={"client_id": "other-browser-owner-1234"},
            )
            self.assertEqual(forbidden.status_code, 404)

            renamed = client.patch(f"/api/sessions/{session_id}", json={
                "client_id": self.owner, "title": "تست ادامه گفتگو",
            })
            self.assertEqual(renamed.status_code, 200)
            listed = client.get("/api/sessions", params={"client_id": self.owner})
            self.assertEqual(listed.json()["items"][0]["title"], "تست ادامه گفتگو")

            deleted = client.delete(
                f"/api/sessions/{session_id}", params={"client_id": self.owner}
            )
            self.assertEqual(deleted.status_code, 204)

            abandoned = client.post("/api/sessions", json={"client_id": self.owner})
            self.assertEqual(abandoned.status_code, 201)
            duplicate = client.post("/api/sessions", json={"client_id": self.owner})
            self.assertEqual(duplicate.json()["id"], abandoned.json()["id"])
            cleanup = client.delete(
                "/api/sessions/empty", params={"client_id": self.owner}
            )
            self.assertEqual(cleanup.status_code, 200)
            self.assertEqual(cleanup.json()["deleted"], 1)
            self.assertEqual(
                client.get("/api/sessions", params={"client_id": self.owner}).json()["items"],
                [],
            )


if __name__ == "__main__":
    unittest.main()
