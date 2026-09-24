from datetime import datetime, timedelta


def compute_health(
    last_poll_at: datetime | None,
    consecutive_errors: int,
    now: datetime,
    poll_interval_seconds: int,
    error_threshold: int = 3,
) -> bool:
    if last_poll_at is None:
        return False
    if now - last_poll_at > timedelta(seconds=poll_interval_seconds * 2):
        return False
    if consecutive_errors >= error_threshold:
        return False
    return True
