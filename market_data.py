from dataclasses import dataclass
from datetime import datetime, timezone

import requests

TWELVE_DATA_URL = "https://api.twelvedata.com/time_series"


class MarketDataError(Exception):
    pass


@dataclass
class Bar:
    time: datetime
    open: float
    high: float
    low: float
    close: float


def fetch_recent_bars(api_key: str, symbol: str, outputsize: int, interval: str) -> list[Bar]:
    try:
        r = requests.get(
            TWELVE_DATA_URL,
            params={
                "symbol": symbol,
                "interval": interval,
                "outputsize": outputsize,
                "apikey": api_key,
                "timezone": "UTC",
            },
            timeout=15,
        )
        r.raise_for_status()
        data = r.json()
    except requests.RequestException as e:
        # HTTPError text includes the request URL, which embeds the API key.
        raise MarketDataError(str(e).replace(api_key, "<apikey>")) from e

    if not isinstance(data, dict):
        raise MarketDataError(f"unexpected response shape: {data!r}")

    if data.get("status") == "error":
        raise MarketDataError(data.get("message", "unknown Twelve Data error"))

    values = data.get("values") or []
    seen: dict[datetime, Bar] = {}
    for v in values:
        try:
            bar_time = datetime.strptime(v["datetime"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
            seen[bar_time] = Bar(
                time=bar_time,
                open=float(v["open"]),
                high=float(v["high"]),
                low=float(v["low"]),
                close=float(v["close"]),
            )
        except (KeyError, ValueError, TypeError) as e:
            raise MarketDataError(f"malformed bar: {v}") from e

    return sorted(seen.values(), key=lambda b: b.time)
