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
import image_db

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


def get_base_url(request: web.Request) -> str:
    """Возвращает bot_base_url из настроек или определяет автоматически из запроса."""
    configured = request.app.get("bot_base_url", "")
    if configured:
        return configured
    # Автоопределение: берём схему и хост из заголовков (Railway / Vercel proxy)
    host = request.headers.get("X-Forwarded-Host") or request.headers.get("Host") or request.url.host
    scheme = request.headers.get("X-Forwarded-Proto") or request.url.scheme
    return f"{scheme}://{host}"


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


def _get_brand(p: Product) -> str | None:
    """Бренд: атрибут МойСклад → первое слово названия."""
    brand = _attr_value(p, "производитель", "бренд", "brand")
    if not brand and p.name:
        brand = p.name.split()[0]
    return str(brand) if brand else None


def _extract_color(p: Product) -> str:
    """Извлекает цвет из характеристик варианта."""
    for a in p.attributes:
        if any(w in a.name.lower() for w in ("цвет", "color", "colour", "колор")):
            return str(a.value)
    # Fallback: текст в скобках в конце названия — "(Prism Blue)"
    m = re.search(r'\((.+)\)$', p.name)
    return m.group(1) if m else p.name


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

    brand = _get_brand(p)

    # Приоритет изображений:
    # 1. Кастомный URL из Supabase (product_images)
    # 2. CDN-ссылка из МойСклад (expand=images → miniature.downloadHref)
    # 3. Прокси через Railway
    custom_image = image_db.find_image(p.name)
    if custom_image:
        image_url: str | None = custom_image
    elif p.image_url:
        image_url = p.image_url
    elif bot_base_url:
        img_entity = "product"
        img_id = p.parent_product_id if p.entity_type == "variant" and p.parent_product_id else p.id
        image_url = f"{bot_base_url}/v1/images/{img_entity}/{img_id}/0"
    else:
        image_url = None

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
    # Обновляем кеш картинок (TTL 5 мин, не блокирует ответ)
    await image_db.load_images(request.app.get("supabase_url", ""), request.app.get("supabase_key", ""))
    client: MoySkladClient = request.app["ms_client"]
    bot_base = get_base_url(request)
    category_id = request.rel_url.query.get("categoryId", "")
    search = request.rel_url.query.get("search", "")
    in_stock = request.rel_url.query.get("inStock", "") == "true"
    store_id = request.rel_url.query.get("storeId", "")
    limit = min(int(request.rel_url.query.get("limit", "50")), 200)
    offset = int(request.rel_url.query.get("offset", "0"))

    # Если передан storeId — фильтруем остатки по конкретному складу
    store_href = f"{BASE_URL}/entity/store/{store_id}" if store_id else None

    if search:
        products = await client.search_products(search)
        if store_href:
            await client.enrich_stock_for_store(products, store_href)
        else:
            # Глобальный поиск — магазин не выбран, считаем всё доступным
            for p in products:
                p.stock = 1.0
    elif category_id:
        folder_href = f"{BASE_URL}/entity/productfolder/{category_id}"
        products = await client.get_products(folder_href, store_href)
    else:
        products = []

    brand_filter = request.rel_url.query.get("brand", "")
    if brand_filter:
        brand_lower = brand_filter.lower()
        products = [p for p in products if (_get_brand(p) or "").lower() == brand_lower]
    if in_stock:
        products = [p for p in products if p.in_stock]

    total = len(products)
    page = products[offset: offset + limit]
    items = [_product_to_dto(p, bot_base) for p in page]
    return json_ok({"items": items, "total": total}, request)


async def api_product(request: web.Request) -> web.Response:
    client: MoySkladClient = request.app["ms_client"]
    bot_base = get_base_url(request)
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
    dto = _product_to_dto(p, bot_base)

    # Загружаем варианты (цвета) если это обычный товар
    if p.entity_type == "product":
        variants = await client.get_product_variants(product_id)
        if variants:
            # Fallback фото: если у родителя нет — берём у первого варианта с фото
            if not p.image_url:
                for v in variants:
                    if v.image_url:
                        p.image_url = v.image_url
                        break
            # Пересобираем DTO с обновлённым image_url
            dto = _product_to_dto(p, bot_base)
            dto["variants"] = [
                {"id": v.id, "color": _extract_color(v)}
                for v in variants
            ]

    return json_ok(dto, request)


