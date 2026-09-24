import telegram as telegram_module


class FakeResponse:
    def __init__(self, ok: bool):
        self._ok = ok

    def raise_for_status(self):
        if not self._ok:
            raise telegram_module.requests.RequestException("boom")


def test_send_telegram_message_success(monkeypatch):
    monkeypatch.setattr(telegram_module.requests, "post", lambda *a, **kw: FakeResponse(ok=True))
    ok, error = telegram_module.send_telegram_message("tok", "chat", "hi")
    assert ok is True
    assert error is None


def test_send_telegram_message_failure(monkeypatch):
    def raise_post(*a, **kw):
        raise telegram_module.requests.RequestException("network down")

    monkeypatch.setattr(telegram_module.requests, "post", raise_post)
    ok, error = telegram_module.send_telegram_message("tok", "chat", "hi")
    assert ok is False
    assert "network down" in error
