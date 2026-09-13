import pytest

import rate_limit
from rate_limit import RateLimitError, check_rate_limit

USER = "33333333-3333-3333-3333-333333333333"
OTHER_USER = "44444444-4444-4444-4444-444444444444"


def test_requests_under_the_limit_are_allowed(aws, monkeypatch):
    monkeypatch.setenv("RATE_LIMIT_MAX_REQUESTS", "3")

    for _ in range(3):
        check_rate_limit(USER)


def test_request_over_the_limit_is_rejected(aws, monkeypatch):
    monkeypatch.setenv("RATE_LIMIT_MAX_REQUESTS", "3")

    for _ in range(3):
        check_rate_limit(USER)

    with pytest.raises(RateLimitError, match="^Too many requests"):
        check_rate_limit(USER)


def test_limit_is_tracked_independently_per_user(aws, monkeypatch):
    monkeypatch.setenv("RATE_LIMIT_MAX_REQUESTS", "1")

    check_rate_limit(USER)
    check_rate_limit(OTHER_USER)


def test_a_new_window_resets_the_counter(aws, monkeypatch):
    monkeypatch.setenv("RATE_LIMIT_MAX_REQUESTS", "1")
    monkeypatch.setenv("RATE_LIMIT_WINDOW_SECONDS", "60")

    monkeypatch.setattr(rate_limit.time, "time", lambda: 0)
    check_rate_limit(USER)

    monkeypatch.setattr(rate_limit.time, "time", lambda: 60)
    check_rate_limit(USER)
