# XXX 策略信号引擎(免订阅复刻)— 设计

日期:2026-09-25
状态:已确认

## 背景

用户在 TradingView 免费版(Basic)账号上,给 XAUUSD 图表挂了一个自定义 Pine 策略脚本
`XXX v6_1_23`(1 分钟图,ALMA 交叉入场 + 三层止盈一层止损),想要这个策略的信号能像
现有 [[2026-07-26-tv-alert-relay-design.md]] 那样推送到 Telegram。

调查发现:TradingView Basic 免费版对"technical alerts"(基于指标/策略/画线的警报)
额度是硬性的 **0**,只有最基础的价格穿越警报(3 个额度)免费。想让 TradingView
自己发出这个策略的警报,必须升级到 Essential 及以上($14.95/月起)。

用户明确要求:**完全不修改这份 Pine 脚本本身**,脚本继续原样挂在图表上跑;同时不想
为这一个策略额外付费订阅。因此本设计的方案是:在 Python 里独立复刻这个脚本的入场
信号判断逻辑和止盈止损结局判断逻辑,绕开 TradingView 的警报系统,直接推送 Telegram。

本设计只覆盖这一个新增子系统,不改动 [[2026-09-24-tv-alert-relay-render-migration-design.md]]
里已经在跑的 Gmail 轮询 / 价格警报转发逻辑,两者在同一个 Render 服务里并列运行,
互不干扰,只共用同一个 Telegram 推送出口和同一个 `/poll` 触发入口。

## 一、脚本关键参数(从用户实际图表配置读出,不是脚本源码默认值)

通过截图核对用户当前 Inputs 面板的真实设置,发现跟脚本源码里写的默认值有明显出入,
必须以实际配置为准:

| 参数 | 脚本默认值 | 用户实际配置 |
|---|---|---|
| 图表蜡烛周期 | (脚本不含此值,由图表决定) | **1 分钟**(用户确认) |
| Multiplier for Alternate Signals (`intRes`) | 8 | 8(未改) |
| MA Type / Period / Sigma / ALMA Offset | ALMA / 2 / 5 / 0.85 | 一致,未改 |
| Level TP1 / Qty TP1 | 1% / 50% | **0.2% / 50%** |
| Level TP2 / Qty TP2 | 1.5% / 30% | **0.35% / 30%** |
| Level TP3 / Qty TP3 | 2% / 20% | **0.45% / 20%** |
| Stop Loss | 0.5% | **0.1%** |

**重要发现**:脚本里 `res = input.timeframe('15', 'TIMEFRAME', ...)` 这个输入框**从未
在后续代码中被引用**,是死代码。真正决定"替代分辨率"的是 TradingView 内置变量
`timeframe.multiplier`(图表当前蜡烛周期)× `intRes`。据此算出:

```
替代分辨率(stratRes)= 图表周期(1 分钟)× intRes(8) = 8 分钟
```

## 二、架构

新增一个独立的"信号引擎"模块,跟现有 Gmail 轮询完全并列,共用同一个 Render 服务、
同一个 `/poll` 入口、同一个 Telegram 推送出口:

```
GitHub Actions(现有,5 分钟一次)
        │
        ▼
POST /poll  ──┬─→ 现有:Gmail 轮询(价格警报邮件转发,不动)
              └─→ 新增:signal_engine.poll_once()
                        │
                        ├─ 1. 拉 Twelve Data 最近的 1 分钟 K 线(自上次处理点以来)
                        ├─ 2. 本地重采样成 8 分钟桶,算 ALMA(2,5,0.85) 交叉
                        │     → 有入场信号就推 Telegram + 写入 signals 表
                        └─ 3. 对所有 status='OPEN' 的信号,用这批 1 分钟 K 线
                              逐根检查 TP1→TP2→TP3/SL,推进状态;
                              结束的信号推一条 Telegram「结局播报」
```

## 三、行情数据源:Twelve Data

对比过 Twelve Data 与 OANDA:OANDA 的行情数据 API 是收费产品,没有真正的免费开发者
额度;Twelve Data 免费层(800 次/天、8 次/分钟,不需要信用卡)足够覆盖需求,支持
XAU/USD 多周期(1 分钟起)、20 年历史。

