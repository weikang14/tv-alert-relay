# tests/test_signal_engine.py
from datetime import datetime, timedelta, timezone
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest

import db as db_module
from market_data import Bar, MarketDataError
from signal_engine import (
    BUCKET_MINUTES,
    CHART_TIMEFRAME_MINUTES,
    INT_RES,
    TIMEOUT_HOURS,
    _bucket_candles,
    _bucket_start,
    _trading_hours_open,
    bucket_samples,
    detect_entries,
    evaluate_signal,
    poll_once,
)

_NY = ZoneInfo("America/New_York")

# A fixed, always-bucket-aligned reference point, derived from the production
# _bucket_start() itself so every test below stays correct regardless of the
# current CHART_TIMEFRAME_MINUTES/BUCKET_MINUTES/INT_RES configuration —
# changing the chart timeframe again should not require rewriting these tests.
_ANCHOR = _bucket_start(datetime(2026, 9, 25, 10, 0, tzinfo=timezone.utc))


def _bar(index: int, o: float, h: float, l: float, c: float) -> Bar:
    """A standalone chart-timeframe bar, `index` chart-bars after _ANCHOR —
    for tests that only care about relative bar ordering (e.g. TP/SL
    crossing checks), not bucket alignment."""
    return Bar(time=_ANCHOR + timedelta(minutes=index * CHART_TIMEFRAME_MINUTES), open=o, high=h, low=l, close=c)


def _bar_at(bucket_index: int, bar_index: int, o: float, h: float, l: float, c: float) -> Bar:
    """bucket_index: which BUCKET_MINUTES-wide bucket, counting from
    _ANCHOR (0-based). bar_index: which chart-timeframe bar within that
    bucket (0..INT_RES-1); INT_RES-1 is the bucket's closing (last) bar."""
    minute_offset = bucket_index * BUCKET_MINUTES + bar_index * CHART_TIMEFRAME_MINUTES
    return Bar(time=_ANCHOR + timedelta(minutes=minute_offset), open=o, high=h, low=l, close=c)


def _bucket_bars(bucket_index: int, o: float, h: float, l: float, c: float) -> list[Bar]:
    """A full, complete bucket's worth of INT_RES bars, all with the same
    OHLC — sufficient for tests that only care about the bucket's
    aggregate open/close (first bar's open, last bar's close)."""
    return [_bar_at(bucket_index, i, o, h, l, c) for i in range(INT_RES)]


def _shortly_after(bars: list[Bar]) -> datetime:
    """A `now` safely after the last bar has fully closed (satisfies the
    "not yet closed" filter) and still well within the staleness window,
    regardless of the current timeframe configuration."""
    return bars[-1].time + timedelta(minutes=CHART_TIMEFRAME_MINUTES * 2)


def _long_after(bars: list[Bar]) -> datetime:
    """A `now` far enough past the last bar to be treated as stale."""
    return bars[-1].time + timedelta(minutes=BUCKET_MINUTES * 10)


def _three_bucket_bullish_cross() -> list[Bar]:
    """Three complete buckets that produce a genuine bullish ALMA crossover
    at the THIRD bucket's closing bar, computed entirely from scratch with
    no carried seed — buckets A and B are both bearish (open=101, close=100),
    bucket C flips bullish (open=100, close=101)."""
    bucket_a = _bucket_bars(0, 101, 101, 101, 100)
    bucket_b = _bucket_bars(1, 101, 101, 101, 100)
    bucket_c = _bucket_bars(2, 100, 101, 100, 101)
    return bucket_a + bucket_b + bucket_c


# --- _bucket_start: session anchor, not UTC midnight ---
# (confirmed against the live TradingView chart for OANDA:XAUUSD: its native
# 120-minute/daily bars start at 21:00 UTC in EDT and 22:00 UTC in EST,
# flipping exactly on the US DST transition dates — not at UTC midnight)

def test_bucket_start_anchors_to_5pm_new_york_in_edt():
    dt = datetime(2026, 7, 1, 22, 30, tzinfo=timezone.utc)  # 18:30 EDT
    assert _bucket_start(dt) == datetime(2026, 7, 1, 21, 0, tzinfo=timezone.utc)


def test_bucket_start_anchors_to_5pm_new_york_in_est():
    dt = datetime(2026, 1, 1, 23, 30, tzinfo=timezone.utc)  # 18:30 EST
    assert _bucket_start(dt) == datetime(2026, 1, 1, 22, 0, tzinfo=timezone.utc)


