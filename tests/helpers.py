import hashlib
import hmac
import json
from urllib.parse import urlencode

BOT_TOKEN = "1234567890:TEST-token"
SALT = "test-salt"


def make_init_data(user_id: int = 42, auth_date: int = 1_700_000_000, bot_token: str = BOT_TOKEN,
                   extra: dict | None = None) -> str:
    """Собирает initData с корректной подписью — так же, как это делает Telegram."""
    fields = {
        "auth_date": str(auth_date),
        "query_id": "AAH-test",
        "user": json.dumps({"id": user_id, "first_name": "Иван", "username": "ivan"}, ensure_ascii=False),
        **(extra or {}),
    }
    check_string = "\n".join(f"{k}={v}" for k, v in sorted(fields.items()))
    secret = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    fields["hash"] = hmac.new(secret, check_string.encode(), hashlib.sha256).hexdigest()
    return urlencode(fields)
