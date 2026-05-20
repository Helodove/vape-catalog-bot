import logging
import urllib.parse
from typing import Optional
import httpx
from .models import Product, ProductFolder, Attribute, SalePrice, PriceType
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

    async def get_root_folders(self) -> list[ProductFolder]:
        key = "folders:root"
        cached = cache.get(key)
        if cached is not None:
            return cached
        data = await self._get("/entity/productfolder", {"filter": "pathName="})
        if data is None:
            return []
        folders = [_parse_folder(r) for r in data.get("rows", [])]
        cache.set(key, folders, TTL_FOLDERS)
        return folders

    async def get_subfolders(self, folder_href: str) -> list[ProductFolder]:
        key = f"folders:{folder_href}"
        cached = cache.get(key)
        if cached is not None:
            return cached
        data = await self._get("/entity/productfolder", {"filter": f"productFolder={folder_href}"})
        if data is None:
            return []
        folders = [_parse_folder(r) for r in data.get("rows", [])]
        cache.set(key, folders, TTL_FOLDERS)
        return folders

    async def get_products(self, folder_href: str) -> list[Product]:
        key = f"products:{folder_href}"
        cached = cache.get(key)
        if cached is not None:
            return cached

        # Товары и варианты из ассортимента
        data = await self._get("/entity/assortment", {
            "filter": f"productFolder={folder_href}",
            "limit": 200,
        })
        if data is None:
            return []

        rows = [r for r in data.get("rows", []) if r.get("meta", {}).get("type") in ALLOWED_TYPES]
        products = [_parse_product(r) for r in rows]

        # Остатки одним запросом для всей папки
        products = await self._enrich_stock_bulk(products, folder_href)
        cache.set(key, products, TTL_PRODUCTS)
        return products

    async def search_products(self, query: str) -> list[Product]:
        data = await self._get("/entity/assortment", {"search": query, "limit": 50})
        if data is None:
            return []
        rows = [r for r in data.get("rows", []) if r.get("meta", {}).get("type") in ALLOWED_TYPES]
        products = [_parse_product(r) for r in rows]
        # Остатки одним запросом без фильтра по папке
        stock_data = await self._get("/report/stock/all", {"search": query, "limit": 200})
        if stock_data:
            stock_map = _build_stock_map(stock_data)
            for p in products:
                p.stock = stock_map.get(p.href, 0.0)
        return products

    async def get_product_image_url(self, product_id: str) -> Optional[str]:
        key = f"image:{product_id}"
        cached = cache.get(key)
        if cached is not None:
            return cached if cached != "__none__" else None
        data = await self._get(f"/entity/product/{product_id}/images", {"limit": 1})
        url = None
        if data and data.get("rows"):
            meta = data["rows"][0].get("meta", {})
            href = meta.get("downloadHref") or meta.get("href")
            if href:
                url = href
        cache.set(key, url if url else "__none__", TTL_IMAGES)
        return url

    async def _enrich_stock_bulk(self, products: list[Product], folder_href: str) -> list[Product]:
        if not products:
            return products
        key = f"stock_bulk:{folder_href}"
        stock_map = cache.get(key)
        if stock_map is None:
            stock_data = await self._get("/report/stock/all", {
                "filter": f"productFolder={folder_href}",
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
    sale_prices = []
    for sp in row.get("salePrices", []):
        pt = sp.get("priceType", {})
        sale_prices.append(SalePrice(
            value=sp.get("value", 0),
            priceType=PriceType(name=pt.get("name", "")),
        ))
    attrs = []
    for a in row.get("attributes", []):
        val = a.get("value")
        if isinstance(val, dict):
            val = val.get("name") or str(val)
        attrs.append(Attribute(name=a.get("name", ""), value=val))
    return Product(
        id=row.get("id", ""),
        name=row.get("name", ""),
        href=href,
        code=row.get("code"),
        description=row.get("description"),
        salePrices=sale_prices,
        attributes=attrs,
    )
