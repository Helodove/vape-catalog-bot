from urllib.parse import parse_qsl, urlencode

import pytest

from analytics.telegram_auth import MAX_AGE_SECONDS, InitDataError, validate_init_data
from tests.helpers import BOT_TOKEN, make_init_data

NOW = 1_700_000_000 + 60


def test_valid_init_data():
    result = validate_init_data(make_init_data(user_id=777), BOT_TOKEN, now=NOW)
    assert result.user_id == 777
    assert result.auth_date == 1_700_000_000
    assert result.start_param is None


def test_start_param_is_returned():
    init = make_init_data(extra={"start_param": "promo_lipetsk"})
    assert validate_init_data(init, BOT_TOKEN, now=NOW).start_param == "promo_lipetsk"


def test_signature_field_is_part_of_check_string():
    # Новые клиенты Telegram добавляют поле signature — оно участвует в проверке hash
    init = make_init_data(extra={"signature": "abc123"})
    assert validate_init_data(init, BOT_TOKEN, now=NOW).user_id == 42


@pytest.mark.parametrize("init, reason", [
    ("", "empty"),
    ("user=%7B%7D&auth_date=1", "no_hash"),
    ("a=1&a=2&hash=x", "duplicate_keys"),
])
def test_rejects_malformed(init, reason):
    with pytest.raises(InitDataError, match=reason):
        validate_init_data(init, BOT_TOKEN, now=NOW)


def test_rejects_wrong_bot_token():
    with pytest.raises(InitDataError, match="bad_signature"):
        validate_init_data(make_init_data(), "999:other-bot", now=NOW)


def test_rejects_tampered_user():
    fields = dict(parse_qsl(make_init_data(user_id=1)))
    fields["user"] = fields["user"].replace('"id": 1', '"id": 2')
    with pytest.raises(InitDataError, match="bad_signature"):
        validate_init_data(urlencode(fields), BOT_TOKEN, now=NOW)


def test_rejects_tampered_hash():
    fields = dict(parse_qsl(make_init_data()))
    fields["hash"] = "0" * 64
    with pytest.raises(InitDataError, match="bad_signature"):
        validate_init_data(urlencode(fields), BOT_TOKEN, now=NOW)


def test_rejects_older_than_24h():
    init = make_init_data(auth_date=1_700_000_000)
    assert validate_init_data(init, BOT_TOKEN, now=1_700_000_000 + MAX_AGE_SECONDS).user_id == 42
    with pytest.raises(InitDataError, match="expired"):
        validate_init_data(init, BOT_TOKEN, now=1_700_000_000 + MAX_AGE_SECONDS + 1)


def test_rejects_auth_date_from_future():
    with pytest.raises(InitDataError, match="from_future"):
        validate_init_data(make_init_data(auth_date=NOW + 3600), BOT_TOKEN, now=NOW)


def test_rejects_without_bot_token():
    with pytest.raises(InitDataError, match="no_bot_token"):
        validate_init_data(make_init_data(), "", now=NOW)
