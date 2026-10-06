"""
Проверка подписи Telegram Mini App initData.
Алгоритм: https://core.telegram.org/bots/webapps#validating-data-received-via-the-mini-app

  secret_key       = HMAC_SHA256(key="WebAppData", msg=bot_token)
  data_check_string = все поля кроме hash, отсортированные по ключу, "key=value" через "\n"
  hash             == hex(HMAC_SHA256(key=secret_key, msg=data_check_string))

Единственная реализация проверки в проекте — используйте её везде, где нужен initData.
"""
import hashlib
import hmac
import json
import time
from dataclasses import dataclass
from urllib.parse import parse_qsl

MAX_AGE_SECONDS = 24 * 3600   # initData старше суток отбрасываем
CLOCK_SKEW_SECONDS = 60       # допуск на расхождение часов


class InitDataError(ValueError):
    """initData не прошёл проверку. str(e) — короткий код причины."""


@dataclass(frozen=True)
class TelegramInitData:
    user_id: int
    auth_date: int
    start_param: str | None


def validate_init_data(
    init_data: str,
    bot_token: str,
    *,
    max_age: int = MAX_AGE_SECONDS,
    now: float | None = None,
) -> TelegramInitData:
    if not init_data:
        raise InitDataError("empty")
    if not bot_token:
        raise InitDataError("no_bot_token")

    try:
        pairs = parse_qsl(init_data, keep_blank_values=True, strict_parsing=True)
    except ValueError:
        raise InitDataError("malformed")
    data = dict(pairs)
    if len(data) != len(pairs):
        raise InitDataError("duplicate_keys")

    received_hash = data.pop("hash", None)
    if not received_hash:
        raise InitDataError("no_hash")

    check_string = "\n".join(f"{k}={v}" for k, v in sorted(data.items()))
    secret_key = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    expected = hmac.new(secret_key, check_string.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, received_hash.lower()):
        raise InitDataError("bad_signature")

    try:
        auth_date = int(data["auth_date"])
    except (KeyError, ValueError):
        raise InitDataError("no_auth_date")

    now = time.time() if now is None else now
    if now - auth_date > max_age:
        raise InitDataError("expired")
    if auth_date - now > CLOCK_SKEW_SECONDS:
        raise InitDataError("from_future")

    try:
        user = json.loads(data["user"])
        user_id = int(user["id"])
    except (KeyError, ValueError, TypeError):
        raise InitDataError("no_user")

    return TelegramInitData(
        user_id=user_id,
        auth_date=auth_date,
        start_param=data.get("start_param") or None,
    )
