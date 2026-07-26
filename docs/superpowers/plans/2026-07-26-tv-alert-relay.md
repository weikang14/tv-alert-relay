# TradingView 免费版警报 → Telegram 推送中转服务 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 一个独立 Python 服务,轮询 Gmail 里的 TradingView 警报邮件,转发到 Telegram,记录历史,并对外暴露一个健康检查端点供外部心跳监控。

**Architecture:** FastAPI 常驻进程,启动时(lifespan)拉起一个后台 asyncio 循环,每 N 秒做一次 IMAP 轮询 → 解析 → Telegram 推送 → 写 SQLite。同一个进程提供 `/`(Basic Auth 看板)和 `/healthz`(供外部 GitHub Actions 定时探测)。部署为 Oracle VM 上第二个独立 systemd 服务,和 `goldbot` 零代码共享。

**Tech Stack:** Python 3.11+、FastAPI + uvicorn、stdlib `imaplib`/`email`(不额外引入 IMAP 库)、stdlib `sqlite3`、`requests`(Telegram HTTP 调用)、Jinja2(看板模板)、pytest。

## Global Constraints

- 代码风格:flat 文件布局(不用 `app/` 包 + 相对导入),仿照 `TradingBot/goldbot` 现有写法,`main.py` 直接 `python main.py` 可跑,避免 systemd `ExecStart` 下相对导入报错。
- 不引入 Gmail/IMAP 第三方库、不引入任务调度框架(APScheduler 等)——`asyncio.sleep` 循环够用。
- 数据库是看板展示缓存,不是唯一数据源(Telegram 聊天记录才是),丢失可接受,不做额外持久化保障。
- 所有密钥(Gmail 应用专用密码、Telegram Bot Token、网页账号密码)只走环境变量(`.env` + systemd `EnvironmentFile`),不进代码库。
- `HEALTHZ_SHARED_SECRET` 可选:配置了就要求请求带 `X-Healthz-Secret` 头,不配置则 `/healthz` 无鉴权。

---

## Task 1: 项目骨架 + 配置加载

**Files:**
- Create: `requirements.txt`
- Create: `.gitignore`
- Create: `conftest.py`
- Create: `config.py`
- Test: `tests/test_config.py`

**Interfaces:**
- Produces: `Config`(dataclass,字段:`gmail_user, gmail_app_password, tg_bot_token, tg_chat_id, web_user, web_password, db_path, tv_sender, poll_interval_seconds, healthz_shared_secret`)、`load_config() -> Config`

- [ ] **Step 1: 建目录和依赖清单**

`requirements.txt`:
```
fastapi
uvicorn[standard]
jinja2
requests
pytest
```

`.gitignore`:
```
.env
*.db
__pycache__/
*.pyc
venv/
.pytest_cache/
```

`conftest.py`(空文件,让 pytest 把仓库根目录加进 `sys.path`,flat 模块才能被 `tests/` 下的用例 `import`):
```python
```

- [ ] **Step 2: 写失败的配置测试**

`tests/test_config.py`:
```python
import pytest
import config as config_module

REQUIRED_VARS = {
    "GMAIL_USER": "user@gmail.com",
    "GMAIL_APP_PASSWORD": "secret",
    "TG_BOT_TOKEN": "tok",
    "TG_CHAT_ID": "chat",
    "WEB_USER": "admin",
    "WEB_PASSWORD": "pw",
}


def test_load_config_reads_required_vars(monkeypatch):
    for k, v in REQUIRED_VARS.items():
        monkeypatch.setenv(k, v)
    cfg = config_module.load_config()
    assert cfg.gmail_user == "user@gmail.com"
    assert cfg.poll_interval_seconds == 300
    assert cfg.tv_sender == "noreply@tradingview.com"
    assert cfg.healthz_shared_secret == ""


def test_load_config_raises_when_missing_required(monkeypatch):
    for k in REQUIRED_VARS:
        monkeypatch.delenv(k, raising=False)
    with pytest.raises(RuntimeError):
        config_module.load_config()
```

- [ ] **Step 3: 运行测试,确认失败**

Run: `pytest tests/test_config.py -v`
Expected: FAIL,`ModuleNotFoundError: No module named 'config'`

