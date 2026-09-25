# XXX Strategy Signal Engine Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Independently replicate the XXX Pine strategy's entry-signal and TP/SL-outcome logic in Python, sourcing XAUUSD price data from Twelve Data, and push results to the same Telegram chat the existing tv-alert-relay already uses — without modifying the Pine script or requiring a paid TradingView plan.

**Architecture:** A new `signal_engine` module runs alongside the existing Gmail-based poller, triggered from the same `/poll` endpoint. Each poll fetches recent 1-minute XAUUSD bars, locally resamples them into 8-minute buckets to detect ALMA-crossover entries (mirroring the script's alternate-resolution logic), and advances a per-signal TP1→TP2→TP3/SL state machine using the same bars' high/low. Entries and closures push Telegram messages; a new `/signals/export` endpoint exposes history and win rate.

**Tech Stack:** Python, FastAPI (existing app), `requests` (existing dependency), Twelve Data REST API (`time_series` endpoint), SQLite (existing `db.py` connection), pytest + `monkeypatch`/mocked HTTP for tests.

**Spec:** `docs/superpowers/specs/2026-09-25-xxx-strategy-signal-engine-design.md`

## Global Constraints

- Chart timeframe is fixed at **1 minute**; alternate resolution (`stratRes`) = 1 × `intRes`(8) = **8-minute buckets**. This is a constant (`BUCKET_MINUTES = 8`), not user-configurable.
- ALMA parameters: `length=2, offset=0.85, sigma=5`, computed as one continuous series over 1-minute close/open, **then sampled** at each bucket's closing (last) 1-minute bar — never compute ALMA on resampled 8-minute OHLC directly.
- Risk levels (from the user's actual live chart config, not the script's coded defaults): **TP1=0.2%, TP2=0.35%, TP3=0.45%, SL=0.1%**. SL is fixed at entry and never moves as TP tiers advance.
- When a single 1-minute bar's high/low satisfies both a TP level and the SL level, **TP takes priority** (mirrors the Pine script's `switch` branch order).
- Market data: Twelve Data free tier only, symbol default `XAU/USD`, `interval=1min` only. Never request a second interval — the 8-minute buckets are derived locally from the same 1-minute bars used for TP/SL tracking.
- Per-poll bar fetch is capped at 200 bars (`outputsize`), even after a long outage — never request unbounded history.
- Win rate = signals with status in `{TP1_THEN_SL, TP2_THEN_SL, TP3_FULL}` ÷ signals with status not `OPEN` (i.e., "reached at least TP1" counts as a win).
- Never modify the Pine script. Never touch `goldbot` or its database. This module pushes notifications only — it never places real trades.
- Reuse existing infrastructure: `telegram.send_telegram_message` for all pushes, the existing `check_auth` Basic-Auth dependency for the new endpoint, and the existing try/except-and-log pattern from `poller.py` (a failure here must never raise out of `poll_once` or affect the Gmail-relay health).

## Review Focus

1. **Twelve Data returns bars out of order or with duplicate timestamps** — a network retry or API quirk could return unsorted/duplicated rows; must sort and the poll must not double-process a bar. (Task 2)
2. **First-ever poll (no prior state at all)** — `last_bar_time` and the carried-over bucket sample are both absent; must bootstrap with a 200-bar fetch and not crash on missing carry-over. (Task 7)
3. **Outage longer than 200 minutes** — the gap since `last_bar_time` exceeds the 200-bar cap; must silently accept the coverage gap rather than requesting unbounded history. (Task 7)
4. **`TWELVE_DATA_API_KEY` not configured** — the feature must no-op quietly (log once, return) and must never cause `/poll` to fail or affect the unrelated Gmail-relay health status. (Task 8)
5. **A bar's high/low crosses both a TP level and the SL level in the same bar** — must resolve to TP, not SL (the single most spec-critical piece of business logic). (Task 6)

---

## Task 1: ALMA calculation

**Files:**
- Create: `alma.py`
- Test: `tests/test_alma.py`

**Interfaces:**
- Produces: `alma(values: list[float], length: int, offset: float, sigma: float) -> list[float | None]` — same length as `values`; entries before enough history exists are `None`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_alma.py
import pytest

from alma import alma


def test_alma_length_one_returns_input_unchanged():
    assert alma([5.0, 10.0, 15.0], length=1, offset=0.85, sigma=5) == [5.0, 10.0, 15.0]


def test_alma_pads_none_until_enough_history():
    result = alma([1.0], length=2, offset=0.85, sigma=5)
    assert result == [None]


def test_alma_matches_hand_computed_value_for_length_two():
    # weights: w0=exp(-((0-0.85)**2)/(2*0.4**2)), w1=exp(-((1-0.85)**2)/(2*0.4**2))
    # m = 0.85*(2-1) = 0.85, s = 2/5 = 0.4
    result = alma([10.0, 20.0], length=2, offset=0.85, sigma=5)
    assert result[0] is None
    assert result[1] == pytest.approx(18.993, abs=0.01)


def test_alma_continues_correctly_across_a_longer_series():
    result = alma([10.0, 20.0, 10.0], length=2, offset=0.85, sigma=5)
    assert result[0] is None
    assert result[1] == pytest.approx(18.993, abs=0.01)
    # window is now [20.0, 10.0]: weighted mostly toward the newest value (10.0)
    assert result[2] == pytest.approx(10.933, abs=0.01)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_alma.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'alma'`

- [ ] **Step 3: Write the implementation**

```python
# alma.py
import math