async def api_subcategories(request: web.Request) -> web.Response:
    """Прямые подпапки категории из МойСклад — используются как бренды/типы."""
    client: MoySkladClient = request.app["ms_client"]
    category_id = request.rel_url.query.get("categoryId", "")
    if not category_id:
        return json_ok([], request)

    folder_href = f"{BASE_URL}/entity/productfolder/{category_id}"
    subfolders = await client.get_subfolders(folder_href)

    data = [
        {
            "id": f.id,
            "title": re.sub(r'^\d+\.\s*', '', f.name).strip(),
            "slug": re.sub(r'\W+', '-', f.name.lower()).strip('-'),
            "cover": None,
        }
        for f in subfolders
    ]
    return json_ok(data, request)


async def api_brands(request: web.Request) -> web.Response:
    """Уникальные бренды в категории — для экрана выбора производителя."""
    client: MoySkladClient = request.app["ms_client"]
    category_id = request.rel_url.query.get("categoryId", "")
    if not category_id:
        return json_ok([], request)

    folder_href = f"{BASE_URL}/entity/productfolder/{category_id}"
    products = await client.get_products(folder_href)

    brand_names: dict[str, str] = {}   # lower → display name
    brand_count: dict[str, int] = {}   # lower → кол-во товаров в наличии

    for p in products:
        brand = _get_brand(p)
        if not brand:
            continue
        key = brand.lower()
        if key not in brand_names:
            brand_names[key] = brand
            brand_count[key] = 0
        if p.in_stock:
            brand_count[key] += 1

    # Сначала бренды с максимальным наличием, затем алфавит
    result = sorted(
        [{"name": brand_names[k], "count": brand_count[k]} for k in brand_names],
        key=lambda b: (-b["count"], b["name"].lower()),
    )
    return json_ok(result, request)


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
    valid_store_ids = {s.id for s in stores}

    # Краткий отчёт /bystore/current: один запрос, работает для product и variant
    # filter=assortmentId принимает UUID напрямую (не href)
    data = await client._get("/report/stock/bystore/current", {
        "filter": f"assortmentId={product_id}",
    })

    result = [
        {"shopId": row["storeId"], "quantity": int(row.get("stock", 0))}
        for row in (data if isinstance(data, list) else [])
        if row.get("storeId") in valid_store_ids and (row.get("stock") or 0) > 0
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

            # Используем miniature.downloadHref — прямой CDN без авторизации, быстрее
            cdn_url = rows[0].get("miniature", {}).get("downloadHref")
            if cdn_url:
                return web.Response(
                    status=302,
                    headers={
                        "Location": cdn_url,
                        "Cache-Control": "public, max-age=86400",
                        **cors_headers(request),
                    },
                )

            # Fallback: скачиваем полное изображение через downloadHref (302 → CDN)
            download_href = rows[0].get("meta", {}).get("downloadHref")
            if not download_href:
                raise web.HTTPNotFound()
            img_r = await http.get(download_href, headers={"Authorization": f"Bearer {ms_token}"}, follow_redirects=True)
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


async def _warm_image_cache(app: web.Application) -> None:
    """Прогрев кеша картинок из Supabase при старте и на каждый запрос (TTL 5 мин)."""
    await image_db.load_images(app.get("supabase_url", ""), app.get("supabase_key", ""))


def register_miniapp_routes(app: web.Application, ms_token: str, bot_base_url: str,
                             miniapp_origin: str, supabase_url: str = "", supabase_key: str = "") -> None:
    """Регистрирует все маршруты API мини-аппа в aiohttp приложении."""
    app["ms_token"] = ms_token
    app["bot_base_url"] = bot_base_url.rstrip("/")
    app["miniapp_origin"] = miniapp_origin
    app["supabase_url"] = supabase_url
    app["supabase_key"] = supabase_key
    app.on_startup.append(_warm_image_cache)

    app.router.add_route("OPTIONS", "/v1/{path_info:.*}", options_handler)
    app.router.add_get("/v1/categories", api_categories)
    app.router.add_get("/v1/subcategories", api_subcategories)
    app.router.add_get("/v1/brands", api_brands)
    app.router.add_get("/v1/products", api_products)
    app.router.add_get("/v1/products/{id}", api_product)
    app.router.add_get("/v1/shops", api_shops)
    app.router.add_get("/v1/stock", api_stock)
    app.router.add_get("/v1/images/{entity_type}/{entity_id}/{idx}", api_image)
