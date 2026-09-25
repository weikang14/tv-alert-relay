# XXX 策略信号引擎(免订阅复刻)— 设计

日期:2026-09-25
状态:已确认(2026-09-25 更正:拿到真实 Pine 源码后发现第五节第 1 点的算法
理解是反的,已更正为"在每个桶自己的开高低收上算 ALMA",详见该节说明。同日晚些
时候,图表周期从 1 分钟改为 15 分钟——见第一节表格及全文相应处的更新)

## 背景

用户在 TradingView 免费版(Basic)账号上,给 XAUUSD 图表挂了一个自定义 Pine 策略脚本
`XXX v6_1_23`(ALMA 交叉入场 + 三层止盈一层止损),想要这个策略的信号能像
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
| 图表蜡烛周期 | (脚本不含此值,由图表决定) | **15 分钟**(2026-09-25 由 1 分钟改为
15 分钟——用户反馈马来西亚时区下 1 分钟图噪音太多;`CHART_TIMEFRAME_MINUTES` 常量) |
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
替代分辨率(stratRes)= 图表周期(15 分钟)× intRes(8) = 120 分钟
```

图表周期是可能再变的配置项,不是写死的算法前提——`signal_engine.py` 里用
`CHART_TIMEFRAME_MINUTES`(图表周期分钟数)、`INT_RES`(8)、
`BUCKET_MINUTES = CHART_TIMEFRAME_MINUTES * INT_RES` 三个常量表达这个关系,后续
如果图表周期再变,只改这一处常量即可,不需要改算法本身。

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
                        ├─ 1. 拉 Twelve Data 最近的图表周期(15 分钟)K 线
                        │     (自上次处理点以来)
                        ├─ 2. 本地把图表周期 K 线聚合成 120 分钟"桶蜡烛"(开=桶内
                        │     第一根的开,收=桶内最后一根的收),在桶蜡烛序列上
                        │     算 ALMA(2,5,0.85) 交叉 → 有入场信号就推 Telegram
                        │     + 写入 signals 表
                        └─ 3. 对所有 status='OPEN' 的信号,用这批图表周期 K 线
                              逐根检查 TP1→TP2→TP3/SL(要求真正"穿越",不是
                              "已经越过"),推进状态;结束的信号推一条
                              Telegram「结局播报」
```

## 三、行情数据源:Twelve Data

对比过 Twelve Data 与 OANDA:OANDA 的行情数据 API 是收费产品,没有真正的免费开发者
额度;Twelve Data 免费层(800 次/天、8 次/分钟,不需要信用卡)足够覆盖需求,支持
XAU/USD 多周期(1 分钟起)、20 年历史。

**只拉图表周期(15 分钟)K 线一种粒度**,替代分辨率(120 分钟)和入场判断都在
Python 本地用这份数据重采样算出来,不额外调用第二个周期的接口——每次轮询只需 1 次 API 调用,
按现有 GitHub Actions 5 分钟一次的节奏,一天约 288 次,远低于 800 次/天额度上限,
留有充足余量应对偶尔的重试。

新增环境变量(`sync: false`,同现有密钥管理方式):
- `TWELVE_DATA_API_KEY`(必填)
- `SIGNAL_SYMBOL`(可选,默认 `XAU/USD`)

**每次拉取的根数(`outputsize`)**:
- 首次运行(`signal_status` 还没有行,即 `last_bar_time` 为空):拉 200 根图表周期
  K 线作为冷启动缓冲(足够覆盖多个桶,让 ALMA 有足够历史暖机)。
- 之后每次轮询:按 `(当前时间 - last_bar_time)` 算出缺口分钟数,换算成图表周期
  根数(向上取整)后再加上完整一个桶的余量(`2 × intRes(8) + 1` 根,详见第五节
  第 6 点),但**封顶 200 根**——即使服务中断了很久导致缺口很大,也不会一次性
  请求过大的返回体,超过 200 根的部分视为不可恢复的缺口,直接跳过,只处理最近
  200 根。

## 四、组件(新增文件)

