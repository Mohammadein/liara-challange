"""Persistent conversation sessions backed by SQLite.

The LLM only receives the last few turns to keep token usage bounded, while the
full transcript remains available to the user and can be reopened later.
"""

from __future__ import annotations

import json
import re
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field, is_dataclass
from pathlib import Path
from typing import Any

from app.flows import FlowState
from app.settings import settings

MAX_CONTEXT_TURNS = 6
DEFAULT_TITLE = "گفتگوی جدید"


class SessionAccessError(Exception):
    """Raised when an anonymous browser tries to access another owner session."""


class SessionNotFoundError(Exception):
    pass


@dataclass
class Session:
    id: str
    owner_id: str = ""
    title: str = DEFAULT_TITLE
    turns: list[dict[str, Any]] = field(default_factory=list)
    service: str | None = None
    variant: str | None = None
    # پلتفرمی که کاربر در گفتگو **گفته** — جدا از پروفایل فرم، چون بیشتر
    # کاربرها فرم را پر نمی‌کنند ولی در پیام اول می‌گویند «داکر». اگر این را
    # نگه نداریم، پیام سوم دوباره پاسخ عمومی می‌گیرد.
    platform: str | None = None
    profile: object | None = None
    # فرآیند چندمرحله‌ای در جریان. کنار پروفایل می‌ماند نه در حافظه‌ی موقت،
    # چون کاربر ممکن است قدم سوم را فردا ادامه بدهد.
    flow: FlowState | None = None
    created_at: float = field(default_factory=time.time)
    touched: float = field(default_factory=time.time)
    _store: "SessionStore | None" = field(default=None, repr=False, compare=False)

    def add(
        self,
        role: str,
        content: str,
        *,
        sources: list[dict[str, Any]] | None = None,
        suggestions: list[dict[str, Any]] | None = None,
    ) -> None:
        message = {
            "role": role,
            "content": content,
            "sources": sources or [],
            "suggestions": suggestions or [],
            "created_at": time.time(),
        }
        self.turns.append(message)
        del self.turns[:-MAX_CONTEXT_TURNS * 2]
        self.touched = message["created_at"]
        if self._store:
            self._store.add_message(self, message)

    def history(self, max_chars: int | None = None) -> list[dict[str, str]]:
        """OpenAI context با سقف turn و کاراکتر؛ transcript کامل در DB می‌ماند."""
        budget = max_chars if max_chars is not None else settings.max_history_chars
        selected: list[dict[str, str]] = []
        remaining = budget

        def clip(text: str, limit: int) -> str:
            if len(text) <= limit:
                return text
            marker = "\n…[بخش میانی حذف شد]…\n"
            available = max(1, limit - len(marker))
            head = max(1, int(available * 0.6))
            tail = max(0, available - head)
            return f"{text[:head]}{marker}{text[-tail:] if tail else ''}"

        for turn in reversed(self.turns[-MAX_CONTEXT_TURNS * 2:]):
            content = str(turn["content"])
            if len(content) <= remaining:
                selected.append({"role": turn["role"], "content": content})
                remaining -= len(content)
                continue

            if remaining < 40:
                break

            # اگر تازه‌ترین پاسخ خیلی بلند بود همه بودجه را نمی‌بلعد؛ بخشی
            # برای پیام کاربری که آن پاسخ را ساخته رزرو می‌شود.
            limit = remaining
            if not selected and turn["role"] == "assistant":
                limit = max(100, int(remaining * 0.7))
            clipped = clip(content, limit)
            selected.append({"role": turn["role"], "content": clipped})
            remaining -= len(clipped)

        return list(reversed(selected))

    def transcript(self, limit: int = 4) -> str:
        return "\n".join(
            f"{'کاربر' if t['role'] == 'user' else 'دستیار'}: {t['content'][:300]}"
            for t in self.turns[-limit:]
        )

    def save(self) -> None:
        if self._store:
            self._store.save_state(self)


