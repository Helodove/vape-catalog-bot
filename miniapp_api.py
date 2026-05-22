"""
REST API для Telegram Mini App TheVaper.
Все эндпоинты: GET /v1/...
Регистрируются в aiohttp-приложении в bot.py.
"""
import re
import json
import logging
import httpx
from aiohttp import web
from moysklad.client import MoySkladClient, BASE_URL
from moysklad.models import Product

log = logging.getLogger(__name__)

ALLOWED_ORIGINS = [
    "http://localhost:5173",
    "http://localhost:4173",
]


# ─── утилиты ────────────────────────────────────────────────────────────────

def cors_headers(request: web.Request) -> dict:
    origin = request.headers.get("Origin", "")
    allowed = ALLOWED_ORIGINS + [request.app.get("miniapp_origin", "")]
    if origin in allowed or any(origin.endswith(".vercel.app") for _ in [1]):
        return {
            "Access-Control-Allow-Origin": origin,
            "Access-Control-Allow-Methods": "GET, OPTIONS",
            "Access-Control-Allow-Headers": "Content-Type, X-Telegram-Init-Data",
        }
    return {}


def json_ok(data, request: web.Request) -> web.Response:
    return web.Response(
        text=json.dumps(data, ensure_ascii=False),
        content_type="application/json",
        headers=cors_headers(request),
    )


async def options_handler(request: web.Request) -> web.Response:
    return web.Response(headers=cors_headers(request))


def _attr_value(product: Product, *names: str):
    """Ищет атрибут по имени (без учёта регистра)."""
    for a in product.attributes:
        if any(n.lower() in a.name.lower() for n in names):
            return a.value
    return None


def _parse_store(name: str) -> tuple[str, str]:
    """"г Липецк ул Космонавтов, 100" → ("Липецк", "ул Космонавтов, 100")"""
    m = re.match(r'^г\s+(\S+)\s+(.+)$', name.strip())
    if m:
        return m.group(1), m.group(2)
    return name, name


def _product_to_dto(p: Product, bot_base_url: str) -> dict:
    flavor = _attr_value(p, "вкус", "линейка") or (
        re.search(r'\((.+)\)$', p.name).group(1)
        if re.search(r'\((.+)\)$', p.name) else None
    )
    puffs_raw = _attr_value(p, "затяжк", "puff")
    puffs = int(puffs_raw) if puffs_raw and str(puffs_raw).isdigit() else None

    brand = _attr_value(p, "производитель", "бренд", "brand")

    # Варианты не имеют собственных фото — берём фото родительского товара
    img_entity = "product"
    img_id = p.parent_product_id if p.entity_type == "variant" and p.parent_product_id else p.id
    if p.entity_type != "variant":
        img_entity = p.entity_type
    image_url = (
        f"{bot_base_url}/v1/images/{img_entity}/{img_id}/0"
        if bot_base_url else None
    )

    return {
        "id": p.id,
        "categoryId": p.category_id or "",
        "brand": str(brand) if brand else None,
        "name": p.name,
        "flavor": str(flavor) if flavor else None,
        "puffs": puffs,
        "price": p.retail_price or 0,
        "images": [image_url] if image_url else [],
        "inStock": p.in_stock,
        "description": p.description,
    }


# ─── handlers ───────────────────────────────────────────────────────────────

async def api_categories(request: web.Request) -> web.Response:
    client: MoySkladClient = request.app["ms_client"]
    folders = await client.get_root_folders()
    def clean_title(name: str) -> str:
        return re.sub(r'^\d+\.\s*', '', name).strip()

    data = [
        {
            "id": f.id,
            "title": clean_title(f.name),
            "slug": re.sub(r'\W+', '-', clean_title(f.name).lower()).strip('-'),
            "productGroupId": f.id,
            "cover": None,
            "sortOrder": i,
        }
        for i, f in enumerate(folders)
    ]
    return json_ok(data, request)


async def api_products(request: web.Request) -> web.Response:
    client: MoySkladClient = request.app["ms_client"]
    bot_base = request.app.get("bot_base_url", "")
    category_id = request.rel_url.query.get("categoryId", "")
    search = request.rel_url.query.get("search", "")
    in_stock = request.rel_url.query.get("inStock", "") == "true"
    limit = min(int(request.rel_url.query.get("limit", "50")), 200)
    offset = int(request.rel_url.query.get("offset", "0"))

    if search:
        products = await client.search_products(search)
    elif category_id:
        folder_href = f"{BASE_URL}/entity/productfolder/{category_id}"
        products = await client.get_products(folder_href)
    else:
        products = []

    if in_stock:
        products = [p for p in products if p.in_stock]

    total = len(products)
    page = products[offset: offset + limit]
    items = [_product_to_dto(p, bot_base) for p in page]
    return json_ok({"items": items, "total": total}, request)


