import imap_client as imap_client_module
from imap_client import ImapClient


class FakeIMAP:
    def __init__(self, host, timeout=None):
        self.host = host
        self.store_calls = []
        self.fetch_calls = []

    def login(self, user, password):
        return "OK", [b"success"]

    def select(self, mailbox):
        return "OK", [b"1"]

    def uid(self, command, *args):
        if command == "search":
            return "OK", [b"101 102"]
        if command == "fetch":
            self.fetch_calls.append(args)
            uid = args[0]
            return "OK", [(b"1 (BODY[] {3}", b"raw-" + uid)]
        if command == "store":
            self.store_calls.append(args)
            return "OK", [b"done"]
        raise ValueError(command)

    def logout(self):
        pass


def test_fetch_unseen_returns_parsed_emails(monkeypatch):
    monkeypatch.setattr(imap_client_module.imaplib, "IMAP4_SSL", FakeIMAP)
    with ImapClient("imap.gmail.com", "user", "pass") as client:
        emails = client.fetch_unseen("noreply@tradingview.com")
        # BODY.PEEK[] must not set \Seen; RFC822/BODY[] would mark read before send succeeds.
        assert client._conn.fetch_calls == [(b"101", "(BODY.PEEK[])"), (b"102", "(BODY.PEEK[])")]
    assert [e.uid for e in emails] == [b"101", b"102"]
    assert emails[0].raw == b"raw-101"


def test_mark_seen_calls_store(monkeypatch):
    monkeypatch.setattr(imap_client_module.imaplib, "IMAP4_SSL", FakeIMAP)
    with ImapClient("imap.gmail.com", "user", "pass") as client:
        client.mark_seen(b"101")
        assert client._conn.store_calls == [(b"101", "+FLAGS", "\\Seen")]
