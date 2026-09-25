import threading
import time
from datetime import datetime

import pytest
from fastapi.testclient import TestClient

import db as db_module
import main as main_module
from config import Config
from imap_client import GMAIL_IMAP_HOST
from main import app, get_config, get_db


def make_cfg(**overrides):
    base = dict(
        gmail_user="u", gmail_app_password="p", tg_bot_token="t", tg_chat_id="c",
        web_user="admin", web_password="secret", db_path=":memory:",
        tv_sender="noreply@tradingview.com", poll_interval_seconds=300,
        healthz_shared_secret="", twelve_data_api_key="", signal_symbol="XAU/USD",
    )
    base.update(overrides)
    return Config(**base)


@pytest.fixture(autouse=True)
def clear_overrides():
    yield
    app.dependency_overrides.clear()


def test_dashboard_requires_auth():
    app.dependency_overrides[get_config] = lambda: make_cfg()
    app.dependency_overrides[get_db] = lambda: db_module.connect(":memory:")
    client = TestClient(app)
    resp = client.get("/")
    assert resp.status_code == 401


def test_dashboard_rejects_wrong_password():
    app.dependency_overrides[get_config] = lambda: make_cfg()
    app.dependency_overrides[get_db] = lambda: db_module.connect(":memory:")
    client = TestClient(app)
    resp = client.get("/", auth=("admin", "wrong"))
    assert resp.status_code == 401


def test_dashboard_ok_with_auth():
    app.dependency_overrides[get_config] = lambda: make_cfg()
    app.dependency_overrides[get_db] = lambda: db_module.connect(":memory:")
    client = TestClient(app)
    resp = client.get("/", auth=("admin", "secret"))
    assert resp.status_code == 200
    assert "TradingView" in resp.text


def test_healthz_unhealthy_before_first_poll():
    app.dependency_overrides[get_config] = lambda: make_cfg()
    app.dependency_overrides[get_db] = lambda: db_module.connect(":memory:")
    client = TestClient(app)
    resp = client.get("/healthz")
    assert resp.status_code == 500


def test_healthz_healthy_after_success():
    conn = db_module.connect(":memory:")
    db_module.record_poll(conn, success=True)
    app.dependency_overrides[get_config] = lambda: make_cfg()
    app.dependency_overrides[get_db] = lambda: conn
    client = TestClient(app)
    resp = client.get("/healthz")
    assert resp.status_code == 200


def test_healthz_requires_secret_when_configured():
    conn = db_module.connect(":memory:")
    db_module.record_poll(conn, success=True)
    app.dependency_overrides[get_config] = lambda: make_cfg(healthz_shared_secret="s3cret")
    app.dependency_overrides[get_db] = lambda: conn
    client = TestClient(app)
    resp = client.get("/healthz")
    assert resp.status_code == 403
    resp2 = client.get("/healthz", headers={"X-Healthz-Secret": "s3cret"})
    assert resp2.status_code == 200


def test_poll_requires_auth():
    app.dependency_overrides[get_config] = lambda: make_cfg()
    app.dependency_overrides[get_db] = lambda: db_module.connect(":memory:")
    client = TestClient(app)
    resp = client.post("/poll")
    assert resp.status_code == 401


def test_poll_triggers_poll_once_and_returns_healthy(monkeypatch):
    conn = db_module.connect(":memory:")
    calls = []

    def fake_poll_once(conn_arg, imap_host, gmail_user, gmail_app_password, tv_sender, tg_bot_token, tg_chat_id):
        calls.append((conn_arg, imap_host, gmail_user, gmail_app_password, tv_sender, tg_bot_token, tg_chat_id))
        db_module.record_poll(conn_arg, success=True)

    monkeypatch.setattr(main_module, "poll_once", fake_poll_once)
    app.dependency_overrides[get_config] = lambda: make_cfg()
    app.dependency_overrides[get_db] = lambda: conn
    client = TestClient(app)
    resp = client.post("/poll", auth=("admin", "secret"))
    assert resp.status_code == 200
    assert len(calls) == 1
    assert calls[0] == (conn, GMAIL_IMAP_HOST, "u", "p", "noreply@tradingview.com", "t", "c")