def test_bucket_start_session_boundary_flips_exactly_at_5pm_new_york():
    before = datetime(2026, 7, 1, 20, 59, tzinfo=timezone.utc)  # 16:59 EDT: previous session
    assert _bucket_start(before) == datetime(2026, 7, 1, 19, 0, tzinfo=timezone.utc)
    after = datetime(2026, 7, 1, 21, 1, tzinfo=timezone.utc)  # 17:01 EDT: new session's first bucket
    assert _bucket_start(after) == datetime(2026, 7, 1, 21, 0, tzinfo=timezone.utc)


# --- _trading_hours_open: real trading time elapsed, weekend excluded ---

def test_trading_hours_open_within_a_single_trading_day():
    entry = datetime(2026, 9, 23, 9, 0, tzinfo=_NY)  # Wednesday
    now = datetime(2026, 9, 23, 15, 0, tzinfo=_NY)  # same day, 6h later
    assert _trading_hours_open(entry, now) == pytest.approx(6.0)


def test_trading_hours_open_excludes_the_full_weekend_closure():
    entry = datetime(2026, 9, 25, 10, 0, tzinfo=_NY)  # Friday, before the 17:00 close
    now = datetime(2026, 9, 28, 10, 0, tzinfo=_NY)  # Monday, same time-of-day
    # 3 real days (72h) minus the 48h Friday-17:00 -> Sunday-17:00 closure = 24h
    assert _trading_hours_open(entry, now) == pytest.approx(24.0)


# --- _bucket_candles / bucket_samples: aggregation on native bucket OHLC ---
# (the C1 fix: ALMA must run on each bucket's own (first-bar-open,
# last-bar-close), not on a continuous chart-timeframe series sampled at the
# bucket boundary — see signal_engine.bucket_samples's docstring)

def test_bucket_candles_uses_first_bar_open_and_last_bar_close():
    bars = [
        _bar_at(0, 0, 100.0, 105.0, 95.0, 102.0),  # first bar: open=100.0 is the bucket's open
        *[_bar_at(0, i, 102.0, 103.0, 101.0, 102.0) for i in range(1, INT_RES - 1)],
        _bar_at(0, INT_RES - 1, 102.0, 103.0, 101.0, 99.0),  # last bar: close=99.0 is the bucket's close
    ]
    candles = _bucket_candles(bars)
    assert len(candles) == 1
    bucket_start, b_open, b_close, bar_time, b_high, b_low = candles[0]
    assert b_open == 100.0
    assert b_close == 99.0
    assert bar_time == bars[-1].time


def test_bucket_candles_needs_the_buckets_first_bar_for_a_correct_open():
    # If the bucket's true first (opening) bar is missing from the input,
    # the candle's open comes from whatever bar happens to be first instead
    # — this is exactly why poll_once must aggregate from the FULL fetch,
    # not a last_bar_time-filtered slice (see the poll_once regression test
    # below).
    full_bucket = [_bar_at(0, i, 100 + i, 101, 99, 100) for i in range(INT_RES)]
    partial_bucket = full_bucket[3:]  # missing the true opening bars
    full_candles = _bucket_candles(full_bucket)
    partial_candles = _bucket_candles(partial_bucket)
    assert full_candles[0][1] == full_bucket[0].open
    assert partial_candles[0][1] == partial_bucket[0].open
    assert full_candles[0][1] != partial_candles[0][1]


def test_bucket_candles_excludes_an_incomplete_bucket():
    bars = [_bar_at(0, i, 100, 100, 100, 100) for i in range(INT_RES - 3)]  # missing the closing bars
    assert _bucket_candles(bars) == []


def test_bucket_samples_returns_empty_without_enough_bucket_history():
    # A single complete bucket with no seed — ALMA(length=2) has nothing to
    # weight it against yet.
    bars = _bucket_bars(0, 100, 100, 100, 100)
    assert bucket_samples(bars) == []


def test_bucket_samples_uses_seed_to_produce_a_sample_from_a_single_new_bucket():
    bars = _bucket_bars(1, 100, 101, 100, 101)
    samples = bucket_samples(bars, seed=(101.0, 100.0))
    assert len(samples) == 1
    assert samples[0].bucket_open == 100.0
    assert samples[0].bucket_close == 101.0
    assert samples[0].bar_time == bars[-1].time


def test_bucket_samples_produces_two_consecutive_samples_from_three_buckets():
    bars = _three_bucket_bullish_cross()
    samples = bucket_samples(bars)
    assert len(samples) == 2  # bucket B and bucket C (bucket A has no seed/history)


# --- detect_entries: crossover detection between consecutive samples ---
# (unaffected by chart timeframe — operates on already-computed samples)

