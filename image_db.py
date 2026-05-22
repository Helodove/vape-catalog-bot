"""
Загрузка кастомных URL картинок из Supabase.
Таблица product_images: ms_id (UUID МойСклад) → image_url
"""
import logging
import time
import httpx

log = logging.getLogger(__name__)

_cache: dict[str, str] = {}      # ms_id → image_url
_cache_ts: float = 0
CACHE_TTL = 300  # 5 минут


async def load_images(supabase_url: str, supabase_key: str) -> dict[str, str]:
    """Загружает все записи из product_images одним запросом."""
    global _cache, _cache_ts

    now = time.monotonic()
    if _cache and now - _cache_ts < CACHE_TTL:
        return _cache

    if not supabase_url or not supabase_key:
        return {}

    try:
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.get(
                f"{supabase_url}/rest/v1/product_images?select=ms_id,image_url",
                headers={
                    "apikey": supabase_key,
                    "Authorization": f"Bearer {supabase_key}",
                },
            )
            if r.status_code == 200:
                rows = r.json()
                _cache = {row["ms_id"]: row["image_url"] for row in rows if row.get("ms_id") and row.get("image_url")}
                _cache_ts = now
                log.info("Loaded %d custom images from Supabase", len(_cache))
                return _cache
    except Exception as e:
        log.warning("Supabase image_db error: %s", e)

    return _cache  # возвращаем старый кеш если обновление не удалось


def get_cached(ms_id: str) -> str | None:
    """Быстрый доступ к кешу без сетевого запроса."""
    return _cache.get(ms_id)