- [ ] **Step 4: 实现 config.py**

```python
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
        db_path=os.environ.get("DB_PATH", "tvalert.db"),
        tv_sender=os.environ.get("TV_SENDER", "noreply@tradingview.com"),
        poll_interval_seconds=int(os.environ.get("POLL_INTERVAL_SECONDS", "300")),
        healthz_shared_secret=os.environ.get("HEALTHZ_SHARED_SECRET", ""),
    )
```

- [ ] **Step 5: 运行测试,确认通过**

Run: `pytest tests/test_config.py -v`
Expected: PASS(2 passed)

- [ ] **Step 6: 提交**

```bash
git add requirements.txt .gitignore conftest.py config.py tests/test_config.py
git commit -m "feat: 项目骨架 + 环境变量配置加载"
```

---

## Task 2: 健康判定纯逻辑

**Files:**
- Create: `health.py`
- Test: `tests/test_health.py`

**Interfaces:**
- Consumes: 无(纯函数,无依赖)
- Produces: `compute_health(last_poll_at: datetime | None, consecutive_errors: int, now: datetime, poll_interval_seconds: int, error_threshold: int = 3) -> bool`

- [ ] **Step 1: 写失败的测试**

`tests/test_health.py`:
```python
from datetime import datetime, timedelta, timezone
from health import compute_health

NOW = datetime(2026, 7, 26, 12, 0, 0, tzinfo=timezone.utc)


def test_unhealthy_when_never_polled():
    assert compute_health(None, 0, NOW, 300) is False


def test_healthy_within_two_intervals():
    last_poll = NOW - timedelta(seconds=600)
    assert compute_health(last_poll, 0, NOW, 300) is True


def test_unhealthy_past_two_intervals():
    last_poll = NOW - timedelta(seconds=601)
    assert compute_health(last_poll, 0, NOW, 300) is False


def test_unhealthy_at_error_threshold():
    assert compute_health(NOW, 3, NOW, 300) is False


def test_healthy_below_error_threshold():
    assert compute_health(NOW, 2, NOW, 300) is True
```

- [ ] **Step 2: 运行测试,确认失败**

Run: `pytest tests/test_health.py -v`
Expected: FAIL,`ModuleNotFoundError: No module named 'health'`

- [ ] **Step 3: 实现 health.py**

```python
from datetime import datetime, timedelta


def compute_health(
    last_poll_at: datetime | None,
    consecutive_errors: int,
    now: datetime,
    poll_interval_seconds: int,
    error_threshold: int = 3,
) -> bool:
    if last_poll_at is None:
        return False
    if now - last_poll_at > timedelta(seconds=poll_interval_seconds * 2):
        return False
    if consecutive_errors >= error_threshold:
        return False
    return True
```

- [ ] **Step 4: 运行测试,确认通过**

Run: `pytest tests/test_health.py -v`
Expected: PASS(5 passed)

- [ ] **Step 5: 提交**

```bash
git add health.py tests/test_health.py
git commit -m "feat: 健康判定纯逻辑(轮询新鲜度 + 连续错误次数)"
```

---

## Task 3: 警报邮件解析

**Files:**
- Create: `mail.py`
- Test: `tests/test_mail.py`

**Interfaces:**
- Consumes: 无(纯函数)
- Produces: `parse_alert_email(raw: bytes) -> tuple[str, str]`(返回 `(subject, body)`)

- [ ] **Step 1: 写失败的测试**

`tests/test_mail.py`:
```python
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
```

- [ ] **Step 2: 运行测试,确认失败**

Run: `pytest tests/test_mail.py -v`
Expected: FAIL,`ModuleNotFoundError: No module named 'mail'`

- [ ] **Step 3: 实现 mail.py**

```python
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
```

- [ ] **Step 4: 运行测试,确认通过**

Run: `pytest tests/test_mail.py -v`
Expected: PASS(2 passed)

- [ ] **Step 5: 提交**

```bash
git add mail.py tests/test_mail.py
git commit -m "feat: 警报邮件解析(主题+正文,优先纯文本)"
```

---

## Task 4: SQLite 存储层

