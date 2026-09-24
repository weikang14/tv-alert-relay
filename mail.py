from email import message_from_bytes
from email.header import decode_header
from email.message import Message


def parse_alert_email(raw: bytes) -> tuple[str, str]:
    msg = message_from_bytes(raw)
    subject = _decode_header(msg.get("Subject", ""))
    body = _extract_body(msg)
    return subject, body


def _decode_header(raw_header: str) -> str:
    decoded = ""
    for text, charset in decode_header(raw_header):
        if isinstance(text, bytes):
            decoded += text.decode(charset or "utf-8", errors="replace")
        else:
            decoded += text
    return decoded


def _extract_body(msg: Message) -> str:
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "text/plain":
                payload = part.get_payload(decode=True)
                if payload:
                    charset = part.get_content_charset() or "utf-8"
                    return payload.decode(charset, errors="replace").strip()
        return ""
    payload = msg.get_payload(decode=True)
    if payload is None:
        return ""
    charset = msg.get_content_charset() or "utf-8"
    return payload.decode(charset, errors="replace").strip()
