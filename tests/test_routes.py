"""Сквозные тесты HTTP-слоя аналитики на настоящем aiohttp-приложении с подменённым хранилищем."""
import asyncio
import json
import time
import uuid

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from analytics.hashing import UNVERIFIED_USER, user_hash
from analytics.routes import STATE_KEY, AnalyticsState, post_events, get_summary, with_reserve_tracking
from analytics.rate_limit import RateLimiter
from miniapp_api import api_create_order
from tests.helpers import BOT_TOKEN, SALT, make_init_data


class FakeStore:
    configured = True

    def __init__(self):
        self.events, self.outcomes, self.rpc_calls = [], [], []

    async def insert_events(self, rows):
        self.events.extend(rows)

    async def insert_outcome(self, row):
        self.outcomes.append(row)

    async def rpc(self, function, params):
        self.rpc_calls.append((function, params))
        return {"ok": True}


def run(scenario, limiter=None, admin_token="admin-secret"):
    async def main():
        store = FakeStore()
        app = web.Application()
        app[STATE_KEY] = AnalyticsState(bot_token=BOT_TOKEN, salt=SALT, admin_token=admin_token, store=store,
                                        limiter=limiter or RateLimiter(30, 60))
        app.router.add_post("/v1/events", post_events)
        app.router.add_get("/v1/analytics/summary", get_summary)
        app.router.add_post("/v1/orders", with_reserve_tracking(api_create_order))
        async with TestClient(TestServer(app)) as client:
            result = await scenario(client, store)
            await asyncio.gather(*app[STATE_KEY].tasks)
            return result, store
    return asyncio.run(main())


def fresh_init(user_id=42):
    return make_init_data(user_id=user_id, auth_date=int(time.time()) - 10)


def batch(n=1):
    now = int(time.time() * 1000)
    return [{"eid": str(uuid.uuid4()), "event": "app_open", "ts": now, "meta": {"platform": "ios"}} for _ in range(n)]


def test_events_are_stored_with_user_hash():
    async def scenario(client, store):
        body = {"initData": fresh_init(987654321), "sent_at": int(time.time() * 1000), "events": batch(3)}
        # как sendBeacon: text/plain
        r = await client.post("/v1/events", data=json.dumps(body), headers={"Content-Type": "text/plain"})
        return r.status, await r.json()
    (status, body), store = run(scenario)
    assert status == 200 and body == {"accepted": 3, "rejected": []}
    assert {e["user_hash"] for e in store.events} == {user_hash(987654321, SALT)}
    assert all("987654321" not in json.dumps(e) for e in store.events), "сырой user id не должен попадать в БД"


def test_events_require_valid_init_data():
    async def scenario(client, store):
        bad = make_init_data(bot_token="000:other")
        r1 = await client.post("/v1/events", json={"initData": bad, "events": batch()})
        old = make_init_data(auth_date=int(time.time()) - 25 * 3600)
        r2 = await client.post("/v1/events", json={"initData": old, "events": batch()})
        r3 = await client.post("/v1/events", json={"events": batch()})
        return [(r.status, (await r.json())["reason"]) for r in (r1, r2, r3)]
    result, store = run(scenario)
    assert result == [(401, "bad_signature"), (401, "expired"), (401, "empty")]
    assert store.events == []


def test_events_rate_limited_per_user():
    async def scenario(client, store):
        statuses = []
        for uid in (1, 1, 1, 2):
            r = await client.post("/v1/events", json={"initData": fresh_init(uid), "events": batch()})
            statuses.append(r.status)
        return statuses
    statuses, _ = run(scenario, limiter=RateLimiter(2, 60))
    assert statuses == [200, 200, 429, 200]


def test_events_batch_over_limit_and_bad_json():
    async def scenario(client, store):
        r1 = await client.post("/v1/events", json={"initData": fresh_init(), "events": batch(51)})
        r2 = await client.post("/v1/events", data="{not json")
        return r1.status, r2.status
    (s1, s2), store = run(scenario)
    assert (s1, s2) == (400, 400) and store.events == []


def test_order_is_unchanged_and_reserve_create_logged_by_server():
    payload = {
        "items": [{"productId": "p1", "name": "Elf Bar", "price": 700, "quantity": 2},
                  {"productId": "p2", "name": "Жидкость", "price": 450, "quantity": 1}],
        "shopId": "store-1", "shopName": "Липецк, Космонавтов, 100",
        "customer": {"name": "Иван", "phone": "+79990000000"},
    }

    async def scenario(client, store):
        r = await client.post("/v1/orders", json=payload, headers={"X-Telegram-Init-Data": fresh_init(42)})
        return r.status, await r.json()
    (status, body), store = run(scenario)

    assert status == 200 and body["status"] == "accepted" and body["total"] == 1850
    [event] = store.events
    assert event["event"] == "reserve_create"
    assert event["order_id"] == body["orderId"] and event["store_id"] == "store-1"
    assert event["user_hash"] == user_hash(42, SALT)
    assert event["meta"] == {"items_count": 2, "units": 3, "sum": 1850, "store_name": "Липецк, Космонавтов, 100"}
    dump = json.dumps(event, ensure_ascii=False)
    assert "Иван" not in dump and "79990000000" not in dump, "ПДн покупателя не логируются"
    [outcome] = store.outcomes
    assert outcome["order_id"] == body["orderId"] and outcome["status"] == "redeemed"
    assert outcome["raw_state"] == "assumed_on_create"


def test_order_without_init_data_is_unverified():
    async def scenario(client, store):
        r = await client.post("/v1/orders", json={"items": [], "shopId": "s1"})
        return r.status
    status, store = run(scenario)
    assert status == 200 and store.events[0]["user_hash"] == UNVERIFIED_USER


def test_order_error_logged_as_reserve_error():
    async def scenario(client, store):
        r = await client.post("/v1/orders", data="{broken", headers={"X-Telegram-Init-Data": fresh_init()})
        return r.status
    status, store = run(scenario)
    assert status == 400
    [event] = store.events
    assert event["event"] == "reserve_error" and event["meta"]["src"] == "server"
    assert event["meta"]["error"].startswith("HTTP 400")
    assert store.outcomes == []


def test_summary_requires_admin_token():
    async def scenario(client, store):
        r1 = await client.get("/v1/analytics/summary")
        r2 = await client.get("/v1/analytics/summary", headers={"Authorization": "Bearer wrong"})
        r3 = await client.get("/v1/analytics/summary?from=2026-06-01&to=2026-06-30",
                              headers={"Authorization": "Bearer admin-secret"})
        r4 = await client.get("/v1/analytics/summary?from=2026-07-01&to=2026-06-01",
                              headers={"Authorization": "Bearer admin-secret"})
        return [r.status for r in (r1, r2, r3, r4)]
    statuses, store = run(scenario)
    assert statuses == [401, 401, 200, 400]
    assert store.rpc_calls == [("analytics_summary", {"p_from": "2026-06-01", "p_to": "2026-06-30"})]


def test_summary_hidden_without_admin_token_configured():
    async def scenario(client, store):
        return (await client.get("/v1/analytics/summary", headers={"Authorization": "Bearer "})).status
    status, _ = run(scenario, admin_token="")
    assert status == 404
