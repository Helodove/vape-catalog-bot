import hashlib
import hmac

import pytest

from analytics.hashing import user_hash


def test_matches_reference_hmac_sha256():
    expected = hmac.new(b"salt", b"123456789", hashlib.sha256).hexdigest()
    assert user_hash(123456789, "salt") == expected


def test_deterministic_and_normalized():
    assert user_hash(42, "s") == user_hash("42", "s") == user_hash("042", "s")


def test_depends_on_salt_and_user():
    assert user_hash(42, "a") != user_hash(42, "b")
    assert user_hash(42, "a") != user_hash(43, "a")


def test_does_not_contain_raw_id():
    h = user_hash(987654321, "salt")
    assert len(h) == 64 and "987654321" not in h


def test_not_plain_sha256_of_id():
    # Без секрета хэш нельзя подобрать перебором id
    assert user_hash(42, "salt") != hashlib.sha256(b"42").hexdigest()


def test_empty_salt_is_rejected():
    with pytest.raises(ValueError):
        user_hash(42, "")
