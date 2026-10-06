import json
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from analytics.schema import (
    CLIENT_EVENTS, EVENT_NAMES, MAX_EVENTS_PER_REQUEST, SERVER_EVENTS,
    EventValidationError, build_server_event, validate_client_batch,
)

NOW = datetime(2026, 10, 7, 12, 0, tzinfo=timezone.utc)
NOW_MS = int(NOW.timestamp() * 1000)


def ev(event, **kw):
    return {"eid": str(uuid.uuid4()), "event": event, "ts": NOW_MS, **kw}


VALID = [
    ev("app_open", meta={"platform": "ios", "start_param": "promo"}),
    ev("app_open", meta={"platform": "android"}),
    ev("search", store_id="store-1", meta={"query": "elf bar", "results_count": 0}),
    ev("category_open", meta={"category_id": "cat-1"}),
    ev("product_view", product_id="p-1", store_id="store-1", meta={"in_stock": False, "name": "Elf Bar"}),
    ev("store_select", store_id="0f1e2d3c-aaaa-bbbb-cccc-1234567890ab"),
    ev("reserve_error", store_id="store-1", meta={"error": "API 500: /orders"}),
]


def test_spec_lists_all_required_events():
    assert EVENT_NAMES == {"app_open", "search", "category_open", "product_view",
                           "store_select", "reserve_create", "reserve_error"}
    assert "reserve_create" not in CLIENT_EVENTS
    assert SERVER_EVENTS == {"reserve_create", "reserve_error"}


def test_accepts_all_valid_client_events():
    rows, rejected = validate_client_batch(VALID, sent_at=NOW_MS, now=NOW)
    assert rejected == []
    assert [r["event"] for r in rows] == [e["event"] for e in VALID]
    # у всех строк одинаковый набор ключей — нужно для пакетной вставки в PostgREST
    assert len({tuple(sorted(r)) for r in rows}) == 1


def test_client_reserve_error_is_marked_with_source():
    rows, _ = validate_client_batch([VALID[-1]], sent_at=NOW_MS, now=NOW)
    assert rows[0]["meta"]["src"] == "client"


@pytest.mark.parametrize("bad, reason", [
    (ev("purchase"), "unknown event"),
    (ev("reserve_create", order_id="ORD-1", store_id="s", meta={"items_count": 1, "units": 1, "sum": 1}), "server-only"),
    (ev("product_view", meta={"in_stock": True}), "product_id: required"),
    (ev("store_select"), "store_id: required"),
    (ev("app_open", product_id="p-1", meta={"platform": "ios"}), "not allowed"),
    (ev("app_open", meta={"platform": "ios", "username": "ivan"}), "unexpected keys"),
    (ev("app_open", meta={}), "meta.platform: required"),
    (ev("search", meta={"query": "x", "results_count": "5"}), "expected int"),
    (ev("search", meta={"query": "x", "results_count": True}), "expected int"),
    (ev("search", meta={"query": "x", "results_count": -1}), "expected int"),
    (ev("search", meta={"query": "x" * 201, "results_count": 1}), "too long"),
    (ev("product_view", product_id="p-1", meta={"in_stock": "yes"}), "expected bool"),
    (ev("store_select", store_id="drop table; --"), "invalid id"),
    ({**ev("store_select", store_id="s"), "eid": "not-a-uuid"}, "eid"),
    ({**ev("store_select", store_id="s"), "phone": "+79990000000"}, "unexpected keys"),
    ({**ev("store_select", store_id="s"), "ts": "yesterday"}, "ts"),
    ("not an object", "expected object"),
])
def test_rejects_invalid_event(bad, reason):
    rows, rejected = validate_client_batch([bad, VALID[0]], sent_at=NOW_MS, now=NOW)
    assert len(rows) == 1, "валидное событие из той же пачки должно пройти"
    assert len(rejected) == 1 and rejected[0]["index"] == 0
    assert reason in rejected[0]["reason"]


def test_batch_limit():
    batch = [ev("app_open", meta={"platform": "ios"}) for _ in range(MAX_EVENTS_PER_REQUEST)]
    assert len(validate_client_batch(batch, sent_at=NOW_MS, now=NOW)[0]) == MAX_EVENTS_PER_REQUEST
    with pytest.raises(EventValidationError, match="max 50"):
        validate_client_batch(batch + [batch[0]], sent_at=NOW_MS, now=NOW)


def test_events_must_be_array():
    with pytest.raises(EventValidationError):
        validate_client_batch({"event": "app_open"}, now=NOW)


def test_duplicate_eid_in_batch_is_rejected():
    e = ev("app_open", meta={"platform": "ios"})
    rows, rejected = validate_client_batch([e, dict(e)], sent_at=NOW_MS, now=NOW)
    assert len(rows) == 1 and rejected[0]["reason"] == "duplicate eid"


def test_timestamp_is_corrected_for_client_clock():
    # Часы телефона спешат на 2 часа; событие произошло за 10 с до отправки
    skew = 2 * 3600 * 1000
    e = {**ev("app_open", meta={"platform": "ios"}), "ts": NOW_MS + skew - 10_000}
    rows, _ = validate_client_batch([e], sent_at=NOW_MS + skew, now=NOW)
    assert rows[0]["ts"] == (NOW - timedelta(seconds=10)).isoformat()
    assert rows[0]["meta"]["client_ts"] == NOW_MS + skew - 10_000


def test_timestamp_is_clamped():
    old = {**ev("app_open", meta={"platform": "ios"}), "ts": NOW_MS - 3 * 86400_000}
    future = {**ev("app_open", meta={"platform": "ios"}), "ts": NOW_MS + 60_000}
    rows, _ = validate_client_batch([old, future], sent_at=NOW_MS, now=NOW)
    assert rows[0]["ts"] == (NOW - timedelta(hours=24)).isoformat()
    assert rows[1]["ts"] == NOW.isoformat()


def test_without_sent_at_server_time_is_used():
    rows, _ = validate_client_batch([ev("app_open", meta={"platform": "ios"})], now=NOW)
    assert rows[0]["ts"] == NOW.isoformat()


def test_build_server_event():
    row = build_server_event("reserve_create", eid="e", user_hash="h", now=NOW, order_id="ORD-1",
                             store_id="store-1", meta={"items_count": 2, "units": 3, "sum": 1500.5})
    assert row["order_id"] == "ORD-1" and row["meta"]["sum"] == 1500.5
    assert "src" not in row["meta"]
    err = build_server_event("reserve_error", eid="e", user_hash="h", meta={"error": "boom"})
    assert err["meta"]["src"] == "server"
    with pytest.raises(EventValidationError):
        build_server_event("app_open", eid="e", user_hash="h", meta={"platform": "ios"})
    with pytest.raises(EventValidationError, match="order_id: required"):
        build_server_event("reserve_create", eid="e", user_hash="h", store_id="s",
                           meta={"items_count": 1, "units": 1, "sum": 1})


FRONTEND_SPEC = Path(__file__).resolve().parents[2] / "thevaper-miniapp" / "src" / "lib" / "analytics" / "events.json"


@pytest.mark.skipif(not FRONTEND_SPEC.exists(), reason="репозиторий фронта не найден рядом")
def test_frontend_spec_is_in_sync():
    backend = json.loads((Path(__file__).resolve().parents[1] / "analytics" / "events.json").read_text("utf-8"))
    assert json.loads(FRONTEND_SPEC.read_text("utf-8")) == backend