class SessionStore:
    def __init__(self, db_path: str | Path | None = None) -> None:
        self.db_path = Path(db_path or settings.session_db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._init_db()

    @contextmanager
    def _connect(self):
        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 10000")
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _init_db(self) -> None:
        with self._lock, self._connect() as conn:
            conn.execute("PRAGMA journal_mode = WAL")
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS sessions (
                    id TEXT PRIMARY KEY,
                    owner_id TEXT NOT NULL DEFAULT '',
                    title TEXT NOT NULL DEFAULT 'گفتگوی جدید',
                    service TEXT,
                    variant TEXT,
                    profile_json TEXT,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS ix_sessions_owner_updated
                    ON sessions(owner_id, updated_at DESC);
                CREATE TABLE IF NOT EXISTS messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
                    role TEXT NOT NULL CHECK(role IN ('user', 'assistant')),
                    content TEXT NOT NULL,
                    sources_json TEXT NOT NULL DEFAULT '[]',
                    created_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS ix_messages_session_id
                    ON messages(session_id, id);
                """
            )
            # مهاجرت درجا برای دیتابیس‌هایی که قبل از افزوده‌شدن فرآیند و
            # پیشنهادها ساخته شده‌اند. بدون این، یک دیپلوی روی دیسک موجود با
            # «no such column» می‌افتد.
            self._add_column(conn, "sessions", "flow_json", "TEXT")
            self._add_column(conn, "sessions", "platform", "TEXT")
            self._add_column(
                conn, "messages", "suggestions_json", "TEXT NOT NULL DEFAULT '[]'"
            )

    @staticmethod
    def _add_column(conn: sqlite3.Connection, table: str, column: str, decl: str) -> None:
        existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
        if column not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")

    @staticmethod
    def _profile_from_json(raw: str | None) -> object | None:
        if not raw:
            return None
        try:
            from app.project import ProjectProfile
            return ProjectProfile(**json.loads(raw))
        except Exception:
            return None

    @staticmethod
    def _profile_to_json(profile: object | None) -> str | None:
        if profile is None:
            return None
        if is_dataclass(profile):
            return json.dumps(asdict(profile), ensure_ascii=False)
        return None

    @staticmethod
    def _flow_from_json(raw: str | None) -> FlowState | None:
        if not raw:
            return None
        try:
            return FlowState.from_dict(json.loads(raw))
        except Exception:
            return None

    @staticmethod
    def _flow_to_json(flow: FlowState | None) -> str | None:
        return json.dumps(flow.as_dict(), ensure_ascii=False) if flow else None

    def _row_to_session(self, row: sqlite3.Row, turns: list[dict]) -> Session:
        return Session(
            id=row["id"], owner_id=row["owner_id"], title=row["title"],
            turns=turns, service=row["service"], variant=row["variant"],
            platform=self._column(row, "platform"),
            profile=self._profile_from_json(row["profile_json"]),
            flow=self._flow_from_json(self._column(row, "flow_json")),
            created_at=row["created_at"], touched=row["updated_at"], _store=self,
        )

    @staticmethod
    def _column(row: sqlite3.Row, name: str) -> Any:
        return row[name] if name in row.keys() else None

    @classmethod
    def _message_from_row(cls, row: sqlite3.Row) -> dict[str, Any]:
        def parse(raw: str | None) -> list:
            try:
                value = json.loads(raw or "[]")
            except json.JSONDecodeError:
                return []
            return value if isinstance(value, list) else []

        return {
            "id": row["id"], "role": row["role"], "content": row["content"],
            "sources": parse(row["sources_json"]),
            "suggestions": parse(cls._column(row, "suggestions_json")),
            "created_at": row["created_at"],
        }

    def create(self, owner_id: str, session_id: str | None = None) -> Session:
        sid = session_id or str(uuid.uuid4())
        now = time.time()
        with self._lock, self._connect() as conn:
            # API ساخت گفتگو هم idempotent است: هر کاربر حداکثر یک سشن خالی.
            if session_id is None:
                existing = conn.execute(
                    """SELECT * FROM sessions s
                       WHERE s.owner_id = ?
                         AND NOT EXISTS (
                             SELECT 1 FROM messages m WHERE m.session_id = s.id
                         )
                       ORDER BY s.updated_at DESC LIMIT 1""",
                    (owner_id,),
                ).fetchone()
                if existing is not None:
                    return self._row_to_session(existing, [])
            conn.execute(
                "INSERT INTO sessions(id, owner_id, title, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (sid, owner_id, DEFAULT_TITLE, now, now),
            )
            row = conn.execute("SELECT * FROM sessions WHERE id = ?", (sid,)).fetchone()
        return self._row_to_session(row, [])

    def healthcheck(self) -> bool:
        """بررسی واقعی دسترسی SQLite برای readiness، بدون تغییر داده."""
        try:
            with self._lock, self._connect() as conn:
                return conn.execute("SELECT 1").fetchone()[0] == 1
        except Exception:
            return False

    def get(
        self,
        session_id: str,
        owner_id: str | None = None,
        *,
        create: bool = True,
    ) -> Session:
        with self._lock, self._connect() as conn:
            row = conn.execute("SELECT * FROM sessions WHERE id = ?", (session_id,)).fetchone()
            if row is None:
                if not create:
                    raise SessionNotFoundError(session_id)
                return self.create(owner_id or "", session_id)
            if owner_id is not None and row["owner_id"] not in ("", owner_id):
                raise SessionAccessError(session_id)
            if owner_id and not row["owner_id"]:
                conn.execute(
                    "UPDATE sessions SET owner_id = ? WHERE id = ?",
                    (owner_id, session_id),
                )
                row = conn.execute("SELECT * FROM sessions WHERE id = ?", (session_id,)).fetchone()
            msg_rows = conn.execute(
                "SELECT * FROM messages WHERE session_id = ? ORDER BY id DESC LIMIT ?",
                (session_id, MAX_CONTEXT_TURNS * 2),
            ).fetchall()
        turns = [self._message_from_row(r) for r in reversed(msg_rows)]
        return self._row_to_session(row, turns)

    def add_message(self, session: Session, message: dict[str, Any]) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                "INSERT INTO messages(session_id, role, content, sources_json, "
                "suggestions_json, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (session.id, message["role"], message["content"],
                 json.dumps(message.get("sources", []), ensure_ascii=False),
                 json.dumps(message.get("suggestions", []), ensure_ascii=False),
                 message["created_at"]),
            )
            if message["role"] == "user" and session.title == DEFAULT_TITLE:
                title = re.sub(r"\s+", " ", message["content"]).strip()[:60]
                session.title = title or DEFAULT_TITLE
            self._write_state(conn, session, message["created_at"])

    def save_state(self, session: Session) -> None:
        session.touched = time.time()
        with self._lock, self._connect() as conn:
            self._write_state(conn, session, session.touched)

    def _write_state(
        self, conn: sqlite3.Connection, session: Session, when: float,
    ) -> None:
        conn.execute(
            "UPDATE sessions SET title = ?, service = ?, variant = ?, "
            "platform = ?, profile_json = ?, flow_json = ?, updated_at = ? "
            "WHERE id = ?",
            (session.title, session.service, session.variant, session.platform,
             self._profile_to_json(session.profile),
             self._flow_to_json(session.flow), when, session.id),
        )

    def list(self, owner_id: str, limit: int = 100) -> list[dict[str, Any]]:
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                """SELECT s.*, COUNT(m.id) AS message_count
                   FROM sessions s LEFT JOIN messages m ON m.session_id = s.id
                   WHERE s.owner_id = ?
                   GROUP BY s.id ORDER BY s.updated_at DESC LIMIT ?""",
                (owner_id, limit),
            ).fetchall()
        return [
            {"id": r["id"], "title": r["title"], "created_at": r["created_at"],
             "updated_at": r["updated_at"], "message_count": r["message_count"]}
            for r in rows
        ]

    def messages(self, session_id: str, owner_id: str) -> tuple[Session, list[dict]]:
        session = self.get(session_id, owner_id, create=False)
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM messages WHERE session_id = ? ORDER BY id",
                (session_id,),
            ).fetchall()
        return session, [self._message_from_row(r) for r in rows]

    def rename(self, session_id: str, owner_id: str, title: str) -> None:
        self.get(session_id, owner_id, create=False)
        with self._lock, self._connect() as conn:
            conn.execute(
                "UPDATE sessions SET title = ?, updated_at = ? WHERE id = ?",
                (title.strip()[:80] or DEFAULT_TITLE, time.time(), session_id),
            )

    def delete(self, session_id: str, owner_id: str) -> None:
        self.get(session_id, owner_id, create=False)
        with self._lock, self._connect() as conn:
            conn.execute("DELETE FROM sessions WHERE id = ?", (session_id,))

    def delete_empty(self, owner_id: str) -> int:
        """Remove abandoned sessions that never received a message."""
        with self._lock, self._connect() as conn:
            cursor = conn.execute(
                """DELETE FROM sessions
                   WHERE owner_id = ?
                     AND NOT EXISTS (
                         SELECT 1 FROM messages WHERE messages.session_id = sessions.id
                     )""",
                (owner_id,),
            )
            return cursor.rowcount


sessions = SessionStore()
