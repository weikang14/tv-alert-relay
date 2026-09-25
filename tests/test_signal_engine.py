# tests/test_signal_engine.py
from datetime import datetime, timezone
from unittest.mock import patch

import db as db_module
from market_data import Bar, MarketDataError
from signal_engine import _bucket_candles, bucket_samples, detect_entries, evaluate_signal, poll_once


def _bar(minute: int, o: float, h: float, l: float, c: float) -> Bar:
    return Bar(
        time=datetime(2026, 9, 25, 10, minute, tzinfo=timezone.utc),
        open=o, high=h, low=l, close=c,
    )


def _three_bucket_bullish_cross() -> list[Bar]:
    """24 bars (3 complete 8-minute buckets: minutes 0-7, 8-15, 16-23) that
    produce a genuine bullish ALMA crossover at the THIRD bucket's closing
    bar (minute 23), computed entirely from scratch with no carried seed —
    bucket A and B are both bearish (open=101, close=100), bucket C flips
    bullish (open=100, close=101)."""
    bucket_a = [_bar(m, 101, 101, 101, 100) for m in range(0, 8)]
    bucket_b = [_bar(m, 101, 101, 101, 100) for m in range(8, 16)]
    bucket_c = [_bar(m, 100, 101, 100, 101) for m in range(16, 24)]
    return bucket_a + bucket_b + bucket_c


# --- _bucket_candles / bucket_samples: aggregation on native bucket OHLC ---
# (the C1 fix: ALMA must run on each bucket's own (first-bar-open,
# last-bar-close), not on a continuous 1-minute series sampled at the
# bucket boundary — see signal_engine.bucket_samples's docstring)

def test_bucket_candles_uses_first_bar_open_and_last_bar_close():
    bars = [
        _bar(0, 100.0, 105.0, 95.0, 102.0),  # first bar: open=100.0 is the bucket's open
        *[_bar(m, 102.0, 103.0, 101.0, 102.0) for m in range(1, 7)],
        _bar(7, 102.0, 103.0, 101.0, 99.0),  # last bar: close=99.0 is the bucket's close
    ]
    candles = _bucket_candles(bars)
    assert len(candles) == 1
    bucket_start, b_open, b_close, bar_time, b_high, b_low = candles[0]
    assert b_open == 100.0
    assert b_close == 99.0
    assert bar_time.minute == 7


def test_bucket_candles_needs_the_buckets_first_bar_for_a_correct_open():
    # If the bucket's true first (opening) bar is missing from the input,
    # the candle's open comes from whatever bar happens to be first instead
    # — this is exactly why poll_once must aggregate from the FULL fetch,
    # not a last_bar_time-filtered slice (see the poll_once regression test
    # below).
    full_bucket = [_bar(m, 100 + m, 101, 99, 100) for m in range(0, 8)]
    partial_bucket = full_bucket[3:]  # missing minutes 0-2, including the true open
    full_candles = _bucket_candles(full_bucket)
    partial_candles = _bucket_candles(partial_bucket)
    assert full_candles[0][1] == full_bucket[0].open
    assert partial_candles[0][1] == partial_bucket[0].open
    assert full_candles[0][1] != partial_candles[0][1]


def test_bucket_candles_excludes_an_incomplete_bucket():
    bars = [_bar(m, 100, 100, 100, 100) for m in range(0, 5)]  # only 5 of 8 minutes
    assert _bucket_candles(bars) == []


def test_bucket_samples_returns_empty_without_enough_bucket_history():
    # A single complete bucket with no seed — ALMA(length=2) has nothing to
    # weight it against yet.
    bars = [_bar(m, 100, 100, 100, 100) for m in range(0, 8)]
    assert bucket_samples(bars) == []


def test_bucket_samples_uses_seed_to_produce_a_sample_from_a_single_new_bucket():
    bars = [_bar(m, 100, 101, 100, 101) for m in range(8, 16)]
    samples = bucket_samples(bars, seed=(101.0, 100.0))
    assert len(samples) == 1
    assert samples[0].bar_time.minute == 15
    assert samples[0].bucket_open == 100.0
    assert samples[0].bucket_close == 101.0