| 文件 | 职责 |
|---|---|
| `market_data.py` | 封装 Twelve Data `time_series` 接口:拉 XAU/USD 图表周期 K 线,处理限流/超时/格式错误 |
| `alma.py` | ALMA(length, sigma, offset) 纯函数计算,不依赖 TradingView,独立可测 |
| `signal_engine.py` | 核心逻辑:本地重采样成桶算入场交叉;维护每笔信号的状态机(`OPEN`→`TP1_HIT`→`TP2_HIT`→`TP3_FULL`,或提前 `SL_ONLY`/`TPn_THEN_SL`) |
| `db.py`(扩展) | 新增 `signals` 表 + `signal_status` 表(见下) |
| `main.py`(扩展) | `/poll` 里追加调用 `signal_engine.poll_once()`;新增 `GET /signals/export`(复用现有 `/export` 的账号密码保护 + JSON 导出模式) |

### 数据表设计

```sql
CREATE TABLE IF NOT EXISTS signals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    direction TEXT NOT NULL,        -- 'long' | 'short'
    entry_price REAL NOT NULL,
    entry_time TEXT NOT NULL,       -- ISO8601 UTC
    entry_high REAL NOT NULL,       -- 入场那根图表周期 K 线自己的高点
    entry_low REAL NOT NULL,        -- 入场那根图表周期 K 线自己的低点
    highest_tier INTEGER NOT NULL DEFAULT 0,  -- 0=尚未中任何TP, 1/2/3=已到TP1/2/3
    status TEXT NOT NULL DEFAULT 'OPEN',      -- OPEN | SL_ONLY | TP1_THEN_SL | TP2_THEN_SL | TP3_FULL
                                               -- | REVERSED_ONLY | TP1_THEN_REVERSED | TP2_THEN_REVERSED
    exited_at TEXT,                   -- ISO8601 UTC(出场那根 K 线的时间,不是轮询时间),OPEN 时为 NULL
    last_bar_high REAL,                -- 上一次评估到的 K 线高点(穿越判断要用,见第五节第 3 点)
    last_bar_low REAL                  -- 上一次评估到的 K 线低点
);

CREATE TABLE IF NOT EXISTS signal_status (
    id INTEGER PRIMARY KEY CHECK (id = 1),    -- 单行表,同现有 status 表模式
    last_bar_time TEXT,                       -- 已处理到的最后一根图表周期 K 线时间戳,避免重复处理
    last_bucket_start TEXT,                   -- 上一次已处理的完整"桶蜡烛"的起始时间
    last_bucket_open REAL,                    -- 该桶蜡烛的开(下一桶算 ALMA 需要的历史值)
    last_bucket_close REAL,                   -- 该桶蜡烛的收
    last_bucket_alma_close REAL,              -- 该桶算出来的 ALMA(close),交叉对比用
    last_bucket_alma_open REAL                -- 该桶算出来的 ALMA(open)
);
```

**为什么这些字段比最初想的多(写实施计划、以及后来拿到真实源码改算法时陆续补
的)**:ALMA(长度2)算一个新桶的值,需要"这个桶"和"上一个桶"两根桶蜡烛的开/收;
判断交叉又需要"上一个桶"和"当前桶"两个已经算出来的 ALMA 值做对比。如果每次
轮询只处理自上次以来的新 K 线,某一轮可能只覆盖到 1 个新完整桶,这时"上一个
桶"的原始开收和 ALMA 值都已经不在这批新数据里(是上一轮处理过的)——所以必须
把上一次算出来的桶蜡烛开收 + ALMA 值都存下来,下一轮取出来接着用。

`胜率 = status IN ('TP1_THEN_SL','TP2_THEN_SL','TP3_FULL','TP1_THEN_REVERSED','TP2_THEN_REVERSED')
的笔数 / 已结束(status != 'OPEN')的总笔数`——即"至少摸到过 TP1"算赢,纯 `SL_ONLY`
或纯 `REVERSED_ONLY`(没摸到任何 TP 就被反向信号平仓)算输。`/signals/export`
同时返回完整结局分布,不只是这一个汇总数字。

## 五、关键实现细节(容易出偏差的地方)

0. **ALMA 公式(`alma.py` 必须实现的精确定义)**,对应 Pine 的 `ta.alma(series, length, offset, sigma)`:
   ```
   m = offset * (length - 1)
   s = length / sigma
   w[j] = exp(-((j - m)^2) / (2 * s^2))          for j = 0 .. length-1
   ALMA[i] = Σ w[j] * price[i - length + 1 + j] / Σ w[j]
   ```
   本设计里 `length=2, sigma=5, offset=0.85`,分别对图表周期收盘价序列和开盘价序列
   各算一份连续的 ALMA。
