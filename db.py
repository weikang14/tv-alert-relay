import sqlite3
from datetime import datetime, timezone

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


def connect(db_path: str) -> sqlite3.Connection:
    # ponytail: FastAPI runs sync route handlers in worker threads, so the
    # single long-lived connection is used across threads; SQLite's C library
    # is thread-safe (serialized) by default, only Python's own same-thread
    # guard needs disabling here.
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    conn.execute("INSERT OR IGNORE INTO status (id, consecutive_errors) VALUES (1, 0)")
    conn.execute("INSERT OR IGNORE INTO signal_status (id) VALUES (1)")
    conn.commit()
    return conn


def insert_alert(conn: sqlite3.Connection, subject: str, body_snippet: str, sent_ok: bool, error: str | None) -> None:
    conn.execute(
        "INSERT INTO alerts (received_at, subject, body_snippet, sent_ok, error) VALUES (?, ?, ?, ?, ?)",
        (datetime.now(timezone.utc).isoformat(), subject, body_snippet, int(sent_ok), error),
    )
    conn.commit()


def recent_alerts(conn: sqlite3.Connection, limit: int = 50) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM alerts ORDER BY id DESC LIMIT ?", (limit,)
    ).fetchall()


def record_poll(conn: sqlite3.Connection, success: bool) -> None:
    now = datetime.now(timezone.utc).isoformat()
    if success:
        conn.execute(
            "UPDATE status SET last_poll_at = ?, last_success_at = ?, consecutive_errors = 0 WHERE id = 1",
            (now, now),
        )
    else:
        conn.execute(
            "UPDATE status SET last_poll_at = ?, consecutive_errors = consecutive_errors + 1 WHERE id = 1",
            (now,),
        )
    conn.commit()


def get_status(conn: sqlite3.Connection) -> sqlite3.Row:
    return conn.execute("SELECT * FROM status WHERE id = 1").fetchone()


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