**Files:**
- Create: `db.py`
- Test: `tests/test_db.py`

**Interfaces:**
- Consumes: 无
- Produces: `connect(db_path: str) -> sqlite3.Connection`、`insert_alert(conn, subject: str, body_snippet: str, sent_ok: bool, error: str | None) -> None`、`recent_alerts(conn, limit: int = 50) -> list[sqlite3.Row]`、`record_poll(conn, success: bool) -> None`、`get_status(conn) -> sqlite3.Row`

- [ ] **Step 1: 写失败的测试**

`tests/test_db.py`:
```python
import db


def test_connect_creates_tables_and_default_status():
    conn = db.connect(":memory:")
    status = db.get_status(conn)
    assert status["consecutive_errors"] == 0
    assert status["last_poll_at"] is None


def test_insert_and_recent_alerts_most_recent_first():
    conn = db.connect(":memory:")
    db.insert_alert(conn, "Subj", "Body", True, None)
    db.insert_alert(conn, "Subj2", "Body2", False, "err")
    rows = db.recent_alerts(conn, limit=10)
    assert len(rows) == 2
    assert rows[0]["subject"] == "Subj2"
    assert rows[0]["sent_ok"] == 0
    assert rows[0]["error"] == "err"


def test_record_poll_success_resets_consecutive_errors():
    conn = db.connect(":memory:")
    db.record_poll(conn, success=False)
    db.record_poll(conn, success=False)
    assert db.get_status(conn)["consecutive_errors"] == 2

    db.record_poll(conn, success=True)
    status = db.get_status(conn)
    assert status["consecutive_errors"] == 0
    assert status["last_success_at"] is not None
    assert status["last_poll_at"] is not None
```

- [ ] **Step 2: 运行测试,确认失败**

Run: `pytest tests/test_db.py -v`
Expected: FAIL,`ModuleNotFoundError: No module named 'db'`

- [ ] **Step 3: 实现 db.py**

```python
import sqlite3
from datetime import datetime, timezone

SCHEMA = """
CREATE TABLE IF NOT EXISTS alerts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    received_at TEXT NOT NULL,
    subject TEXT NOT NULL,
    body_snippet TEXT NOT NULL,
    sent_ok INTEGER NOT NULL,
    error TEXT
);

CREATE TABLE IF NOT EXISTS status (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    last_poll_at TEXT,
    last_success_at TEXT,
    consecutive_errors INTEGER NOT NULL DEFAULT 0
);
"""


def connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    conn.execute("INSERT OR IGNORE INTO status (id, consecutive_errors) VALUES (1, 0)")
    conn.commit()
    return conn


def insert_alert(conn: sqlite3.Connection, subject: str, body_snippet: str, sent_ok: bool, error: str | None) -> None:
    conn.execute(
        "INSERT INTO alerts (received_at, subject, body_snippet, sent_ok, error) VALUES (?, ?, ?, ?, ?)",
        (datetime.now(timezone.utc).isoformat(), subject, body_snippet, int(sent_ok), error),
    )
    conn.commit()


def recent_alerts(conn: sqlite3.Connection, limit: int = 50) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM alerts ORDER BY id DESC LIMIT ?", (limit,)
    ).fetchall()


def record_poll(conn: sqlite3.Connection, success: bool) -> None:
    now = datetime.now(timezone.utc).isoformat()
    if success:
        conn.execute(
            "UPDATE status SET last_poll_at = ?, last_success_at = ?, consecutive_errors = 0 WHERE id = 1",
            (now, now),
        )
    else:
        conn.execute(
            "UPDATE status SET last_poll_at = ?, consecutive_errors = consecutive_errors + 1 WHERE id = 1",
            (now,),
        )
    conn.commit()


def get_status(conn: sqlite3.Connection) -> sqlite3.Row:
    return conn.execute("SELECT * FROM status WHERE id = 1").fetchone()
```

- [ ] **Step 4: 运行测试,确认通过**

Run: `pytest tests/test_db.py -v`
Expected: PASS(3 passed)

- [ ] **Step 5: 提交**

```bash
git add db.py tests/test_db.py
git commit -m "feat: SQLite 存储层(alerts 历史 + status 心跳)"
```

