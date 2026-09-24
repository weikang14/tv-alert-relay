import imaplib
from dataclasses import dataclass

GMAIL_IMAP_HOST = "imap.gmail.com"


@dataclass
class FetchedEmail:
    uid: bytes
    raw: bytes


class ImapClient:
    def __init__(self, host: str, user: str, password: str):
        self._host = host
        self._user = user
        self._password = password
        self._conn = None

    def __enter__(self) -> "ImapClient":
        self._conn = imaplib.IMAP4_SSL(self._host)
        self._conn.login(self._user, self._password)
        self._conn.select("INBOX")
        return self

    def __exit__(self, *exc) -> None:
        if self._conn is not None:
            self._conn.logout()

    def fetch_unseen(self, sender_filter: str) -> list[FetchedEmail]:
        typ, data = self._conn.uid("search", None, "UNSEEN", "FROM", f'"{sender_filter}"')
        if typ != "OK" or not data or not data[0]:
            return []
        results = []
        for uid in data[0].split():
            typ, msg_data = self._conn.uid("fetch", uid, "(RFC822)")
            if typ == "OK" and msg_data and msg_data[0]:
                results.append(FetchedEmail(uid=uid, raw=msg_data[0][1]))
        return results

    def mark_seen(self, uid: bytes) -> None:
        self._conn.uid("store", uid, "+FLAGS", "\\Seen")