def test_bucket_samples_produces_two_consecutive_samples_from_three_buckets():
    bars = _three_bucket_bullish_cross()
    samples = bucket_samples(bars)
    assert len(samples) == 2  # bucket B and bucket C (bucket A has no seed/history)
    assert samples[0].bar_time.minute == 15
    assert samples[1].bar_time.minute == 23


# --- detect_entries: crossover detection between consecutive samples ---

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

def test_evaluate_signal_long_hits_sl_only():
    entry_time = datetime(2026, 9, 25, 10, 15, tzinfo=timezone.utc)
    bars = [_bar(16, 100, 100, 99.85, 99.9)]  # low crosses under SL=99.9
    tier, status, prev_high, prev_low, exit_time = evaluate_signal(
        "long", 100.0, entry_time, 0, prev_high=100.0, prev_low=100.0, bars=bars,
    )
    assert tier == 0
    assert status == "SL_ONLY"
    assert exit_time == bars[0].time


def test_evaluate_signal_long_advances_through_all_tiers():
    entry_time = datetime(2026, 9, 25, 10, 15, tzinfo=timezone.utc)
    bars = [
        _bar(16, 100, 100.25, 100, 100.2),
        _bar(17, 100.2, 100.4, 100.2, 100.3),
        _bar(18, 100.3, 100.5, 100.3, 100.45),
    ]
    tier, status, prev_high, prev_low, exit_time = evaluate_signal(
        "long", 100.0, entry_time, 0, prev_high=100.0, prev_low=100.0, bars=bars,
    )
    assert tier == 3
    assert status == "TP3_FULL"
    assert exit_time == bars[-1].time


def test_evaluate_signal_short_hits_tp1_then_sl():
    entry_time = datetime(2026, 9, 25, 10, 15, tzinfo=timezone.utc)
    bars = [
        _bar(16, 100, 100, 99.79, 99.8),
        _bar(17, 99.8, 100.15, 99.8, 100.1),
    ]
    tier, status, prev_high, prev_low, exit_time = evaluate_signal(
        "short", 100.0, entry_time, 0, prev_high=100.0, prev_low=100.0, bars=bars,
    )
    assert tier == 1
    assert status == "TP1_THEN_SL"
    assert exit_time == bars[-1].time


def test_evaluate_signal_prioritizes_tp_over_sl_in_same_bar():
    entry_time = datetime(2026, 9, 25, 10, 15, tzinfo=timezone.utc)
    bars = [_bar(16, 100, 100.3, 99.8, 100.0)]  # crosses both TP1 (100.2) and SL (99.9)
    tier, status, prev_high, prev_low, exit_time = evaluate_signal(
        "long", 100.0, entry_time, 0, prev_high=100.0, prev_low=100.0, bars=bars,
    )
    assert tier == 1
    assert status is None  # advanced to TP1, still open, not closed by SL


def test_evaluate_signal_does_not_trigger_without_a_genuine_crossing():
    # f_cross() requires the PREVIOUS bar to have been on the safe side —
    # merely "still below" on a later bar does not re-trigger.
    entry_time = datetime(2026, 9, 25, 10, 15, tzinfo=timezone.utc)
    bars = [_bar(16, 99.8, 99.9, 99.7, 99.85)]
    tier, status, prev_high, prev_low, exit_time = evaluate_signal(
        "long", 100.0, entry_time, 0, prev_high=100.0, prev_low=99.85, bars=bars,
        # prev_low (99.85) is ALREADY below SL (99.9) — no genuine crossunder
    )
    assert tier == 0
    assert status is None