---

## Task 5: Telegram 发送

**Files:**
- Create: `telegram.py`
- Test: `tests/test_telegram.py`

**Interfaces:**
- Consumes: 无(内部用 `requests`)
- Produces: `send_telegram_message(bot_token: str, chat_id: str, text: str) -> tuple[bool, str | None]`

- [ ] **Step 1: 写失败的测试**

`tests/test_telegram.py`:
```python
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
```

- [ ] **Step 2: 运行测试,确认失败**

Run: `pytest tests/test_telegram.py -v`
Expected: FAIL,`ModuleNotFoundError: No module named 'telegram'`

- [ ] **Step 3: 实现 telegram.py**

```python
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
        return False, str(e)
```

- [ ] **Step 4: 运行测试,确认通过**

Run: `pytest tests/test_telegram.py -v`
Expected: PASS(2 passed)

- [ ] **Step 5: 提交**

```bash
git add telegram.py tests/test_telegram.py
git commit -m "feat: Telegram 消息发送"
```

---

## Task 6: IMAP 客户端

**Files:**
- Create: `imap_client.py`
- Test: `tests/test_imap_client.py`

**Interfaces:**
- Consumes: 无(内部用 stdlib `imaplib`)
- Produces: `GMAIL_IMAP_HOST` 常量(`"imap.gmail.com"`)、`FetchedEmail`(dataclass:`uid: bytes, raw: bytes`)、`ImapClient(host, user, password)`(上下文管理器,方法 `fetch_unseen(sender_filter: str) -> list[FetchedEmail]`、`mark_seen(uid: bytes) -> None`)

- [ ] **Step 1: 写失败的测试**

`tests/test_imap_client.py`:
```python
import imap_client as imap_client_module
from imap_client import ImapClient


class FakeIMAP:
    def __init__(self, host):
        self.host = host
        self.store_calls = []

    def login(self, user, password):
        return "OK", [b"success"]

    def select(self, mailbox):
        return "OK", [b"1"]

    def uid(self, command, *args):
        if command == "search":
            return "OK", [b"101 102"]
        if command == "fetch":
            uid = args[0]
            return "OK", [(b"1 (RFC822 {3}", b"raw-" + uid)]
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
    assert [e.uid for e in emails] == [b"101", b"102"]
    assert emails[0].raw == b"raw-101"


def test_mark_seen_calls_store(monkeypatch):
    monkeypatch.setattr(imap_client_module.imaplib, "IMAP4_SSL", FakeIMAP)
    with ImapClient("imap.gmail.com", "user", "pass") as client:
        client.mark_seen(b"101")
        assert client._conn.store_calls == [(b"101", "+FLAGS", "\\Seen")]
```

- [ ] **Step 2: 运行测试,确认失败**

Run: `pytest tests/test_imap_client.py -v`
Expected: FAIL,`ModuleNotFoundError: No module named 'imap_client'`

- [ ] **Step 3: 实现 imap_client.py**

```python
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
```

- [ ] **Step 4: 运行测试,确认通过**

Run: `pytest tests/test_imap_client.py -v`
Expected: PASS(2 passed)

- [ ] **Step 5: 提交**

```bash
git add imap_client.py tests/test_imap_client.py
git commit -m "feat: IMAP 客户端(拉未读邮件 + 标记已读)"
```

---

## Task 7: 轮询编排

**Files:**
- Create: `poller.py`
- Test: `tests/test_poller.py`

**Interfaces:**
- Consumes: `ImapClient`、`GMAIL_IMAP_HOST`、`FetchedEmail`(imap_client.py)、`parse_alert_email`(mail.py)、`send_telegram_message`(telegram.py)、`db.insert_alert`、`db.record_poll`(db.py)
- Produces: `poll_once(conn, imap_host: str, gmail_user: str, gmail_app_password: str, tv_sender: str, tg_bot_token: str, tg_chat_id: str) -> None`

- [ ] **Step 1: 写失败的测试**

`tests/test_poller.py`:
```python
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
```

- [ ] **Step 2: 运行测试,确认失败**

Run: `pytest tests/test_poller.py -v`
Expected: FAIL,`ModuleNotFoundError: No module named 'poller'`

