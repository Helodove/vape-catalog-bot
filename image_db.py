"""
Кастомные URL картинок из Supabase (таблица product_images).
Поиск по вхождению имени — одна запись покрывает товар и все его модификации.

Пример: запись "Smoant Pasito 2" → image_url
применяется к "Smoant Pasito 2 Pod Kit", "Smoant Pasito 2 Pod Kit (Malachite)" и т.д.
"""
import logging
import time
import httpx

log = logging.getLogger(__name__)

# Кеш: список [(product_name_lower, image_url)]
_entries: list[tuple[str, str]] = []
_cache_ts: float = 0
CACHE_TTL = 300  # 5 минут


async def load_images(supabase_url: str, supabase_key: str) -> None:
    """Загружает все записи из product_images и обновляет кеш."""
    global _entries, _cache_ts

    now = time.monotonic()
    if _entries and now - _cache_ts < CACHE_TTL:
        return

    if not supabase_url or not supabase_key:
        return

    try:
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.get(
                f"{supabase_url}/rest/v1/product_images?select=product_name,image_url&order=id",
                headers={
                    "apikey": supabase_key,
                    "Authorization": f"Bearer {supabase_key}",
                },
            )
            if r.status_code == 200:
                rows = r.json()
                _entries = [
                    (row["product_name"].lower(), row["image_url"])
                    for row in rows
                    if row.get("product_name") and row.get("image_url")  # пропускаем строки без URL
                ]
                _cache_ts = now
                log.info("Loaded %d custom image rules from Supabase", len(_entries))
    except Exception as e:
        log.warning("Supabase image_db load error: %s", e)


def find_image(product_name: str) -> str | None:
    """
    Ищет URL картинки по имени товара.
    Возвращает первое совпадение где product_name из БД входит в название товара.
    """
    if not _entries:
        return None
    name_lower = product_name.lower()
    for entry_name, image_url in _entries:
        if entry_name in name_lower:
            return image_url
    return None