def test_evaluate_signal_ignores_bars_at_or_before_entry_time():
    entry_time = datetime(2026, 9, 25, 10, 15, tzinfo=timezone.utc)
    bars = [_bar(15, 100, 100, 90, 90)]  # the entry bar itself, must be skipped
    tier, status, prev_high, prev_low, exit_time = evaluate_signal(
        "long", 100.0, entry_time, 0, prev_high=100.0, prev_low=100.0, bars=bars,
    )
    assert tier == 0
    assert status is None
    assert prev_high == 100.0  # unchanged: the bar was skipped entirely
    assert prev_low == 100.0


def test_evaluate_signal_blowing_through_two_tiers_in_one_bar_advances_only_one():
    # Claim 3 (verified against real Pine `switch` semantics): even if a
    # single bar's range technically clears TWO tiers' thresholds, Pine's
    # switch only executes its first matching branch per bar — only tier 1
    # advances; tier 2 needs its own fresh crossing on a LATER bar.
    entry_time = datetime(2026, 9, 25, 10, 15, tzinfo=timezone.utc)
    bars = [_bar(16, 100, 100.5, 100, 100.4)]  # high clears both TP1(100.2) and TP2(100.35)
    tier, status, prev_high, prev_low, exit_time = evaluate_signal(
        "long", 100.0, entry_time, 0, prev_high=100.0, prev_low=100.0, bars=bars,
    )
    assert tier == 1  # NOT 2
    assert status is None


def test_evaluate_signal_no_change_when_no_bars_qualify():
    entry_time = datetime(2026, 9, 25, 10, 15, tzinfo=timezone.utc)
    bars = [_bar(16, 100, 100.05, 99.95, 100.0)]  # inside all thresholds
    tier, status, prev_high, prev_low, exit_time = evaluate_signal(
        "long", 100.0, entry_time, 0, prev_high=100.0, prev_low=100.0, bars=bars,
    )
    assert tier == 0
    assert status is None
    assert prev_high == 100.05  # still updated: this bar WAS processed
    assert prev_low == 99.95


# --- poll_once orchestration ---

def test_poll_once_noops_when_api_key_missing():
    conn = db_module.connect(":memory:")
    with patch("signal_engine.fetch_recent_bars") as fake_fetch:
        poll_once(conn, "", "XAU/USD", "tok", "chat")
        fake_fetch.assert_not_called()


def test_poll_once_bootstraps_with_200_bars_on_first_run():
    conn = db_module.connect(":memory:")
    with patch("signal_engine.fetch_recent_bars", return_value=[]) as fake_fetch, \
         patch("signal_engine.send_telegram_message", return_value=(True, None)):
        poll_once(conn, "key", "XAU/USD", "tok", "chat")
        assert fake_fetch.call_args.args[2] == 200  # outputsize


def test_poll_once_caps_outputsize_at_200_after_long_gap(monkeypatch):
    conn = db_module.connect(":memory:")
    db_module.set_last_bar_time(conn, "2026-01-01T00:00:00+00:00")  # far in the past
    with patch("signal_engine.fetch_recent_bars", return_value=[]) as fake_fetch:
        poll_once(conn, "key", "XAU/USD", "tok", "chat")
        assert fake_fetch.call_args.args[2] == 200


def test_poll_once_outputsize_includes_full_bucket_margin():
    # Regression: a small poll gap alone is not enough margin — a bucket
    # that closed mid-gap still needs its true first bar in the fetch, which
    # requires reaching back a full bucket width, not just the gap.
    conn = db_module.connect(":memory:")
    db_module.set_last_bar_time(conn, "2026-09-25T10:20:00+00:00")
    with patch("signal_engine.fetch_recent_bars", return_value=[]) as fake_fetch:
        poll_once(conn, "key", "XAU/USD", "tok", "chat", now=datetime(2026, 9, 25, 10, 25, tzinfo=timezone.utc))
    assert fake_fetch.call_args.args[2] == 5 + 2 * 8 + 1