- [ ] **Step 3: 实现 poller.py**

```python
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
```

- [ ] **Step 4: 运行测试,确认通过**

Run: `pytest tests/test_poller.py -v`
Expected: PASS(3 passed)

- [ ] **Step 5: 提交**

```bash
git add poller.py tests/test_poller.py
git commit -m "feat: 轮询编排(IMAP→解析→Telegram→记录),异常不让循环崩掉"
```

---

## Task 8: FastAPI 应用(看板 + 健康检查)

**Files:**
- Create: `main.py`
- Create: `templates/dashboard.html`
- Test: `tests/test_main.py`

**Interfaces:**
- Consumes: `Config`、`load_config`(config.py)、`db.connect`、`db.get_status`、`db.recent_alerts`、`db.record_poll`(db.py)、`compute_health`(health.py)、`GMAIL_IMAP_HOST`(imap_client.py)、`poll_once`(poller.py)
- Produces: FastAPI `app`,依赖函数 `get_config`、`get_db`(供测试用 `app.dependency_overrides` 替换)

- [ ] **Step 1: 写看板模板**

`templates/dashboard.html`:
```html
<!doctype html>
<html>
<head><meta charset="utf-8"><title>TradingView Alert Relay</title></head>
<body>
  <h1>TradingView Alert Relay</h1>
  {% if healthy %}
    <p style="color:green">● healthy — last poll: {{ status['last_poll_at'] }}</p>
  {% else %}
    <p style="color:red">● unhealthy — last poll: {{ status['last_poll_at'] }}, consecutive errors: {{ status['consecutive_errors'] }}</p>
  {% endif %}
  <table border="1" cellpadding="6">
    <tr><th>Time</th><th>Subject</th><th>Sent</th></tr>
    {% for a in alerts %}
    <tr>
      <td>{{ a['received_at'] }}</td>
      <td>{{ a['subject'] }}</td>
      <td>{{ '✅' if a['sent_ok'] else '❌ ' ~ (a['error'] or '') }}</td>
    </tr>
    {% endfor %}
  </table>
</body>
</html>
```

- [ ] **Step 2: 写失败的测试**

`tests/test_main.py`:
```python
import pytest
from fastapi.testclient import TestClient

import db as db_module
from config import Config
from main import app, get_config, get_db


def make_cfg(**overrides):
    base = dict(
        gmail_user="u", gmail_app_password="p", tg_bot_token="t", tg_chat_id="c",
        web_user="admin", web_password="secret", db_path=":memory:",
        tv_sender="noreply@tradingview.com", poll_interval_seconds=300,
        healthz_shared_secret="",
    )
    base.update(overrides)
    return Config(**base)


@pytest.fixture(autouse=True)
def clear_overrides():
    yield
    app.dependency_overrides.clear()


def test_dashboard_requires_auth():
    app.dependency_overrides[get_config] = lambda: make_cfg()
    app.dependency_overrides[get_db] = lambda: db_module.connect(":memory:")
    client = TestClient(app)
    resp = client.get("/")
    assert resp.status_code == 401


def test_dashboard_ok_with_auth():
    app.dependency_overrides[get_config] = lambda: make_cfg()
    app.dependency_overrides[get_db] = lambda: db_module.connect(":memory:")
    client = TestClient(app)
    resp = client.get("/", auth=("admin", "secret"))
    assert resp.status_code == 200
    assert "TradingView" in resp.text


def test_healthz_unhealthy_before_first_poll():
    app.dependency_overrides[get_config] = lambda: make_cfg()
    app.dependency_overrides[get_db] = lambda: db_module.connect(":memory:")
    client = TestClient(app)
    resp = client.get("/healthz")
    assert resp.status_code == 500


def test_healthz_healthy_after_success():
    conn = db_module.connect(":memory:")
    db_module.record_poll(conn, success=True)
    app.dependency_overrides[get_config] = lambda: make_cfg()
    app.dependency_overrides[get_db] = lambda: conn
    client = TestClient(app)
    resp = client.get("/healthz")
    assert resp.status_code == 200


def test_healthz_requires_secret_when_configured():
    conn = db_module.connect(":memory:")
    db_module.record_poll(conn, success=True)
    app.dependency_overrides[get_config] = lambda: make_cfg(healthz_shared_secret="s3cret")
    app.dependency_overrides[get_db] = lambda: conn
    client = TestClient(app)
    resp = client.get("/healthz")
    assert resp.status_code == 403
    resp2 = client.get("/healthz", headers={"X-Healthz-Secret": "s3cret"})
    assert resp2.status_code == 200
```

