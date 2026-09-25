# tests/test_signal_engine.py
from datetime import datetime, timezone

from market_data import Bar
from signal_engine import bucket_samples, detect_entries


def _bar(minute: int, o: float, h: float, l: float, c: float) -> Bar:
    return Bar(
        time=datetime(2026, 9, 25, 10, minute, tzinfo=timezone.utc),
        open=o, high=h, low=l, close=c,
    )


def test_bucket_samples_only_emits_on_bucket_closing_bar():
    # bucket 0: minutes 0-7 (aligned to a multiple of 8 since Unix epoch);
    # use minute 0 as a bucket start so minute 7 is the closing bar.
    bars = [_bar(m, 100 + m, 100 + m, 100 + m, 100 + m) for m in range(0, 8)]
    samples = bucket_samples(bars)
    assert len(samples) == 1
    assert samples[0].bar_time.minute == 7


def test_bucket_samples_needs_two_bars_for_alma_length_two():
    bars = [_bar(0, 100, 100, 100, 100)]
    samples = bucket_samples(bars)
    assert samples == []


def test_detect_entries_emits_long_on_close_crossing_above_open():
    samples = [
        # prev bucket: close <= open (no long position yet)
        type("S", (), {"bucket_start": datetime(2026, 9, 25, 10, 0, tzinfo=timezone.utc),
                        "alma_close": 100.0, "alma_open": 100.5,
                        "bar_time": datetime(2026, 9, 25, 10, 7, tzinfo=timezone.utc), "bar_close": 100.0})(),
        # curr bucket: close > open -> crossover long
        type("S", (), {"bucket_start": datetime(2026, 9, 25, 10, 8, tzinfo=timezone.utc),
                        "alma_close": 101.0, "alma_open": 100.2,
                        "bar_time": datetime(2026, 9, 25, 10, 15, tzinfo=timezone.utc), "bar_close": 101.0})(),
    ]
    entries = detect_entries(samples)
    assert len(entries) == 1
    assert entries[0].direction == "long"
    assert entries[0].entry_price == 101.0
    assert entries[0].entry_time == datetime(2026, 9, 25, 10, 15, tzinfo=timezone.utc)


def test_detect_entries_emits_short_on_close_crossing_below_open():
    samples = [
        type("S", (), {"bucket_start": datetime(2026, 9, 25, 10, 0, tzinfo=timezone.utc),
                        "alma_close": 101.0, "alma_open": 100.0,
                        "bar_time": datetime(2026, 9, 25, 10, 7, tzinfo=timezone.utc), "bar_close": 101.0})(),
        type("S", (), {"bucket_start": datetime(2026, 9, 25, 10, 8, tzinfo=timezone.utc),
                        "alma_close": 99.0, "alma_open": 100.0,
                        "bar_time": datetime(2026, 9, 25, 10, 15, tzinfo=timezone.utc), "bar_close": 99.0})(),
    ]
    entries = detect_entries(samples)
    assert len(entries) == 1
    assert entries[0].direction == "short"


def test_detect_entries_emits_nothing_without_a_crossover():
    samples = [
        type("S", (), {"bucket_start": datetime(2026, 9, 25, 10, 0, tzinfo=timezone.utc),
                        "alma_close": 101.0, "alma_open": 100.0,
                        "bar_time": datetime(2026, 9, 25, 10, 7, tzinfo=timezone.utc), "bar_close": 101.0})(),
        type("S", (), {"bucket_start": datetime(2026, 9, 25, 10, 8, tzinfo=timezone.utc),
                        "alma_close": 102.0, "alma_open": 100.5,
                        "bar_time": datetime(2026, 9, 25, 10, 15, tzinfo=timezone.utc), "bar_close": 102.0})(),
    ]
    assert detect_entries(samples) == []