def test_poll_once_detects_entry_pushes_telegram_and_records_signal():
    conn = db_module.connect(":memory:")
    bars = _three_bucket_bullish_cross()
    with patch("signal_engine.fetch_recent_bars", return_value=bars), \
         patch("signal_engine.send_telegram_message", return_value=(True, None)) as fake_send:
        poll_once(conn, "key", "XAU/USD", "tok", "chat", now=datetime(2026, 9, 25, 10, 25, tzinfo=timezone.utc))
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
    conn = db_module.connect(":memory:")
    db_module.set_last_bar_time(conn, "2026-09-25T09:59:00+00:00")
    # Carried bucket B: bearish (alma_close <= alma_open), raw open=101/close=100.
    db_module.set_last_bucket_carry(conn, "2026-09-25T10:08:00+00:00", 101.0, 100.0, 100.0, 101.0)
    bucket_c = [_bar(m, 100, 101, 100, 101) for m in range(16, 24)]
    with patch("signal_engine.fetch_recent_bars", return_value=bucket_c), \
         patch("signal_engine.send_telegram_message", return_value=(True, None)):
        poll_once(conn, "key", "XAU/USD", "tok", "chat", now=datetime(2026, 9, 25, 10, 25, tzinfo=timezone.utc))
    rows = db_module.recent_signals(conn)
    assert len(rows) == 1
    assert rows[0]["direction"] == "long"


def test_poll_once_aggregates_bucket_from_full_fetch_not_filtered_bars():
    # Regression: bucket_samples must aggregate candles from the full fetch,
    # not the last_bar_time-filtered `bars` — otherwise a bucket whose early
    # minutes were already seen in a prior poll gets the WRONG open (the
    # filtered slice's first bar, not the bucket's TRUE first bar),
    # corrupting the ALMA input and potentially flipping the result.
    conn = db_module.connect(":memory:")
    db_module.set_last_bar_time(conn, "2026-09-25T10:22:00+00:00")
    db_module.set_last_bucket_carry(conn, "2026-09-25T10:08:00+00:00", 101.0, 100.0, 100.0, 101.0)
    bucket_c = (
        [_bar(16, 90, 101, 89, 100)]  # TRUE bucket open = 90
        + [_bar(m, 100, 101, 99, 100) for m in range(17, 23)]
        + [_bar(23, 105, 101, 100, 101)]  # if wrongly used as "the bucket's open", flips the result
    )
    with patch("signal_engine.fetch_recent_bars", return_value=bucket_c), \
         patch("signal_engine.send_telegram_message", return_value=(True, None)):
        poll_once(conn, "key", "XAU/USD", "tok", "chat", now=datetime(2026, 9, 25, 10, 25, tzinfo=timezone.utc))
    rows = db_module.recent_signals(conn)
    assert len(rows) == 1
    assert rows[0]["direction"] == "long"


def test_poll_once_ignores_not_yet_closed_bar():
    # Regression for C2: a bar whose minute has not fully elapsed yet must
    # not be treated as final.
    conn = db_module.connect(":memory:")
    entry_time = datetime(2026, 9, 25, 10, 15, tzinfo=timezone.utc)
    db_module.insert_signal(conn, "long", 100.0, entry_time.isoformat(), 100.1, 100.0)
    still_forming = _bar(17, 100, 100, 99.85, 99.9)  # would trigger SL if treated as final
    frozen_now = datetime(2026, 9, 25, 10, 17, 30, tzinfo=timezone.utc)  # inside minute 17
    with patch("signal_engine.fetch_recent_bars", return_value=[still_forming]), \
         patch("signal_engine.send_telegram_message", return_value=(True, None)):
        poll_once(conn, "key", "XAU/USD", "tok", "chat", now=frozen_now)
    rows = db_module.recent_signals(conn)
    assert rows[0]["status"] == "OPEN"


