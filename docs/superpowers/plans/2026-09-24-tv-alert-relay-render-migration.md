# tv-alert-relay 迁移到 Render + 定期备份 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把 `tv-alert-relay` 从"部署在常驻 Oracle VM 上、进程内定时轮询"改造成"部署在 Render 免费层、外部定时器触发轮询",并新增每周把历史记录备份进 git 仓库的机制。

**Architecture:** 新增 `POST /poll`(外部定时器触发一次真实轮询)和 `GET /export`(导出历史记录用于备份)两个接口,都复用现有的账号密码认证。GitHub Actions 用两个工作流分别驱动这两个接口:一个每 5 分钟触发轮询兼报警,一个每周做一次备份提交。部署方式从 systemd+Caddy+DuckDNS 换成 Render Blueprint。

**Tech Stack:** 不变(Python 3、FastAPI、stdlib、pytest);新增 `render.yaml`(Render Blueprint 格式)。

**Spec:** `docs/superpowers/specs/2026-09-24-tv-alert-relay-render-migration-design.md`

## Global Constraints

- 核心业务逻辑模块不动:`poller.py`、`mail.py`、`telegram.py`、`imap_client.py`、`db.py`、`health.py`、`config.py` 本次不做任何修改。
- `/poll`、`/export` 都必须挂 `Depends(check_auth)`(跟看板同一套 `WEB_USER`/`WEB_PASSWORD`),不引入新的密钥类型。
- GitHub Actions 里 curl 打 Render 的 `--max-time` 用 60 秒,不是原来 `/healthz` 那个 10 秒——Render 免费层容器从休眠唤醒(冷启动)可能要几十秒,10 秒会导致误报失败。
- `poll.yml` 频率 `*/5 * * * *`;`backup.yml` 频率每周日 `0 0 * * 0`。两个工作流共用 3 个仓库 Secrets:`TVALERT_APP_URL`、`TVALERT_WEB_USER`、`TVALERT_WEB_PASSWORD`(仓库层面的配置,不在本计划的代码任务里,由协调者在所有代码任务完成后手动配置)。
- 备份文件是单个会被覆盖的 `backups/alerts.json`,不按日期建多个文件。
- 原 Oracle VM 的部署文档挪到新文件保留,不删除。

## Review Focus

- **`GET /export` 在 `alerts` 表为空时**(比如刚部署、还没收到过警报,备份工作流第一次就跑了):应该返回 `{"exported_at": ..., "alerts": []}`,不能报错或返回 500——测试加在 Task 2。
- **新增的认证路由忘记挂 `Depends(check_auth)`**:这是加认证路由时最容易犯的错(复制粘贴漏了一行),`/poll`、`/export` 各自的"无认证 → 401"测试直接钉住这个失败模式——测试加在 Task 1、Task 2。

---

## Task 1: `POST /poll` 接口

**Files:**
- Modify: `main.py`(在 `/healthz` 路由之后、`if __name__ == "__main__":` 之前插入新路由;当前文件第 102 行是 `/healthz` 函数结尾,第 105 行开始是 `if __name__`)
- Test: `tests/test_main.py`(在文件末尾追加;需要在文件顶部的 import 区新增 `import main as main_module` 和 `from imap_client import GMAIL_IMAP_HOST`,用于 monkeypatch 和断言)

**Interfaces:**
- Consumes:`poll_once(conn, imap_host, gmail_user, gmail_app_password, tv_sender, tg_bot_token, tg_chat_id)`(`poller.py`,已存在)、`GMAIL_IMAP_HOST`(`imap_client.py`,已存在)、`check_auth`/`get_config`/`get_db`/`compute_health`/`_parse_dt`(`main.py`,已存在)、`db_module.get_status(conn)`(已存在)
- Produces:`POST /poll` 路由,返回 JSON `{"healthy": bool, "last_poll_at": str|None, "consecutive_errors": int}`,状态码 200(健康)或 500(不健康)

- [ ] **Step 1: 写失败的测试**

在 `tests/test_main.py` 顶部的 import 区,把:
```python
import db as db_module
from config import Config
from main import app, get_config, get_db
```
改成:
```python
import db as db_module
import main as main_module
from config import Config
from imap_client import GMAIL_IMAP_HOST
from main import app, get_config, get_db
```

