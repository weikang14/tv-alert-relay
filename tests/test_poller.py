import poller as poller_module
import db as db_module
from imap_client import FetchedEmail


class FakeImapClient:
    def __init__(self, host, user, password, emails):
        self._emails = emails
        self.marked_seen = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def fetch_unseen(self, sender_filter):
        return self._emails

    def mark_seen(self, uid):
        self.marked_seen.append(uid)


def test_poll_once_sends_and_records_success(monkeypatch):
    conn = db_module.connect(":memory:")
    fake_email = FetchedEmail(uid=b"1", raw=b"raw")
    monkeypatch.setattr(
        poller_module, "ImapClient",
        lambda host, user, pw: FakeImapClient(host, user, pw, [fake_email]),
    )
    monkeypatch.setattr(poller_module, "parse_alert_email", lambda raw: ("Subject A", "Body A"))
    monkeypatch.setattr(poller_module, "send_telegram_message", lambda token, chat, text: (True, None))

    poller_module.poll_once(conn, "imap.gmail.com", "u", "p", "noreply@tradingview.com", "tok", "chat")

    alerts = db_module.recent_alerts(conn)
    assert len(alerts) == 1
    assert alerts[0]["subject"] == "Subject A"
    assert alerts[0]["sent_ok"] == 1
    status = db_module.get_status(conn)
    assert status["consecutive_errors"] == 0
    assert status["last_success_at"] is not None


def test_poll_once_records_failure_when_send_fails(monkeypatch):
    conn = db_module.connect(":memory:")
    fake_email = FetchedEmail(uid=b"1", raw=b"raw")
    monkeypatch.setattr(
        poller_module, "ImapClient",
        lambda host, user, pw: FakeImapClient(host, user, pw, [fake_email]),
    )
    monkeypatch.setattr(poller_module, "parse_alert_email", lambda raw: ("Subject A", "Body A"))
    monkeypatch.setattr(poller_module, "send_telegram_message", lambda token, chat, text: (False, "boom"))

    poller_module.poll_once(conn, "imap.gmail.com", "u", "p", "noreply@tradingview.com", "tok", "chat")

    alerts = db_module.recent_alerts(conn)
    assert alerts[0]["sent_ok"] == 0
    assert alerts[0]["error"] == "boom"
    status = db_module.get_status(conn)
    assert status["consecutive_errors"] == 1


def test_poll_once_survives_imap_exception(monkeypatch):
    conn = db_module.connect(":memory:")

    def raise_connect(host, user, pw):
        raise OSError("imap down")

    monkeypatch.setattr(poller_module, "ImapClient", raise_connect)

    poller_module.poll_once(conn, "imap.gmail.com", "u", "p", "noreply@tradingview.com", "tok", "chat")

    status = db_module.get_status(conn)
    assert status["consecutive_errors"] == 1