- [ ] **Step 3: 运行测试,确认失败**

Run: `pytest tests/test_main.py -v`
Expected: FAIL,`ModuleNotFoundError: No module named 'main'`

- [ ] **Step 4: 实现 main.py**

```python
import asyncio
import secrets
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.templating import Jinja2Templates

import db as db_module
from config import Config, load_config
from health import compute_health
from imap_client import GMAIL_IMAP_HOST
from poller import poll_once

templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
security = HTTPBasic()


def get_config() -> Config:
    raise RuntimeError("get_config dependency not overridden")


def get_db():
    raise RuntimeError("get_db dependency not overridden")


async def _poll_loop(cfg: Config, conn) -> None:
    while True:
        await asyncio.to_thread(
            poll_once, conn, GMAIL_IMAP_HOST, cfg.gmail_user, cfg.gmail_app_password,
            cfg.tv_sender, cfg.tg_bot_token, cfg.tg_chat_id,
        )
        await asyncio.sleep(cfg.poll_interval_seconds)


@asynccontextmanager
async def lifespan(app: FastAPI):
    cfg = load_config()
    conn = db_module.connect(cfg.db_path)
    app.dependency_overrides[get_config] = lambda: cfg
    app.dependency_overrides[get_db] = lambda: conn
    task = asyncio.create_task(_poll_loop(cfg, conn))
    yield
    task.cancel()
    conn.close()


app = FastAPI(lifespan=lifespan)


def check_auth(credentials: HTTPBasicCredentials = Depends(security), cfg: Config = Depends(get_config)) -> None:
    ok_user = secrets.compare_digest(credentials.username, cfg.web_user)
    ok_pass = secrets.compare_digest(credentials.password, cfg.web_password)
    if not (ok_user and ok_pass):
        raise HTTPException(status_code=401, detail="Unauthorized", headers={"WWW-Authenticate": "Basic"})


def _parse_dt(value):
    return datetime.fromisoformat(value) if value else None


@app.get("/", response_class=HTMLResponse)
def dashboard(
    request: Request,
    cfg: Config = Depends(get_config),
    conn=Depends(get_db),
    _auth: None = Depends(check_auth),
):
    status = db_module.get_status(conn)
    healthy = compute_health(
        _parse_dt(status["last_poll_at"]), status["consecutive_errors"],
        datetime.now(timezone.utc), cfg.poll_interval_seconds,
    )
    alerts = db_module.recent_alerts(conn, limit=50)
    return templates.TemplateResponse(
        "dashboard.html",
        {"request": request, "healthy": healthy, "status": status, "alerts": alerts},
    )


@app.get("/healthz")
def healthz(request: Request, cfg: Config = Depends(get_config), conn=Depends(get_db)):
    if cfg.healthz_shared_secret:
        if request.headers.get("X-Healthz-Secret") != cfg.healthz_shared_secret:
            raise HTTPException(status_code=403, detail="Forbidden")
    status = db_module.get_status(conn)
    healthy = compute_health(
        _parse_dt(status["last_poll_at"]), status["consecutive_errors"],
        datetime.now(timezone.utc), cfg.poll_interval_seconds,
    )
    body = {
        "healthy": healthy,
        "last_poll_at": status["last_poll_at"],
        "consecutive_errors": status["consecutive_errors"],
    }
    return JSONResponse(body, status_code=200 if healthy else 500)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8788)
```

- [ ] **Step 5: 运行测试,确认通过**

Run: `pytest tests/test_main.py -v`
Expected: PASS(5 passed)

- [ ] **Step 6: 跑全量测试确认没有互相破坏**

Run: `pytest -v`
Expected: 全部 PASS(config/health/mail/db/telegram/imap_client/poller/main 累计 22 个用例)

