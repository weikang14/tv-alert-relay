# signal_engine.py
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from alma import alma
from market_data import Bar

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