def alma(values: list[float], length: int, offset: float, sigma: float) -> list[float | None]:
    if length < 1:
        raise ValueError("length must be >= 1")

    m = offset * (length - 1)
    s = length / sigma
    weights = [math.exp(-((j - m) ** 2) / (2 * s * s)) for j in range(length)]
    weight_sum = sum(weights)

    result: list[float | None] = []
    for i in range(len(values)):
        if i < length - 1:
            result.append(None)
            continue
        window = values[i - length + 1 : i + 1]
        result.append(sum(w * v for w, v in zip(weights, window)) / weight_sum)
    return result
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_alma.py -v`
Expected: PASS (4 tests)

- [ ] **Step 5: Commit**

```bash
git add alma.py tests/test_alma.py
git commit -m "feat: add ALMA calculation for signal engine"
```

---

## Task 2: Twelve Data market data client

**Files:**
- Create: `market_data.py`
- Test: `tests/test_market_data.py`

**Interfaces:**
- Produces: `Bar` dataclass (`time: datetime`, `open: float`, `high: float`, `low: float`, `close: float`); `MarketDataError(Exception)`; `fetch_recent_bars(api_key: str, symbol: str, outputsize: int) -> list[Bar]` (ascending by `time`, deduped).

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_market_data.py
import pytest
import requests

from market_data import Bar, MarketDataError, fetch_recent_bars


class FakeResponse:
    def __init__(self, json_data, status_code=200):
        self._json = json_data
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"{self.status_code} error")

    def json(self):
        return self._json


def test_fetch_recent_bars_parses_and_sorts_ascending(monkeypatch):
    # Twelve Data returns most-recent-first; client must sort ascending.
    payload = {
        "status": "ok",
        "values": [
            {"datetime": "2026-09-25 10:02:00", "open": "3650.5", "high": "3651.0", "low": "3650.0", "close": "3650.8"},
            {"datetime": "2026-09-25 10:01:00", "open": "3650.0", "high": "3650.6", "low": "3649.8", "close": "3650.5"},
        ],
    }
    monkeypatch.setattr(requests, "get", lambda *a, **k: FakeResponse(payload))
    bars = fetch_recent_bars("key", "XAU/USD", 200)
    assert len(bars) == 2
    assert bars[0].time < bars[1].time
    assert bars[0].close == 3650.5
    assert isinstance(bars[0], Bar)


def test_fetch_recent_bars_dedupes_repeated_timestamps(monkeypatch):
    payload = {
        "status": "ok",
        "values": [
            {"datetime": "2026-09-25 10:01:00", "open": "1", "high": "1", "low": "1", "close": "1"},
            {"datetime": "2026-09-25 10:01:00", "open": "1", "high": "1", "low": "1", "close": "1"},
        ],
    }
    monkeypatch.setattr(requests, "get", lambda *a, **k: FakeResponse(payload))
    bars = fetch_recent_bars("key", "XAU/USD", 200)
    assert len(bars) == 1


def test_fetch_recent_bars_raises_on_api_error_status(monkeypatch):
    payload = {"status": "error", "message": "invalid api key"}
    monkeypatch.setattr(requests, "get", lambda *a, **k: FakeResponse(payload))
    with pytest.raises(MarketDataError, match="invalid api key"):
        fetch_recent_bars("bad-key", "XAU/USD", 200)


def test_fetch_recent_bars_raises_on_http_error(monkeypatch):
    monkeypatch.setattr(requests, "get", lambda *a, **k: FakeResponse({}, status_code=429))
    with pytest.raises(MarketDataError):
        fetch_recent_bars("key", "XAU/USD", 200)


def test_fetch_recent_bars_returns_empty_list_when_no_values(monkeypatch):
    monkeypatch.setattr(requests, "get", lambda *a, **k: FakeResponse({"status": "ok", "values": []}))
    assert fetch_recent_bars("key", "XAU/USD", 200) == []


def test_fetch_recent_bars_raises_on_malformed_row(monkeypatch):
    payload = {"status": "ok", "values": [{"datetime": "2026-09-25 10:01:00", "open": "1"}]}
    monkeypatch.setattr(requests, "get", lambda *a, **k: FakeResponse(payload))
    with pytest.raises(MarketDataError):
        fetch_recent_bars("key", "XAU/USD", 200)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_market_data.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'market_data'`

- [ ] **Step 3: Write the implementation**