**只拉 1 分钟 K 线一种粒度**,替代分辨率(8 分钟)和入场判断都在 Python 本地用这份
1 分钟数据重采样算出来,不额外调用第二个周期的接口——每次轮询只需 1 次 API 调用,
按现有 GitHub Actions 5 分钟一次的节奏,一天约 288 次,远低于 800 次/天额度上限,
留有充足余量应对偶尔的重试。

新增环境变量(`sync: false`,同现有密钥管理方式):
- `TWELVE_DATA_API_KEY`(必填)
- `SIGNAL_SYMBOL`(可选,默认 `XAU/USD`)

**每次拉取的根数(`outputsize`)**:
- 首次运行(`signal_status` 还没有行,即 `last_bar_time` 为空):拉 200 根 1 分钟
  K 线作为冷启动缓冲(足够覆盖多个 8 分钟桶,让 ALMA 有足够历史暖机)。
- 之后每次轮询:按 `(当前时间 - last_bar_time)` 算出缺口分钟数 + 5 分钟余量,
  但**封顶 200 根**——即使服务中断了很久导致缺口很大,也不会一次性请求过大的
  返回体,超过 200 根的部分视为不可恢复的缺口,直接跳过,只处理最近 200 根。

## 四、组件(新增文件)

| 文件 | 职责 |
|---|---|
| `market_data.py` | 封装 Twelve Data `time_series` 接口:拉 XAU/USD 1 分钟 K 线,处理限流/超时/格式错误 |
| `alma.py` | ALMA(length, sigma, offset) 纯函数计算,不依赖 TradingView,独立可测 |
| `signal_engine.py` | 核心逻辑:本地重采样成 8 分钟桶算入场交叉;维护每笔信号的状态机(`OPEN`→`TP1_HIT`→`TP2_HIT`→`TP3_FULL`,或提前 `SL_ONLY`/`TPn_THEN_SL`) |
| `db.py`(扩展) | 新增 `signals` 表 + `signal_status` 表(见下) |
| `main.py`(扩展) | `/poll` 里追加调用 `signal_engine.poll_once()`;新增 `GET /signals/export`(复用现有 `/export` 的账号密码保护 + JSON 导出模式) |

### 数据表设计

```sql
CREATE TABLE IF NOT EXISTS signals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    direction TEXT NOT NULL,        -- 'long' | 'short'
    entry_price REAL NOT NULL,
    entry_time TEXT NOT NULL,       -- ISO8601 UTC
    highest_tier INTEGER NOT NULL DEFAULT 0,  -- 0=尚未中任何TP, 1/2/3=已到TP1/2/3
    status TEXT NOT NULL DEFAULT 'OPEN',      -- OPEN | SL_ONLY | TP1_THEN_SL | TP2_THEN_SL | TP3_FULL
    exited_at TEXT                    -- ISO8601 UTC, OPEN 时为 NULL
);

CREATE TABLE IF NOT EXISTS signal_status (
    id INTEGER PRIMARY KEY CHECK (id = 1),    -- 单行表,同现有 status 表模式
    last_bar_time TEXT,                       -- 已处理到的最后一根 1 分钟 K 线时间戳,避免重复处理
    last_bucket_start TEXT,                   -- 上一次已处理的完整 8 分钟桶的起始时间
    last_bucket_alma_close REAL,              -- 该桶取样到的 ALMA(close)
    last_bucket_alma_open REAL                -- 该桶取样到的 ALMA(open)
);
```

**为什么多了后三个字段(写实施计划时补的)**:判断交叉需要"上一个完整桶"和
"当前完整桶"两个样本对比。如果每次轮询只处理自上次以来的新 K 线,某一轮可能只
覆盖到 1 个新完整桶,这时"上一个桶"的样本已经不在这批新数据里(是上一轮处理过
的)——所以必须把上一次算出来的桶样本存下来,下一轮取出来当"上一个桶"用,而
不是每次都要求批次里至少凑够 2 个完整桶。

`胜率 = status IN ('TP1_THEN_SL','TP2_THEN_SL','TP3_FULL') 的笔数 / 已结束(status != 'OPEN')的总笔数`——
即"至少摸到过 TP1"算赢,纯 `SL_ONLY` 算输。`/signals/export` 同时返回完整结局分布,
不只是这一个汇总数字。

## 五、关键实现细节(容易出偏差的地方)