def test_detect_entries_emits_long_on_close_crossing_above_open():
    samples = [
        type("S", (), {"bucket_start": datetime(2026, 9, 25, 10, 0, tzinfo=timezone.utc),
                        "alma_close": 100.0, "alma_open": 100.5,
                        "bar_time": datetime(2026, 9, 25, 10, 7, tzinfo=timezone.utc),
                        "bucket_close": 100.0, "bucket_high": 100.5, "bucket_low": 99.5})(),
        type("S", (), {"bucket_start": datetime(2026, 9, 25, 10, 8, tzinfo=timezone.utc),
                        "alma_close": 101.0, "alma_open": 100.2,
                        "bar_time": datetime(2026, 9, 25, 10, 15, tzinfo=timezone.utc),
                        "bucket_close": 101.0, "bucket_high": 101.5, "bucket_low": 100.5})(),
    ]
    entries = detect_entries(samples)
    assert len(entries) == 1
    assert entries[0].direction == "long"
    assert entries[0].entry_price == 101.0
    assert entries[0].entry_high == 101.5
    assert entries[0].entry_low == 100.5
    assert entries[0].entry_time == datetime(2026, 9, 25, 10, 15, tzinfo=timezone.utc)


def test_detect_entries_emits_short_on_close_crossing_below_open():
    samples = [
        type("S", (), {"bucket_start": datetime(2026, 9, 25, 10, 0, tzinfo=timezone.utc),
                        "alma_close": 101.0, "alma_open": 100.0,
                        "bar_time": datetime(2026, 9, 25, 10, 7, tzinfo=timezone.utc),
                        "bucket_close": 101.0, "bucket_high": 101.5, "bucket_low": 100.5})(),
        type("S", (), {"bucket_start": datetime(2026, 9, 25, 10, 8, tzinfo=timezone.utc),
                        "alma_close": 99.0, "alma_open": 100.0,
                        "bar_time": datetime(2026, 9, 25, 10, 15, tzinfo=timezone.utc),
                        "bucket_close": 99.0, "bucket_high": 100.0, "bucket_low": 98.5})(),
    ]
    entries = detect_entries(samples)
    assert len(entries) == 1
    assert entries[0].direction == "short"


def test_detect_entries_emits_nothing_without_a_crossover():
    samples = [
        type("S", (), {"bucket_start": datetime(2026, 9, 25, 10, 0, tzinfo=timezone.utc),
                        "alma_close": 101.0, "alma_open": 100.0,
                        "bar_time": datetime(2026, 9, 25, 10, 7, tzinfo=timezone.utc),
                        "bucket_close": 101.0, "bucket_high": 101.5, "bucket_low": 100.5})(),
        type("S", (), {"bucket_start": datetime(2026, 9, 25, 10, 8, tzinfo=timezone.utc),
                        "alma_close": 102.0, "alma_open": 100.5,
                        "bar_time": datetime(2026, 9, 25, 10, 15, tzinfo=timezone.utc),
                        "bucket_close": 102.0, "bucket_high": 102.5, "bucket_low": 101.5})(),
    ]
    assert detect_entries(samples) == []


# --- evaluate_signal: crossover/crossunder-based TP/SL (mirrors f_cross()) ---
# (unaffected by chart timeframe — operates on relative bar ordering only)

def test_evaluate_signal_long_hits_sl_only():
    entry_time = _ANCHOR
    bars = [_bar(1, 100, 100, 99.85, 99.9)]  # low crosses under SL=99.9
    tier, status, prev_high, prev_low, exit_time = evaluate_signal(
        "long", 100.0, entry_time, 0, prev_high=100.0, prev_low=100.0, bars=bars,
    )
    assert tier == 0
    assert status == "SL_ONLY"
    assert exit_time == bars[0].time


def test_evaluate_signal_long_advances_through_all_tiers():
    entry_time = _ANCHOR
    bars = [
        _bar(1, 100, 100.25, 100, 100.2),
        _bar(2, 100.2, 100.4, 100.2, 100.3),
        _bar(3, 100.3, 100.5, 100.3, 100.45),
    ]
    tier, status, prev_high, prev_low, exit_time = evaluate_signal(
        "long", 100.0, entry_time, 0, prev_high=100.0, prev_low=100.0, bars=bars,
    )
    assert tier == 3
    assert status == "TP3_FULL"
    assert exit_time == bars[-1].time


def test_evaluate_signal_short_hits_tp1_then_sl():
    entry_time = _ANCHOR
    bars = [
        _bar(1, 100, 100, 99.79, 99.8),
        _bar(2, 99.8, 100.15, 99.8, 100.1),
    ]
    tier, status, prev_high, prev_low, exit_time = evaluate_signal(
        "short", 100.0, entry_time, 0, prev_high=100.0, prev_low=100.0, bars=bars,
    )
    assert tier == 1
    assert status == "TP1_THEN_SL"
    assert exit_time == bars[-1].time


