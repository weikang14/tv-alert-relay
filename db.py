from datetime import datetime, timezone

from sqlalchemy import CheckConstraint, Column, Float, Integer, MetaData, String, Table, create_engine, insert, select, update
from sqlalchemy.engine import Engine
from sqlalchemy.pool import StaticPool

metadata = MetaData()

alerts = Table(
    "alerts", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("received_at", String, nullable=False),
    Column("subject", String, nullable=False),
    Column("body_snippet", String, nullable=False),
    Column("sent_ok", Integer, nullable=False),
    Column("error", String),
)

status = Table(
    "status", metadata,
    Column("id", Integer, primary_key=True),
    CheckConstraint("id = 1"),
    Column("last_poll_at", String),
    Column("last_success_at", String),
    Column("consecutive_errors", Integer, nullable=False, server_default="0"),
)

signals = Table(
    "signals", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("direction", String, nullable=False),
    Column("entry_price", Float, nullable=False),
    Column("entry_time", String, nullable=False),
    Column("entry_high", Float, nullable=False),
    Column("entry_low", Float, nullable=False),
    Column("highest_tier", Integer, nullable=False, server_default="0"),
    Column("status", String, nullable=False, server_default="OPEN"),
    Column("exited_at", String),
    Column("last_bar_high", Float),
    Column("last_bar_low", Float),
)

signal_status = Table(
    "signal_status", metadata,
    Column("id", Integer, primary_key=True),
    CheckConstraint("id = 1"),
    Column("last_bar_time", String),
    Column("last_bucket_start", String),
    Column("last_bucket_open", Float),
    Column("last_bucket_close", Float),
    Column("last_bucket_alma_close", Float),
    Column("last_bucket_alma_open", Float),
)


def _normalize_url(url: str) -> str:
    # Supabase/Render dashboards hand out plain postgres:// or postgresql://
    # connection strings — SQLAlchemy needs the driver named explicitly.
    for prefix in ("postgres://", "postgresql://"):
        if url.startswith(prefix):
            return "postgresql+psycopg://" + url[len(prefix):]
    return url


def connect(url: str) -> Engine:
    url = _normalize_url(url)
    if url.startswith("sqlite"):
        # A SQLite in-memory database is per-connection: without StaticPool,
        # the pool would hand out a fresh (empty) database on every checkout.
        # check_same_thread=False mirrors the app's single-Engine-shared-
        # across-worker-threads usage (FastAPI runs sync handlers in threads).
        engine = create_engine(url, connect_args={"check_same_thread": False}, poolclass=StaticPool)
    else:
        engine = create_engine(url)
    metadata.create_all(engine)
    with engine.begin() as conn:
        if conn.execute(select(status.c.id).where(status.c.id == 1)).first() is None:
            conn.execute(insert(status).values(id=1, consecutive_errors=0))
        if conn.execute(select(signal_status.c.id).where(signal_status.c.id == 1)).first() is None:
            conn.execute(insert(signal_status).values(id=1))
    return engine


def insert_alert(engine: Engine, subject: str, body_snippet: str, sent_ok: bool, error: str | None) -> None:
    with engine.begin() as conn:
        conn.execute(insert(alerts).values(
            received_at=datetime.now(timezone.utc).isoformat(),
            subject=subject, body_snippet=body_snippet, sent_ok=int(sent_ok), error=error,
        ))


def recent_alerts(engine: Engine, limit: int = 50) -> list[dict]:
    with engine.connect() as conn:
        rows = conn.execute(select(alerts).order_by(alerts.c.id.desc()).limit(limit)).mappings().all()
    return [dict(r) for r in rows]


def record_poll(engine: Engine, success: bool) -> None:
    now = datetime.now(timezone.utc).isoformat()
    with engine.begin() as conn:
        if success:
            conn.execute(
                update(status).where(status.c.id == 1)
                .values(last_poll_at=now, last_success_at=now, consecutive_errors=0)
            )
        else:
            conn.execute(
                update(status).where(status.c.id == 1)
                .values(last_poll_at=now, consecutive_errors=status.c.consecutive_errors + 1)
            )


def get_status(engine: Engine) -> dict | None:
    with engine.connect() as conn:
        row = conn.execute(select(status).where(status.c.id == 1)).mappings().first()
    return dict(row) if row is not None else None


def insert_signal(
    engine: Engine, direction: str, entry_price: float, entry_time: str,
    entry_high: float, entry_low: float,
) -> int:
    with engine.begin() as conn:
        result = conn.execute(
            insert(signals).values(
                direction=direction, entry_price=entry_price, entry_time=entry_time,
                entry_high=entry_high, entry_low=entry_low, highest_tier=0, status="OPEN",
                last_bar_high=entry_high, last_bar_low=entry_low,
            ).returning(signals.c.id)
        )
        return result.scalar_one()


def open_signals(engine: Engine) -> list[dict]:
    with engine.connect() as conn:
        rows = conn.execute(
            select(signals).where(signals.c.status == "OPEN").order_by(signals.c.id)
        ).mappings().all()
    return [dict(r) for r in rows]


def update_signal(
    engine: Engine, signal_id: int, highest_tier: int, status_value: str, exited_at: str | None,
    last_bar_high: float, last_bar_low: float,
) -> None:
    with engine.begin() as conn:
        conn.execute(
            update(signals).where(signals.c.id == signal_id).values(
                highest_tier=highest_tier, status=status_value, exited_at=exited_at,
                last_bar_high=last_bar_high, last_bar_low=last_bar_low,
            )
        )


def recent_signals(engine: Engine, limit: int = 100000) -> list[dict]:
    with engine.connect() as conn:
        rows = conn.execute(select(signals).order_by(signals.c.id.desc()).limit(limit)).mappings().all()
    return [dict(r) for r in rows]


def get_last_bar_time(engine: Engine) -> str | None:
    with engine.connect() as conn:
        row = conn.execute(select(signal_status.c.last_bar_time).where(signal_status.c.id == 1)).first()
    return row[0] if row is not None else None


def set_last_bar_time(engine: Engine, value: str) -> None:
    with engine.begin() as conn:
        conn.execute(update(signal_status).where(signal_status.c.id == 1).values(last_bar_time=value))


def get_last_bucket_carry(engine: Engine) -> tuple[str, float, float, float, float] | None:
    with engine.connect() as conn:
        row = conn.execute(
            select(
                signal_status.c.last_bucket_start, signal_status.c.last_bucket_open,
                signal_status.c.last_bucket_close, signal_status.c.last_bucket_alma_close,
                signal_status.c.last_bucket_alma_open,
            ).where(signal_status.c.id == 1)
        ).first()
    if row is None or row[0] is None:
        return None
    return tuple(row)


def set_last_bucket_carry(
    engine: Engine, bucket_start: str, bucket_open: float, bucket_close: float,
    alma_close: float, alma_open: float,
) -> None:
    with engine.begin() as conn:
        conn.execute(
            update(signal_status).where(signal_status.c.id == 1).values(
                last_bucket_start=bucket_start, last_bucket_open=bucket_open, last_bucket_close=bucket_close,
                last_bucket_alma_close=alma_close, last_bucket_alma_open=alma_open,
            )
        )
