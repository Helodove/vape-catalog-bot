from analytics.rate_limit import RateLimiter


def test_sliding_window():
    t = [0.0]
    rl = RateLimiter(2, 60, clock=lambda: t[0])
    assert rl.allow("u") and rl.allow("u")
    assert not rl.allow("u")
    assert rl.allow("other"), "лимит считается отдельно для каждого user_hash"
    t[0] = 60.0
    assert rl.allow("u")
