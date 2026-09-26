import os
from dataclasses import dataclass


@dataclass
class Config:
    gmail_user: str
    gmail_app_password: str
    tg_bot_token: str
    tg_chat_id: str
    web_user: str
    web_password: str
    db_path: str
    tv_sender: str
    poll_interval_seconds: int
    healthz_shared_secret: str
    twelve_data_api_key: str
    signal_symbol: str
    database_url: str = ""


def load_config() -> Config:
    def required(name: str) -> str:
        value = os.environ.get(name)
        if not value:
            raise RuntimeError(f"missing required env var: {name}")
        return value

    return Config(
        gmail_user=required("GMAIL_USER"),
        gmail_app_password=required("GMAIL_APP_PASSWORD"),
        tg_bot_token=required("TG_BOT_TOKEN"),
        tg_chat_id=required("TG_CHAT_ID"),
        web_user=required("WEB_USER"),
        web_password=required("WEB_PASSWORD"),
        # `or`, not a .get() default: a set-but-empty var must fall back too
        # (empty TV_SENDER would make the IMAP FROM search match every sender).
        db_path=os.environ.get("DB_PATH") or "tvalert.db",
        tv_sender=os.environ.get("TV_SENDER") or "noreply@tradingview.com",
        poll_interval_seconds=int(os.environ.get("POLL_INTERVAL_SECONDS") or "300"),
        healthz_shared_secret=os.environ.get("HEALTHZ_SHARED_SECRET") or "",
        twelve_data_api_key=os.environ.get("TWELVE_DATA_API_KEY") or "",
        signal_symbol=os.environ.get("SIGNAL_SYMBOL") or "XAU/USD",
        # Empty -> db_path (a local SQLite file) is used instead; see main.py.
        database_url=os.environ.get("DATABASE_URL") or "",
    )
