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
