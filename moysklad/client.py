import logging
import urllib.parse
from typing import Optional
import httpx
from .models import Product, ProductFolder, Store, Attribute, SalePrice, PriceType
from .cache import cache, TTL_FOLDERS, TTL_PRODUCTS, TTL_STOCK, TTL_IMAGES

log = logging.getLogger(__name__)

BASE_URL = "https://api.moysklad.ru/api/remap/1.2"
ALLOWED_TYPES = {"product", "variant"}


class MoySkladClient:
    def __init__(self, token: str):
        self._headers = {"Authorization": f"Bearer {token}"}

    async def _get(self, path: str, params: dict = None) -> Optional[dict]:
        # МойСклад требует filter без URL-кодирования внутренних символов =, : и /
        url = BASE_URL + path
        if params:
            parts = []
            for k, v in params.items():
                if k == "filter":
                    parts.append(f"filter={v}")
                else:
                    parts.append(f"{k}={urllib.parse.quote(str(v), safe='')}")
            url += "?" + "&".join(parts)
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                r = await client.get(url, headers=self._headers)
                r.raise_for_status()
                return r.json()
        except httpx.HTTPStatusError as e:
            log.error("MoySklad HTTP %d: %s — %s", e.response.status_code, url, e.response.text[:300])
            return None
        except Exception as e:
            log.error("MoySklad request failed: %s — %s", url, e)
            return None

    async def get_stores(self) -> list[Store]:
        key = "stores"
        cached = cache.get(key)
        if cached is not None:
            return cached
        data = await self._get("/entity/store", {"limit": 100})
        if data is None:
            return []
        stores = [Store(id=r["id"], name=r["name"], href=r["meta"]["href"])
                  for r in data.get("rows", [])
                  if r.get("name", "").lower().startswith("г ")]
        cache.set(key, stores, 86400)
        return stores

    async def get_root_folders(self) -> list[ProductFolder]:
        key = "folders:root"
        cached = cache.get(key)
        if cached is not None:
            return cached
        data = await self._get("/entity/productfolder", {"filter": "pathName="})
        if data is None:
            return []
        folders = sorted([_parse_folder(r) for r in data.get("rows", [])], key=lambda f: f.name)
        cache.set(key, folders, TTL_FOLDERS)
        return folders

    async def get_all_subfolders(self, folder_href: str) -> list[ProductFolder]:
        """Рекурсивно все подпапки любой глубины (с кэшом)."""
        key = f"all_subfolders:{folder_href}"
        cached = cache.get(key)
        if cached is not None:
            return cached
        direct = await self.get_subfolders(folder_href)
        result = list(direct)
        for sf in direct:
            nested = await self.get_all_subfolders(sf.href)
            result.extend(nested)
        cache.set(key, result, TTL_FOLDERS)
        return result

    async def get_subfolders(self, folder_href: str) -> list[ProductFolder]:
        key = f"folders:{folder_href}"
        cached = cache.get(key)
        if cached is not None:
            return cached
        data = await self._get("/entity/productfolder", {"filter": f"productFolder={folder_href}"})
        if data is None:
            return []
        folders = sorted([_parse_folder(r) for r in data.get("rows", [])], key=lambda f: f.name)
        cache.set(key, folders, TTL_FOLDERS)
        return folders

    async def get_products(self, folder_href: str, store_href: str | None = None) -> list[Product]:
        key = f"products:{folder_href}:{store_href or 'all'}"
        cached = cache.get(key)
        if cached is not None:
            return cached

        # Получаем имя папки чтобы использовать pathName фильтр для подпапок
        folder_id = folder_href.rstrip("/").split("/")[-1]
        folder_info = await self._get(f"/entity/productfolder/{folder_id}")
        folder_name = (folder_info or {}).get("name", "")

        # Запрос 1: товары напрямую в этой папке
        data1 = await self._get("/entity/assortment", {
            "filter": f"productFolder={folder_href}",
            "limit": 200,
            "expand": "images",
        })

        # Запрос 2: товары во всех подпапках через pathName (находит любую глубину вложенности)
        data2 = None
        if folder_name:
            data2 = await self._get("/entity/assortment", {
                "filter": f"pathName~={folder_name}",
                "limit": 200,
                "expand": "images",
            })

        # Объединяем результаты, убираем дубли по id
        seen_ids: set[str] = set()
        combined_rows = []
        for row in list((data1 or {}).get("rows", [])) + list((data2 or {}).get("rows", [])):
            rid = row.get("id")
            if rid and rid not in seen_ids and row.get("meta", {}).get("type") in ALLOWED_TYPES:
                seen_ids.add(rid)
                combined_rows.append(row)

        if not combined_rows:
            cache.set(key, [], TTL_PRODUCTS)
            return []

        folder_filter = f"productFolder={folder_href}"
        products = [_parse_product(r) for r in combined_rows]
        products = await self._enrich_stock_bulk(products, folder_href, store_href, folder_filter)

        # Агрегируем остатки вариантов → родительский товар
        # Варианты, чей родитель есть в списке, скрываем — их заменяет карточка родителя с выбором цвета
        by_id = {p.id: p for p in products}
        parent_ids_with_variants: set[str] = set()
        for p in products:
            if p.entity_type == "variant" and p.parent_product_id and p.parent_product_id in by_id:
                parent_ids_with_variants.add(p.parent_product_id)
                parent = by_id[p.parent_product_id]
                parent.stock = (parent.stock or 0.0) + (p.stock or 0.0)

        # Убираем варианты, у которых родитель присутствует в списке
        products = [p for p in products if not (
            p.entity_type == "variant" and p.parent_product_id in parent_ids_with_variants
        )]

        cache.set(key, products, TTL_PRODUCTS)
        return products

    async def search_products(self, query: str) -> list[Product]:
        data = await self._get("/entity/product", {"search": query, "limit": 100})
        if data is None:
            return []
        return [_parse_product(r) for r in data.get("rows", [])]

    async def get_product_variants(self, product_id: str) -> list["Product"]:
        key = f"variants:{product_id}"
        cached = cache.get(key)
        if cached is not None:
            return cached

        # Узнаём папку товара
        prod_data = await self._get(f"/entity/product/{product_id}")
        folder_href = (prod_data or {}).get("productFolder", {}).get("meta", {}).get("href")
        if not folder_href:
            cache.set(key, [], TTL_PRODUCTS)
            return []

        # Запрашиваем ассортимент папки напрямую (НЕ через get_products — он фильтрует варианты)
        subfolders = await self.get_subfolders(folder_href)
        all_hrefs = [folder_href] + [sf.href for sf in subfolders]
        folder_filter = ";".join(f"productFolder={h}" for h in all_hrefs)

        data = await self._get("/entity/assortment", {
            "filter": folder_filter,
            "limit": 200,
        })
        if not data:
            cache.set(key, [], TTL_PRODUCTS)
            return []

        all_items = [_parse_product(r) for r in data.get("rows", [])
                     if r.get("meta", {}).get("type") in ALLOWED_TYPES]
        variants = [p for p in all_items
                    if p.entity_type == "variant" and p.parent_product_id == product_id]
        cache.set(key, variants, TTL_PRODUCTS)
        return variants

    async def get_variants_stock(self, variant_hrefs: list[str]) -> dict[str, dict[str, float]]:
        """Bulk: возвращает {variant_href: {store_name: qty}} для списка вариантов."""
        if not variant_hrefs:
            return {}
        filter_str = ";".join(f"assortment={h}" for h in variant_hrefs)
        data = await self._get("/report/stock/bystore", {
            "filter": filter_str,
            "quantityMode": "positiveOnly",
        })
        result: dict[str, dict[str, float]] = {}
        if data:
            for row in data.get("rows", []):
                href = row.get("assortment", {}).get("meta", {}).get("href", "").split("?")[0]
                if not href:
                    continue
                stores: dict[str, float] = {}
                for entry in row.get("stockByStore", []):
                    qty = entry.get("quantity", 0.0)
                    name = entry.get("store", {}).get("name", "")
                    if qty > 0 and name.lower().startswith("г "):
                        stores[name] = stores.get(name, 0.0) + qty
                if stores:
                    result[href] = stores
        return result

    async def get_stock_by_store(self, product_href: str) -> dict[str, float]:
        """Возвращает {название_склада: количество} для одного товара/варианта."""
        key = f"bystore:{product_href}"
        cached = cache.get(key)
        if cached is not None:
            return cached
        # Определяем папку товара и берём сводку по всей папке
        entity_type = "variant" if "/entity/variant/" in product_href else "product"
        entity_id = product_href.rstrip("/").split("/")[-1]
        prod_data = await self._get(f"/entity/{entity_type}/{entity_id}")
        folder_href = (prod_data or {}).get("productFolder", {}).get("meta", {}).get("href", "")
        if not folder_href:
            cache.set(key, {}, TTL_STOCK)
            return {}
        folder_map = await self.get_folder_stock_by_store(folder_href)
        result = folder_map.get(product_href.split("?")[0], {})
        cache.set(key, result, TTL_STOCK)
        return result

    async def get_folder_stock_by_store(self, folder_href: str) -> dict[str, dict[str, float]]:
        """Возвращает {item_href: {store_name: qty}} для всей папки одним запросом."""
        key = f"folder_bystore:{folder_href}"
        cached = cache.get(key)
        if cached is not None:
            return cached
        data = await self._get("/report/stock/bystore", {
            "filter": f"productFolder={folder_href}",
        })
        result: dict[str, dict[str, float]] = {}
        if data:
            for row in data.get("rows", []):
                href = row.get("assortment", {}).get("meta", {}).get("href", "").split("?")[0]
                if not href:
                    continue
                stores: dict[str, float] = {}
                for entry in row.get("stockByStore", []):
                    qty = entry.get("quantity", 0.0)
                    name = entry.get("store", {}).get("name", "")
                    if qty > 0 and name.lower().startswith("г "):
                        stores[name] = stores.get(name, 0.0) + qty
                if stores:
                    result[href] = stores
        cache.set(key, result, TTL_STOCK)
        return result

    async def get_product_image_url(self, product_id: str, entity_type: str = "product",
                                     parent_product_id: str | None = None) -> Optional[str]:
        key = f"image:{product_id}"
        cached = cache.get(key)
        if cached is not None:
            return cached if cached != "__none__" else None

        url = await self._fetch_image(entity_type, product_id)
        # Для варианта без фото — берём фото родительского товара
        if not url and entity_type == "variant" and parent_product_id:
            url = await self._fetch_image("product", parent_product_id)

        cache.set(key, url if url else "__none__", TTL_IMAGES)
        return url

    async def _fetch_image(self, entity_type: str, entity_id: str) -> Optional[str]:
        data = await self._get(f"/entity/{entity_type}/{entity_id}/images", {"limit": 1})
        if data and data.get("rows"):
            meta = data["rows"][0].get("meta", {})
            return meta.get("downloadHref") or meta.get("href")
        return None

    async def _enrich_stock_bulk(
        self, products: list[Product], folder_href: str, store_href: str | None = None,
        folder_filter: str | None = None,
    ) -> list[Product]:
        if not products:
            return products
        effective_filter = folder_filter or f"productFolder={folder_href}"
        key = f"stock_bulk:{effective_filter}:{store_href or 'all'}"
        stock_map = cache.get(key)
        if stock_map is None:
            f = effective_filter
            if store_href:
                f += f";store={store_href}"
            stock_data = await self._get("/report/stock/all", {
                "filter": f,
                "quantityMode": "positiveOnly",
                "limit": 1000,
            })
            stock_map = _build_stock_map(stock_data) if stock_data else {}
            cache.set(key, stock_map, TTL_STOCK)
        for p in products:
            p.stock = stock_map.get(p.href, 0.0)
        return products