1. **ALMA 计算顺序(2026-09-25 更正,拿到真实源码后重新确认)**:最初这里写的是
   "先在图表周期收盘/开盘价上连续计算 ALMA,再按桶边界取样"——**这个理解是
   错的**,已经用真实 Pine 源码验证并推翻。真实机制:`closeSeriesAlt =
   request.security(syminfo.tickerid, "120", closeSeries)`(`"120"` 即
   `stratRes`,随图表周期变化),而 `closeSeries` 是一个由 `close`(内建变量)
   和几个 `input()` 参数算出来的表达式,不含任何依赖图表周期专属状态的东西。
   Pine 的 `request.security()` 对这种"纯粹由内建量+输入参数算出的表达式"会
   **在目标分辨率的原生 K 线上重新执行整个计算**,不是把当前分辨率已经算好的
   序列拿去做重采样/取样。也就是说 ALMA(2,0.85,5) 实际上是算在**每个桶自己的
   开(桶内第一根图表周期 K 线的开)和收(桶内最后一根图表周期 K 线的收)**上,
   等于把每 `intRes`(8)根图表周期 K 线合成一根桶蜡烛,再对这个蜡烛序列算
   ALMA——**正是当初设计时特意排除掉的"重采样再算 ALMA"那条路线**。已改正,
   `signal_engine.bucket_samples()` 现在按这个（正确）算法实现。
2. **SL 价位入场后固定不变**:`slLine` 只在开仓那一刻计算一次,后续 TP1/TP2/TP3
   推进不会移动止损线,状态机不需要处理"移动止损"。
3. **TP/SL 判断要求真正"穿越",不是"已经越过"(2026-09-25 补,同样是看了真实源码
   才发现)**:脚本每一处 TP/SL 判断都是用 `f_cross()`(等价于
   `ta.crossover`/`ta.crossunder`):`_scr1 > _scr2 and _scr1[1] < _scr2[1]`——要求
   **上一根 K 线严格在线的另一侧**,不是"这根 K 线的高/低已经越过阈值"就算数。
   由于 SL(0.1%)、TP1(0.2%)这些阈值相对黄金单根 K 线波动比较窄,这个区别在实盘
   里会经常影响判断,不是罕见边界情况。已改正:`evaluate_signal()` 现在要求
   "上一根 K 线的高/低"严格在阈值另一侧才算命中,`signals` 表新增
   `entry_high`/`entry_low`(入场那根 K 线自己的高低,给第一次判断当参照)和
   `last_bar_high`/`last_bar_low`(每次轮询后更新,给下一次判断当参照)。
   另外确认了 `switch` 语句本身每根 K 线只会走一个分支(标准 switch/case 语义,
   不会在同一根 K 线内连续推进多级),所以"一根 K 线最多推进一级"这个原有实现
   是对的,不需要改。