def test_poll_returns_500_when_unhealthy_after_failures(monkeypatch):
    conn = db_module.connect(":memory:")

    def fake_poll_once(conn_arg, *args):
        db_module.record_poll(conn_arg, success=False)
        db_module.record_poll(conn_arg, success=False)
        db_module.record_poll(conn_arg, success=False)

    monkeypatch.setattr(main_module, "poll_once", fake_poll_once)
    app.dependency_overrides[get_config] = lambda: make_cfg()
    app.dependency_overrides[get_db] = lambda: conn
    client = TestClient(app)
    resp = client.post("/poll", auth=("admin", "secret"))
    assert resp.status_code == 500


def test_poll_lock_prevents_concurrent_execution(monkeypatch):
    conn = db_module.connect(":memory:")
    concurrent = {"count": 0, "max": 0}
    counter_lock = threading.Lock()

    def fake_poll_once(conn_arg, *args):
        with counter_lock:
            concurrent["count"] += 1
            concurrent["max"] = max(concurrent["max"], concurrent["count"])
        time.sleep(0.05)
        with counter_lock:
            concurrent["count"] -= 1
        db_module.record_poll(conn_arg, success=True)

    monkeypatch.setattr(main_module, "poll_once", fake_poll_once)
    cfg = make_cfg()
    threads = [
        threading.Thread(target=main_module._poll, args=(cfg, conn))
        for _ in range(2)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert concurrent["max"] == 1


def test_export_requires_auth():
    app.dependency_overrides[get_config] = lambda: make_cfg()
    app.dependency_overrides[get_db] = lambda: db_module.connect(":memory:")
    client = TestClient(app)
    resp = client.get("/export")
    assert resp.status_code == 401


def test_export_returns_empty_list_when_no_alerts():
    app.dependency_overrides[get_config] = lambda: make_cfg()
    app.dependency_overrides[get_db] = lambda: db_module.connect(":memory:")
    client = TestClient(app)
    resp = client.get("/export", auth=("admin", "secret"))
    assert resp.status_code == 200
    data = resp.json()
    assert data["alerts"] == []
    datetime.fromisoformat(data["exported_at"])


def test_export_returns_all_alerts_as_json():
    conn = db_module.connect(":memory:")
    db_module.insert_alert(conn, "Subj1", "Body1", True, None)
    db_module.insert_alert(conn, "Subj2", "Body2", False, "boom")
    app.dependency_overrides[get_config] = lambda: make_cfg()
    app.dependency_overrides[get_db] = lambda: conn
    client = TestClient(app)
    resp = client.get("/export", auth=("admin", "secret"))
    assert resp.status_code == 200
    data = resp.json()
    datetime.fromisoformat(data["exported_at"])
    assert len(data["alerts"]) == 2
    subjects = {a["subject"] for a in data["alerts"]}
    assert subjects == {"Subj1", "Subj2"}
    errored = [a for a in data["alerts"] if a["subject"] == "Subj2"][0]
    assert errored["sent_ok"] == 0
    assert errored["error"] == "boom"


def test_signals_export_requires_auth():
    app.dependency_overrides[get_config] = lambda: make_cfg()
    app.dependency_overrides[get_db] = lambda: db_module.connect(":memory:")
    client = TestClient(app)
    resp = client.get("/signals/export")
    assert resp.status_code == 401


def test_signals_export_returns_summary_and_distribution():
    conn = db_module.connect(":memory:")
    db_module.insert_signal(conn, "long", 100.0, "2026-09-25T10:15:00+00:00", 100.1, 99.9)
    db_module.update_signal(conn, 1, 3, "TP3_FULL", "2026-09-25T10:40:00+00:00", 100.5, 100.3)
    db_module.insert_signal(conn, "short", 100.0, "2026-09-25T11:15:00+00:00", 100.1, 99.9)
    db_module.update_signal(conn, 2, 0, "SL_ONLY", "2026-09-25T11:20:00+00:00", 100.2, 99.8)
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