def test_poll_once_processes_bar_once_fully_closed():
    conn = db_module.connect(":memory:")
    entry_time = datetime(2026, 9, 25, 10, 15, tzinfo=timezone.utc)
    db_module.insert_signal(conn, "long", 100.0, entry_time.isoformat(), 100.1, 100.0)
    sl_bar = _bar(17, 100, 100, 99.85, 99.9)
    frozen_now = datetime(2026, 9, 25, 10, 18, 30, tzinfo=timezone.utc)  # minute 17 fully elapsed
    with patch("signal_engine.fetch_recent_bars", return_value=[sl_bar]), \
         patch("signal_engine.send_telegram_message", return_value=(True, None)):
        poll_once(conn, "key", "XAU/USD", "tok", "chat", now=frozen_now)
    rows = db_module.recent_signals(conn)
    assert rows[0]["status"] == "SL_ONLY"
    assert rows[0]["exited_at"] == sl_bar.time.isoformat()  # exit uses the exit BAR's time (M1 fix)


def test_poll_once_survives_telegram_failure_on_entry_push():
    conn = db_module.connect(":memory:")
    bars = _three_bucket_bullish_cross()
    with patch("signal_engine.fetch_recent_bars", return_value=bars), \
         patch("signal_engine.send_telegram_message", return_value=(False, "boom")):
        poll_once(conn, "key", "XAU/USD", "tok", "chat", now=datetime(2026, 9, 25, 10, 25, tzinfo=timezone.utc))  # must not raise
    assert len(db_module.recent_signals(conn)) == 1  # still recorded despite push failure


def test_poll_once_swallows_market_data_errors():
    conn = db_module.connect(":memory:")
    with patch("signal_engine.fetch_recent_bars", side_effect=MarketDataError("boom")):
        poll_once(conn, "key", "XAU/USD", "tok", "chat")  # must not raise
    assert db_module.get_last_bar_time(conn) is None  # nothing advanced


def test_poll_once_closes_open_signal_and_pushes_result():
    conn = db_module.connect(":memory:")
    entry_time = datetime(2026, 9, 25, 10, 15, tzinfo=timezone.utc)
    db_module.insert_signal(conn, "long", 100.0, entry_time.isoformat(), 100.1, 100.0)
    sl_bar = _bar(16, 100, 100, 99.85, 99.9)  # crosses under SL (prev_low=100.0 > 99.9)
    with patch("signal_engine.fetch_recent_bars", return_value=[sl_bar]), \
         patch("signal_engine.send_telegram_message", return_value=(True, None)) as fake_send:
        poll_once(conn, "key", "XAU/USD", "tok", "chat", now=datetime(2026, 9, 25, 10, 18, tzinfo=timezone.utc))
    rows = db_module.recent_signals(conn)
    assert rows[0]["status"] == "SL_ONLY"
    assert fake_send.called


def test_poll_once_survives_telegram_failure_on_close_push():
    conn = db_module.connect(":memory:")
    entry_time = datetime(2026, 9, 25, 10, 15, tzinfo=timezone.utc)
    db_module.insert_signal(conn, "long", 100.0, entry_time.isoformat(), 100.1, 100.0)
    sl_bar = _bar(16, 100, 100, 99.85, 99.9)
    with patch("signal_engine.fetch_recent_bars", return_value=[sl_bar]), \
         patch("signal_engine.send_telegram_message", return_value=(False, "boom")):
        poll_once(conn, "key", "XAU/USD", "tok", "chat", now=datetime(2026, 9, 25, 10, 18, tzinfo=timezone.utc))  # must not raise
    rows = db_module.recent_signals(conn)
    assert rows[0]["status"] == "SL_ONLY"  # still updated despite push failure


