"""Cache کوچک، TTLدار و thread-safe برای حذف تماس‌های تکراری مدل."""

from __future__ import annotations

import copy
import hashlib
import json
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any


def cache_key(namespace: str, *parts: Any) -> str:
    """کلید ثابت بدون نگه‌داشتن سؤال/تاریخچه خام کاربر در dictionary."""
    payload = json.dumps(
        parts, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    digest = hashlib.sha256(payload).hexdigest()
    return f"{namespace}:{digest}"


@dataclass
class _Entry:
    value: Any
    expires_at: float


class TTLCache:
    """LRU + TTL؛ مقدارها copy می‌شوند تا مصرف‌کننده Cache را mutate نکند."""

    def __init__(self, max_entries: int, ttl_seconds: int) -> None:
        self.max_entries = max_entries
        self.ttl_seconds = ttl_seconds
        self._items: OrderedDict[str, _Entry] = OrderedDict()
        self._lock = threading.Lock()

    @property
    def enabled(self) -> bool:
        return self.ttl_seconds > 0 and self.max_entries > 0

    def get(self, key: str) -> tuple[bool, Any | None]:
        if not self.enabled:
            return False, None
        now = time.monotonic()
        with self._lock:
            entry = self._items.get(key)
            if entry is None:
                return False, None
            if entry.expires_at <= now:
                del self._items[key]
                return False, None
            self._items.move_to_end(key)
            return True, copy.deepcopy(entry.value)

    def set(self, key: str, value: Any) -> None:
        if not self.enabled:
            return
        with self._lock:
            self._items[key] = _Entry(
                value=copy.deepcopy(value),
                expires_at=time.monotonic() + self.ttl_seconds,
            )
            self._items.move_to_end(key)
            while len(self._items) > self.max_entries:
                self._items.popitem(last=False)

    def clear(self) -> None:
        with self._lock:
            self._items.clear()

    def __len__(self) -> int:
        with self._lock:
            return len(self._items)