在文件末尾追加:
```python
def test_poll_requires_auth():
    app.dependency_overrides[get_config] = lambda: make_cfg()
    app.dependency_overrides[get_db] = lambda: db_module.connect(":memory:")
    client = TestClient(app)
    resp = client.post("/poll")
    assert resp.status_code == 401


def test_poll_triggers_poll_once_and_returns_healthy(monkeypatch):
    conn = db_module.connect(":memory:")
    calls = []

    def fake_poll_once(conn_arg, imap_host, gmail_user, gmail_app_password, tv_sender, tg_bot_token, tg_chat_id):
        calls.append((conn_arg, imap_host, gmail_user, gmail_app_password, tv_sender, tg_bot_token, tg_chat_id))
        db_module.record_poll(conn_arg, success=True)

    monkeypatch.setattr(main_module, "poll_once", fake_poll_once)
    app.dependency_overrides[get_config] = lambda: make_cfg()
    app.dependency_overrides[get_db] = lambda: conn
    client = TestClient(app)
    resp = client.post("/poll", auth=("admin", "secret"))
    assert resp.status_code == 200
    assert len(calls) == 1
    assert calls[0][1] == GMAIL_IMAP_HOST


def test_poll_returns_500_when_unhealthy_after_failures(monkeypatch):
    conn = db_module.connect(":memory:")

    def fake_poll_once(conn_arg, *args):
        db_module.record_poll(conn_arg, success=False)
        db_module.record_poll(conn_arg, success=False)
        db_module.record_poll(conn_arg, success=False)

    monkeypatch.setattr(main_module, "poll_once", fake_poll_once)
    app.dependency_overrides[get_config] = lambda: make_cfg()
    app.dependency_overrides[get_db] = lambda: conn
    client = TestClient(app)
    resp = client.post("/poll", auth=("admin", "secret"))
    assert resp.status_code == 500
```

- [ ] **Step 2: 运行测试,确认失败**

Run: `pytest tests/test_main.py -v -k poll`
Expected: FAIL,404(路由不存在)

- [ ] **Step 3: 实现 `/poll` 路由**

在 `main.py` 的 `/healthz` 路由(第 86-102 行)之后、`if __name__ == "__main__":` 之前插入:

```python
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
```

- [ ] **Step 4: 运行测试,确认通过**

Run: `pytest tests/test_main.py -v`
Expected: 全部 PASS(含之前已有的用例)

- [ ] **Step 5: 提交**

```bash
git add main.py tests/test_main.py
git commit -m "feat: 新增 POST /poll,供外部定时器触发轮询"
```

---

## Task 2: `GET /export` 接口

**Files:**
- Modify: `main.py`(在 `/poll` 路由之后插入)
- Test: `tests/test_main.py`(文件末尾追加)

**Interfaces:**
- Consumes:`db_module.recent_alerts(conn, limit=50)`(已存在,签名支持传更大的 `limit`)、`get_db`/`check_auth`(已存在)
- Produces:`GET /export` 路由,返回 JSON `{"exported_at": str, "alerts": [{"id": int, "received_at": str, "subject": str, "body_snippet": str, "sent_ok": int, "error": str|None}, ...]}`,状态码固定 200

- [ ] **Step 1: 写失败的测试**

在 `tests/test_main.py` 末尾追加:

```python
def test_export_requires_auth():
    app.dependency_overrides[get_config] = lambda: make_cfg()
    app.dependency_overrides[get_db] = lambda: db_module.connect(":memory:")
    client = TestClient(app)
    resp = client.get("/export")
    assert resp.status_code == 401


def test_export_returns_empty_list_when_no_alerts():
    app.dependency_overrides[get_config] = lambda: make_cfg()
    app.dependency_overrides[get_db] = lambda: db_module.connect(":memory:")
    client = TestClient(app)
    resp = client.get("/export", auth=("admin", "secret"))
    assert resp.status_code == 200
    data = resp.json()
    assert data["alerts"] == []
    assert "exported_at" in data


def test_export_returns_all_alerts_as_json():
    conn = db_module.connect(":memory:")
    db_module.insert_alert(conn, "Subj1", "Body1", True, None)
    db_module.insert_alert(conn, "Subj2", "Body2", False, "boom")
    app.dependency_overrides[get_config] = lambda: make_cfg()
    app.dependency_overrides[get_db] = lambda: conn
    client = TestClient(app)
    resp = client.get("/export", auth=("admin", "secret"))
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["alerts"]) == 2
    subjects = {a["subject"] for a in data["alerts"]}
    assert subjects == {"Subj1", "Subj2"}
    errored = [a for a in data["alerts"] if a["subject"] == "Subj2"][0]
    assert errored["sent_ok"] == 0
    assert errored["error"] == "boom"
```