def test_poll_once_advances_tier_without_closing_or_pushing():
    conn = db_module.connect(":memory:")
    entry_time = datetime(2026, 9, 25, 10, 15, tzinfo=timezone.utc)
    db_module.insert_signal(conn, "long", 100.0, entry_time.isoformat(), 100.0, 99.9)
    tp1_bar = _bar(16, 100, 100.25, 100, 100.2)  # crosses TP1 only (prev_high=100.0 < 100.2)
    with patch("signal_engine.fetch_recent_bars", return_value=[tp1_bar]), \
         patch("signal_engine.send_telegram_message", return_value=(True, None)) as fake_send:
        poll_once(conn, "key", "XAU/USD", "tok", "chat", now=datetime(2026, 9, 25, 10, 18, tzinfo=timezone.utc))
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
    conn = db_module.connect(":memory:")
    signal_id = db_module.insert_signal(conn, "long", 95.0, "2026-09-25T09:00:00+00:00", 95.1, 94.9)
    # Already at TP1, and its remaining TP2 threshold (~95.33) was already
    # cleared a while ago (last_bar_high/low near bucket_c's own range) —
    # bucket_c's bars produce no FRESH crossing (no prev-side reversal), so
    # the normal TP/SL pass correctly leaves it OPEN at tier 1 for the
    # reversal handling below to act on.
    db_module.update_signal(conn, signal_id, 1, "OPEN", None, 100.5, 100.4)
    # Seed a bullish previous bucket so the new (bearish) bucket triggers SHORT.
    db_module.set_last_bucket_carry(conn, "2026-09-25T10:08:00+00:00", 100.0, 101.0, 101.0, 100.0)
    bucket_c = [_bar(m, 101, 101, 100, 100) for m in range(16, 24)]
    frozen_now = datetime(2026, 9, 25, 10, 25, tzinfo=timezone.utc)
    with patch("signal_engine.fetch_recent_bars", return_value=bucket_c), \
         patch("signal_engine.send_telegram_message", return_value=(True, None)) as fake_send:
        poll_once(conn, "key", "XAU/USD", "tok", "chat", now=frozen_now)
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
    conn = db_module.connect(":memory:")
    signal_id = db_module.insert_signal(conn, "long", 95.0, "2026-09-24T09:00:00+00:00", 95.1, 94.9)
    # Its TP1 threshold (~95.19) was already cleared a while ago (last_bar_
    # high/low near bucket_c's own range), so bucket_c's bars produce no
    # fresh crossing — stays OPEN at tier 0 for the reversal to act on.
    db_module.update_signal(conn, signal_id, 0, "OPEN", None, 100.5, 100.4)
    db_module.set_last_bucket_carry(conn, "2026-09-25T10:08:00+00:00", 100.0, 101.0, 101.0, 100.0)
    bucket_c = [_bar(m, 101, 101, 100, 100) for m in range(16, 24)]
    frozen_now = datetime(2026, 9, 26, 10, 25, tzinfo=timezone.utc)  # a full day later -> stale
    with patch("signal_engine.fetch_recent_bars", return_value=bucket_c), \
         patch("signal_engine.send_telegram_message", return_value=(True, None)) as fake_send:
        poll_once(conn, "key", "XAU/USD", "tok", "chat", now=frozen_now)
    rows = db_module.recent_signals(conn)
    long_row = next(r for r in rows if r["direction"] == "long")
    assert long_row["status"] == "REVERSED_ONLY"  # still recorded
    assert not fake_send.called  # neither the reversal close nor the new entry was pushed


def test_poll_once_suppresses_push_but_still_records_stale_entries_on_bootstrap():
    # I1: a bootstrap/long-outage backlog must not be PUSHED as if it just
    # happened, but must still be RECORDED for win-rate history.
    conn = db_module.connect(":memory:")
    bars = _three_bucket_bullish_cross()
    frozen_now = datetime(2026, 9, 26, 10, 0, 0, tzinfo=timezone.utc)  # a full day later
    with patch("signal_engine.fetch_recent_bars", return_value=bars), \
         patch("signal_engine.send_telegram_message", return_value=(True, None)) as fake_send:
        poll_once(conn, "key", "XAU/USD", "tok", "chat", now=frozen_now)
    rows = db_module.recent_signals(conn)
    assert len(rows) == 1
    assert rows[0]["direction"] == "long"
    assert not fake_send.called
