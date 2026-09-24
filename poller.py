from imap_client import ImapClient
from mail import parse_alert_email
from telegram import send_telegram_message
import db


def poll_once(
    conn,
    imap_host: str,
    gmail_user: str,
    gmail_app_password: str,
    tv_sender: str,
    tg_bot_token: str,
    tg_chat_id: str,
) -> None:
    try:
        with ImapClient(imap_host, gmail_user, gmail_app_password) as client:
            emails = client.fetch_unseen(tv_sender)
            all_ok = True
            for item in emails:
                subject, body = parse_alert_email(item.raw)
                text = f"\U0001F4C8 {subject}\n\n{body[:500]}"
                ok, error = send_telegram_message(tg_bot_token, tg_chat_id, text)
                db.insert_alert(conn, subject, body[:500], ok, error)
                if ok:
                    client.mark_seen(item.uid)
                else:
                    all_ok = False
        db.record_poll(conn, success=all_ok)
    except Exception:
        db.record_poll(conn, success=False)
