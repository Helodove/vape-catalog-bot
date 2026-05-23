"""
Умное заполнение product_images:
- Ароматизаторы, расходники → группирует по линейке (первые 2 слова после префикса)
- Устройства и уникальные товары → добавляет по одному
- Новые товары МойСклад подхватятся автоматически через substring match
"""
import httpx, re, time
from collections import Counter

API = "https://vape-catalog-bot-production.up.railway.app"
SB_URL = "https://saijrkaolvpfraafwdfg.supabase.co"
SB_KEY = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6InNhaWpya2FvbHZwZnJhYWZ3ZGZnIiwicm9sZSI6InNlcnZpY2Vfcm9sZSIsImlhdCI6MTc3OTM5NTQ3OCwiZXhwIjoyMDk0OTcxNDc4fQ.8xdRT5fXLIq2pBo0MBLpi0KT3To-IWvIzGiL4a_SvC4"
SB_H = {"apikey": SB_KEY, "Authorization": f"Bearer {SB_KEY}", "Content-Type": "application/json", "Prefer": "return=minimal,resolution=ignore-duplicates"}

# Префиксы которые убираем перед группировкой
STRIP = ["Ароматизатор ", "Испаритель ", "Картридж ", "Жидкость ", "Картриджи "]

# Категории где имеет смысл группировка по линейке
GROUP_CATS = {
    "839aa744-e2eb-11ef-0a80-08b9000d3009": "Ароматизаторы",
    "15a406c3-3dd8-11ea-0a80-01bd0016ba75": "Расходники",
    "27934153-3dd8-11ea-0a80-050e00144d43": "Комплектующие",
    "207bdaea-a784-11ef-0a80-14070018687b": "Напитки",
    "8f48fdf2-25cc-11f1-0a80-18fc001d8134": "Табак",
    "360a79eb-e66c-11f0-0a80-0c370056ffdb": "Жевательный табак",
}
# Категории где НЕ группируем — каждый товар уникален
NO_GROUP_CATS = {
    "3a5bc87a-3d50-11ea-0a80-03a2000c7e6c": "Устройства",
    "07878cd2-b39a-11ea-0a80-0068000911d2": "ЭОП",
}


def strip_prefix(name):
    for p in STRIP:
        if name.startswith(p):
            return name[len(p):]
    return name


def group_key(name, words=2):
    """Первые N слов = ключ линейки."""
    parts = strip_prefix(name).split()
    return " ".join(parts[:words])


def get_products(cat_id):
    items = []
    try:
        r = httpx.get(f"{API}/v1/products?categoryId={cat_id}", timeout=15)
        items += r.json().get("items", [])
    except: pass
    try:
        subs = httpx.get(f"{API}/v1/subcategories?categoryId={cat_id}", timeout=10).json()
        for sub in subs:
            items += get_products(sub["id"])
    except: pass
    return items


def get_all_cats():
    return httpx.get(f"{API}/v1/categories", timeout=10).json()


def get_existing():
    rows = httpx.get(f"{SB_URL}/rest/v1/product_images?select=product_name&limit=2000", headers=SB_H, timeout=10).json()
    return {r["product_name"].lower() for r in rows if r.get("product_name")}


def insert(rows):
    if not rows:
        return
    httpx.post(f"{SB_URL}/rest/v1/product_images", headers=SB_H, json=rows, timeout=30)


def process():
    print("Загружаю категории...")
    cats = get_all_cats()
    existing = get_existing()
    print(f"Уже в таблице: {len(existing)} записей\n")

    to_insert = []

    for cat in cats:
        cat_id = cat["id"]
        cat_name = cat["title"]
        print(f"Сканирую: {cat_name}")
        products = get_products(cat_id)
        names = list({p["name"] for p in products})

        if cat_id in GROUP_CATS:
            # Группируем по первым 2 словам (линейка)
            counts_2 = Counter(group_key(n, 2) for n in names)
            counts_1 = Counter(group_key(n, 1) for n in names)
            added = set()

            for name in sorted(names):
                key2 = group_key(name, 2)
                key1 = group_key(name, 1)

                if counts_2[key2] >= 2:
                    # Есть 2+ товара с этим префиксом → добавляем линейку
                    entry = key2
                elif counts_1[key1] >= 2:
                    entry = key1
                else:
                    # Уникальный товар → добавляем полное имя (без категорийного префикса)
                    entry = strip_prefix(name)

                if entry.lower() not in existing and entry not in added:
                    to_insert.append({"product_name": entry, "note": cat_name})
                    added.add(entry)

            unique = len(added)
            total = len(names)
            print(f"  {total} tovarov -> {unique} zapisej (gruppirovka)")

        else:
            # Устройства и ЭОП — без группировки, каждый товар отдельно
            added = 0
            for name in names:
                if name.lower() not in existing:
                    to_insert.append({"product_name": name, "note": cat_name})
                    added += 1
            print(f"  {len(names)} товаров, добавляем {added} новых")

    print(f"\nВставляю {len(to_insert)} записей...")
    # Пакетная вставка по 200
    for i in range(0, len(to_insert), 200):
        insert(to_insert[i:i+200])
        time.sleep(0.3)

    # Финальный счёт
    total = len(httpx.get(f"{SB_URL}/rest/v1/product_images?select=id&limit=3000", headers=SB_H, timeout=10).json())
    print(f"\nГотово! Всего в таблице: {total} записей")


if __name__ == "__main__":
    process()