0. **ALMA 公式(`alma.py` 必须实现的精确定义)**,对应 Pine 的 `ta.alma(series, length, offset, sigma)`:
   ```
   m = offset * (length - 1)
   s = length / sigma
   w[j] = exp(-((j - m)^2) / (2 * s^2))          for j = 0 .. length-1
   ALMA[i] = Σ w[j] * price[i - length + 1 + j] / Σ w[j]
   ```
   本设计里 `length=2, sigma=5, offset=0.85`,分别对 1 分钟收盘价序列和开盘价序列
   各算一份连续的 ALMA。
1. **ALMA 计算顺序**:脚本是"先在 1 分钟收盘/开盘价上连续计算 ALMA,再按 8 分钟
   边界取样"(`closeSeriesAlt = reso(closeSeries, ...)`,`closeSeries` 本身已经是
   基于 1 分钟数据算出来的连续 ALMA 序列),**不是**"先把 1 分钟 K 线合成 8 分钟
   K 线,再对 8 分钟 K 线算 ALMA"——这两种算法数值结果不同,必须照前者复刻。
2. **SL 价位入场后固定不变**:`slLine` 只在开仓那一刻计算一次,后续 TP1/TP2/TP3
   推进不会移动止损线,状态机不需要处理"移动止损"。
3. **同一根 1 分钟 K 线内 TP 和 SL 都被摸到时,TP 优先记账**——照抄脚本 `switch`
   语句里 TP 分支排在 SL 分支前面的裁决顺序。
4. **已知复刻限制(有意为之,不追求 100% 一致)**:脚本用
   `request.security(..., lookahead = barmerge.lookahead_on)` 拉高周期数据,理论上
   在"当前尚未收盘的 8 分钟桶"上存在实时重绘的可能(尽管脚本分组标注写的是
   "NON REPAINT")。本设计的 Python 版本**只在每个 8 分钟桶收盘之后**才判断一次
   交叉,不追踪桶内的实时重绘行为——这是刻意简化,极少数边界情况下可能跟
   TradingView 实盘的即时表现对不上,但对于"是否要跟这个信号"这类判断影响很小,
   记入下面的明确不做范围。

## 六、错误处理

- Twelve Data 请求失败/限流(HTTP 429/5xx):记录日志,这一轮跳过,不影响下一轮
  轮询(沿用现有 `poller.py` 的 try/except 全覆盖模式,`signal_engine.poll_once()`
  同样保证异常不外抛)。
- 拉回来的 1 分钟 K 线有缺口(网络抖动导致某几分钟没数据):按时间戳去重 + 排序,
  缺口内没有数据的部分直接跳过判断,不做插值假设。
- Telegram 推送失败:复用现有 `telegram.send_telegram_message` 的失败返回值处理
  方式(不重复实现)。

## 七、测试

- `alma.py`:纯函数单元测试,构造已知输入序列验证 ALMA 输出数值。
- `signal_engine.py`:mock 一串构造好的 1 分钟 K 线,断言:
  - 正确识别入场交叉(含"先在 1 分钟连续算 ALMA 再 8 分钟取样"这个顺序)
  - 同一根 K 线内 TP 和 SL 都命中时,优先记 TP
  - 状态机正确推进到 `TP3_FULL`,或在任一环节提前 `*_THEN_SL`/`SL_ONLY` 退出
  - `last_bar_time` 正确推进,不重复处理已处理过的 K 线
- `market_data.py`:mock Twelve Data 的 HTTP 响应,测试限流(429)、超时、
  返回格式异常(缺字段/空数组)时的处理路径。
- `main.py`:`/signals/export` 无认证 → 401;有认证 → 正确返回 JSON,含结局分布
  和汇总胜率。

## 明确不做(YAGNI)

- 不追踪部分仓位加权盈亏(50%/30%/20% 仓位对应的美元/百分比收益)——只记录
  "这笔信号最终走到哪一级",不计算实际盈亏金额
- 不尝试 100% 复刻 TradingView `lookahead_on` 在未收盘桶上的实时重绘行为——只在
  8 分钟桶收盘后判断一次(第五节第 4 点已说明)
- 不接入 goldbot、不共享任何代码或数据库——继续保持 tv-alert-relay 完全独立
- 不做实盘自动下单——纯粹是信号通知 + 事后统计,不驱动任何真实交易
- 不改动这份 Pine 脚本本身——脚本原样留在图表上,不做任何修改
- 不额外接入第二个行情周期的 API 调用——替代分辨率和基础周期都从同一份 1 分钟
  数据本地重采样得到