4. **已知复刻限制(有意为之,不追求 100% 一致;2026-09-25 更正影响程度的描述)**:
   脚本用 `request.security(..., lookahead = barmerge.lookahead_on)` 拉高周期数据。
   独立复核确认这**不是"极少数边界情况"**,而是有实质影响的两点:
   - **图表历史回放上**:某个桶算出来的最终 ALMA 值,会被"画"在这个桶**第
     一根图表周期 K 线**上(未来数据提前泄露到过去)——也就是说,你在图表上回看
     历史时看到的信号标记时间,跟本设计"信号在桶收盘那一刻才触发"的时间,天生
     就对不上,差可以到接近一个桶宽度(120 分钟)。
   - **实盘运行时**:桶内任意时刻,只要那个尚未收盘的"临时蜡烛"的 ALMA
     值变化导致交叉条件成立,脚本就可能立刻触发,且同一个桶内理论上可能反复
     触发/撤销。
   本设计的 Python 版本**只在每个桶完全收盘之后**才判断一次交叉,不追踪
   桶内的实时重绘/提前触发行为——这仍然是刻意简化(否则要复刻的是"任意时刻都可能
   变化的临时蜡烛"这种更复杂的状态),但请注意:这意味着信号**触发的时间点**和
   **入场价**跟 TradingView 图表上实际显示的会有系统性差异(不只是极端情况才
   出现),不是"发不发信号"层面的差异(该出现的信号方向和大致位置基本一致,
   已用大批量模拟验证零漏检零误报),而是"信号具体在哪一分钟、以什么价格触发"
   层面的差异。记入下面的明确不做范围。
5. **反向信号会平掉原有仓位(2026-09-25 补,独立复核发现遗漏)**:脚本
   `strategy()` 声明了 `pyramiding = 0`,且 `leTrigger`/`seTrigger` 的判断条件
   (`condition[1] <= 0.0` / `condition[1] >= 0.0`)本身就保证了不会在已有多头仓位
   时再开一次多——也就是说反向信号出现时,TradingView 会先平掉原有仓位再反向
   开仓,同一时刻不会有两个方向的仓位同时存在。最初的实现遗漏了这一点:检测到
   新方向的信号后,只顾插入新信号,原来那个还开着的反方向信号会被永远晾在
   `OPEN` 状态,不会被平仓,污染胜率统计。已改正:`poll_once()` 检测到新入场时,
   会先把任何还开着的反方向信号按当前已到的那一级平仓,状态记为
   `REVERSED_ONLY`/`TP1_THEN_REVERSED`/`TP2_THEN_REVERSED`(平仓时间用新信号的
   入场时间),胜率统计里 `TP1_THEN_REVERSED`/`TP2_THEN_REVERSED` 算赢,
   `REVERSED_ONLY` 算输,跟 `SL_ONLY`/`TPn_THEN_SL` 的算法逻辑一致。
6. **拉取根数(`outputsize`)必须覆盖完整一个桶,不能只看轮询间隔(2026-09-25
   补,独立复核用批量模拟发现)**:最初的公式是"轮询间隔分钟数 + 5",但轮询间隔
   跟桶宽度是两回事——轮询间隔哪怕很小,也完全可能卡在某个桶的中间,
   这时如果拉回来的数据不够覆盖到这个桶真正的第一根 K 线,桶的"开"就会用错
   (变成用凑巧留在这批数据里的某根 K 线的开,不是桶真正的开),连带算出来的
   ALMA 就是错的。已改正为"⌈轮询间隔分钟数 / 图表周期分钟数⌉(缺口根数,向上
   取整)+ 2×intRes(8)+1 根",经批量模拟验证在多种轮询间隔下都做到零漏检
   零误报。(2026-09-25 图表周期从 1 分钟改为 15 分钟时,把公式从"按分钟数"
   改成了"按根数",因为 `outputsize` 本身就是 Twelve Data 接口的根数参数,不是
   分钟数——之前 1 分钟图表下两者数值恰好相同,换成 15 分钟图表后才暴露这个
   本该一直存在的换算步骤。)

## 六、错误处理

- Twelve Data 请求失败/限流(HTTP 429/5xx):记录日志,这一轮跳过,不影响下一轮
  轮询(沿用现有 `poller.py` 的 try/except 全覆盖模式,`signal_engine.poll_once()`
  同样保证异常不外抛)。
- 拉回来的图表周期 K 线有缺口(网络抖动导致某几根没数据):按时间戳去重 + 排序,
  缺口内没有数据的部分直接跳过判断,不做插值假设。
- Telegram 推送失败:复用现有 `telegram.send_telegram_message` 的失败返回值处理
  方式(不重复实现)。

## 七、测试

- `alma.py`:纯函数单元测试,构造已知输入序列验证 ALMA 输出数值。
- `signal_engine.py`:mock 一串构造好的图表周期 K 线,断言:
  - 正确识别入场交叉(含"ALMA 算在每个桶自己的开收上"这个聚合方式,以及
    "桶蜡烛的开必须来自桶内第一根 K 线,不能用轮询过滤后剩下的那一根"这条边界)
  - TP/SL 判断要求真正的穿越(上一根 K 线严格在阈值另一侧),不是"已经越过"
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
  桶收盘后判断一次(第五节第 4 点已说明)
- 不接入 goldbot、不共享任何代码或数据库——继续保持 tv-alert-relay 完全独立
- 不做实盘自动下单——纯粹是信号通知 + 事后统计,不驱动任何真实交易
- 不改动这份 Pine 脚本本身——脚本原样留在图表上,不做任何修改
- 不额外接入第二个行情周期的 API 调用——替代分辨率和基础周期都从同一份图表周期
  数据本地重采样得到
