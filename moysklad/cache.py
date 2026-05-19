import time
from typing import Any, Optional


class TTLCache:
    def __init__(self):
        self._store: dict[str, tuple[Any, float]] = {}

    def get(self, key: str) -> Optional[Any]:
        entry = self._store.get(key)
        if entry is None:
            return None
        value, expires_at = entry
        if time.monotonic() > expires_at:
            del self._store[key]
            return None
        return value

    def set(self, key: str, value: Any, ttl: int) -> None:
        self._store[key] = (value, time.monotonic() + ttl)

    def clear(self) -> None:
        self._store.clear()


TTL_FOLDERS = 3600      # 60 минут
TTL_PRODUCTS = 3600     # 60 минут
TTL_STOCK = 900         # 15 минут
TTL_IMAGES = 86400      # 24 часа

cache = TTLCache()