- [ ] **Step 2: 运行测试,确认失败**

Run: `pytest tests/test_main.py -v -k export`
Expected: FAIL,404(路由不存在)

- [ ] **Step 3: 实现 `/export` 路由**

在 `main.py` 里 `/poll` 路由之后插入:

```python
@app.get("/export")
def export_alerts(conn=Depends(get_db), _auth: None = Depends(check_auth)):
    rows = db_module.recent_alerts(conn, limit=100000)
    return JSONResponse({
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "alerts": [dict(row) for row in rows],
    })
```

- [ ] **Step 4: 运行测试,确认通过**

Run: `pytest tests/test_main.py -v`
Expected: 全部 PASS

- [ ] **Step 5: 提交**

```bash
git add main.py tests/test_main.py
git commit -m "feat: 新增 GET /export,导出历史记录供备份用"
```

---

## Task 3: Render 端口绑定 + Blueprint 配置

**Files:**
- Modify: `main.py`(`if __name__ == "__main__":` 块,即文件末尾那 5 行)
- Create: `render.yaml`

**Interfaces:**
- Consumes:无新接口依赖
- Produces:`render.yaml`(Render Blueprint,供 Task 6 的部署文档引用)

- [ ] **Step 1: 修改 `main.py` 的启动块**

把文件末尾的:
```python
if __name__ == "__main__":
    import logging
    import uvicorn
    logging.basicConfig(level=logging.INFO)
    uvicorn.run(app, host="127.0.0.1", port=8788)
```
改成:
```python
if __name__ == "__main__":
    import logging
    import os
    import uvicorn
    logging.basicConfig(level=logging.INFO)
    port = int(os.environ.get("PORT", "8788"))
    uvicorn.run(app, host="0.0.0.0", port=port)
```

- [ ] **Step 2: 写 render.yaml**

```yaml
services:
  - type: web
    name: tv-alert-relay
    runtime: python
    plan: free
    buildCommand: pip install -r requirements.txt
    startCommand: python main.py
    envVars:
      - key: GMAIL_USER
        sync: false
      - key: GMAIL_APP_PASSWORD
        sync: false
      - key: TG_BOT_TOKEN
        sync: false
      - key: TG_CHAT_ID
        sync: false
      - key: WEB_USER
        sync: false
      - key: WEB_PASSWORD
        sync: false
      - key: TV_SENDER
        value: noreply@tradingview.com
      - key: POLL_INTERVAL_SECONDS
        value: "300"
      - key: HEALTHZ_SHARED_SECRET
        sync: false
```

- [ ] **Step 3: 跑一遍全量测试,确认 main.py 改动没弄坏别的**

Run: `pytest -v`
Expected: 全部 PASS(这个改动本身不改变任何被 TestClient 走到的代码路径,`__main__` 块不会被 pytest 执行到)

- [ ] **Step 4: 手动验证端口绑定逻辑**

本地开一个终端,设好必需的环境变量后指定自定义端口跑起来:

```bash
GMAIL_USER=u GMAIL_APP_PASSWORD=p TG_BOT_TOKEN=t TG_CHAT_ID=c \
WEB_USER=admin WEB_PASSWORD=secret PORT=9999 DB_PATH=:memory: \
python main.py
```

另开一个终端:
```bash
curl -i http://127.0.0.1:9999/healthz
```
Expected: 收到 HTTP 响应(不是 "connection refused"),证明监听在 `0.0.0.0:9999` 上生效。验证完 `Ctrl+C` 停掉第一个终端的进程。

- [ ] **Step 5: 提交**

```bash
git add main.py render.yaml
git commit -m "feat: 支持 Render 的 PORT 环境变量 + Blueprint 配置"
```

---

## Task 4: `.github/workflows/poll.yml`(取代 `heartbeat.yml`)

**Files:**
- Delete: `.github/workflows/heartbeat.yml`
- Create: `.github/workflows/poll.yml`

**Interfaces:**
- Consumes:Task 1 的 `POST /poll` 接口契约(200/500,Basic Auth)
- Produces:无(纯 CI 配置,不被其他任务消费)

- [ ] **Step 1: 删除旧文件,写新文件**

```bash
git rm .github/workflows/heartbeat.yml
```