- [ ] **Step 7: 提交**

```bash
git add main.py templates/dashboard.html tests/test_main.py
git commit -m "feat: FastAPI 应用(Basic Auth 看板 + /healthz,进程内轮询循环)"
```

---

## Task 9: 部署配置(复用现有 Oracle VM)

**Files:**
- Create: `.env.example`
- Create: `deploy/tvalert.service`
- Create: `deploy/Caddyfile.snippet`
- Create: `docs/DEPLOY.md`

**Interfaces:**
- Consumes: `main.py` 的 `python main.py` 启动方式、`config.py` 读取的全部环境变量名

- [ ] **Step 1: 写 .env.example**

```
GMAIL_USER=youraccount@gmail.com
GMAIL_APP_PASSWORD=your-16-char-app-password
TG_BOT_TOKEN=123456:your-bot-token
TG_CHAT_ID=your-chat-id
WEB_USER=admin
WEB_PASSWORD=change-me
DB_PATH=/opt/tvalert/tvalert.db
TV_SENDER=noreply@tradingview.com
POLL_INTERVAL_SECONDS=300
HEALTHZ_SHARED_SECRET=
```

- [ ] **Step 2: 写 systemd unit(仿 goldbot.service)**

`deploy/tvalert.service`:
```ini
[Unit]
Description=TradingView alert relay to Telegram
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=tvalert
WorkingDirectory=/opt/tvalert/src/tv-alert-relay
ExecStart=/opt/tvalert/venv/bin/python /opt/tvalert/src/tv-alert-relay/main.py
Restart=always
RestartSec=10
EnvironmentFile=/opt/tvalert/.env

[Install]
WantedBy=multi-user.target
```

- [ ] **Step 3: 写 Caddy 反代片段**

`deploy/Caddyfile.snippet`:
```
tvalert.<你的duckdns前缀>.duckdns.org {
    reverse_proxy 127.0.0.1:8788
}
```

- [ ] **Step 4: 写部署文档**

`docs/DEPLOY.md`:
```markdown
# tv-alert-relay 部署(复用现有 Oracle Cloud VM)

前提:goldbot 已经在同一台 VM 上跑着,VM、Caddy、DuckDNS 账号都已就绪。这里
只加第二个独立服务,不改动 goldbot 任何东西(不同用户、不同目录、不同端口、
不同数据库)。

## 1. 建专属系统用户 + 目录

    sudo useradd -r -m -d /opt/tvalert tvalert
    sudo -u tvalert git clone <repo> /opt/tvalert/src/tv-alert-relay
    cd /opt/tvalert && sudo -u tvalert python3 -m venv venv
    sudo -u tvalert venv/bin/pip install -r src/tv-alert-relay/requirements.txt

## 2. Gmail 应用专用密码

Google 账号 → 安全性 → 两步验证(需先开启)→ 应用专用密码 → 生成一个,
填进下一步的 `.env`。

## 3. Telegram Bot

Telegram 里找 `@BotFather` → `/newbot` 建一个新 bot(跟 goldbot 用的不是同
一个,两边推送物理隔离),拿到 token;给 bot 发条消息后访问
`https://api.telegram.org/bot<token>/getUpdates` 找 `chat_id`。

## 4. 配置

    sudo -u tvalert cp src/tv-alert-relay/.env.example /opt/tvalert/.env
    # 编辑 /opt/tvalert/.env,填 Gmail/Telegram/网页账号密码等真实值
    sudo -u tvalert chmod 600 /opt/tvalert/.env

## 5. 起服务

    sudo cp src/tv-alert-relay/deploy/tvalert.service /etc/systemd/system/
    sudo systemctl daemon-reload && sudo systemctl enable --now tvalert
    journalctl -u tvalert -f   # 看到轮询日志正常 = 部署成功

## 6. 网站(看板)

1. DuckDNS 建一个新子域(比如 `tvalert.<你的前缀>.duckdns.org`),指到同一
   个 VPS 公网 IP(跟 goldbot 用同一个 IP 即可)。