async def api_product(request: web.Request) -> web.Response:
    client: MoySkladClient = request.app["ms_client"]
    bot_base = request.app.get("bot_base_url", "")
    product_id = request.match_info["id"]

    from moysklad.client import _parse_product
    # Пробуем как обычный товар, затем как вариант
    raw = await client._get(f"/entity/product/{product_id}")
    if not raw:
        raw = await client._get(f"/entity/variant/{product_id}")
    if not raw:
        raise web.HTTPNotFound()

    p = _parse_product(raw)
    await client._enrich_stock_bulk([p], p.href.rsplit("/", 1)[0] + "/")
    return json_ok(_product_to_dto(p, bot_base), request)


async def api_shops(request: web.Request) -> web.Response:
    client: MoySkladClient = request.app["ms_client"]
    stores = await client.get_stores()
    data = []
    for s in stores:
        city, address = _parse_store(s.name)
        data.append({
            "id": s.id,
            "city": city,
            "address": address,
            "hours": "10:00–22:00",
            "schedule": "Ежедневно",
            "cover": None,
        })
    return json_ok(data, request)


async def api_stock(request: web.Request) -> web.Response:
    client: MoySkladClient = request.app["ms_client"]
    product_id = request.rel_url.query.get("productId", "")
    if not product_id:
        return json_ok([], request)

    stores = await client.get_stores()
    store_ids = {s.name: s.id for s in stores}

    # Пробуем как обычный товар; если пусто — пробуем как вариант
    product_href = f"{BASE_URL}/entity/product/{product_id}"
    stock_map = await client.get_stock_by_store(product_href)
    if not stock_map:
        variant_href = f"{BASE_URL}/entity/variant/{product_id}"
        stock_map = await client.get_stock_by_store(variant_href)

    result = [
        {"shopId": store_ids.get(name, name), "quantity": int(qty)}
        for name, qty in stock_map.items()
        if qty > 0
    ]
    return json_ok(result, request)


async def api_image(request: web.Request) -> web.Response:
    """Прокси изображений МойСклад — токен не попадает во фронт."""
    entity_type = request.match_info["entity_type"]
    entity_id = request.match_info["entity_id"]
    ms_token = request.app["ms_token"]

    try:
        async with httpx.AsyncClient(timeout=15) as http:
            # Получаем список изображений
            imgs_url = f"{BASE_URL}/entity/{entity_type}/{entity_id}/images?limit=1"
            r = await http.get(imgs_url, headers={"Authorization": f"Bearer {ms_token}"})
            if r.status_code != 200:
                raise web.HTTPNotFound()
            rows = r.json().get("rows", [])
            if not rows:
                raise web.HTTPNotFound()
            download_href = (
                rows[0].get("meta", {}).get("downloadHref")
                or rows[0].get("meta", {}).get("href")
            )
            if not download_href:
                raise web.HTTPNotFound()
            # Скачиваем само изображение
            img_r = await http.get(download_href, headers={"Authorization": f"Bearer {ms_token}"})
            if img_r.status_code != 200:
                raise web.HTTPNotFound()
            content_type = img_r.headers.get("content-type", "image/jpeg")
            return web.Response(
                body=img_r.content,
                content_type=content_type,
                headers={
                    "Cache-Control": "public, max-age=86400",
                    **cors_headers(request),
                },
            )
    except web.HTTPException:
        raise
    except Exception as e:
        log.error("Image proxy error: %s", e)
        raise web.HTTPInternalServerError()


def register_miniapp_routes(app: web.Application, ms_token: str, bot_base_url: str, miniapp_origin: str) -> None:
    """Регистрирует все маршруты API мини-аппа в aiohttp приложении."""
    app["ms_token"] = ms_token
    app["bot_base_url"] = bot_base_url.rstrip("/")
    app["miniapp_origin"] = miniapp_origin

    app.router.add_route("OPTIONS", "/v1/{path_info:.*}", options_handler)
    app.router.add_get("/v1/categories", api_categories)
    app.router.add_get("/v1/products", api_products)
    app.router.add_get("/v1/products/{id}", api_product)
    app.router.add_get("/v1/shops", api_shops)
    app.router.add_get("/v1/stock", api_stock)
    app.router.add_get("/v1/images/{entity_type}/{entity_id}/{idx}", api_image)
