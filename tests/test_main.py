import pytest
from fastapi.testclient import TestClient

import db as db_module
from config import Config
from main import app, get_config, get_db


def make_cfg(**overrides):
    base = dict(
        gmail_user="u", gmail_app_password="p", tg_bot_token="t", tg_chat_id="c",
        web_user="admin", web_password="secret", db_path=":memory:",
        tv_sender="noreply@tradingview.com", poll_interval_seconds=300,
        healthz_shared_secret="",
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
