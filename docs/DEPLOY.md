# tv-alert-relay 部署(Render)

Oracle Cloud VM 版本的部署步骤挪到了 `docs/DEPLOY-ORACLE.md`(以后如果要重新
搬回某台常驻服务器,可以照那份抄)。这一份是当前实际在用的 Render 部署方式。

## 1. Gmail 应用专用密码

Google 账号 → 安全性 → 两步验证(需先开启)→ 应用专用密码 → 生成一个,填进
下一步 Render 的环境变量。

## 2. Telegram Bot

Telegram 里找 `@BotFather` → `/newbot` 建一个新 bot,拿到 token;给 bot 发条
消息后访问 `https://api.telegram.org/bot<token>/getUpdates` 找 `chat_id`。

## 3. Twelve Data API Key(信号引擎用)

XXX 策略信号引擎(独立于 Gmail 邮件转发那套)需要一个免费的 Twelve Data 账号
来拉 XAUUSD 行情:

1. 去 [twelvedata.com](https://twelvedata.com) 注册一个免费账号(不需要绑卡)。
2. 登录后在 Dashboard 里能看到你的 API Key,复制下来。
3. 填进下一步 Render 的环境变量 `TWELVE_DATA_API_KEY`。
4. `SIGNAL_SYMBOL` 不用改,默认就是 `XAU/USD`;只有以后想换别的品种才需要改。

## 4. 部署到 Render

1. 去 [render.com](https://render.com) 用 GitHub 账号登录(不需要绑卡)。
2. New → Blueprint,选这个仓库,Render 会读到根目录的 `render.yaml` 自动建好
   服务骨架。
3. 部署过程中 Render 会提示你填标了 `sync: false` 的那几个环境变量:
   `GMAIL_USER`、`GMAIL_APP_PASSWORD`、`TG_BOT_TOKEN`、`TG_CHAT_ID`、
   `WEB_USER`、`WEB_PASSWORD`、`HEALTHZ_SHARED_SECRET`(留空即可,除非你想启
   用 `/healthz` 的额外密钥校验)、`TWELVE_DATA_API_KEY`(第 3 步拿到的那个)。
4. 部署完成后,Render 会给一个形如 `https://tv-alert-relay-xxxx.onrender.com`
   的域名——这就是后面 GitHub Secrets 里要填的 `TVALERT_APP_URL`。

## 5. TradingView 侧

建警报时在 Notifications 里勾选 "Send Email",不用配置别的——这个服务会自动
去 Gmail 里捞 TradingView 发来的邮件。

## 6. GitHub Actions 配的 3 个仓库 Secrets

GitHub 仓库 → Settings → Secrets and variables → Actions → New repository
secret,建 3 个,`poll.yml`(每 5 分钟触发一次轮询,兼报警)和 `backup.yml`
(每周备份一次历史记录)两个工作流共用:

- `TVALERT_APP_URL` = 第 4 步 Render 给的域名(不带路径,比如
  `https://tv-alert-relay-xxxx.onrender.com`)
- `TVALERT_WEB_USER` = 跟 Render 环境变量里的 `WEB_USER` 填一样的值
- `TVALERT_WEB_PASSWORD` = 跟 Render 环境变量里的 `WEB_PASSWORD` 填一样的值

注意:`poll.yml` 一推到默认分支就会开始按计划运行;在上面 3 个 Secret 配好
之前,它每 5 分钟都会失败一次(GitHub 默认会给你发工作流失败邮件)。所以部署
完 Render、拿到域名后尽快配好这 3 个 Secret。

## 7. 看板访问

浏览器直接访问 Render 给的域名(比如
`https://tv-alert-relay-xxxx.onrender.com`),会弹 Basic Auth 登录框,填
`WEB_USER`/`WEB_PASSWORD`。信号引擎的历史记录和胜率在 `/signals/export`
(同一套账号密码)。

## 8. 备份文件在哪

`backups/alerts.json`,每周日自动更新,提交历史本身就是各个时间点的快照,不需
要额外去别处找。

Render 免费层没有持久盘,每次重新部署都会清空网页历史。`render.yaml` 里的
`buildFilter.ignoredPaths: backups/**` 让只改动 `backups/` 的备份提交不触发
自动重新部署。如果发现每周备份提交后网页历史被清空,说明 `render.yaml` 里的
`buildFilter.ignoredPaths` 配置在当前 Render 版本上没生效,去 Render 控制台的
Settings → Build & Deploy 里手动关闭这个服务的 'Auto-Deploy',改成只在你自己推
代码改动时手动点 'Deploy latest commit'。
