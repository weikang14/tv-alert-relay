import db


def test_connect_creates_tables_and_default_status():
    conn = db.connect(":memory:")
    status = db.get_status(conn)
    assert status["consecutive_errors"] == 0
    assert status["last_poll_at"] is None


def test_insert_and_recent_alerts_most_recent_first():
    conn = db.connect(":memory:")
    db.insert_alert(conn, "Subj", "Body", True, None)
    db.insert_alert(conn, "Subj2", "Body2", False, "err")
    rows = db.recent_alerts(conn, limit=10)
    assert len(rows) == 2
    assert rows[0]["subject"] == "Subj2"
    assert rows[0]["sent_ok"] == 0
    assert rows[0]["error"] == "err"


def test_record_poll_success_resets_consecutive_errors():
    conn = db.connect(":memory:")
    db.record_poll(conn, success=False)
    db.record_poll(conn, success=False)
    assert db.get_status(conn)["consecutive_errors"] == 2

    db.record_poll(conn, success=True)
    status = db.get_status(conn)
    assert status["consecutive_errors"] == 0
    assert status["last_success_at"] is not None
    assert status["last_poll_at"] is not None


def test_insert_signal_and_open_signals():
    conn = db.connect(":memory:")
    signal_id = db.insert_signal(conn, "long", 3650.5, "2026-09-25T10:08:00+00:00", 3651.0, 3650.0)
    open_ = db.open_signals(conn)
    assert len(open_) == 1
    assert open_[0]["id"] == signal_id
    assert open_[0]["direction"] == "long"
    assert open_[0]["status"] == "OPEN"
    assert open_[0]["highest_tier"] == 0
    assert open_[0]["entry_high"] == 3651.0
    assert open_[0]["entry_low"] == 3650.0
    # last_bar_high/low seed from the entry bar's own high/low
    assert open_[0]["last_bar_high"] == 3651.0
    assert open_[0]["last_bar_low"] == 3650.0


def test_update_signal_changes_status_and_excludes_from_open():
    conn = db.connect(":memory:")
    signal_id = db.insert_signal(conn, "short", 3650.5, "2026-09-25T10:08:00+00:00", 3651.0, 3650.0)
    db.update_signal(conn, signal_id, 3, "TP3_FULL", "2026-09-25T10:30:00+00:00", 3660.0, 3655.0)
    assert db.open_signals(conn) == []
    rows = db.recent_signals(conn)
    assert rows[0]["status"] == "TP3_FULL"
    assert rows[0]["highest_tier"] == 3
    assert rows[0]["exited_at"] == "2026-09-25T10:30:00+00:00"
    assert rows[0]["last_bar_high"] == 3660.0
    assert rows[0]["last_bar_low"] == 3655.0


def test_recent_signals_most_recent_first():
    conn = db.connect(":memory:")
    db.insert_signal(conn, "long", 1.0, "2026-09-25T10:00:00+00:00", 1.1, 0.9)
    db.insert_signal(conn, "short", 2.0, "2026-09-25T10:08:00+00:00", 2.1, 1.9)
    rows = db.recent_signals(conn)
    assert rows[0]["direction"] == "short"


def test_last_bar_time_defaults_to_none_then_roundtrips():
    conn = db.connect(":memory:")
    assert db.get_last_bar_time(conn) is None
    db.set_last_bar_time(conn, "2026-09-25T10:08:00+00:00")
    assert db.get_last_bar_time(conn) == "2026-09-25T10:08:00+00:00"
    db.set_last_bar_time(conn, "2026-09-25T10:09:00+00:00")
    assert db.get_last_bar_time(conn) == "2026-09-25T10:09:00+00:00"


def test_last_bucket_carry_defaults_to_none_then_roundtrips():
    conn = db.connect(":memory:")
    assert db.get_last_bucket_carry(conn) is None
    db.set_last_bucket_carry(conn, "2026-09-25T10:08:00+00:00", 3649.0, 3650.5, 3650.1, 3650.2)
    assert db.get_last_bucket_carry(conn) == ("2026-09-25T10:08:00+00:00", 3649.0, 3650.5, 3650.1, 3650.2)
