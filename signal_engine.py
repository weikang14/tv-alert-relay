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
    alma_close: float
    alma_open: float
    bar_time: datetime
    bar_close: float


@dataclass
class EntrySignal:
    direction: str
    entry_time: datetime
    entry_price: float


def _bucket_start(dt: datetime) -> datetime:
    epoch_minutes = int(dt.timestamp() // 60)
    bucket_index = epoch_minutes // BUCKET_MINUTES
    return datetime.fromtimestamp(bucket_index * BUCKET_MINUTES * 60, tz=timezone.utc)


def bucket_samples(bars: list[Bar]) -> list[BucketSample]:
    """One sample per COMPLETE 8-minute bucket: the continuous 1-minute ALMA
    value taken at the bucket's last (closing) 1-minute bar. Mirrors the
    script's `reso(closeSeries, ...)` — ALMA computed on the continuous
    1-minute series first, then sampled at the bucket boundary."""
    closes = [b.close for b in bars]
    opens = [b.open for b in bars]
    alma_close = alma(closes, ALMA_LENGTH, ALMA_OFFSET, ALMA_SIGMA)
    alma_open = alma(opens, ALMA_LENGTH, ALMA_OFFSET, ALMA_SIGMA)

    samples = []
    for i, bar in enumerate(bars):
        bucket_start = _bucket_start(bar.time)
        bucket_last_minute = bucket_start + timedelta(minutes=BUCKET_MINUTES - 1)
        if bar.time == bucket_last_minute and alma_close[i] is not None and alma_open[i] is not None:
            samples.append(BucketSample(
                bucket_start=bucket_start,
                alma_close=alma_close[i],
                alma_open=alma_open[i],
                bar_time=bar.time,
                bar_close=bar.close,
            ))
    return samples


def detect_entries(samples: list) -> list[EntrySignal]:
    """samples: consecutive BucketSample-like objects, ascending by bucket_start."""
    entries = []
    for prev, curr in zip(samples, samples[1:]):
        crossed_up = curr.alma_close > curr.alma_open and prev.alma_close <= prev.alma_open
        crossed_down = curr.alma_close < curr.alma_open and prev.alma_close >= prev.alma_open
        if crossed_up:
            entries.append(EntrySignal("long", curr.bar_time, curr.bar_close))
        elif crossed_down:
            entries.append(EntrySignal("short", curr.bar_time, curr.bar_close))
    return entries


_STATUS_AFTER_SL = {0: "SL_ONLY", 1: "TP1_THEN_SL", 2: "TP2_THEN_SL"}


def evaluate_signal(
    direction: str,
    entry_price: float,
    entry_time: datetime,
    highest_tier: int,
    bars: list[Bar],
) -> tuple[int, str | None]:
    tier = highest_tier
    for bar in bars:
        if bar.time <= entry_time:
            continue
        if tier >= len(TP_LEVELS_PCT):
            break

        tp_pct = TP_LEVELS_PCT[tier]
        if direction == "long":
            tp_price = entry_price * (1 + tp_pct / 100)
            sl_price = entry_price * (1 - SL_PCT / 100)
            tp_hit = bar.high >= tp_price
            sl_hit = bar.low <= sl_price
        else:
            tp_price = entry_price * (1 - tp_pct / 100)
            sl_price = entry_price * (1 + SL_PCT / 100)
            tp_hit = bar.low <= tp_price
            sl_hit = bar.high >= sl_price

        if tp_hit:
            tier += 1
            if tier == len(TP_LEVELS_PCT):
                return tier, "TP3_FULL"
        elif sl_hit:
            return tier, _STATUS_AFTER_SL[tier]

    return tier, None


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
        # its preceding bar available for ALMA warm-up (C1).
        full_samples = bucket_samples(fetched)
        new_samples = [s for s in full_samples if last_bar_time is None or s.bar_time > last_bar_time]

        carried = db.get_last_bucket_sample(conn)
        all_samples = list(new_samples)
        if carried is not None:
            carried_start_str, carried_close, carried_open = carried
            carried_sample = BucketSample(
                bucket_start=datetime.fromisoformat(carried_start_str),
                alma_close=carried_close,
                alma_open=carried_open,
                bar_time=datetime.fromisoformat(carried_start_str),
                bar_close=0.0,
            )
            all_samples = [carried_sample] + new_samples

        entries = detect_entries(all_samples)
        # Suppress stale replays on cold start / after a long outage (I1).
        fresh_entries = [e for e in entries if (now - e.entry_time) <= timedelta(minutes=2 * BUCKET_MINUTES)]
        for entry in fresh_entries:
            signal_id = db.insert_signal(conn, entry.direction, entry.entry_price, entry.entry_time.isoformat())
            text = f"\U0001F4CA Signal Entry #{signal_id}\n{entry.direction.upper()} @ {entry.entry_price}\n{entry.entry_time.isoformat()}"
            ok, error = send_telegram_message(tg_bot_token, tg_chat_id, text)
            if not ok:
                log.error("signal entry telegram send failed: %s", error)

        for sig in db.open_signals(conn):
            new_tier, closing_status = evaluate_signal(
                sig["direction"], sig["entry_price"], datetime.fromisoformat(sig["entry_time"]),
                sig["highest_tier"], bars,
            )
            if closing_status is not None:
                db.update_signal(conn, sig["id"], new_tier, closing_status, now.isoformat())
                text = f"\U0001F3C1 Signal #{sig['id']} closed: {closing_status}"
                ok, error = send_telegram_message(tg_bot_token, tg_chat_id, text)
                if not ok:
                    log.error("signal result telegram send failed: %s", error)
            elif new_tier != sig["highest_tier"]:
                db.update_signal(conn, sig["id"], new_tier, "OPEN", None)

        if new_samples:
            last_sample = new_samples[-1]
            db.set_last_bucket_sample(
                conn, last_sample.bucket_start.isoformat(), last_sample.alma_close, last_sample.alma_open,
            )
        db.set_last_bar_time(conn, bars[-1].time.isoformat())
        log.info("signal poll ok, %d new bars, %d entries", len(bars), len(fresh_entries))
    except MarketDataError as e:
        log.error("signal poll: market data error: %s", e)
    except Exception:
        log.exception("signal poll failed")
