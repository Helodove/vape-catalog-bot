"""
Псевдонимизация пользователя: Telegram user id в открытом виде не хранится.

  user_hash = hex(HMAC_SHA256(key=ANALYTICS_SALT, msg=str(user_id)))

Один и тот же пользователь при одном и том же ANALYTICS_SALT всегда даёт один и тот же хэш,
поэтому воронки и DAU/MAU воспроизводимы. Смена ANALYTICS_SALT «обнуляет» связность
пользователей — не меняйте его в течение периода исследования.
"""
import hashlib
import hmac

# Заказ без валидного initData (например, открыт вне Telegram) — пользователь неизвестен.
UNVERIFIED_USER = "unverified"


def user_hash(user_id: int | str, salt: str) -> str:
    if not salt:
        raise ValueError("ANALYTICS_SALT is empty")
    uid = int(user_id)  # нормализация: 42, "42", "042" → один и тот же хэш
    return hmac.new(salt.encode(), str(uid).encode(), hashlib.sha256).hexdigest()
