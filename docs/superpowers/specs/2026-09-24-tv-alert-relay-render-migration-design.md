# tv-alert-relay 迁移到 Render + 定期备份 — 设计

日期:2026-09-24
状态:已确认

## 背景

`tv-alert-relay`(设计见 [[2026-07-26-tv-alert-relay-design.md]])原计划部署在用户已有的
Oracle Cloud Always Free VM 上。后来发现:该 Oracle Cloud 账号从未完成绑卡注册,
goldbot 也已经停掉,现在没有可用的 Oracle VM。

改用 Render 免费 Web Service,不需要绑卡。但 Render 免费层有两个跟原设计冲突的限制:

1. **容器会休眠**:超过一段时间没有 HTTP 请求会休眠,进程内的 `asyncio` 轮询循环
   (`main.py` 的 `_poll_loop`)在休眠期间不会运行,不能再指望它自己按时轮询。
2. **没有持久盘**:重新部署/迁移实例时本地 SQLite 文件会被清空,`alerts` 历史记录
   (虽然不是唯一数据源,Telegram 聊天记录才是,但网页看板会因此看不到历史)会跟着丢。

本设计只解决"怎么在 Render 上跑起来 + 怎么防止历史记录彻底消失",不改动核心轮询/
解析/推送逻辑(`poller.py`、`mail.py`、`telegram.py`、`imap_client.py`、`db.py`、
`health.py`、`config.py` 均不变)。goldbot 要不要重新搭是用户之后的独立决定,不在
本次范围内。

## 一、轮询驱动方式:从"进程内定时器"改成"外部触发"

新增 `POST /poll`(复用现有 `check_auth` 账号密码保护,跟网页看板用同一套
`WEB_USER`/`WEB_PASSWORD`):同步调用现成的 `poll_once(...)`,再用 `compute_health`
算一次健康状态,返回 JSON + 200/500。

GitHub Actions 定时任务(原 `heartbeat.yml` 改名为 `poll.yml`,内容重写)每 5 分钟
`curl -u 账号:密码 -X POST` 打这个接口。一次操作同时做到三件事:

- 叫醒休眠中的 Render 容器
- 真正驱动一次轮询(替代原来失效的进程内定时器)
- 失败时(`--fail` 命中非 2xx)GitHub Actions 工作流跑红,GitHub 按账号设置自动发
  失败邮件——心跳报警功能保留,只是触发方式从"单纯探测 `/healthz`"变成"顺便探测
  `/poll`"

原有 `_poll_loop` 和 `GET /healthz` 都不删——`_poll_loop` 在容器碰巧醒着的时候依然
会跑,是免费的额外一层;`/healthz` 继续作为纯只读的健康状态查询,供网页看板和人工
检查使用,只是不再是外部定时器打的那个接口。

## 二、频率 vs 仓库可见性

私有仓库 Actions 免费额度每月 2000 分钟,每次触发不管多快都按 1 分钟计费。5 分钟
一次相当于每月 8000+ 分钟,大幅超额。用户选择:**把仓库改成公开**,GitHub Actions
对公开仓库不限量计费,保持 5 分钟轮询一次的时效性。

仓库里从未提交过真实密钥(`.env` 全程被 `.gitignore` 排除,`.env.example` 只有
占位符,`pine/` 策略文件和现有 9 个 Python 模块都没有硬编码密钥)。切换可见性前会
再跑一遍 grep 确认,作为最后一道保险。

## 三、历史记录备份到 git

新增 `GET /export`(同样账号密码保护):把 `alerts` 表全量导出成

```json
{"exported_at": "2026-09-24T00:00:00+00:00", "alerts": [{"id": 1, "received_at": "...", "subject": "...", "body_snippet": "...", "sent_ok": 1, "error": null}, ...]}
```