def test_evaluate_signal_prioritizes_tp_over_sl_in_same_bar():
    entry_time = _ANCHOR
    bars = [_bar(1, 100, 100.3, 99.8, 100.0)]  # crosses both TP1 (100.2) and SL (99.9)
    tier, status, prev_high, prev_low, exit_time = evaluate_signal(
        "long", 100.0, entry_time, 0, prev_high=100.0, prev_low=100.0, bars=bars,
    )
    assert tier == 1
    assert status is None  # advanced to TP1, still open, not closed by SL


def test_evaluate_signal_blowing_through_two_tiers_in_one_bar_advances_only_one():
    # Claim 3 (verified against real Pine `switch` semantics): even if a
    # single bar's range technically clears TWO tiers' thresholds, Pine's
    # switch only executes its first matching branch per bar — only tier 1
    # advances; tier 2 needs its own fresh crossing on a LATER bar.
    entry_time = _ANCHOR
    bars = [_bar(1, 100, 100.5, 100, 100.4)]  # high clears both TP1(100.2) and TP2(100.35)
    tier, status, prev_high, prev_low, exit_time = evaluate_signal(
        "long", 100.0, entry_time, 0, prev_high=100.0, prev_low=100.0, bars=bars,
    )
    assert tier == 1  # NOT 2
    assert status is None


def test_evaluate_signal_does_not_trigger_without_a_genuine_crossing():
    # f_cross() requires the PREVIOUS bar to have been on the safe side —
    # merely "still below" on a later bar does not re-trigger.
    entry_time = _ANCHOR
    bars = [_bar(1, 99.8, 99.9, 99.7, 99.85)]
    tier, status, prev_high, prev_low, exit_time = evaluate_signal(
        "long", 100.0, entry_time, 0, prev_high=100.0, prev_low=99.85, bars=bars,
        # prev_low (99.85) is ALREADY below SL (99.9) — no genuine crossunder
    )
    assert tier == 0
    assert status is None


def test_evaluate_signal_ignores_bars_at_or_before_entry_time():
    entry_time = _ANCHOR
    bars = [_bar(0, 100, 100, 90, 90)]  # index 0 == entry_time itself, must be skipped
    tier, status, prev_high, prev_low, exit_time = evaluate_signal(
        "long", 100.0, entry_time, 0, prev_high=100.0, prev_low=100.0, bars=bars,
    )
    assert tier == 0
    assert status is None
    assert prev_high == 100.0  # unchanged: the bar was skipped entirely
    assert prev_low == 100.0


def test_evaluate_signal_no_change_when_no_bars_qualify():
    entry_time = _ANCHOR
    bars = [_bar(1, 100, 100.05, 99.95, 100.0)]  # inside all thresholds
    tier, status, prev_high, prev_low, exit_time = evaluate_signal(
        "long", 100.0, entry_time, 0, prev_high=100.0, prev_low=100.0, bars=bars,
    )
    assert tier == 0
    assert status is None
    assert prev_high == 100.05  # still updated: this bar WAS processed
    assert prev_low == 99.95


# --- poll_once orchestration ---

def test_poll_once_noops_when_api_key_missing():
    conn = db_module.connect("sqlite:///:memory:")
    with patch("signal_engine.fetch_recent_bars") as fake_fetch:
        poll_once(conn, "", "XAU/USD", "tok", "chat")
        fake_fetch.assert_not_called()


def test_poll_once_bootstraps_with_200_bars_on_first_run():
    conn = db_module.connect("sqlite:///:memory:")
    with patch("signal_engine.fetch_recent_bars", return_value=[]) as fake_fetch, \
         patch("signal_engine.send_telegram_message", return_value=(True, None)):
        poll_once(conn, "key", "XAU/USD", "tok", "chat")
        assert fake_fetch.call_args.args[2] == 200  # outputsize


def test_poll_once_caps_outputsize_at_200_after_long_gap(monkeypatch):
    conn = db_module.connect("sqlite:///:memory:")
    db_module.set_last_bar_time(conn, "2026-01-01T00:00:00+00:00")  # far in the past
    with patch("signal_engine.fetch_recent_bars", return_value=[]) as fake_fetch:
        poll_once(conn, "key", "XAU/USD", "tok", "chat")
        assert fake_fetch.call_args.args[2] == 200