2. 把 `deploy/Caddyfile.snippet` 的内容加进 `/etc/caddy/Caddyfile`(goldbot
   已有的那段保留不动,新增这一段)。
3. `sudo systemctl reload caddy`,首次访问自动签 Let's Encrypt 证书。
4. 浏览器访问 `https://tvalert.<你的前缀>.duckdns.org`,会弹 Basic Auth
   登录框,填 `.env` 里的 `WEB_USER`/`WEB_PASSWORD`。

## 7. TradingView 侧

建警报时在 Notifications 里勾选 "Send Email",不用配置别的——这个服务会
自动去 Gmail 里捞 TradingView 发来的邮件。

## 8. 心跳监控(GitHub Actions)

见仓库 `.github/workflows/heartbeat.yml` 和其配套的仓库 Secrets 设置说明。
```

- [ ] **Step 5: 手动验证(无法用 pytest 覆盖的部分,记录验证方式而非现在执行)**

验证清单(实际部署到 VM 后手动跑):
- `sudo systemctl status tvalert` 显示 `active (running)`
- `curl -u admin:change-me https://tvalert.<前缀>.duckdns.org/` 返回 200 且包含 "TradingView"
- 故意把 `.env` 里 `GMAIL_APP_PASSWORD` 改错重启服务,几分钟后 `curl .../healthz` 应返回 500

- [ ] **Step 6: 提交**

```bash
git add .env.example deploy/tvalert.service deploy/Caddyfile.snippet docs/DEPLOY.md
git commit -m "docs: 部署配置(systemd + Caddy + Gmail/Telegram 设置步骤)"
```

---

## Task 10: GitHub Actions 外部心跳

**Files:**
- Create: `.github/workflows/heartbeat.yml`

**Interfaces:**
- Consumes: 部署好的 `/healthz` URL(仓库 Secret `TVALERT_HEALTHZ_URL`)、可选的 `HEALTHZ_SHARED_SECRET`(仓库 Secret `TVALERT_HEALTHZ_SECRET`)

- [ ] **Step 1: 写 workflow**

`.github/workflows/heartbeat.yml`:
```yaml
name: heartbeat

on:
  schedule:
    - cron: "*/10 * * * *"
  workflow_dispatch: {}

jobs:
  check-healthz:
    runs-on: ubuntu-latest
    steps:
      - name: curl healthz
        run: |
          curl --fail --max-time 10 \
            -H "X-Healthz-Secret: ${{ secrets.TVALERT_HEALTHZ_SECRET }}" \
            "${{ secrets.TVALERT_HEALTHZ_URL }}"
```

- [ ] **Step 2: 配 Secrets(部署完成、拿到真实 URL 后手动做)**

GitHub 仓库 → Settings → Secrets and variables → Actions → New repository secret:
- `TVALERT_HEALTHZ_URL` = `https://tvalert.<你的前缀>.duckdns.org/healthz`
- `TVALERT_HEALTHZ_SECRET` = `.env` 里配的 `HEALTHZ_SHARED_SECRET`(留空则这个 secret 也留空)

- [ ] **Step 3: 手动验证**

推到 GitHub 后,仓库 Actions 标签页 → `heartbeat` → "Run workflow" 手动触发一次:
- 服务健康时:工作流跑绿
- 故意传错 `TVALERT_HEALTHZ_URL` 测一次:工作流跑红,并检查 GitHub 是否按账号通知设置发来失败邮件

- [ ] **Step 4: 提交**

```bash
git add .github/workflows/heartbeat.yml
git commit -m "ci: GitHub Actions 外部心跳,healthz 失败时利用 GitHub 内置失败邮件报警"
```

---

## 完成后的整体验证

1. `pytest -v` 全绿(约 22 个用例)
2. 本地跑 `python main.py`(先 `export` 好全部必需环境变量,`DB_PATH` 用相对路径即可),浏览器访问 `http://127.0.0.1:8788/`,应弹 Basic Auth
3. 用真实 Gmail 测试账号发一封模拟 TradingView 格式的邮件,等一个轮询周期,确认:Telegram 收到推送、看板列表出现新记录、原邮件被标记已读
4. 部署到 Oracle VM 后按 Task 9/10 的手动验证清单跑一遍