```python
# market_data.py
from dataclasses import dataclass
from datetime import datetime, timezone

import requests

TWELVE_DATA_URL = "https://api.twelvedata.com/time_series"


class MarketDataError(Exception):
    pass


@dataclass
class Bar:
    time: datetime
    open: float
    high: float
    low: float
    close: float


def fetch_recent_bars(api_key: str, symbol: str, outputsize: int) -> list[Bar]:
    try:
        r = requests.get(
            TWELVE_DATA_URL,
            params={
                "symbol": symbol,
                "interval": "1min",
                "outputsize": outputsize,
                "apikey": api_key,
                "timezone": "UTC",
            },
            timeout=15,
        )
        r.raise_for_status()
        data = r.json()
    except requests.RequestException as e:
        raise MarketDataError(str(e)) from e

    if data.get("status") == "error":
        raise MarketDataError(data.get("message", "unknown Twelve Data error"))

    values = data.get("values") or []
    seen: dict[datetime, Bar] = {}
    for v in values:
        try:
            bar_time = datetime.strptime(v["datetime"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
            seen[bar_time] = Bar(
                time=bar_time,
                open=float(v["open"]),
                high=float(v["high"]),
                low=float(v["low"]),
                close=float(v["close"]),
            )
        except (KeyError, ValueError) as e:
            raise MarketDataError(f"malformed bar: {v}") from e

    return sorted(seen.values(), key=lambda b: b.time)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_market_data.py -v`
Expected: PASS (6 tests)

- [ ] **Step 5: Commit**

```bash
git add market_data.py tests/test_market_data.py
git commit -m "feat: add Twelve Data market data client"
```

---

## Task 3: Database schema and access functions for signals

**Files:**
- Modify: `db.py`
- Test: `tests/test_db.py`

**Interfaces:**
- Consumes: existing `sqlite3.Connection` from `db.connect()`.
- Produces: `insert_signal(conn, direction, entry_price, entry_time) -> int`, `open_signals(conn) -> list[sqlite3.Row]`, `update_signal(conn, signal_id, highest_tier, status, exited_at) -> None`, `recent_signals(conn, limit=100000) -> list[sqlite3.Row]`, `get_last_bar_time(conn) -> str | None`, `set_last_bar_time(conn, value) -> None`, `get_last_bucket_sample(conn) -> tuple[str, float, float] | None` (bucket_start, alma_close, alma_open), `set_last_bucket_sample(conn, bucket_start, alma_close, alma_open) -> None`.

- [ ] **Step 1: Write the failing tests**

```python
# append to tests/test_db.py
def test_insert_signal_and_open_signals():
    conn = db.connect(":memory:")
    signal_id = db.insert_signal(conn, "long", 3650.5, "2026-09-25T10:08:00+00:00")
    open_ = db.open_signals(conn)
    assert len(open_) == 1
    assert open_[0]["id"] == signal_id
    assert open_[0]["direction"] == "long"
    assert open_[0]["status"] == "OPEN"
    assert open_[0]["highest_tier"] == 0


def test_update_signal_changes_status_and_excludes_from_open():
    conn = db.connect(":memory:")
    signal_id = db.insert_signal(conn, "short", 3650.5, "2026-09-25T10:08:00+00:00")
    db.update_signal(conn, signal_id, 3, "TP3_FULL", "2026-09-25T10:30:00+00:00")
    assert db.open_signals(conn) == []
    rows = db.recent_signals(conn)
    assert rows[0]["status"] == "TP3_FULL"
    assert rows[0]["highest_tier"] == 3
    assert rows[0]["exited_at"] == "2026-09-25T10:30:00+00:00"


def test_recent_signals_most_recent_first():
    conn = db.connect(":memory:")
    db.insert_signal(conn, "long", 1.0, "2026-09-25T10:00:00+00:00")
    db.insert_signal(conn, "short", 2.0, "2026-09-25T10:08:00+00:00")
    rows = db.recent_signals(conn)
    assert rows[0]["direction"] == "short"


def test_last_bar_time_defaults_to_none_then_roundtrips():
    conn = db.connect(":memory:")
    assert db.get_last_bar_time(conn) is None
    db.set_last_bar_time(conn, "2026-09-25T10:08:00+00:00")
    assert db.get_last_bar_time(conn) == "2026-09-25T10:08:00+00:00"
    db.set_last_bar_time(conn, "2026-09-25T10:09:00+00:00")
    assert db.get_last_bar_time(conn) == "2026-09-25T10:09:00+00:00"


def test_last_bucket_sample_defaults_to_none_then_roundtrips():
    conn = db.connect(":memory:")
    assert db.get_last_bucket_sample(conn) is None
    db.set_last_bucket_sample(conn, "2026-09-25T10:08:00+00:00", 3650.1, 3650.2)
    assert db.get_last_bucket_sample(conn) == ("2026-09-25T10:08:00+00:00", 3650.1, 3650.2)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_db.py -v`
Expected: FAIL with `AttributeError: module 'db' has no attribute 'insert_signal'`

- [ ] **Step 3: Write the implementation**

Replace the `SCHEMA` constant in `db.py`:

```python
SCHEMA = """
CREATE TABLE IF NOT EXISTS alerts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    received_at TEXT NOT NULL,
    subject TEXT NOT NULL,
    body_snippet TEXT NOT NULL,
    sent_ok INTEGER NOT NULL,
    error TEXT
);

CREATE TABLE IF NOT EXISTS status (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    last_poll_at TEXT,
    last_success_at TEXT,
    consecutive_errors INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS signals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    direction TEXT NOT NULL,
    entry_price REAL NOT NULL,
    entry_time TEXT NOT NULL,
    highest_tier INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'OPEN',
    exited_at TEXT
);

CREATE TABLE IF NOT EXISTS signal_status (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    last_bar_time TEXT,
    last_bucket_start TEXT,
    last_bucket_alma_close REAL,
    last_bucket_alma_open REAL
);
"""
```

