# tests/test_signal_engine.py
from datetime import datetime, timezone

from market_data import Bar
from signal_engine import bucket_samples, detect_entries, evaluate_signal


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


def test_evaluate_signal_long_hits_sl_only():
    entry_time = datetime(2026, 9, 25, 10, 15, tzinfo=timezone.utc)
    bars = [_bar(16, 100, 100, 99.85, 99.9)]  # low 99.85 <= 100 * (1 - 0.1/100) = 99.9
    tier, status = evaluate_signal("long", 100.0, entry_time, 0, bars)
    assert tier == 0
    assert status == "SL_ONLY"


def test_evaluate_signal_long_advances_through_all_tiers():
    entry_time = datetime(2026, 9, 25, 10, 15, tzinfo=timezone.utc)
    bars = [
        _bar(16, 100, 100.25, 100, 100.2),   # high >= 100.2 (TP1 = +0.2%)
        _bar(17, 100.2, 100.4, 100.2, 100.3),  # high >= 100.35 (TP2 = +0.35%)
        _bar(18, 100.3, 100.5, 100.3, 100.45),  # high >= 100.45 (TP3 = +0.45%)
    ]
    tier, status = evaluate_signal("long", 100.0, entry_time, 0, bars)
    assert tier == 3
    assert status == "TP3_FULL"


def test_evaluate_signal_short_hits_tp1_then_sl():
    entry_time = datetime(2026, 9, 25, 10, 15, tzinfo=timezone.utc)
    bars = [
        _bar(16, 100, 100, 99.79, 99.8),   # low <= 99.8 (TP1 short = -0.2%)
        _bar(17, 99.8, 100.15, 99.8, 100.1),  # high >= 100.1 (SL short = +0.1%)
    ]
    tier, status = evaluate_signal("short", 100.0, entry_time, 0, bars)
    assert tier == 1
    assert status == "TP1_THEN_SL"


def test_evaluate_signal_prioritizes_tp_over_sl_in_same_bar():
    entry_time = datetime(2026, 9, 25, 10, 15, tzinfo=timezone.utc)
    # single bar's range covers both TP1 (100.2) and SL (99.9) for a long entry
    bars = [_bar(16, 100, 100.3, 99.8, 100.0)]
    tier, status = evaluate_signal("long", 100.0, entry_time, 0, bars)
    assert tier == 1
    assert status is None  # advanced to TP1, still open, not closed by SL


def test_evaluate_signal_ignores_bars_at_or_before_entry_time():
    entry_time = datetime(2026, 9, 25, 10, 15, tzinfo=timezone.utc)
    bars = [_bar(15, 100, 100, 90, 90)]  # would be SL, but is the entry bar itself
    tier, status = evaluate_signal("long", 100.0, entry_time, 0, bars)
    assert tier == 0
    assert status is None


def test_evaluate_signal_no_change_when_no_bars_qualify():
    entry_time = datetime(2026, 9, 25, 10, 15, tzinfo=timezone.utc)
    bars = [_bar(16, 100, 100.05, 99.95, 100.0)]  # inside all thresholds
    tier, status = evaluate_signal("long", 100.0, entry_time, 0, bars)
    assert tier == 0
    assert status is None
