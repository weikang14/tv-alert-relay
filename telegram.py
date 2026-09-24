import requests


def send_telegram_message(bot_token: str, chat_id: str, text: str) -> tuple[bool, str | None]:
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{bot_token}/sendMessage",
            json={"chat_id": chat_id, "text": text},
            timeout=10,
        )
        r.raise_for_status()
        return True, None
    except requests.RequestException as e:
        # HTTPError text includes the request URL, which embeds the token.
        return False, str(e).replace(bot_token, "<token>")