In `connect()`, add the second default-row insert right after the existing `status` insert (still before `conn.commit()`):

```python
def connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    conn.execute("INSERT OR IGNORE INTO status (id, consecutive_errors) VALUES (1, 0)")
    conn.execute("INSERT OR IGNORE INTO signal_status (id) VALUES (1)")
    conn.commit()
    return conn
```

Append these functions at the end of `db.py`:

```python
def insert_signal(conn: sqlite3.Connection, direction: str, entry_price: float, entry_time: str) -> int:
    cur = conn.execute(
        "INSERT INTO signals (direction, entry_price, entry_time, highest_tier, status) "
        "VALUES (?, ?, ?, 0, 'OPEN')",
        (direction, entry_price, entry_time),
    )
    conn.commit()
    return cur.lastrowid


def open_signals(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM signals WHERE status = 'OPEN' ORDER BY id").fetchall()


def update_signal(conn: sqlite3.Connection, signal_id: int, highest_tier: int, status: str, exited_at: str | None) -> None:
    conn.execute(
        "UPDATE signals SET highest_tier = ?, status = ?, exited_at = ? WHERE id = ?",
        (highest_tier, status, exited_at, signal_id),
    )
    conn.commit()


def recent_signals(conn: sqlite3.Connection, limit: int = 100000) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM signals ORDER BY id DESC LIMIT ?", (limit,)).fetchall()


def get_last_bar_time(conn: sqlite3.Connection) -> str | None:
    row = conn.execute("SELECT last_bar_time FROM signal_status WHERE id = 1").fetchone()
    return row["last_bar_time"] if row else None


def set_last_bar_time(conn: sqlite3.Connection, value: str) -> None:
    conn.execute("UPDATE signal_status SET last_bar_time = ? WHERE id = 1", (value,))
    conn.commit()


def get_last_bucket_sample(conn: sqlite3.Connection) -> tuple[str, float, float] | None:
    row = conn.execute(
        "SELECT last_bucket_start, last_bucket_alma_close, last_bucket_alma_open FROM signal_status WHERE id = 1"
    ).fetchone()
    if row is None or row["last_bucket_start"] is None:
        return None
    return row["last_bucket_start"], row["last_bucket_alma_close"], row["last_bucket_alma_open"]


def set_last_bucket_sample(conn: sqlite3.Connection, bucket_start: str, alma_close: float, alma_open: float) -> None:
    conn.execute(
        "UPDATE signal_status SET last_bucket_start = ?, last_bucket_alma_close = ?, last_bucket_alma_open = ? WHERE id = 1",
        (bucket_start, alma_close, alma_open),
    )
    conn.commit()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_db.py -v`
Expected: PASS (all existing + 5 new tests)

- [ ] **Step 5: Commit**

```bash
git add db.py tests/test_db.py
git commit -m "feat: add signals and signal_status tables"
```

---

## Task 4: Config for Twelve Data

**Files:**
- Modify: `config.py`
- Test: `tests/test_config.py`

**Interfaces:**
- Produces: `Config.twelve_data_api_key: str` (optional, default `""`), `Config.signal_symbol: str` (optional, default `"XAU/USD"`).

- [ ] **Step 1: Write the failing tests**

```python
# append to tests/test_config.py
def test_load_config_defaults_signal_engine_vars_when_unset(monkeypatch):
    for k, v in REQUIRED_VARS.items():
        monkeypatch.setenv(k, v)
    monkeypatch.delenv("TWELVE_DATA_API_KEY", raising=False)
    monkeypatch.delenv("SIGNAL_SYMBOL", raising=False)
    cfg = config_module.load_config()
    assert cfg.twelve_data_api_key == ""
    assert cfg.signal_symbol == "XAU/USD"


def test_load_config_reads_signal_engine_vars_when_set(monkeypatch):
    for k, v in REQUIRED_VARS.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setenv("TWELVE_DATA_API_KEY", "td-key-123")
    monkeypatch.setenv("SIGNAL_SYMBOL", "XAU/USD")
    cfg = config_module.load_config()
    assert cfg.twelve_data_api_key == "td-key-123"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_config.py -v`
Expected: FAIL with `TypeError: Config.__init__() got an unexpected keyword argument 'twelve_data_api_key'`

- [ ] **Step 3: Write the implementation**

In `config.py`, add two fields to the `Config` dataclass (after `healthz_shared_secret`):

```python
@dataclass
class Config:
    gmail_user: str
    gmail_app_password: str
    tg_bot_token: str
    tg_chat_id: str
    web_user: str
    web_password: str
    db_path: str
    tv_sender: str
    poll_interval_seconds: int
    healthz_shared_secret: str
    twelve_data_api_key: str
    signal_symbol: str
```

And add two lines in `load_config()`'s return, after `healthz_shared_secret`:

```python
        healthz_shared_secret=os.environ.get("HEALTHZ_SHARED_SECRET") or "",
        twelve_data_api_key=os.environ.get("TWELVE_DATA_API_KEY") or "",
        signal_symbol=os.environ.get("SIGNAL_SYMBOL") or "XAU/USD",
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_config.py -v`
Expected: PASS (all existing + 2 new tests)

