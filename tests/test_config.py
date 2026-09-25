import pytest
import config as config_module

REQUIRED_VARS = {
    "GMAIL_USER": "user@gmail.com",
    "GMAIL_APP_PASSWORD": "secret",
    "TG_BOT_TOKEN": "tok",
    "TG_CHAT_ID": "chat",
    "WEB_USER": "admin",
    "WEB_PASSWORD": "pw",
}


def test_load_config_reads_required_vars(monkeypatch):
    for k, v in REQUIRED_VARS.items():
        monkeypatch.setenv(k, v)
    cfg = config_module.load_config()
    assert cfg.gmail_user == "user@gmail.com"
    assert cfg.poll_interval_seconds == 300
    assert cfg.tv_sender == "noreply@tradingview.com"
    assert cfg.healthz_shared_secret == ""


def test_load_config_raises_when_missing_required(monkeypatch):
    for k in REQUIRED_VARS:
        monkeypatch.delenv(k, raising=False)
    with pytest.raises(RuntimeError):
        config_module.load_config()


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
