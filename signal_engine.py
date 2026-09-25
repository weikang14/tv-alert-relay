# signal_engine.py
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import db
from alma import alma
from market_data import Bar, MarketDataError, fetch_recent_bars
from telegram import send_telegram_message

log = logging.getLogger(__name__)

# Chart timeframe (1 minute) x Multiplier for Alternate Signals (8), per the
# design spec section 1 — the script's own "TIMEFRAME" input is unused dead
# code, so this is a fixed constant, not read from any input.
BUCKET_MINUTES = 8

ALMA_LENGTH = 2
ALMA_OFFSET = 0.85
ALMA_SIGMA = 5

# Live chart config (not the script's coded defaults) — see design spec section 1.
TP_LEVELS_PCT = [0.2, 0.35, 0.45]
SL_PCT = 0.1


@dataclass
class BucketSample:
    bucket_start: datetime
    bucket_open: float
    bucket_close: float
    bucket_high: float
    bucket_low: float
    alma_close: float
    alma_open: float
    bar_time: datetime


@dataclass
class EntrySignal:
    direction: str
    entry_time: datetime
    entry_price: float
    entry_high: float
    entry_low: float


def _bucket_start(dt: datetime) -> datetime:
    epoch_minutes = int(dt.timestamp() // 60)
    bucket_index = epoch_minutes // BUCKET_MINUTES
    return datetime.fromtimestamp(bucket_index * BUCKET_MINUTES * 60, tz=timezone.utc)


def _bucket_candles(bars: list[Bar]) -> list[tuple[datetime, float, float, datetime, float, float]]:
    """Group 1-minute bars into complete 8-minute candles: (bucket_start,
    open, close, bar_time, high, low). `open` is the bucket's FIRST bar's
    open, `close`/`high`/`low` come from its LAST (closing) 1-minute bar —
    that closing bar is Pine's "current bar" at the moment the cross fires,
    so its own high/low (not the bucket's aggregate range) is what the
    script's entry/TP/SL logic actually references. A bucket is only
    included once its closing (8th) 1-minute bar is present in `bars`."""
    groups: dict[datetime, list[Bar]] = {}
    for bar in bars:
        groups.setdefault(_bucket_start(bar.time), []).append(bar)

    candles = []
    for bucket_start in sorted(groups):
        group = sorted(groups[bucket_start], key=lambda b: b.time)
        bucket_last_minute = bucket_start + timedelta(minutes=BUCKET_MINUTES - 1)
        if group[-1].time != bucket_last_minute:
            continue
        closing_bar = group[-1]
        candles.append((bucket_start, group[0].open, closing_bar.close, closing_bar.time, closing_bar.high, closing_bar.low))
    return candles


def bucket_samples(bars: list[Bar], seed: tuple[float, float] | None = None) -> list[BucketSample]:
    """Mirrors the script's `reso(closeSeries, ...)`, i.e.
    `request.security(ticker, stratRes, closeSeries)`: passing an already
    -calculated expression (not a raw `close`/`open`) into `request.security`
    makes Pine RE-EXECUTE that expression's formula using the requested
    timeframe's OWN bars — it does not resample an already-computed series.
    So ALMA(length=2, offset=0.85, sigma=5) must be computed on each 8-minute
    bucket's own (open, close) — the first bar's open and the last bar's
    close — not on the continuous 1-minute series.

    `seed`: (prev_bucket_open, prev_bucket_close) carried from the last
    bucket processed in an earlier poll, so ALMA(length=2) has a prior value
    to weight against for the first bucket in `bars`. None on the very first
    bucket ever processed.
    """
    candles = _bucket_candles(bars)
    closes = [c[2] for c in candles]
    opens = [c[1] for c in candles]
    offset = 0
    if seed is not None:
        seed_open, seed_close = seed
        closes = [seed_close] + closes
        opens = [seed_open] + opens
        offset = 1

    alma_close_seq = alma(closes, ALMA_LENGTH, ALMA_OFFSET, ALMA_SIGMA)
    alma_open_seq = alma(opens, ALMA_LENGTH, ALMA_OFFSET, ALMA_SIGMA)

    samples = []
    for i, (bucket_start, b_open, b_close, bar_time, b_high, b_low) in enumerate(candles):
        idx = i + offset
        if alma_close_seq[idx] is None or alma_open_seq[idx] is None:
            continue
        samples.append(BucketSample(
            bucket_start=bucket_start, bucket_open=b_open, bucket_close=b_close,
            bucket_high=b_high, bucket_low=b_low,
            alma_close=alma_close_seq[idx], alma_open=alma_open_seq[idx], bar_time=bar_time,
        ))
    return samples


def detect_entries(samples: list) -> list[EntrySignal]:
    """samples: consecutive BucketSample-like objects, ascending by bucket_start."""
    entries = []
    for prev, curr in zip(samples, samples[1:]):
        crossed_up = curr.alma_close > curr.alma_open and prev.alma_close <= prev.alma_open
        crossed_down = curr.alma_close < curr.alma_open and prev.alma_close >= prev.alma_open
        if crossed_up:
            entries.append(EntrySignal("long", curr.bar_time, curr.bucket_close, curr.bucket_high, curr.bucket_low))
        elif crossed_down:
            entries.append(EntrySignal("short", curr.bar_time, curr.bucket_close, curr.bucket_high, curr.bucket_low))
    return entries


_STATUS_AFTER_SL = {0: "SL_ONLY", 1: "TP1_THEN_SL", 2: "TP2_THEN_SL"}


def evaluate_signal(
    direction: str,
    entry_price: float,
    entry_time: datetime,
    highest_tier: int,
    prev_high: float,
    prev_low: float,
    bars: list[Bar],
) -> tuple[int, str | None, float, float, datetime | None]:
    """Returns (new_highest_tier, closing_status, new_prev_high, new_prev_low,
    exit_bar_time). Mirrors the script's `f_cross()` helper used for every
    TP/SL check: a level only fires on the bar where price genuinely CROSSES
    it — the previous bar must have been strictly on the other side, not
    merely "is currently past it" — because `ta.crossover`/`ta.crossunder`
    -style checks require that. `prev_high`/`prev_low` seed this: the entry
    bar's own high/low on the very first call for a signal (Pine's `high[1]`
    on the first post-entry bar refers to the entry bar), and the
    last-processed bar's high/low on every call after."""
    tier = highest_tier
    exit_bar_time = None
    for bar in bars:
        if bar.time <= entry_time:
            continue
        if tier >= len(TP_LEVELS_PCT):
            break

        tp_pct = TP_LEVELS_PCT[tier]
        if direction == "long":
            tp_price = entry_price * (1 + tp_pct / 100)
            sl_price = entry_price * (1 - SL_PCT / 100)
            tp_hit = bar.high > tp_price and prev_high < tp_price
            sl_hit = bar.low < sl_price and prev_low > sl_price
        else:
            tp_price = entry_price * (1 - tp_pct / 100)
            sl_price = entry_price * (1 + SL_PCT / 100)
            tp_hit = bar.low < tp_price and prev_low > tp_price
            sl_hit = bar.high > sl_price and prev_high < sl_price

        prev_high, prev_low = bar.high, bar.low

        if tp_hit:
            tier += 1
            if tier == len(TP_LEVELS_PCT):
                return tier, "TP3_FULL", prev_high, prev_low, bar.time
        elif sl_hit:
            return tier, _STATUS_AFTER_SL[tier], prev_high, prev_low, bar.time

    return tier, None, prev_high, prev_low, exit_bar_time


def poll_once(conn, api_key: str, symbol: str, tg_bot_token: str, tg_chat_id: str, now: datetime | None = None) -> None:
    if not api_key:
        return

    if now is None:
        now = datetime.now(timezone.utc)

    try:
        last_bar_time_str = db.get_last_bar_time(conn)
        if last_bar_time_str is None:
            outputsize = 200
            last_bar_time = None
        else:
            last_bar_time = datetime.fromisoformat(last_bar_time_str)
            gap_minutes = int((now - last_bar_time).total_seconds() // 60)
            outputsize = min(200, max(1, gap_minutes + 5))

        fetched = fetch_recent_bars(api_key, symbol, outputsize)

        # Ignore the still-forming latest bar — its OHLC isn't final yet (C2).
        fetched = [b for b in fetched if b.time + timedelta(minutes=1) <= now]

        bars = [b for b in fetched if last_bar_time is None or b.time > last_bar_time]

        if not bars:
            log.info("signal poll: no new bars")
            return

        # Sample the FULL fetch (not just the post-last_bar_time slice) so a
        # bucket-closing bar that happens to be the first new bar still has
        # its preceding bar available for ALMA warm-up (C1). `seed` carries
        # the previous poll's last bucket's raw (open, close) so ALMA(2) has
        # history for the first NEW bucket too, even when that bucket's own
        # preceding bucket fell outside this poll's fetch window entirely.
        carried = db.get_last_bucket_carry(conn)
        seed = (carried[1], carried[2]) if carried is not None else None
        full_samples = bucket_samples(fetched, seed=seed)
        new_samples = [s for s in full_samples if last_bar_time is None or s.bar_time > last_bar_time]

        all_samples = list(new_samples)
        if carried is not None:
            carried_start_str, carried_open, carried_close, carried_alma_close, carried_alma_open = carried
            carried_sample = BucketSample(
                bucket_start=datetime.fromisoformat(carried_start_str),
                bucket_open=carried_open,
                bucket_close=carried_close,
                bucket_high=0.0,  # unused: detect_entries never builds an EntrySignal from `prev`
                bucket_low=0.0,
                alma_close=carried_alma_close,
                alma_open=carried_alma_open,
                bar_time=datetime.fromisoformat(carried_start_str),
            )
            all_samples = [carried_sample] + new_samples

        entries = detect_entries(all_samples)
        pushed = 0
        for entry in entries:
            signal_id = db.insert_signal(
                conn, entry.direction, entry.entry_price, entry.entry_time.isoformat(),
                entry.entry_high, entry.entry_low,
            )
            # Suppress the PUSH for a stale replay (cold start / long outage) —
            # still recorded for win-rate history either way (I1).
            if (now - entry.entry_time) > timedelta(minutes=2 * BUCKET_MINUTES):
                log.info("signal poll: entry #%d is stale, recorded but not pushed", signal_id)
                continue
            pushed += 1
            text = f"\U0001F4CA Signal Entry #{signal_id}\n{entry.direction.upper()} @ {entry.entry_price}\n{entry.entry_time.isoformat()}"
            ok, error = send_telegram_message(tg_bot_token, tg_chat_id, text)
            if not ok:
                log.error("signal entry telegram send failed: %s", error)

        for sig in db.open_signals(conn):
            new_tier, closing_status, new_prev_high, new_prev_low, exit_bar_time = evaluate_signal(
                sig["direction"], sig["entry_price"], datetime.fromisoformat(sig["entry_time"]),
                sig["highest_tier"], sig["last_bar_high"], sig["last_bar_low"], bars,
            )
            if closing_status is not None:
                exited_at = exit_bar_time.isoformat() if exit_bar_time is not None else now.isoformat()
                db.update_signal(conn, sig["id"], new_tier, closing_status, exited_at, new_prev_high, new_prev_low)
                text = f"\U0001F3C1 Signal #{sig['id']} closed: {closing_status}"
                ok, error = send_telegram_message(tg_bot_token, tg_chat_id, text)
                if not ok:
                    log.error("signal result telegram send failed: %s", error)
            elif new_tier != sig["highest_tier"] or new_prev_high != sig["last_bar_high"] or new_prev_low != sig["last_bar_low"]:
                db.update_signal(conn, sig["id"], new_tier, "OPEN", None, new_prev_high, new_prev_low)

        if new_samples:
            last_sample = new_samples[-1]
            db.set_last_bucket_carry(
                conn, last_sample.bucket_start.isoformat(), last_sample.bucket_open, last_sample.bucket_close,
                last_sample.alma_close, last_sample.alma_open,
            )
        db.set_last_bar_time(conn, bars[-1].time.isoformat())
        log.info("signal poll ok, %d new bars, %d entries (%d pushed)", len(bars), len(entries), pushed)
    except MarketDataError as e:
        log.error("signal poll: market data error: %s", e)
    except Exception:
        log.exception("signal poll failed")