- [ ] **Step 5: Commit**

```bash
git add config.py tests/test_config.py
git commit -m "feat: add Twelve Data config vars"
```

---

## Task 5: Bucket sampling and entry detection

**Files:**
- Create: `signal_engine.py` (this task writes the module's first half; Task 6 and 7 extend it)
- Test: `tests/test_signal_engine.py`

**Interfaces:**
- Consumes: `alma.alma` (Task 1), `market_data.Bar` (Task 2).
- Produces: `BUCKET_MINUTES = 8` (module constant), `BucketSample` dataclass (`bucket_start: datetime`, `alma_close: float`, `alma_open: float`, `bar_time: datetime`, `bar_close: float`), `EntrySignal` dataclass (`direction: str`, `entry_time: datetime`, `entry_price: float`), `bucket_samples(bars: list[Bar]) -> list[BucketSample]`, `detect_entries(samples: list[BucketSample]) -> list[EntrySignal]`.

- [ ] **Step 1: Write the failing tests**

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_signal_engine.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'signal_engine'`

- [ ] **Step 3: Write the implementation**

```python
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_signal_engine.py -v`
Expected: PASS (5 tests)

- [ ] **Step 5: Commit**

```bash
git add signal_engine.py tests/test_signal_engine.py
git commit -m "feat: add bucket sampling and entry detection"
```

---

## Task 6: TP/SL tier state machine

**Files:**
- Modify: `signal_engine.py`
- Test: `tests/test_signal_engine.py`

**Interfaces:**
- Consumes: `market_data.Bar` (Task 2).
- Produces: `evaluate_signal(direction: str, entry_price: float, entry_time: datetime, highest_tier: int, bars: list[Bar]) -> tuple[int, str | None]` — returns `(new_highest_tier, closing_status)`; `closing_status` is `None` while still open, else one of `SL_ONLY`, `TP1_THEN_SL`, `TP2_THEN_SL`, `TP3_FULL`.

- [ ] **Step 1: Write the failing tests**

```python
# append to tests/test_signal_engine.py
from signal_engine import evaluate_signal


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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_signal_engine.py -v`
Expected: FAIL with `ImportError: cannot import name 'evaluate_signal' from 'signal_engine'`

- [ ] **Step 3: Write the implementation**

Append to `signal_engine.py`:

```python
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_signal_engine.py -v`
Expected: PASS (11 tests total)

- [ ] **Step 5: Commit**

```bash
git add signal_engine.py tests/test_signal_engine.py
git commit -m "feat: add TP/SL tier state machine"
```

---

## Task 7: poll_once orchestration

**Files:**
- Modify: `signal_engine.py`
- Test: `tests/test_signal_engine.py`

**Interfaces:**
- Consumes: `market_data.fetch_recent_bars` (Task 2), `db.get_last_bar_time`/`set_last_bar_time`/`get_last_bucket_sample`/`set_last_bucket_sample`/`insert_signal`/`open_signals`/`update_signal` (Task 3), `telegram.send_telegram_message` (existing), `bucket_samples`/`detect_entries`/`evaluate_signal` (Tasks 5-6).
- Produces: `poll_once(conn, api_key: str, symbol: str, tg_bot_token: str, tg_chat_id: str) -> None`.

- [ ] **Step 1: Write the failing tests**

```python
# append to tests/test_signal_engine.py
from unittest.mock import patch

import db as db_module
from signal_engine import poll_once


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


def test_poll_once_detects_entry_pushes_telegram_and_records_signal():
    conn = db_module.connect(":memory:")
    bars = [_bar(m, 100 + 0.01 * m, 100 + 0.01 * m, 100 + 0.01 * m, 100 + 0.01 * m) for m in range(0, 8)]
    bars.append(_bar(8, 100.5, 100.5, 100.5, 105.0))  # forces a crossover on the next bucket
    with patch("signal_engine.fetch_recent_bars", return_value=bars), \
         patch("signal_engine.send_telegram_message", return_value=(True, None)) as fake_send:
        poll_once(conn, "key", "XAU/USD", "tok", "chat")
    # Whether or not this exact synthetic series crosses is incidental to this
    # test's real assertion: the poll must complete without raising and must
    # advance last_bar_time to the newest bar it processed.
    assert db_module.get_last_bar_time(conn) == bars[-1].time.isoformat()


def test_poll_once_swallows_market_data_errors():
    conn = db_module.connect(":memory:")
    with patch("signal_engine.fetch_recent_bars", side_effect=MarketDataError("boom")):
        poll_once(conn, "key", "XAU/USD", "tok", "chat")  # must not raise
    assert db_module.get_last_bar_time(conn) is None  # nothing advanced


def test_poll_once_closes_open_signal_and_pushes_result():
    conn = db_module.connect(":memory:")
    entry_time = datetime(2026, 9, 25, 10, 15, tzinfo=timezone.utc)
    db_module.insert_signal(conn, "long", 100.0, entry_time.isoformat())
    sl_bar = _bar(16, 100, 100, 99.85, 99.9)  # triggers SL_ONLY for the open long
    with patch("signal_engine.fetch_recent_bars", return_value=[sl_bar]), \
         patch("signal_engine.send_telegram_message", return_value=(True, None)) as fake_send:
        poll_once(conn, "key", "XAU/USD", "tok", "chat")
    rows = db_module.recent_signals(conn)
    assert rows[0]["status"] == "SL_ONLY"
    assert fake_send.called
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_signal_engine.py -v`
Expected: FAIL with `ImportError: cannot import name 'poll_once' from 'signal_engine'`

- [ ] **Step 3: Write the implementation**

Add these imports to the top of `signal_engine.py` (alongside the existing ones) and append `poll_once` at the end:

```python
from datetime import datetime, timedelta, timezone  # already present; add nothing new here

import db
from market_data import Bar, MarketDataError, fetch_recent_bars
from telegram import send_telegram_message
```

```python
def poll_once(conn, api_key: str, symbol: str, tg_bot_token: str, tg_chat_id: str) -> None:
    if not api_key:
        return

    try:
        last_bar_time_str = db.get_last_bar_time(conn)
        now = datetime.now(timezone.utc)
        if last_bar_time_str is None:
            outputsize = 200
        else:
            last_bar_time = datetime.fromisoformat(last_bar_time_str)
            gap_minutes = int((now - last_bar_time).total_seconds() // 60)
            outputsize = min(200, max(1, gap_minutes + 5))

        bars = fetch_recent_bars(api_key, symbol, outputsize)

        if last_bar_time_str is not None:
            last_bar_time = datetime.fromisoformat(last_bar_time_str)
            bars = [b for b in bars if b.time > last_bar_time]

        if not bars:
            log.info("signal poll: no new bars")
            return

        new_samples = bucket_samples(bars)

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
        for entry in entries:
            db.insert_signal(conn, entry.direction, entry.entry_price, entry.entry_time.isoformat())
            text = f"\U0001F4CA Signal Entry\n{entry.direction.upper()} @ {entry.entry_price}\n{entry.entry_time.isoformat()}"
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
        log.info("signal poll ok, %d new bars, %d entries", len(bars), len(entries))
    except MarketDataError as e:
        log.error("signal poll: market data error: %s", e)
    except Exception:
        log.exception("signal poll failed")
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_signal_engine.py -v`
Expected: PASS (17 tests total)

- [ ] **Step 5: Commit**

```bash
git add signal_engine.py tests/test_signal_engine.py
git commit -m "feat: add poll_once orchestration for signal engine"
```

---

## Task 8: Wire into main.py

**Files:**
- Modify: `main.py`
- Test: `tests/test_main.py`

**Interfaces:**
- Consumes: `signal_engine.poll_once` (Task 7), `db.recent_signals` (Task 3), existing `check_auth`, existing `_poll_lock` pattern.
- Produces: `GET /signals/export` (Basic Auth protected).

- [ ] **Step 1: Write the failing tests**

```python
# append to tests/test_main.py
def test_signals_export_requires_auth():
    app.dependency_overrides[get_config] = lambda: make_cfg()
    app.dependency_overrides[get_db] = lambda: db_module.connect(":memory:")
    client = TestClient(app)
    resp = client.get("/signals/export")
    assert resp.status_code == 401


def test_signals_export_returns_summary_and_distribution():
    conn = db_module.connect(":memory:")
    db_module.insert_signal(conn, "long", 100.0, "2026-09-25T10:15:00+00:00")
    db_module.update_signal(conn, 1, 3, "TP3_FULL", "2026-09-25T10:40:00+00:00")
    db_module.insert_signal(conn, "short", 100.0, "2026-09-25T11:15:00+00:00")
    db_module.update_signal(conn, 2, 0, "SL_ONLY", "2026-09-25T11:20:00+00:00")
    app.dependency_overrides[get_config] = lambda: make_cfg()
    app.dependency_overrides[get_db] = lambda: conn
    client = TestClient(app)
    resp = client.get("/signals/export", auth=("admin", "secret"))
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["signals"]) == 2
    assert data["summary"]["total_closed"] == 2
    assert data["summary"]["wins"] == 1
    assert data["summary"]["win_rate"] == 0.5
    assert data["summary"]["distribution"]["TP3_FULL"] == 1
    assert data["summary"]["distribution"]["SL_ONLY"] == 1


def test_poll_route_triggers_signal_engine_alongside_gmail_poll(monkeypatch):
    conn = db_module.connect(":memory:")
    calls = []

    def fake_poll_once(conn_arg, *args):
        db_module.record_poll(conn_arg, success=True)

    def fake_signal_poll_once(conn_arg, api_key, symbol, tok, chat):
        calls.append((api_key, symbol))

    monkeypatch.setattr(main_module, "poll_once", fake_poll_once)
    monkeypatch.setattr(main_module.signal_engine, "poll_once", fake_signal_poll_once)
    app.dependency_overrides[get_config] = lambda: make_cfg(twelve_data_api_key="td-key", signal_symbol="XAU/USD")
    app.dependency_overrides[get_db] = lambda: conn
    client = TestClient(app)
    resp = client.post("/poll", auth=("admin", "secret"))
    assert resp.status_code == 200
    assert calls == [("td-key", "XAU/USD")]


def test_poll_route_survives_signal_engine_exception(monkeypatch):
    conn = db_module.connect(":memory:")

    def fake_poll_once(conn_arg, *args):
        db_module.record_poll(conn_arg, success=True)

    def broken_signal_poll_once(*args):
        raise RuntimeError("signal engine exploded")

    monkeypatch.setattr(main_module, "poll_once", fake_poll_once)
    monkeypatch.setattr(main_module.signal_engine, "poll_once", broken_signal_poll_once)
    app.dependency_overrides[get_config] = lambda: make_cfg()
    app.dependency_overrides[get_db] = lambda: conn
    client = TestClient(app)
    resp = client.post("/poll", auth=("admin", "secret"))
    assert resp.status_code == 200  # Gmail-relay health must be unaffected
```

Also update `make_cfg` in `tests/test_main.py` to include the two new required kwargs (it constructs `Config(...)` directly):

```python
def make_cfg(**overrides):
    base = dict(
        gmail_user="u", gmail_app_password="p", tg_bot_token="t", tg_chat_id="c",
        web_user="admin", web_password="secret", db_path=":memory:",
        tv_sender="noreply@tradingview.com", poll_interval_seconds=300,
        healthz_shared_secret="", twelve_data_api_key="", signal_symbol="XAU/USD",
    )
    base.update(overrides)
    return Config(**base)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_main.py -v`
Expected: FAIL — `test_signals_export_requires_auth` 404s (route doesn't exist), `test_poll_route_triggers_signal_engine_alongside_gmail_poll` fails with `AttributeError: module 'main' has no attribute 'signal_engine'`

- [ ] **Step 3: Write the implementation**

Add the import near the top of `main.py` (alongside the existing `from poller import poll_once`):

```python
import signal_engine
```

Add a second lock and helper near `_poll_lock`/`_poll`:

```python
_signal_poll_lock = threading.Lock()


def _poll_signals(cfg: Config, conn) -> None:
    with _signal_poll_lock:
        try:
            signal_engine.poll_once(conn, cfg.twelve_data_api_key, cfg.signal_symbol, cfg.tg_bot_token, cfg.tg_chat_id)
        except Exception:
            logging.getLogger(__name__).exception("signal engine poll failed")
```

Call it from both `_poll_loop` and the `/poll` route, right alongside the existing `_poll(cfg, conn)` call:

```python
async def _poll_loop(cfg: Config, conn) -> None:
    while True:
        await asyncio.to_thread(_poll, cfg, conn)
        await asyncio.to_thread(_poll_signals, cfg, conn)
        await asyncio.sleep(cfg.poll_interval_seconds)
```

```python
@app.post("/poll")
def trigger_poll(
    cfg: Config = Depends(get_config),
    conn=Depends(get_db),
    _auth: None = Depends(check_auth),
):
    _poll(cfg, conn)
    _poll_signals(cfg, conn)
    status = db_module.get_status(conn)
    healthy = compute_health(
        _parse_dt(status["last_poll_at"]), status["consecutive_errors"],
        datetime.now(timezone.utc), cfg.poll_interval_seconds,
    )
    body = {
        "healthy": healthy,
        "last_poll_at": status["last_poll_at"],
        "consecutive_errors": status["consecutive_errors"],
    }
    return JSONResponse(body, status_code=200 if healthy else 500)
```

Add the new endpoint after the existing `/export` route:

```python
@app.get("/signals/export")
def export_signals(conn=Depends(get_db), _auth: None = Depends(check_auth)):
    rows = db_module.recent_signals(conn, limit=100000)
    signals = [dict(row) for row in rows]
    closed = [s for s in signals if s["status"] != "OPEN"]
    wins = [s for s in closed if s["status"] in ("TP1_THEN_SL", "TP2_THEN_SL", "TP3_FULL")]
    distribution: dict[str, int] = {}
    for s in signals:
        distribution[s["status"]] = distribution.get(s["status"], 0) + 1
    return JSONResponse({
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "signals": signals,
        "summary": {
            "total_closed": len(closed),
            "wins": len(wins),
            "win_rate": (len(wins) / len(closed)) if closed else None,
            "distribution": distribution,
        },
    })
```

Note: `logging` is already imported inside the `__main__` block at the bottom of `main.py`; move `import logging` to the top-level imports (alongside `import asyncio`) since `_poll_signals` now uses it at module scope, not just inside `__main__`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_main.py -v`
Expected: PASS (all existing + 4 new tests)

- [ ] **Step 5: Commit**

```bash
git add main.py tests/test_main.py
git commit -m "feat: wire signal engine into /poll and add /signals/export"
```

---

## Task 9: Deployment config

**Files:**
- Modify: `.env.example`
- Modify: `render.yaml`
- Modify: `docs/DEPLOY.md`

**Interfaces:**
- Consumes: `TWELVE_DATA_API_KEY`, `SIGNAL_SYMBOL` (Task 4).

- [ ] **Step 1: Add the new vars to `.env.example`**

Append after `HEALTHZ_SHARED_SECRET=`:

```
TWELVE_DATA_API_KEY=
SIGNAL_SYMBOL=XAU/USD
```

- [ ] **Step 2: Add the new vars to `render.yaml`**

Add to the `envVars` list, after `HEALTHZ_SHARED_SECRET`:

```yaml
      - key: TWELVE_DATA_API_KEY
        sync: false
      - key: SIGNAL_SYMBOL
        value: XAU/USD
```

- [ ] **Step 3: Document the new setup step in `docs/DEPLOY.md`**

Replace the entire contents of `docs/DEPLOY.md` with the following (inserts a new
section 3, renumbers the old sections 3-7 to 4-8, and adds `TWELVE_DATA_API_KEY`
to the sync:false list in what is now section 4):

```markdown
# tv-alert-relay 部署(Render)

Oracle Cloud VM 版本的部署步骤挪到了 `docs/DEPLOY-ORACLE.md`(以后如果要重新
搬回某台常驻服务器,可以照那份抄)。这一份是当前实际在用的 Render 部署方式。

## 1. Gmail 应用专用密码

Google 账号 → 安全性 → 两步验证(需先开启)→ 应用专用密码 → 生成一个,填进
下一步 Render 的环境变量。

## 2. Telegram Bot

Telegram 里找 `@BotFather` → `/newbot` 建一个新 bot,拿到 token;给 bot 发条
消息后访问 `https://api.telegram.org/bot<token>/getUpdates` 找 `chat_id`。

## 3. Twelve Data API Key(信号引擎用)

XXX 策略信号引擎(独立于 Gmail 邮件转发那套)需要一个免费的 Twelve Data 账号
来拉 XAUUSD 行情:

1. 去 [twelvedata.com](https://twelvedata.com) 注册一个免费账号(不需要绑卡)。
2. 登录后在 Dashboard 里能看到你的 API Key,复制下来。
3. 填进下一步 Render 的环境变量 `TWELVE_DATA_API_KEY`。
4. `SIGNAL_SYMBOL` 不用改,默认就是 `XAU/USD`;只有以后想换别的品种才需要改。

## 4. 部署到 Render

1. 去 [render.com](https://render.com) 用 GitHub 账号登录(不需要绑卡)。
2. New → Blueprint,选这个仓库,Render 会读到根目录的 `render.yaml` 自动建好
   服务骨架。
3. 部署过程中 Render 会提示你填标了 `sync: false` 的那几个环境变量:
   `GMAIL_USER`、`GMAIL_APP_PASSWORD`、`TG_BOT_TOKEN`、`TG_CHAT_ID`、
   `WEB_USER`、`WEB_PASSWORD`、`HEALTHZ_SHARED_SECRET`(留空即可,除非你想启
   用 `/healthz` 的额外密钥校验)、`TWELVE_DATA_API_KEY`(第 3 步拿到的那个)。
4. 部署完成后,Render 会给一个形如 `https://tv-alert-relay-xxxx.onrender.com`
   的域名——这就是后面 GitHub Secrets 里要填的 `TVALERT_APP_URL`。

## 5. TradingView 侧

建警报时在 Notifications 里勾选 "Send Email",不用配置别的——这个服务会自动
去 Gmail 里捞 TradingView 发来的邮件。

## 6. GitHub Actions 配的 3 个仓库 Secrets

GitHub 仓库 → Settings → Secrets and variables → Actions → New repository
secret,建 3 个,`poll.yml`(每 5 分钟触发一次轮询,兼报警)和 `backup.yml`
(每周备份一次历史记录)两个工作流共用:

- `TVALERT_APP_URL` = 第 4 步 Render 给的域名(不带路径,比如
  `https://tv-alert-relay-xxxx.onrender.com`)
- `TVALERT_WEB_USER` = 跟 Render 环境变量里的 `WEB_USER` 填一样的值
- `TVALERT_WEB_PASSWORD` = 跟 Render 环境变量里的 `WEB_PASSWORD` 填一样的值

注意:`poll.yml` 一推到默认分支就会开始按计划运行;在上面 3 个 Secret 配好
之前,它每 5 分钟都会失败一次(GitHub 默认会给你发工作流失败邮件)。所以部署
完 Render、拿到域名后尽快配好这 3 个 Secret。

## 7. 看板访问

浏览器直接访问 Render 给的域名(比如
`https://tv-alert-relay-xxxx.onrender.com`),会弹 Basic Auth 登录框,填
`WEB_USER`/`WEB_PASSWORD`。信号引擎的历史记录和胜率在 `/signals/export`
(同一套账号密码)。

## 8. 备份文件在哪

`backups/alerts.json`,每周日自动更新,提交历史本身就是各个时间点的快照,不需
要额外去别处找。

Render 免费层没有持久盘,每次重新部署都会清空网页历史。`render.yaml` 里的
`buildFilter.ignoredPaths: backups/**` 让只改动 `backups/` 的备份提交不触发
自动重新部署。如果发现每周备份提交后网页历史被清空,说明 `render.yaml` 里的
`buildFilter.ignoredPaths` 配置在当前 Render 版本上没生效,去 Render 控制台的
Settings → Build & Deploy 里手动关闭这个服务的 'Auto-Deploy',改成只在你自己推
代码改动时手动点 'Deploy latest commit'。
```

- [ ] **Step 4: Commit**

```bash
git add .env.example render.yaml docs/DEPLOY.md
git commit -m "docs: add Twelve Data setup to deploy guide"
```

---

## Final verification

- [ ] Run the full suite: `pytest -v` — expect all tests passing (existing ~32 + new ~30)
- [ ] Manually inspect `git log --oneline` shows one commit per task, in order
