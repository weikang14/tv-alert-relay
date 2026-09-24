from datetime import datetime, timedelta, timezone
from health import compute_health

NOW = datetime(2026, 7, 26, 12, 0, 0, tzinfo=timezone.utc)


def test_unhealthy_when_never_polled():
    assert compute_health(None, 0, NOW, 300) is False


def test_healthy_within_two_intervals():
    last_poll = NOW - timedelta(seconds=600)
    assert compute_health(last_poll, 0, NOW, 300) is True


def test_unhealthy_past_two_intervals():
    last_poll = NOW - timedelta(seconds=601)
    assert compute_health(last_poll, 0, NOW, 300) is False


def test_unhealthy_at_error_threshold():
    assert compute_health(NOW, 3, NOW, 300) is False


def test_healthy_below_error_threshold():
    assert compute_health(NOW, 2, NOW, 300) is True
