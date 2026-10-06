"""
HTTP-слой аналитики:
  POST /v1/events               — пачка событий с клиента (initData + до 50 событий)
  GET  /v1/analytics/summary    — метрики за период, закрыт ANALYTICS_ADMIN_TOKEN
  with_reserve_tracking(handler) — обёртка над POST /v1/orders: пишет reserve_create /
                                   reserve_error сервером, не меняя логику самого заказа.
"""
import asyncio
import hmac
import json
import logging
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone

import httpx
from aiohttp import web

from .hashing import UNVERIFIED_USER, user_hash
from .rate_limit import RateLimiter
from .schema import MAX_STRING, EventValidationError, build_server_event, validate_client_batch
from .storage import AnalyticsStore, StorageError
from .telegram_auth import InitDataError, validate_init_data

log = logging.getLogger(__name__)

STATE_KEY = "analytics"
MAX_BODY_BYTES = 64 * 1024
MSK = timezone(timedelta(hours=3))   # Воронеж/Липецк; в РФ нет перехода на летнее время
RATE_LIMIT_REQUESTS = 30             # запросов /v1/events на пользователя
RATE_LIMIT_WINDOW = 60               # за 60 секунд
MAX_PERIOD_DAYS = 366


@dataclass
class AnalyticsState:
    bot_token: str
    salt: str
    admin_token: str
    store: AnalyticsStore
    limiter: RateLimiter = field(default_factory=lambda: RateLimiter(RATE_LIMIT_REQUESTS, RATE_LIMIT_WINDOW))
    tasks: set = field(default_factory=set)

    @property
    def enabled(self) -> bool:
        return bool(self.salt and self.bot_token and self.store.configured)


def _cors(request: web.Request) -> dict:
    from miniapp_api import cors_headers  # локальный импорт: miniapp_api импортирует этот модуль
    return cors_headers(request)


def _reply(request: web.Request, status: int, body: dict, extra_headers: dict | None = None) -> web.Response:
    return web.json_response(body, status=status, headers={**_cors(request), **(extra_headers or {})})


# ─── POST /v1/events ────────────────────────────────────────────────────────

async def post_events(request: web.Request) -> web.Response:
    st: AnalyticsState | None = request.app.get(STATE_KEY)
    if st is None or not st.enabled:
        return _reply(request, 503, {"error": "analytics_disabled"})

    if (request.content_length or 0) > MAX_BODY_BYTES:
        return _reply(request, 413, {"error": "too_large"})
    raw = await request.read()
    if len(raw) > MAX_BODY_BYTES:
        return _reply(request, 413, {"error": "too_large"})
    try:
        # sendBeacon шлёт text/plain, поэтому тело разбираем независимо от Content-Type
        body = json.loads(raw)
        if not isinstance(body, dict):
            raise ValueError
    except ValueError:
        return _reply(request, 400, {"error": "invalid_json"})

    init_data = body.get("initData") or request.headers.get("X-Telegram-Init-Data", "")
    try:
        tg = validate_init_data(init_data, st.bot_token)
    except InitDataError as e:
        return _reply(request, 401, {"error": "unauthorized", "reason": str(e)})

    uh = user_hash(tg.user_id, st.salt)
    if not st.limiter.allow(uh):
        return _reply(request, 429, {"error": "rate_limited"}, {"Retry-After": str(RATE_LIMIT_WINDOW)})

    try:
        rows, rejected = validate_client_batch(body.get("events"), sent_at=body.get("sent_at"))
    except EventValidationError as e:
        return _reply(request, 400, {"error": "invalid_events", "reason": str(e)})

    for row in rows:
        row["user_hash"] = uh
    try:
        await st.store.insert_events(rows)
    except (StorageError, httpx.HTTPError) as e:
        log.error("analytics insert failed (%d events): %s", len(rows), e)
        return _reply(request, 502, {"error": "storage_failed"})

    if rejected:
        log.info("analytics: accepted=%d rejected=%s", len(rows), rejected)
    return _reply(request, 200, {"accepted": len(rows), "rejected": rejected})


# ─── серверные события заказа ───────────────────────────────────────────────

def _spawn(st: AnalyticsState, coro) -> None:
    """Запись аналитики не задерживает и не ломает ответ покупателю."""
    task = asyncio.ensure_future(coro)
    st.tasks.add(task)
    task.add_done_callback(st.tasks.discard)


def _order_user_hash(st: AnalyticsState, request: web.Request) -> str:
    try:
        tg = validate_init_data(request.headers.get("X-Telegram-Init-Data", ""), st.bot_token)
        return user_hash(tg.user_id, st.salt)
    except (InitDataError, ValueError):
        return UNVERIFIED_USER


async def _request_payload(request: web.Request) -> dict:
    try:
        payload = await request.json()   # тело уже прочитано обработчиком и закешировано aiohttp
        return payload if isinstance(payload, dict) else {}
    except Exception:
        return {}


async def _write(st: AnalyticsState, event_row: dict, outcome_row: dict | None = None) -> None:
    try:
        await st.store.insert_events([event_row])
        if outcome_row:
            await st.store.insert_outcome(outcome_row)
    except Exception as e:
        # Полная строка в логе — чтобы событие можно было восстановить вручную
        log.error("ANALYTICS_LOST %s event=%s outcome=%s",
                  e, json.dumps(event_row, ensure_ascii=False), json.dumps(outcome_row, ensure_ascii=False))