新增 `.github/workflows/backup.yml`:每周日 00:00 UTC 跑一次,拉取 `/export` 的
JSON,提交到仓库的 `backups/alerts.json`(用 GitHub Actions 内置的 `GITHUB_TOKEN`
+ `contents: write` 权限,不需要用户另配 PAT)。JSON 内容有变化才提交,没变化则
跳过(避免空提交)。

单文件覆盖式存档(不是每次都新建一个带日期的文件)——git 提交历史本身就是版本
记录,不需要额外的目录管理。

## 四、Render 部署方式

- `main.py` 的 `if __name__ == "__main__":` 改成监听 `0.0.0.0` + 读 Render 自动
  注入的 `PORT` 环境变量(没有则回退到 8788,兼容以后如果又要部署回某台 VM 的
  情况):`uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 8788)))`
- 新增 `render.yaml`(Render Blueprint):定义一个 free plan 的 Python Web
  Service,`buildCommand: pip install -r requirements.txt`,
  `startCommand: python main.py`,10 个环境变量声明为骨架(密钥类标
  `sync: false`,部署时 Render 会提示手动填,不会写进这个文件里)
- `DB_PATH` 不设置,用代码默认值 `tvalert.db`(相对路径,反正是临时盘)
- 不再需要 Caddy/DuckDNS——Render 自带 HTTPS 域名(`https://tv-alert-relay.onrender.com`
  这种形式)
- `docs/DEPLOY.md` 改写成 Render 步骤;原来 Oracle VM 那套完整挪到新建的
  `docs/DEPLOY-ORACLE.md` 留档,不删除(以后如果重新弄到 VM 上还能照抄)
- `deploy/tvalert.service`、`deploy/Caddyfile.snippet` 保留原样,不再是部署主
  路径的一部分,但留着当参考,不删

## 五、GitHub Secrets 调整

新增 3 个仓库 Secrets,`poll.yml` 和 `backup.yml` 共用:

- `TVALERT_APP_URL`:Render 服务的完整域名(不带路径),如
  `https://tv-alert-relay.onrender.com`
- `TVALERT_WEB_USER` / `TVALERT_WEB_PASSWORD`:跟 Render 上配的 `WEB_USER`/
  `WEB_PASSWORD` 环境变量值一致

旧的 `TVALERT_HEALTHZ_URL`、`TVALERT_HEALTHZ_SECRET` 两个 Secrets 不再被任何
workflow 使用,部署完成后删除。

## 六、测试

`tests/test_main.py` 新增:

- `POST /poll` 无认证 → 401(复用现有 `check_auth` 的既有测试模式)
- `POST /poll` 有认证 → mock 掉 `poll_once`(不真的连 IMAP/Telegram),断言被
  调用一次、返回码根据 mock 后的健康状态是 200 或 500
- `GET /export` 无认证 → 401
- `GET /export` 有认证 → 数据库里插入两条 `alerts` 记录,断言返回 JSON 的
  `alerts` 数组长度、字段名、`exported_at` 字段存在且是合法 ISO 时间

不需要新的 mock 基础设施,复用 Task 8 已经建好的 `app.dependency_overrides`
测试模式。

## 明确不做(YAGNI)

- 不做多份带日期的备份文件——git 历史就是版本记录,一个覆盖式文件够用
- 不引入新的密钥类型给 `/poll`/`/export`——复用看板现成的账号密码
- 不改动核心业务逻辑模块(`poller.py`/`mail.py`/`telegram.py`/`imap_client.py`/
  `db.py`/`health.py`/`config.py`)——这次只是换个运行环境 + 加一层备份,不是重写
- 不用 Render 自带的 Cron Job 功能——免费层是否包含这个功能不确定,用已经验证
  免费的 GitHub Actions 更稳妥
- 不改造 `_poll_loop`/`/healthz` 的既有实现——原样保留,新增的 `/poll` 是叠加,
  不是替换
- goldbot 要不要也搬家、Oracle 账号要不要绑卡重新启用——用户自己的后续决定,不
  在本次范围
