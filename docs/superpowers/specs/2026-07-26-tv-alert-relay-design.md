# TradingView 免费版警报 → Telegram 推送中转服务 — 设计

日期:2026-07-26
状态:已确认

## 背景

用户的 TradingView 是免费版(Basic),不支持 Webhook 警报,只能靠警报邮件通知。
需要一个把警报邮件自动转发到 Telegram 的中转服务,并且要能看历史记录、知道
这条链路有没有正常运行。

用户在 `TradingBot/goldbot` 已经有一套跑在 Oracle Cloud Always Free VM 上的
成熟系统(Telegram 推送、SQLite、带密码网页、systemd 部署),本可以直接接进
去复用 `notify.send_telegram()`,但用户明确要求跟 goldbot 完全独立
(TradingView 原始警报信号和 goldbot 自产的 AI 信号是两码事,不想混在一起)。
因此这是一个新的独立项目,只是复用同一台已经付过"配置成本"的 VM 和部署模式
(systemd + Caddy + DuckDNS),不复用 goldbot 的任何代码或数据库。

## 一、整体架构

单个 Python(FastAPI)常驻进程,部署为 Oracle VM 上第二个 systemd 服务:

```
Gmail(IMAP,应用专用密码)
      │  每 5 分钟轮询一次(进程内 asyncio 后台任务)
      ▼
  解析警报邮件(主题+正文)
      │
      ├──▶ Telegram Bot API 推送
      ├──▶ 写入 SQLite alerts 表
      └──▶ 更新心跳时间戳
      ▼
  网页看板(Basic Auth)+ /healthz(供外部探测)
      ▲
      │  每 10 分钟 curl --fail
  GitHub Actions 定时任务 ──失败──▶ GitHub 自动发邮件报警
```

代码、数据库、Telegram Bot Token 均与 `goldbot` 完全独立;只共享同一台物理
VM、同一套 systemd/Caddy/DuckDNS 部署方式。

## 二、组件

### 轮询循环
FastAPI 启动时(lifespan)拉起一个后台 `asyncio` 任务,`while True`:每 5 分钟
(可配置)执行一次轮询,不额外依赖 cron。

### IMAP 邮件处理
- 用 `imaplib` + Gmail 应用专用密码登录
- 搜索条件:`UNSEEN` + `FROM` 命中 TradingView 发件地址(具体地址上线前从一
  封真实警报邮件确认,先用 `noreply@tradingview.com` 占位)
- 每封命中的邮件:解析主题/正文 → 调 Telegram Bot API 发送 → **发送成功才
  标记 `\Seen`**,失败则保持未读,下一轮自然重试(天然去重 + 重试,不需要
  额外的已处理 ID 表)
- 每次处理(无论成不成功)都写一行到 `alerts` 表

### 数据库(SQLite)
`alerts` 表:`id, received_at, subject, body_snippet, sent_ok, error`
`status` 表(单行):`last_poll_at, last_success_at, consecutive_errors`

### 网页看板 `GET /`
HTTP Basic Auth(`WEB_USER` / `WEB_PASSWORD` 环境变量,命名对齐 goldbot 的
`WEB_PASSWORD` 习惯)。内容:
- 健康横幅:根据 `status` 表算绿/红
- 最近 N 条 `alerts`(时间、主题、发送状态)

### 健康检查 `GET /healthz`
无鉴权。判定:
- `now - last_poll_at` 超过 2 个轮询周期(默认 10 分钟)→ 500
- 或 `consecutive_errors` ≥ 3 → 500
- 否则 200,body 带 JSON 摘要

### 外部心跳(GitHub Actions)
新建这个项目自己的 GitHub 仓库(私有),`.github/workflows/heartbeat.yml`:
每 10 分钟 `curl --fail` 部署好的 `/healthz` URL。失败即工作流失败,GitHub
按账号设置自动发失败邮件给仓库所有者——这是 VM 内部监控覆盖不到的盲区
(整台机器/公网断了,只有外部探测能发现),零额外代码,免费额度足够
(每 10 分钟一次远小于 Actions 免费分钟数上限)。

## 三、错误处理

- IMAP 连接/登录失败:捕获,记录到 `status.consecutive_errors`,本轮跳过,
  不让后台任务崩掉
- Telegram 发送失败(限流、token 失效等):单条捕获,记 `alerts.error`,该邮件
  保持未读以便重试
- 进程级崩溃兜底:systemd `Restart=always`(跟 `goldbot.service` 同一模式)

## 四、部署

- 目录:`/opt/tvalert/`(与 `/opt/goldbot/` 平级,互不共享)
- `tvalert.service`(systemd,仿 `goldbot.service` 写法),`Restart=always`
- 新端口(如 8788),Caddy 加一段 `tvalert.<duckdns前缀>.duckdns.org` 反代
  到 `127.0.0.1:8788`
- 环境变量(`.env`,`chmod 600`):`GMAIL_USER`、`GMAIL_APP_PASSWORD`、
  `TG_BOT_TOKEN`、`TG_CHAT_ID`、`WEB_USER`、`WEB_PASSWORD`、
  `HEALTHZ_SHARED_SECRET`(可选,防止 `/healthz` 被无关人扫到)

## 五、测试

轮询/解析/健康判定都是纯逻辑,不需要真连 IMAP/Telegram 才能测:
- 邮件解析函数:输入原始邮件文本 → 输出 `(subject, body)`,断言几个样例
- 健康判定函数:输入 `(last_poll_at, consecutive_errors, now)` → 输出
  健康/不健康布尔值,断言边界(刚好 2 个周期、刚好 3 次错误)
- Telegram 发送和 IMAP 客户端在测试里 mock 掉
不上测试框架,一个 `test_relay.py` + `assert`。

## 明确不做(YAGNI)

- 结构化解析警报正文(提取具体价格/方向字段)——先原文转发,够用了再加
- 多渠道通知(邮件失败再发短信之类)——Telegram + GitHub Actions 失败邮件
  已经是两条独立通道,够了
- 邮件永久持久化存档——SQLite 会因为 systemd 重启/VM 迁移而可能丢历史,
  但 Telegram 聊天记录本身就是永久真实记录,SQLite 只是网页看板的展示缓存,
  不是唯一数据源,丢了不影响追溯
- 接入 goldbot 的 Telegram 推送/数据库/网页——用户明确要求物理隔离,只共享
  部署环境
- Webhook 接入路径——等用户升级 TradingView 付费版再作为独立后续工作评估