def test_poll_once_outputsize_includes_full_bucket_margin():
    # Regression: `outputsize` is a BAR count, not a minute count — a small
    # poll gap alone is not enough margin either way: a bucket that closed
    # mid-gap still needs its true first bar in the fetch, which requires
    # reaching back a full bucket width (in bars), not just the gap.
    conn = db_module.connect("sqlite:///:memory:")
    db_module.set_last_bar_time(conn, (_ANCHOR - timedelta(minutes=5)).isoformat())
    with patch("signal_engine.fetch_recent_bars", return_value=[]) as fake_fetch:
        poll_once(conn, "key", "XAU/USD", "tok", "chat", now=_ANCHOR)
    gap_bars = -(-5 // CHART_TIMEFRAME_MINUTES)  # ceil division, matching poll_once
    assert fake_fetch.call_args.args[2] == gap_bars + 2 * INT_RES + 1


def test_poll_once_detects_entry_pushes_telegram_and_records_signal():
    conn = db_module.connect("sqlite:///:memory:")
    bars = _three_bucket_bullish_cross()
    with patch("signal_engine.fetch_recent_bars", return_value=bars), \
         patch("signal_engine.send_telegram_message", return_value=(True, None)) as fake_send:
        poll_once(conn, "key", "XAU/USD", "tok", "chat", now=_shortly_after(bars))
    rows = db_module.recent_signals(conn)
    assert len(rows) == 1
    assert rows[0]["direction"] == "long"
    assert rows[0]["entry_price"] == 101.0
    assert rows[0]["entry_high"] == 101.0
    assert rows[0]["entry_low"] == 100.0
    assert fake_send.called
    # I3: entry push carries the signal id so it can be matched to its later close push
    assert f"Signal Entry #{rows[0]['id']}" in fake_send.call_args.args[2]
    assert db_module.get_last_bar_time(conn) == bars[-1].time.isoformat()


def test_poll_once_detects_entry_using_carried_over_bucket_sample():
    conn = db_module.connect("sqlite:///:memory:")
    bucket_b = _bucket_bars(1, 101, 101, 101, 100)
    db_module.set_last_bar_time(conn, bucket_b[-1].time.isoformat())
    # Carried bucket B: bearish (alma_close <= alma_open), raw open=101/close=100.
    db_module.set_last_bucket_carry(conn, _bucket_start(bucket_b[0].time).isoformat(), 101.0, 100.0, 100.0, 101.0)
    bucket_c = _bucket_bars(2, 100, 101, 100, 101)
    with patch("signal_engine.fetch_recent_bars", return_value=bucket_c), \
         patch("signal_engine.send_telegram_message", return_value=(True, None)):
        poll_once(conn, "key", "XAU/USD", "tok", "chat", now=_shortly_after(bucket_c))
    rows = db_module.recent_signals(conn)
    assert len(rows) == 1
    assert rows[0]["direction"] == "long"


def test_poll_once_aggregates_bucket_from_full_fetch_not_filtered_bars():
    # Regression: bucket_samples must aggregate candles from the full fetch,
    # not the last_bar_time-filtered `bars` — otherwise a bucket whose early
    # bars were already seen in a prior poll gets the WRONG open (the
    # filtered slice's first bar, not the bucket's TRUE first bar),
    # corrupting the ALMA input and potentially flipping the result.
    conn = db_module.connect("sqlite:///:memory:")
    bucket_c = (
        [_bar_at(2, 0, 90, 101, 89, 100)]  # TRUE bucket open = 90
        + [_bar_at(2, i, 100, 101, 99, 100) for i in range(1, INT_RES - 1)]
        + [_bar_at(2, INT_RES - 1, 105, 101, 100, 101)]  # if wrongly used as "the bucket's open", flips the result
    )
    db_module.set_last_bar_time(conn, bucket_c[-2].time.isoformat())  # only the closing bar is "new"
    carry_bucket_start = _bar_at(1, 0, 0, 0, 0, 0).time
    db_module.set_last_bucket_carry(conn, carry_bucket_start.isoformat(), 101.0, 100.0, 100.0, 101.0)
    with patch("signal_engine.fetch_recent_bars", return_value=bucket_c), \
         patch("signal_engine.send_telegram_message", return_value=(True, None)):
        poll_once(conn, "key", "XAU/USD", "tok", "chat", now=_shortly_after(bucket_c))
    rows = db_module.recent_signals(conn)
    assert len(rows) == 1
    assert rows[0]["direction"] == "long"


def test_poll_once_ignores_not_yet_closed_bar():
    # Regression for C2: a bar whose period has not fully elapsed yet must
    # not be treated as final.
    conn = db_module.connect("sqlite:///:memory:")
    entry_time = _ANCHOR
    db_module.insert_signal(conn, "long", 100.0, entry_time.isoformat(), 100.1, 100.0)
    still_forming = _bar(1, 100, 100, 99.85, 99.9)  # would trigger SL if treated as final
    frozen_now = still_forming.time + timedelta(minutes=CHART_TIMEFRAME_MINUTES - 1)  # not yet closed
    with patch("signal_engine.fetch_recent_bars", return_value=[still_forming]), \
         patch("signal_engine.send_telegram_message", return_value=(True, None)):
        poll_once(conn, "key", "XAU/USD", "tok", "chat", now=frozen_now)
    rows = db_module.recent_signals(conn)
    assert rows[0]["status"] == "OPEN"


def test_poll_once_processes_bar_once_fully_closed():
    conn = db_module.connect("sqlite:///:memory:")
    entry_time = _ANCHOR
    db_module.insert_signal(conn, "long", 100.0, entry_time.isoformat(), 100.1, 100.0)
    sl_bar = _bar(1, 100, 100, 99.85, 99.9)
    frozen_now = sl_bar.time + timedelta(minutes=CHART_TIMEFRAME_MINUTES)  # fully elapsed
    with patch("signal_engine.fetch_recent_bars", return_value=[sl_bar]), \
         patch("signal_engine.send_telegram_message", return_value=(True, None)):
        poll_once(conn, "key", "XAU/USD", "tok", "chat", now=frozen_now)
    rows = db_module.recent_signals(conn)
    assert rows[0]["status"] == "SL_ONLY"
    assert rows[0]["exited_at"] == sl_bar.time.isoformat()  # exit uses the exit BAR's time (M1 fix)


def test_poll_once_survives_telegram_failure_on_entry_push():
    conn = db_module.connect("sqlite:///:memory:")
    bars = _three_bucket_bullish_cross()
    with patch("signal_engine.fetch_recent_bars", return_value=bars), \
         patch("signal_engine.send_telegram_message", return_value=(False, "boom")):
        poll_once(conn, "key", "XAU/USD", "tok", "chat", now=_shortly_after(bars))  # must not raise
    assert len(db_module.recent_signals(conn)) == 1  # still recorded despite push failure


def test_poll_once_swallows_market_data_errors():
    conn = db_module.connect("sqlite:///:memory:")
    with patch("signal_engine.fetch_recent_bars", side_effect=MarketDataError("boom")):
        poll_once(conn, "key", "XAU/USD", "tok", "chat")  # must not raise
    assert db_module.get_last_bar_time(conn) is None  # nothing advanced


def test_poll_once_closes_open_signal_and_pushes_result():
    conn = db_module.connect("sqlite:///:memory:")
    entry_time = _ANCHOR
    db_module.insert_signal(conn, "long", 100.0, entry_time.isoformat(), 100.1, 100.0)
    sl_bar = _bar(1, 100, 100, 99.85, 99.9)  # crosses under SL (prev_low=100.0 > 99.9)
    with patch("signal_engine.fetch_recent_bars", return_value=[sl_bar]), \
         patch("signal_engine.send_telegram_message", return_value=(True, None)) as fake_send:
        poll_once(conn, "key", "XAU/USD", "tok", "chat", now=sl_bar.time + timedelta(minutes=CHART_TIMEFRAME_MINUTES))
    rows = db_module.recent_signals(conn)
    assert rows[0]["status"] == "SL_ONLY"
    assert fake_send.called


def test_poll_once_survives_telegram_failure_on_close_push():
    conn = db_module.connect("sqlite:///:memory:")
    entry_time = _ANCHOR
    db_module.insert_signal(conn, "long", 100.0, entry_time.isoformat(), 100.1, 100.0)
    sl_bar = _bar(1, 100, 100, 99.85, 99.9)
    with patch("signal_engine.fetch_recent_bars", return_value=[sl_bar]), \
         patch("signal_engine.send_telegram_message", return_value=(False, "boom")):
        poll_once(conn, "key", "XAU/USD", "tok", "chat", now=sl_bar.time + timedelta(minutes=CHART_TIMEFRAME_MINUTES))
    rows = db_module.recent_signals(conn)
    assert rows[0]["status"] == "SL_ONLY"  # still updated despite push failure


def test_poll_once_advances_tier_without_closing_or_pushing():
    conn = db_module.connect("sqlite:///:memory:")
    entry_time = _ANCHOR
    db_module.insert_signal(conn, "long", 100.0, entry_time.isoformat(), 100.0, 99.9)
    tp1_bar = _bar(1, 100, 100.25, 100, 100.2)  # crosses TP1 only (prev_high=100.0 < 100.2)
    with patch("signal_engine.fetch_recent_bars", return_value=[tp1_bar]), \
         patch("signal_engine.send_telegram_message", return_value=(True, None)) as fake_send:
        poll_once(conn, "key", "XAU/USD", "tok", "chat", now=tp1_bar.time + timedelta(minutes=CHART_TIMEFRAME_MINUTES))
    rows = db_module.recent_signals(conn)
    assert rows[0]["status"] == "OPEN"
    assert rows[0]["highest_tier"] == 1
    assert rows[0]["last_bar_high"] == 100.25  # persisted for the next poll's crossing check
    assert rows[0]["last_bar_low"] == 100.0
    assert not fake_send.called  # no push for a tier advance that doesn't close


def test_poll_once_reverses_open_signal_on_opposite_entry():
    # Pine's strategy.entry() (pyramiding=0) reverses the position when an
    # opposite-direction trigger fires — the old signal must close (at
    # whatever tier it had reached) exactly when the new one opens.
    conn = db_module.connect("sqlite:///:memory:")
    signal_id = db_module.insert_signal(
        conn, "long", 95.0, (_ANCHOR - timedelta(minutes=BUCKET_MINUTES * 5)).isoformat(), 95.1, 94.9,
    )
    # Already at TP1, and its remaining TP2 threshold (~95.33) was already
    # cleared a while ago (last_bar_high/low near the new bucket's own
    # range) — the new bucket's bars produce no FRESH crossing, so the
    # normal TP/SL pass correctly leaves it OPEN at tier 1 for the reversal
    # handling below to act on.
    db_module.update_signal(conn, signal_id, 1, "OPEN", None, 100.5, 100.4)
    # Seed a bullish previous bucket so the new (bearish) bucket triggers SHORT.
    carry_bucket_start = _bar_at(1, 0, 0, 0, 0, 0).time
    db_module.set_last_bucket_carry(conn, carry_bucket_start.isoformat(), 100.0, 101.0, 101.0, 100.0)
    bucket_c = _bucket_bars(2, 101, 101, 100, 100)
    with patch("signal_engine.fetch_recent_bars", return_value=bucket_c), \
         patch("signal_engine.send_telegram_message", return_value=(True, None)) as fake_send:
        poll_once(conn, "key", "XAU/USD", "tok", "chat", now=_shortly_after(bucket_c))
    rows = db_module.recent_signals(conn)
    assert len(rows) == 2
    long_row = next(r for r in rows if r["direction"] == "long")
    short_row = next(r for r in rows if r["direction"] == "short")
    assert long_row["status"] == "TP1_THEN_REVERSED"
    assert long_row["exited_at"] == short_row["entry_time"]
    assert short_row["status"] == "OPEN"
    texts = [c.args[2] for c in fake_send.call_args_list]
    assert any("TP1_THEN_REVERSED" in t for t in texts)
    assert any("Signal Entry" in t for t in texts)


def test_poll_once_reversal_close_not_pushed_when_entry_is_stale():
    conn = db_module.connect("sqlite:///:memory:")
    signal_id = db_module.insert_signal(
        conn, "long", 95.0, (_ANCHOR - timedelta(minutes=BUCKET_MINUTES * 5)).isoformat(), 95.1, 94.9,
    )
    # Its TP1 threshold (~95.19) was already cleared a while ago (last_bar_
    # high/low near the new bucket's own range), so the new bucket's bars
    # produce no fresh crossing — stays OPEN at tier 0 for the reversal to
    # act on.
    db_module.update_signal(conn, signal_id, 0, "OPEN", None, 100.5, 100.4)
    carry_bucket_start = _bar_at(1, 0, 0, 0, 0, 0).time
    db_module.set_last_bucket_carry(conn, carry_bucket_start.isoformat(), 100.0, 101.0, 101.0, 100.0)
    bucket_c = _bucket_bars(2, 101, 101, 100, 100)
    frozen_now = _long_after(bucket_c)  # stale
    with patch("signal_engine.fetch_recent_bars", return_value=bucket_c), \
         patch("signal_engine.send_telegram_message", return_value=(True, None)) as fake_send:
        poll_once(conn, "key", "XAU/USD", "tok", "chat", now=frozen_now)
    rows = db_module.recent_signals(conn)
    long_row = next(r for r in rows if r["direction"] == "long")
    assert long_row["status"] == "REVERSED_ONLY"  # still recorded
    assert not fake_send.called  # neither the reversal close nor the new entry was pushed


def test_poll_once_evaluates_tp_sl_between_two_entries_in_the_same_poll():
    # Regression: a poll batch can contain more than one entry (cold-start
    # bootstrap replay, or catching up after an outage spanning several
    # bucket cycles). The first entry's own TP/SL must still be checked
    # against the bars between it and the next (reversing) entry — not
    # skipped straight to REVERSED_ONLY just because a later entry lands in
    # the same poll.
    conn = db_module.connect("sqlite:///:memory:")
    bucket0 = _bucket_bars(0, 101, 101, 101, 100)
    bucket1 = _bucket_bars(1, 101, 101, 101, 100)
    bucket2 = _bucket_bars(2, 100, 101, 100, 101)  # -> LONG entry: price=101, high=101, low=100
    bucket3 = (
        [_bar_at(3, 0, 101, 101, 101, 100)]
        + [_bar_at(3, 1, 100, 101.3, 100, 100.5)]  # high=101.3 crosses LONG's TP1 (101.202)
        + [_bar_at(3, i, 100, 100.5, 99.5, 100) for i in range(2, INT_RES - 1)]
        + [_bar_at(3, INT_RES - 1, 100.2, 100.5, 99.5, 100)]  # bucket close=100 -> SHORT entry (reverses)
    )
    bars = bucket0 + bucket1 + bucket2 + bucket3
    with patch("signal_engine.fetch_recent_bars", return_value=bars), \
         patch("signal_engine.send_telegram_message", return_value=(True, None)):
        poll_once(conn, "key", "XAU/USD", "tok", "chat", now=_shortly_after(bars))
    rows = db_module.recent_signals(conn)
    long_row = next(r for r in rows if r["direction"] == "long")
    short_row = next(r for r in rows if r["direction"] == "short")
    assert long_row["status"] == "TP1_THEN_REVERSED"
    assert long_row["highest_tier"] == 1
    assert short_row["status"] == "OPEN"


def test_poll_once_suppresses_push_but_still_records_stale_entries_on_bootstrap():
    # I1: a bootstrap/long-outage backlog must not be PUSHED as if it just
    # happened, but must still be RECORDED for win-rate history.
    conn = db_module.connect("sqlite:///:memory:")
    bars = _three_bucket_bullish_cross()
    frozen_now = _long_after(bars)
    with patch("signal_engine.fetch_recent_bars", return_value=bars), \
         patch("signal_engine.send_telegram_message", return_value=(True, None)) as fake_send:
        poll_once(conn, "key", "XAU/USD", "tok", "chat", now=frozen_now)
    rows = db_module.recent_signals(conn)
    assert len(rows) == 1
    assert rows[0]["direction"] == "long"
    assert not fake_send.called


def test_poll_once_times_out_signal_open_past_threshold_with_no_new_bars():
    # The timeout sweep is a wall-clock decision, not a bar-driven one — it
    # must still run on a poll that finds no new market data.
    conn = db_module.connect("sqlite:///:memory:")
    entry_time = _ANCHOR - timedelta(hours=TIMEOUT_HOURS + 1)
    db_module.insert_signal(conn, "long", 100.0, entry_time.isoformat(), 100.1, 99.9)
    with patch("signal_engine.fetch_recent_bars", return_value=[]), \
         patch("signal_engine.send_telegram_message", return_value=(True, None)) as fake_send:
        poll_once(conn, "key", "XAU/USD", "tok", "chat", now=_ANCHOR)
    rows = db_module.recent_signals(conn)
    assert rows[0]["status"] == "TIMES_UP"
    assert rows[0]["exited_at"] == _ANCHOR.isoformat()
    assert not fake_send.called  # no push for a timeout closure


def test_poll_once_times_out_signal_at_its_reached_tier():
    conn = db_module.connect("sqlite:///:memory:")
    entry_time = _ANCHOR - timedelta(hours=TIMEOUT_HOURS + 1)
    signal_id = db_module.insert_signal(conn, "long", 100.0, entry_time.isoformat(), 100.1, 99.9)
    db_module.update_signal(conn, signal_id, 1, "OPEN", None, 100.1, 99.9)
    with patch("signal_engine.fetch_recent_bars", return_value=[]):
        poll_once(conn, "key", "XAU/USD", "tok", "chat", now=_ANCHOR)
    rows = db_module.recent_signals(conn)
    assert rows[0]["status"] == "TP1_THEN_TIMES_UP"


def test_poll_once_keeps_signal_open_when_under_timeout_threshold():
    conn = db_module.connect("sqlite:///:memory:")
    entry_time = _ANCHOR - timedelta(hours=TIMEOUT_HOURS - 1)
    db_module.insert_signal(conn, "long", 100.0, entry_time.isoformat(), 100.1, 99.9)
    with patch("signal_engine.fetch_recent_bars", return_value=[]):
        poll_once(conn, "key", "XAU/USD", "tok", "chat", now=_ANCHOR)
    rows = db_module.recent_signals(conn)
    assert rows[0]["status"] == "OPEN"
