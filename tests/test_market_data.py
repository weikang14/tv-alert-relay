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


def test_fetch_recent_bars_redacts_api_key_from_http_error(monkeypatch):
    # requests' HTTPError text includes the request URL, which embeds apikey=...
    def fake_get(*a, **k):
        raise requests.HTTPError(
            "429 Client Error: Too Many Requests for url: "
            "https://api.twelvedata.com/time_series?symbol=XAU%2FUSD&apikey=secret-td-key"
        )
    monkeypatch.setattr(requests, "get", fake_get)
    with pytest.raises(MarketDataError) as exc_info:
        fetch_recent_bars("secret-td-key", "XAU/USD", 200)
    assert "secret-td-key" not in str(exc_info.value)
    assert "<apikey>" in str(exc_info.value)


def test_fetch_recent_bars_returns_empty_list_when_no_values(monkeypatch):
    monkeypatch.setattr(requests, "get", lambda *a, **k: FakeResponse({"status": "ok", "values": []}))
    assert fetch_recent_bars("key", "XAU/USD", 200) == []


def test_fetch_recent_bars_raises_on_malformed_row(monkeypatch):
    payload = {"status": "ok", "values": [{"datetime": "2026-09-25 10:01:00", "open": "1"}]}
    monkeypatch.setattr(requests, "get", lambda *a, **k: FakeResponse(payload))
    with pytest.raises(MarketDataError):
        fetch_recent_bars("key", "XAU/USD", 200)


def test_fetch_recent_bars_raises_on_null_field_in_row(monkeypatch):
    # Test that None values in OHLC fields are caught and raise MarketDataError
    payload = {
        "status": "ok",
        "values": [
            {"datetime": "2026-09-25 10:01:00", "open": None, "high": "1", "low": "1", "close": "1"}
        ],
    }
    monkeypatch.setattr(requests, "get", lambda *a, **k: FakeResponse(payload))
    with pytest.raises(MarketDataError):
        fetch_recent_bars("key", "XAU/USD", 200)


def test_fetch_recent_bars_raises_on_null_datetime(monkeypatch):
    # Test that None datetime is caught and raises MarketDataError
    payload = {
        "status": "ok",
        "values": [
            {"datetime": None, "open": "1", "high": "1", "low": "1", "close": "1"}
        ],
    }
    monkeypatch.setattr(requests, "get", lambda *a, **k: FakeResponse(payload))
    with pytest.raises(MarketDataError):
        fetch_recent_bars("key", "XAU/USD", 200)


def test_fetch_recent_bars_raises_on_null_response_body(monkeypatch):
    # Test that a null JSON response body is caught and raises MarketDataError
    monkeypatch.setattr(requests, "get", lambda *a, **k: FakeResponse(None))
    with pytest.raises(MarketDataError):
        fetch_recent_bars("key", "XAU/USD", 200)
