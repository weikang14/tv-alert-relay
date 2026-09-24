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
"""


def connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    conn.execute("INSERT OR IGNORE INTO status (id, consecutive_errors) VALUES (1, 0)")
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
