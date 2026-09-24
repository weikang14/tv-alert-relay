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