`.github/workflows/poll.yml`:
```yaml
name: poll

on:
  schedule:
    - cron: "*/5 * * * *"
  workflow_dispatch: {}

jobs:
  trigger-poll:
    runs-on: ubuntu-latest
    steps:
      - name: trigger poll
        run: |
          curl --fail --max-time 60 -X POST \
            -u "${{ secrets.TVALERT_WEB_USER }}:${{ secrets.TVALERT_WEB_PASSWORD }}" \
            "${{ secrets.TVALERT_APP_URL }}/poll"
```

- [ ] **Step 2: 自检**

没有 pytest 覆盖这个文件。手动确认:
- 缩进是 2 空格,没有 tab
- `cron: "*/5 * * * *"` 语法合法(5 个字段:分 时 日 月 周)
- `curl` 命令里的三个 secret 名字(`TVALERT_WEB_USER`、`TVALERT_WEB_PASSWORD`、`TVALERT_APP_URL`)跟 Global Constraints 里定义的一致

- [ ] **Step 3: 提交**

```bash
git add .github/workflows/poll.yml
git commit -m "ci: heartbeat.yml 改名重写为 poll.yml,每5分钟触发一次真实轮询"
```

---

## Task 5: `.github/workflows/backup.yml`

**Files:**
- Create: `.github/workflows/backup.yml`

**Interfaces:**
- Consumes:Task 2 的 `GET /export` 接口契约(200,JSON body,Basic Auth)
- Produces:`backups/alerts.json`(仓库里的备份文件,首次运行时由这个工作流自己创建)

- [ ] **Step 1: 写 workflow**

`.github/workflows/backup.yml`:
```yaml
name: backup

on:
  schedule:
    - cron: "0 0 * * 0"
  workflow_dispatch: {}

permissions:
  contents: write

jobs:
  backup:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - name: fetch export
        run: |
          mkdir -p backups
          curl --fail --max-time 60 \
            -u "${{ secrets.TVALERT_WEB_USER }}:${{ secrets.TVALERT_WEB_PASSWORD }}" \
            "${{ secrets.TVALERT_APP_URL }}/export" -o backups/alerts.json
      - name: commit if changed
        run: |
          git config user.name "github-actions[bot]"
          git config user.email "github-actions[bot]@users.noreply.github.com"
          git add backups/alerts.json
          git diff --cached --quiet || git commit -m "chore: weekly alerts backup"
          git push
```

- [ ] **Step 2: 自检**

手动确认:
- `permissions: contents: write` 存在(不然 `git push` 会因为默认只读权限的 `GITHUB_TOKEN` 而失败)
- `git diff --cached --quiet || git commit ...` 这一行保证数据没变化时不产生空提交(`git diff --cached --quiet` 无变化时退出码 0,`||` 右边就不会执行;有变化时退出码 1,才会走 `git commit`)
- cron `"0 0 * * 0"` 是每周日 UTC 0 点(5 个字段:分 时 日 月 周,`0` 在最后一位代表周日)

- [ ] **Step 3: 提交**

```bash
git add .github/workflows/backup.yml
git commit -m "ci: 新增每周备份 alerts 到仓库的工作流"
```

---

## Task 6: 部署文档(Render 步骤 + Oracle 步骤归档)

**Files:**
- Create: `docs/DEPLOY-ORACLE.md`(把当前 `docs/DEPLOY.md` 的全部内容原样搬过去)
- Modify: `docs/DEPLOY.md`(替换成 Render 部署步骤)

**Interfaces:**
- Consumes:Task 3 的 `render.yaml`、Task 4/5 的两个 workflow 文件名和它们各自需要的 Secret 名字
- Produces:无(文档,不被其他任务消费)

- [ ] **Step 1: 归档 Oracle 文档**

把当前 `docs/DEPLOY.md`(标题"tv-alert-relay 部署(复用现有 Oracle Cloud VM)"那份,共 64 行,从"前提:goldbot 已经在同一台 VM 上跑着"到最后的 Secret 配置说明)原样复制一份到新文件 `docs/DEPLOY-ORACLE.md`,一个字不改。

- [ ] **Step 2: 改写 `docs/DEPLOY.md`**

```markdown
# tv-alert-relay 部署(Render)

Oracle Cloud VM 版本的部署步骤挪到了 `docs/DEPLOY-ORACLE.md`(以后如果要重新
搬回某台常驻服务器,可以照那份抄)。这一份是当前实际在用的 Render 部署方式。

## 1. Gmail 应用专用密码

Google 账号 → 安全性 → 两步验证(需先开启)→ 应用专用密码 → 生成一个,填进
下一步 Render 的环境变量。

## 2. Telegram Bot

Telegram 里找 `@BotFather` → `/newbot` 建一个新 bot,拿到 token;给 bot 发条
消息后访问 `https://api.telegram.org/bot<token>/getUpdates` 找 `chat_id`。