def _shop_id(payload: dict) -> str | None:
    value = payload.get("shopId")
    return value if isinstance(value, str) and value else None


def _units(items: list) -> int:
    total = 0
    for i in items:
        q = i.get("quantity", 1) if isinstance(i, dict) else 0
        total += q if isinstance(q, int) and not isinstance(q, bool) and q > 0 else 0
    return total


async def _track_reserve_create(st: AnalyticsState, request: web.Request, response: web.StreamResponse) -> None:
    try:
        if response.status >= 300 or not isinstance(response, web.Response):
            return
        result = json.loads(response.text)
        order_id = str(result["orderId"])
        total = result.get("total") or 0
        payload = await _request_payload(request)
        items = payload.get("items") if isinstance(payload.get("items"), list) else []
        shop_name = payload.get("shopName")
        now = datetime.now(timezone.utc)
        row = build_server_event(
            "reserve_create", eid=str(uuid.uuid4()), user_hash=_order_user_hash(st, request), now=now,
            order_id=order_id, store_id=_shop_id(payload),
            meta={
                "items_count": len(items),
                "units": _units(items),
                "sum": total,
                "store_name": shop_name[:MAX_STRING] if isinstance(shop_name, str) else None,
            },
        )
        # Принятое допущение исследования: созданный заказ считается выкупленным.
        # raw_state фиксирует, что статус не подтверждён учётной системой.
        outcome = {"order_id": order_id, "status": "redeemed", "outcome_ts": now.isoformat(),
                   "sum": total, "raw_state": "assumed_on_create"}
    except Exception as e:
        log.error("analytics reserve_create skipped: %s", e)
        return
    _spawn(st, _write(st, row, outcome))


async def _track_reserve_error(st: AnalyticsState, request: web.Request, exc: BaseException) -> None:
    try:
        if isinstance(exc, web.HTTPException):
            error = f"HTTP {exc.status}: {exc.reason}"
        else:
            error = f"{type(exc).__name__}: {exc}"
        payload = await _request_payload(request)
        row = build_server_event(
            "reserve_error", eid=str(uuid.uuid4()), user_hash=_order_user_hash(st, request),
            store_id=_shop_id(payload), meta={"error": error[:MAX_STRING]},
        )
    except Exception as e:
        log.error("analytics reserve_error skipped: %s", e)
        return
    _spawn(st, _write(st, row))


def with_reserve_tracking(handler):
    """Оборачивает обработчик создания заказа. Сам заказ обрабатывается без изменений."""
    async def tracked(request: web.Request) -> web.StreamResponse:
        st: AnalyticsState | None = request.app.get(STATE_KEY)
        if st is None or not st.enabled:
            return await handler(request)
        try:
            response = await handler(request)
        except Exception as e:
            await _track_reserve_error(st, request, e)
            raise
        await _track_reserve_create(st, request, response)
        return response
    return tracked


# ─── GET /v1/analytics/summary ──────────────────────────────────────────────

def parse_period(date_from: str | None, date_to: str | None, today: date | None = None) -> tuple[date, date]:
    """Период включительно по датам МСК. По умолчанию — последние 30 дней."""
    today = today or datetime.now(MSK).date()
    d_to = date.fromisoformat(date_to) if date_to else today
    d_from = date.fromisoformat(date_from) if date_from else d_to - timedelta(days=29)
    if d_from > d_to:
        raise ValueError("from > to")
    if (d_to - d_from).days >= MAX_PERIOD_DAYS:
        raise ValueError(f"period longer than {MAX_PERIOD_DAYS} days")
    return d_from, d_to


async def get_summary(request: web.Request) -> web.Response:
    st: AnalyticsState | None = request.app.get(STATE_KEY)
    if st is None or not st.admin_token or not st.store.configured:
        raise web.HTTPNotFound()

    auth = request.headers.get("Authorization", "")
    token = auth[len("Bearer "):] if auth.startswith("Bearer ") else ""
    if not token or not hmac.compare_digest(token.encode(), st.admin_token.encode()):
        return web.json_response({"error": "unauthorized"}, status=401)

    try:
        d_from, d_to = parse_period(request.rel_url.query.get("from"), request.rel_url.query.get("to"))
    except ValueError as e:
        return web.json_response({"error": "invalid_period", "reason": str(e)}, status=400)

    try:
        data = await st.store.rpc("analytics_summary", {"p_from": d_from.isoformat(), "p_to": d_to.isoformat()})
    except (StorageError, httpx.HTTPError) as e:
        log.error("analytics summary failed: %s", e)
        return web.json_response({"error": "storage_failed"}, status=502)
    return web.json_response(data, dumps=lambda o: json.dumps(o, ensure_ascii=False))


def register_analytics_routes(app: web.Application, *, bot_token: str, salt: str, admin_token: str,
                              supabase_url: str, supabase_key: str) -> None:
    st = AnalyticsState(bot_token=bot_token, salt=salt, admin_token=admin_token,
                        store=AnalyticsStore(supabase_url, supabase_key))
    app[STATE_KEY] = st
    if not st.enabled:
        log.warning("Analytics disabled: set ANALYTICS_SALT, TELEGRAM_BOT_TOKEN and Supabase credentials")
    app.router.add_post("/v1/events", post_events)
    app.router.add_get("/v1/analytics/summary", get_summary)
