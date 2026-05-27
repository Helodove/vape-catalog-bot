"""
REST API для Telegram Mini App TheVaper.
Все эндпоинты: GET /v1/...
Регистрируются в aiohttp-приложении в bot.py.
"""
import re
import json
import logging
import asyncio
import httpx
from aiohttp import web
from moysklad.client import MoySkladClient, BASE_URL, _build_stock_map_with_parents
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
    """"г Липецк ул Космонавтов, 100" → ("Липецк", "Космонавтов, 100")"""
    m = re.match(r'^г\s+(\S+)\s+(.+)$', name.strip())
    if m:
        city = m.group(1)
        address = re.sub(r'^(ул|пр|пл|пер|бул|наб|ш|пр-т)\s+', '', m.group(2), flags=re.IGNORECASE)
        return city, address
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
        "stockQty": int(p.stock) if p.stock is not None else None,
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
        if store_href and products:
            # Один запрос стока для всех папок + expand=assortment чтобы маппить variant→parent
            folder_ids = list({p.category_id for p in products if p.category_id})
            if folder_ids:
                folder_filter = ";".join(
                    f"productFolder={BASE_URL}/entity/productfolder/{fid}"
                    for fid in folder_ids
                )
                stock_data = await client._get("/report/stock/all", {
                    "filter": f"{folder_filter};store={store_href}",
                    "quantityMode": "positiveOnly",
                    "limit": 1000,
                    "expand": "assortment",
                })
                stock_map = _build_stock_map_with_parents(stock_data) if stock_data else {}
                for p in products:
                    p.stock = stock_map.get(p.href, 0.0)
            matched = sum(1 for p in products if p.in_stock)
            sample_p = [p.href for p in products[:3]]
            sample_s = list(stock_map.keys())[:3] if folder_ids else []
            log.info("search '%s' stock: folders=%d stock_map=%d matched=%d/%d | p.href=%s | stock_key=%s",
                     search, len(folder_ids), len(stock_map) if folder_ids else 0,
                     matched, len(products), sample_p, sample_s)
        else:
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
    # Пробуем как обычный товар, затем как вариант (expand=images даёт CDN-ссылку напрямую)
    raw = await client._get(f"/entity/product/{product_id}", {"expand": "images"})
    if not raw:
        raw = await client._get(f"/entity/variant/{product_id}", {"expand": "images"})
    if not raw:
        raise web.HTTPNotFound()

    p = _parse_product(raw)
    await client._enrich_stock_bulk([p], p.href.rsplit("/", 1)[0] + "/")
    dto = _product_to_dto(p, bot_base)

    # Загружаем варианты (цвета) если это обычный товар
    if p.entity_type == "product":
        variants = await client.get_product_variants(product_id)
        if variants:
            dto["variants"] = [
                {
                    "id": v.id,
                    "color": _extract_color(v),
                    # Изображение варианта: прокси-эндпоинт умеет брать фото из /entity/variant/{id}/images
                    "image": f"{bot_base}/v1/images/variant/{v.id}/0",
                }
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


_SHOP_HOURS: dict[str, str] = {
    "катукова":         "10:00–21:00",
    "8 марта":          "11:00–22:00",
    "плеханова":        "11:00–22:00",
    "космонавтов":      "11:00–22:00",
    "зои космодемьянской": "10:00–22:00",
    "газина":           "09:00–21:00",
    "хренникова":       "10:00–22:00",
    "виктора музыки":   "10:00–22:00",
    "куколкина":        "10:00–22:00",
    "комиссаржевской":  "10:00–22:00",
}


def _get_shop_hours(address: str) -> str:
    addr_lower = address.lower()
    for key, hours in _SHOP_HOURS.items():
        if key in addr_lower:
            return hours
    return "10:00–22:00"


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
            "hours": _get_shop_hours(address),
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


_image_url_cache: dict[str, str] = {}  # entity_id → cdn_url


async def api_image(request: web.Request) -> web.Response:
    """Прокси изображений МойСклад — токен не попадает во фронт."""
    entity_type = request.match_info["entity_type"]
    entity_id = request.match_info["entity_id"]
    ms_token = request.app["ms_token"]

    cached_url = _image_url_cache.get(entity_id)
    if cached_url:
        if cached_url == "__none__":
            raise web.HTTPNotFound()
        return web.Response(status=302, headers={
            "Location": cached_url,
            "Cache-Control": "public, max-age=86400",
            **cors_headers(request),
        })

    try:
        async with httpx.AsyncClient(timeout=15) as http:
            # Получаем список изображений
            imgs_url = f"{BASE_URL}/entity/{entity_type}/{entity_id}/images?limit=1"
            r = await http.get(imgs_url, headers={"Authorization": f"Bearer {ms_token}"})
            if r.status_code != 200:
                raise web.HTTPNotFound()
            rows = r.json().get("rows", [])

            if not rows:
                _image_url_cache[entity_id] = "__none__"
                raise web.HTTPNotFound()

            # Используем miniature.downloadHref — прямой CDN без авторизации, быстрее
            cdn_url = rows[0].get("miniature", {}).get("downloadHref")
            if cdn_url:
                _image_url_cache[entity_id] = cdn_url
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


async def api_create_order(request: web.Request) -> web.Response:
    try:
        payload = await request.json()
    except Exception:
        raise web.HTTPBadRequest(reason="Invalid JSON")

    customer = payload.get("customer", {})
    items = payload.get("items", [])
    shop_id = payload.get("shopId", "")
    shop_name = payload.get("shopName", shop_id)
    comment = payload.get("comment", "")

    import time
    order_id = f"ORD-{int(time.time())}"
    total = sum(i.get("price", 0) * i.get("quantity", 1) for i in items)

    # Формируем список товаров для уведомления
    lines = []
    for i in items:
        name = i.get("name", i.get("productId", "?"))
        qty = i.get("quantity", 1)
        price = i.get("price", 0)
        lines.append(f"• {name} × {qty} — {price * qty:,.0f} ₽".replace(",", " "))

    items_text = "\n".join(lines) if lines else "—"
    comment_text = f"\n💬 Комментарий: {comment}" if comment else ""

    msg = (
        f"📦 Новый заказ #{order_id}\n\n"
        f"👤 {customer.get('name', '—')}\n"
        f"📞 {customer.get('phone', '—')}\n\n"
        f"🏪 {shop_name}{comment_text}\n\n"
        f"🛒 Состав:\n{items_text}\n\n"
        f"💰 Итого: {total:,.0f} ₽".replace(",", " ")
    )

    # Уведомление сотрудников, подписанных на этот магазин
    from staff_bot import notify_store_subscribers
    await notify_store_subscribers(
        store_id=shop_id,
        message=msg,
        supabase_url=request.app.get("supabase_url", ""),
        supabase_key=request.app.get("supabase_key", ""),
        staff_bot_token=request.app.get("staff_bot_token", ""),
    )

    log.info("Order %s: customer=%s phone=%s shop=%s total=%.0f items=%d",
             order_id, customer.get("name"), customer.get("phone"), shop_name, total, len(items))

    return json_ok({"orderId": order_id, "total": total, "status": "accepted"}, request)


def register_miniapp_routes(app: web.Application, ms_token: str, bot_base_url: str,
                             miniapp_origin: str, supabase_url: str = "", supabase_key: str = "",
                             bot_token: str = "", admin_chat_id: str = "",
                             staff_bot_token: str = "") -> None:
    """Регистрирует все маршруты API мини-аппа в aiohttp приложении."""
    app["ms_token"] = ms_token
    app["bot_base_url"] = bot_base_url.rstrip("/")
    app["miniapp_origin"] = miniapp_origin
    app["supabase_url"] = supabase_url
    app["supabase_key"] = supabase_key
    app["bot_token"] = bot_token
    app["admin_chat_id"] = admin_chat_id
    app["staff_bot_token"] = staff_bot_token
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
    app.router.add_post("/v1/orders", api_create_order)
