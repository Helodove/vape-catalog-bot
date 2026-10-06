"""
Простой rate limit «скользящее окно» в памяти процесса.
Сервис работает в одном контейнере, поэтому внешнее хранилище не нужно.
"""
import time
from collections import deque
from typing import Callable


class RateLimiter:
    def __init__(self, max_requests: int, window_seconds: float,
                 clock: Callable[[], float] = time.monotonic):
        self.max_requests = max_requests
        self.window = window_seconds
        self._clock = clock
        self._hits: dict[str, deque[float]] = {}
        self._calls = 0

    def allow(self, key: str) -> bool:
        now = self._clock()
        hits = self._hits.setdefault(key, deque())
        while hits and now - hits[0] >= self.window:
            hits.popleft()
        self._calls += 1
        if self._calls % 1000 == 0:
            self._purge(now)
        if len(hits) >= self.max_requests:
            return False
        hits.append(now)
        return True

    def _purge(self, now: float) -> None:
        stale = [k for k, h in self._hits.items() if not h or now - h[-1] >= self.window]
        for k in stale:
            del self._hits[k]
