from email.message import EmailMessage
import mail


def test_parse_simple_email():
    msg = EmailMessage()
    msg["Subject"] = "XAUUSD Buy Signal"
    msg["From"] = "noreply@tradingview.com"
    msg.set_content("Price crossed above 2400")
    raw = msg.as_bytes()

    subject, body = mail.parse_alert_email(raw)

    assert subject == "XAUUSD Buy Signal"
    assert "Price crossed above 2400" in body


def test_parse_multipart_prefers_plain_text():
    msg = EmailMessage()
    msg["Subject"] = "Alert"
    msg.set_content("plain version")
    msg.add_alternative("<p>html version</p>", subtype="html")
    raw = msg.as_bytes()

    subject, body = mail.parse_alert_email(raw)

    assert subject == "Alert"
    assert "plain version" in body