def _build_stock_map(stock_data: dict) -> dict:
    result = {}
    for row in stock_data.get("rows", []):
        href = row.get("meta", {}).get("href", "").split("?")[0]
        if href:
            result[href] = row.get("quantity", row.get("stock", 0.0))
    return result


def _parse_folder(row: dict) -> ProductFolder:
    meta = row.get("meta", {})
    href = meta.get("href", "")
    parent_href = None
    pf = row.get("productFolder")
    if pf:
        parent_href = pf.get("meta", {}).get("href")
    return ProductFolder(
        id=row.get("id", ""),
        name=row.get("name", ""),
        href=href,
        pathName=row.get("pathName", ""),
        parent_href=parent_href,
    )


def _parse_product(row: dict) -> Product:
    meta = row.get("meta", {})
    href = meta.get("href", "")
    entity_type = meta.get("type", "product")  # "product" или "variant"

    sale_prices = []
    for sp in row.get("salePrices", []):
        pt = sp.get("priceType", {})
        sale_prices.append(SalePrice(
            value=sp.get("value", 0),
            priceType=PriceType(name=pt.get("name", "")),
        ))

    # Обычные атрибуты (для товаров) + характеристики (для вариантов)
    attrs = []
    for a in row.get("attributes", []):
        val = a.get("value")
        if isinstance(val, dict):
            val = val.get("name") or str(val)
        if val is not None and str(val).strip():
            attrs.append(Attribute(name=a.get("name", ""), value=val))
    for c in row.get("characteristics", []):
        val = c.get("value")
        if val is not None and str(val).strip():
            attrs.append(Attribute(name=c.get("name", ""), value=val))

    # ID родительского товара (для вариантов)
    parent_product_id = None
    if entity_type == "variant":
        parent_href = row.get("product", {}).get("meta", {}).get("href", "")
        if parent_href:
            parent_product_id = parent_href.rstrip("/").split("/")[-1]

    # ID папки/категории товара
    folder_href = row.get("productFolder", {}).get("meta", {}).get("href", "")
    category_id = folder_href.rstrip("/").split("/")[-1] if folder_href else None

    # Если изображения были запрошены через expand=images — берём CDN-ссылку на миниатюру
    image_url: str | None = None
    images_field = row.get("images", {})
    if isinstance(images_field, dict):
        img_rows = images_field.get("rows", [])
        if img_rows:
            image_url = img_rows[0].get("miniature", {}).get("downloadHref")

    return Product(
        id=row.get("id", ""),
        name=row.get("name", ""),
        href=href,
        entity_type=entity_type,
        parent_product_id=parent_product_id,
        category_id=category_id,
        code=row.get("code"),
        description=row.get("description"),
        salePrices=sale_prices,
        attributes=attrs,
        image_url=image_url,
    )
