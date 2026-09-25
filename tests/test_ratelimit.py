from claw_telegram.ratelimit import RateLimiter


def test_sliding_window_per_user():
    now = [0.0]
    rl = RateLimiter(2, clock=lambda: now[0])
    assert rl.allow(1) and rl.allow(1)
    assert not rl.allow(1)
    assert rl.allow(2)  # other users unaffected
    now[0] = 60.0
    assert rl.allow(1)


def test_zero_disables():
    rl = RateLimiter(0)
    assert all(rl.allow(1) for _ in range(100))
