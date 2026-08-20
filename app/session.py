"""
حافظه‌ی مکالمه — درون‌حافظه‌ای.

برای یک اپلیکیشن تک‌نمونه‌ای کافی است. اگر بعداً چند نمونه شد، همین رابط
با Redis جایگزین می‌شود بدون تغییر در بقیه کد.

سقف‌ها عمدی‌اند: مکالمه‌ی بلند هم هزینه‌ی توکن می‌سازد هم کیفیت را پایین
می‌آورد، پس فقط چند نوبت آخر نگه داشته می‌شود.
"""

from __future__ import annotations

import time
from collections import OrderedDict
from dataclasses import dataclass, field

MAX_SESSIONS = 500
MAX_TURNS = 6           # نوبت (کاربر + دستیار)
SESSION_TTL = 60 * 60   # یک ساعت


@dataclass
class Session:
    id: str
    turns: list[dict] = field(default_factory=list)
    service: str | None = None    # سرویسی که کاربر رویش کار می‌کند
    variant: str | None = None    # فریم‌ورک/روش ترجیحی کاربر
    touched: float = field(default_factory=time.time)

    def add(self, role: str, content: str) -> None:
        self.turns.append({"role": role, "content": content})
        del self.turns[:-MAX_TURNS * 2]
        self.touched = time.time()

    def history(self) -> list[dict]:
        return list(self.turns)

    def transcript(self, limit: int = 4) -> str:
        """چند نوبت آخر به‌صورت متن ساده — برای بازنویسی کوئری."""
        return "\n".join(
            f"{'کاربر' if t['role'] == 'user' else 'دستیار'}: {t['content'][:300]}"
            for t in self.turns[-limit:]
        )


class SessionStore:
    def __init__(self) -> None:
        self._data: OrderedDict[str, Session] = OrderedDict()

    def get(self, session_id: str) -> Session:
        self._evict()
        s = self._data.get(session_id)
        if s is None:
            s = Session(id=session_id)
            self._data[session_id] = s
        self._data.move_to_end(session_id)
        s.touched = time.time()
        return s

    def _evict(self) -> None:
        now = time.time()
        for sid in [k for k, v in self._data.items() if now - v.touched > SESSION_TTL]:
            del self._data[sid]
        while len(self._data) > MAX_SESSIONS:
            self._data.popitem(last=False)


sessions = SessionStore()
