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


app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)


def check_auth(credentials: HTTPBasicCredentials = Depends(security), cfg: Config = Depends(get_config)) -> None:
    # bytes, not str: compare_digest raises TypeError on non-ASCII str
    ok_user = secrets.compare_digest(credentials.username.encode(), cfg.web_user.encode())
    ok_pass = secrets.compare_digest(credentials.password.encode(), cfg.web_password.encode())
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
        request,
        "dashboard.html",
        {"healthy": healthy, "status": status, "alerts": alerts},
    )


@app.get("/healthz")
def healthz(request: Request, cfg: Config = Depends(get_config), conn=Depends(get_db)):
    if cfg.healthz_shared_secret:
        header = request.headers.get("X-Healthz-Secret")
        if header is None or not secrets.compare_digest(header.encode(), cfg.healthz_shared_secret.encode()):
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


@app.post("/poll")
def trigger_poll(
    cfg: Config = Depends(get_config),
    conn=Depends(get_db),
    _auth: None = Depends(check_auth),
):
    poll_once(
        conn, GMAIL_IMAP_HOST, cfg.gmail_user, cfg.gmail_app_password,
        cfg.tv_sender, cfg.tg_bot_token, cfg.tg_chat_id,
    )
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


@app.get("/export")
def export_alerts(conn=Depends(get_db), _auth: None = Depends(check_auth)):
    rows = db_module.recent_alerts(conn, limit=100000)
    return JSONResponse({
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "alerts": [dict(row) for row in rows],
    })


if __name__ == "__main__":
    import logging
    import uvicorn
    logging.basicConfig(level=logging.INFO)
    uvicorn.run(app, host="127.0.0.1", port=8788)
