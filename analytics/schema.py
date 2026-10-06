"""
Валидация событий по единому списку analytics/events.json.

Событие с клиента:
  {"eid": "<uuid4>", "event": "product_view", "ts": <мс, часы клиента>,
   "product_id": "...", "store_id": "...", "meta": {...}}

Время события (ts в БД) считается на сервере с поправкой на часы клиента:
  ts = server_now - (sent_at - client_ts),  ограничено [server_now - 24ч, server_now]
где sent_at — время отправки пачки по часам клиента. Так неверно выставленные часы
телефона не искажают распределение по дням, а исходное значение сохраняется в meta.client_ts.
"""
import json
import math
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

SPEC_PATH = Path(__file__).with_name("events.json")
SPEC: dict = json.loads(SPEC_PATH.read_text(encoding="utf-8"))
EVENTS: dict[str, dict] = SPEC["events"]
EVENT_NAMES = frozenset(EVENTS)
CLIENT_EVENTS = frozenset(n for n, s in EVENTS.items() if "client" in s["source"])
SERVER_EVENTS = frozenset(n for n, s in EVENTS.items() if "server" in s["source"])

MAX_EVENTS_PER_REQUEST = 50
MAX_STRING = 200
MAX_INT = 10**9
MAX_EVENT_AGE = timedelta(hours=24)
ID_FIELDS = ("product_id", "store_id", "order_id")

_ID_RE = re.compile(r"^[A-Za-z0-9._:-]{1,64}$")
_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")
_CLIENT_KEYS = {"eid", "event", "ts", "meta", *ID_FIELDS}


class EventValidationError(ValueError):
    pass


def _field_rules(event: str) -> dict[str, bool]:
    """{"product_id": True (обязательное), "store_id": False (необязательное)}"""
    rules = {}
    for f in EVENTS[event]["fields"]:
        rules[f.rstrip("?")] = not f.endswith("?")
    return rules


def _check_value(key: str, value, type_spec: str):
    t = type_spec.rstrip("?")
    if t == "string":
        if not isinstance(value, str):
            raise EventValidationError(f"meta.{key}: expected string")
        value = value.strip()
        if not value:
            raise EventValidationError(f"meta.{key}: empty")
        if len(value) > MAX_STRING:
            raise EventValidationError(f"meta.{key}: too long")
        return value
    if t == "int":
        # bool — подкласс int в Python, отсекаем явно
        if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= MAX_INT:
            raise EventValidationError(f"meta.{key}: expected int 0..{MAX_INT}")
        return value
    if t == "number":
        if isinstance(value, bool) or not isinstance(value, (int, float)) \
                or not math.isfinite(value) or not 0 <= value <= MAX_INT:
            raise EventValidationError(f"meta.{key}: expected number 0..{MAX_INT}")
        return value
    if t == "bool":
        if not isinstance(value, bool):
            raise EventValidationError(f"meta.{key}: expected bool")
        return value
    raise EventValidationError(f"meta.{key}: unknown type {type_spec}")  # ошибка в events.json


def validate_payload(event: str, fields: dict, meta: dict | None) -> tuple[dict, dict]:
    """Проверяет поля и meta события по схеме. Возвращает очищенные (fields, meta)."""
    if event not in EVENT_NAMES:
        raise EventValidationError("unknown event")

    rules = _field_rules(event)
    clean_fields: dict = {f: None for f in ID_FIELDS}
    for f in ID_FIELDS:
        value = fields.get(f)
        if value is None or value == "":
            if rules.get(f):
                raise EventValidationError(f"{f}: required")
            continue
        if f not in rules:
            raise EventValidationError(f"{f}: not allowed for {event}")
        if not isinstance(value, str) or not _ID_RE.match(value):
            raise EventValidationError(f"{f}: invalid id")
        clean_fields[f] = value

    meta = meta or {}
    if not isinstance(meta, dict):
        raise EventValidationError("meta: expected object")
    meta_spec: dict[str, str] = EVENTS[event]["meta"]
    extra = set(meta) - set(meta_spec)
    if extra:
        raise EventValidationError(f"meta: unexpected keys {sorted(extra)}")
    clean_meta = {}
    for key, type_spec in meta_spec.items():
        value = meta.get(key)
        if value is None or value == "":
            if not type_spec.endswith("?"):
                raise EventValidationError(f"meta.{key}: required")
            continue
        clean_meta[key] = _check_value(key, value, type_spec)
    return clean_fields, clean_meta