## 3. 部署到 Render

1. 去 [render.com](https://render.com) 用 GitHub 账号登录(不需要绑卡)。
2. New → Blueprint,选这个仓库,Render 会读到根目录的 `render.yaml` 自动建好
   服务骨架。
3. 部署过程中 Render 会提示你填标了 `sync: false` 的那几个环境变量:
   `GMAIL_USER`、`GMAIL_APP_PASSWORD`、`TG_BOT_TOKEN`、`TG_CHAT_ID`、
   `WEB_USER`、`WEB_PASSWORD`、`HEALTHZ_SHARED_SECRET`(留空即可,除非你想启
   用 `/healthz` 的额外密钥校验)。
4. 部署完成后,Render 会给一个形如 `https://tv-alert-relay-xxxx.onrender.com`
   的域名——这就是后面 GitHub Secrets 里要填的 `TVALERT_APP_URL`。

## 4. TradingView 侧

建警报时在 Notifications 里勾选 "Send Email",不用配置别的——这个服务会自动
去 Gmail 里捞 TradingView 发来的邮件。

## 5. GitHub Actions 配的 3 个仓库 Secrets

GitHub 仓库 → Settings → Secrets and variables → Actions → New repository
secret,建 3 个,`poll.yml`(每 5 分钟触发一次轮询,兼报警)和 `backup.yml`
(每周备份一次历史记录)两个工作流共用:

- `TVALERT_APP_URL` = 第 3 步 Render 给的域名(不带路径,比如
  `https://tv-alert-relay-xxxx.onrender.com`)
- `TVALERT_WEB_USER` = 跟 Render 环境变量里的 `WEB_USER` 填一样的值
- `TVALERT_WEB_PASSWORD` = 跟 Render 环境变量里的 `WEB_PASSWORD` 填一样的值

注意:`poll.yml` 一推到默认分支就会开始按计划运行;在上面 3 个 Secret 配好
之前,它每 5 分钟都会失败一次(GitHub 默认会给你发工作流失败邮件)。所以部署
完 Render、拿到域名后尽快配好这 3 个 Secret。

## 6. 看板访问

浏览器直接访问 Render 给的域名(比如
`https://tv-alert-relay-xxxx.onrender.com`),会弹 Basic Auth 登录框,填
`WEB_USER`/`WEB_PASSWORD`。

## 7. 备份文件在哪

`backups/alerts.json`,每周日自动更新,提交历史本身就是各个时间点的快照,不需
要额外去别处找。
```

- [ ] **Step 3: 提交**

```bash
git add docs/DEPLOY-ORACLE.md docs/DEPLOY.md
git commit -m "docs: DEPLOY.md 改写成 Render 步骤,Oracle 版本归档到 DEPLOY-ORACLE.md"
```

---

## 完成后的整体验证

1. `pytest -v` 全绿,共 31 个用例(原有 25 个 + `/poll` 新增 3 个 + `/export` 新增 3 个)
2. 本地按 Task 3 Step 4 的方式验证一次端口绑定
3. 手动检查 `render.yaml`、`poll.yml`、`backup.yml` 三个 YAML 文件缩进和字段名正确(没有自动化 YAML 校验,纯人工过一遍)
4. `docs/DEPLOY.md` 从头读一遍,确认步骤能顺着走下来、没有引用不存在的文件或 Secret 名字

## 本计划不包含的收尾操作(由协调者在所有任务完成后手动执行,不走 SDD 的 task 流程)

这些是对 GitHub 仓库本身的操作,没有对应的代码 diff,不适合走"实现→审查"的
task 循环:

1. 把仓库从 private 切成 public 前,先跑一遍 `git log -p | grep -iE "password|token|secret|api[_-]?key"` 之类的粗筛,确认历史提交里没有真的密钥(预期:只会命中变量名和占位符,不会命中真实值)
2. 在 GitHub 仓库设置里把可见性改成 Public
3. 部署到 Render,拿到真实域名
4. 配置 3 个仓库 Secrets(`TVALERT_APP_URL`、`TVALERT_WEB_USER`、`TVALERT_WEB_PASSWORD`)
5. 删除不再使用的旧 Secrets(`TVALERT_HEALTHZ_URL`、`TVALERT_HEALTHZ_SECRET`)