def _event_ts(client_ts, sent_at, now: datetime) -> datetime:
    if isinstance(client_ts, bool) or not isinstance(client_ts, (int, float)) or not math.isfinite(client_ts):
        raise EventValidationError("ts: expected epoch milliseconds")
    if isinstance(sent_at, bool) or not isinstance(sent_at, (int, float)) or not math.isfinite(sent_at):
        return now  # без времени отправки поправку посчитать нельзя
    ts = now - timedelta(milliseconds=sent_at - client_ts)
    return min(max(ts, now - MAX_EVENT_AGE), now)


def validate_client_event(raw, *, now: datetime, sent_at) -> dict:
    """Одно событие с клиента → строка для таблицы events (без user_hash)."""
    if not isinstance(raw, dict):
        raise EventValidationError("event: expected object")
    extra = set(raw) - _CLIENT_KEYS
    if extra:
        raise EventValidationError(f"unexpected keys {sorted(extra)}")

    event = raw.get("event")
    if event not in EVENT_NAMES:
        raise EventValidationError("unknown event")
    if event not in CLIENT_EVENTS:
        raise EventValidationError(f"{event}: server-only event")

    eid = raw.get("eid")
    if not isinstance(eid, str) or not _UUID_RE.match(eid.lower()):
        raise EventValidationError("eid: expected uuid4")

    fields, meta = validate_payload(event, raw, raw.get("meta"))
    ts = _event_ts(raw.get("ts"), sent_at, now)
    meta["client_ts"] = int(raw["ts"])
    if event in SERVER_EVENTS:
        meta["src"] = "client"   # событие может прийти и с клиента, и с сервера
    return {"eid": eid.lower(), "ts": ts.isoformat(), "event": event, **fields, "meta": meta}


def validate_client_batch(events, *, sent_at=None, now: datetime | None = None) -> tuple[list[dict], list[dict]]:
    """
    Пачка с клиента → (валидные строки, отклонённые [{index, reason}]).
    Невалидное событие отбрасывается поштучно, остальные из пачки принимаются.
    Превышение лимита пачки — ошибка всего запроса.
    """
    if not isinstance(events, list):
        raise EventValidationError("events: expected array")
    if len(events) > MAX_EVENTS_PER_REQUEST:
        raise EventValidationError(f"events: max {MAX_EVENTS_PER_REQUEST} per request")
    now = now or datetime.now(timezone.utc)
    rows, rejected, seen = [], [], set()
    for i, raw in enumerate(events):
        try:
            row = validate_client_event(raw, now=now, sent_at=sent_at)
        except EventValidationError as e:
            rejected.append({"index": i, "reason": str(e)})
            continue
        if row["eid"] in seen:
            rejected.append({"index": i, "reason": "duplicate eid"})
            continue
        seen.add(row["eid"])
        rows.append(row)
    return rows, rejected


def build_server_event(event: str, *, eid: str, user_hash: str, now: datetime | None = None,
                       meta: dict | None = None, **fields) -> dict:
    """Событие, которое пишет сам сервер (reserve_create/reserve_error). Проверяется той же схемой."""
    if event not in SERVER_EVENTS:
        raise EventValidationError(f"{event}: not a server event")
    clean_fields, clean_meta = validate_payload(event, fields, meta)
    if event in CLIENT_EVENTS:
        clean_meta["src"] = "server"
    now = now or datetime.now(timezone.utc)
    return {"eid": eid, "ts": now.isoformat(), "event": event, "user_hash": user_hash,
            **clean_fields, "meta": clean_meta}
