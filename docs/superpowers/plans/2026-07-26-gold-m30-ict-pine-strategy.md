# 黄金 M30 趋势追随 + ICT/SMC 入场 Pine Script 策略 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 交付一个可在 TradingView Strategy Tester 里跑出胜率/盈亏比/最大回撤数据的
Pine Script v6 策略脚本：H4 方向过滤 + M30 EMA 回调 + ICT/SMC 三因子评分入场 +
ATR 止盈止损 + 权益风险仓位管理 + 日内熔断。

**Architecture:** 单文件 Pine Script v6 `strategy()` 脚本，本地保存在仓库里做版本
控制，每个任务往同一个文件里追加一段新逻辑，用 `tradingview-mcp` 的
`pine_check`（离线服务端编译校验，不需要打开图表）做快速反馈；等入场逻辑和可视
化都写完，再用完整的 `pine_set_source` → `pine_smart_compile` → 图表截图/调试表
流程在真实 XAUUSD M30 图表上跑一遍 Strategy Tester。

**Tech Stack:** Pine Script v6（TradingView）；`tradingview-mcp`（本机已通过
`claude mcp add tradingview -s user` 注册，工具名前缀 `mcp__tradingview__`，
Claude Code 会话里如果这些工具显示为 deferred，先用 ToolSearch 加载，query 例：
`select:mcp__tradingview__pine_check,mcp__tradingview__pine_set_source,mcp__tradingview__pine_smart_compile,mcp__tradingview__pine_get_errors,mcp__tradingview__pine_new,mcp__tradingview__pine_save,mcp__tradingview__chart_set_symbol,mcp__tradingview__chart_set_timeframe,mcp__tradingview__capture_screenshot,mcp__tradingview__data_get_pine_tables,mcp__tradingview__symbol_search,mcp__tradingview__tv_health_check`）；
TradingView Desktop 需要已用 `--remote-debugging-port=9222` 启动（本机已跑过
`scripts\launch_tv_debug.bat`，如果重启了电脑要重新跑一次或用 `tv_launch`）。

## Global Constraints

- 品种默认 `OANDA:XAUUSD`，周期 M30（`chart_set_timeframe` 传 `"30"`）——Task 1
  第一步会用 `symbol_search` 确认这个代码在当前账号能用，不能用就换成账号里实际
  可用的黄金现货/CFD 代码，后续所有任务沿用同一个品种。
- 唯一交付文件：`TradingBot/pine/gold-m30-ict-strategy.pine`，每个任务往文件末尾
  追加新的代码块，不重排前面任务已写好的代码。
- **验证方式说明（本计划对标准 TDD 步骤的调整）**：Pine Script 没有 pytest 那种
  独立单元测试框架，官方唯一的正确性校验渠道就是编译器和实际图表运行结果。所以
  每个任务的"测试"步骤是：① 用 `pine_check` 做服务端编译校验（离线，不需要开
  图表），确认 `success: true` 且没有报错；② 涉及图表可视表现的任务（Task 1 的
  EMA 线、Task 8-9 的信号箭头/熔断背景色/回测报告）额外用
  `pine_set_source` → `pine_smart_compile` → `capture_screenshot` /
  `data_get_pine_tables` 在真实图表上核对。没有"先写会失败的测试"这一步——编译
  器本身就是校验，代码不对就是编译报错，不存在"先失败再通过"的中间态。
- 所有可调数值都用 `input.*` 声明，不写死进逻辑（对应设计文档第十节）。
- Pine 版本固定 `//@version=6`。
- 每个任务完成后提交一次 git commit，只 `git add` 这一个 `.pine` 文件（仓库里还
  有其他未跟踪文件，不要一起加进去）。

---

## Task 1: 脚本骨架 + 输入参数 + M30 EMA/ATR + 图表环境确认

**Files:**
- Create: `TradingBot/pine/gold-m30-ict-strategy.pine`

**Interfaces:**
- Produces（后续任务按这些精确名字引用，不要改名）:
  `emaFastLen, emaSlowLen, h4EmaLen, atrLen, slAtrMult, tpAtrMult,
  pullbackAtrMult, riskPct, emaFast, emaSlow, atrVal, bullStack, bearStack`

- [ ] **Step 1: 确认品种代码可用 + TradingView 连接正常**

调用 `tv_health_check`，确认 `cdp_connected: true`。再调用
`symbol_search({ query: "XAUUSD" })`，确认返回结果里有 `OANDA:XAUUSD`（没有就记下
账号里实际可用的黄金代码，本计划后续所有 `chart_set_symbol` 调用都用这个确认过
的代码替换）。

- [ ] **Step 2: 创建 Pine 文件**

```pinescript
//@version=6
strategy("Gold M30 Trend+ICT Strategy", shorttitle="GoldM30ICT", overlay=true,
     initial_capital=10000,
     default_qty_type=strategy.fixed,
     commission_type=strategy.commission.percent,
     commission_value=0.03,
     slippage=3)

// ============================================================
// Task 1: 输入参数 + M30 EMA/ATR
// ============================================================

// ---- EMA ----
emaFastLen = input.int(20, "M30 EMA Fast", group="EMA")
emaSlowLen = input.int(50, "M30 EMA Slow", group="EMA")
h4EmaLen   = input.int(50, "H4 EMA", group="EMA")

// ---- ATR / 风险 ----
atrLen           = input.int(14, "ATR Length", group="ATR/Risk")
slAtrMult        = input.float(1.5, "SL ATR Multiple", group="ATR/Risk")
tpAtrMult        = input.float(3.0, "TP ATR Multiple", group="ATR/Risk")
pullbackAtrMult  = input.float(0.3, "回调容差 (x ATR)", group="ATR/Risk")
riskPct          = input.float(1.0, "单笔风险 % of equity", group="ATR/Risk")

// ---- M30 EMA / ATR 计算 ----
emaFast = ta.ema(close, emaFastLen)
emaSlow = ta.ema(close, emaSlowLen)
atrVal  = ta.atr(atrLen)

bullStack = emaFast > emaSlow
bearStack = emaFast < emaSlow

plot(emaFast, "EMA Fast", color=color.blue)
plot(emaSlow, "EMA Slow", color=color.orange)
```

- [ ] **Step 3: 离线编译校验**

调用 `pine_check({ source: <上面完整文件内容> })`。
期望：返回 JSON 里 `success: true`，错误列表为空。有报错就照报错信息修文件后重试。

- [ ] **Step 4: 图表上视觉确认**

依次调用：
1. `chart_set_symbol({ symbol: "OANDA:XAUUSD" })`（或 Step 1 确认过的实际代码）
2. `chart_set_timeframe({ timeframe: "30" })`
3. `pine_new({ type: "strategy" })`
4. `pine_set_source({ source: <文件完整内容> })`
5. `pine_smart_compile()`
6. `pine_get_errors()` —— 期望空数组
7. `capture_screenshot({ region: "chart", wait_for_render: true })` —— 人工确认
   图上出现蓝色/橙色两条 EMA 线，价格在均线附近正常波动，没有报错弹窗

- [ ] **Step 5: 提交**

```bash
cd TradingBot
git add pine/gold-m30-ict-strategy.pine
git commit -m "feat(pine): 黄金M30策略骨架 + 输入参数 + EMA/ATR计算"
```

---

## Task 2: H4 方向判断（EMA50 + BOS/CHoCH，OR 组合）

**Files:**
- Modify: `TradingBot/pine/gold-m30-ict-strategy.pine`（追加到文件末尾）

**Interfaces:**
- Consumes: `h4EmaLen`（Task 1）
- Produces: `h4SwingLookback, h4Close, h4Ema, h4EmaBull, h4EmaBear, h4Bias,
  h4BosBull, h4BosBear, allowedLong, allowedShort`

- [ ] **Step 1: 追加 H4 方向判断代码**

```pinescript

// ============================================================
// Task 2: H4 方向判断（EMA50 + BOS/CHoCH，任一支持即可）
// ============================================================

h4SwingLookback = input.int(5, "H4 BOS/CHoCH 摆动点回看根数", group="Direction")

// H4 EMA 方向
h4Close = request.security(syminfo.tickerid, "240", close, lookahead=barmerge.lookahead_off)
h4Ema   = request.security(syminfo.tickerid, "240", ta.ema(close, h4EmaLen), lookahead=barmerge.lookahead_off)

h4EmaBull = h4Close > h4Ema
h4EmaBear = h4Close < h4Ema

// H4 BOS/CHoCH 结构方向：在 H4 上下文里维护摆动高低点 + 结构偏向状态
h4StructureBias(swingLen) =>
    var float lastSwingHigh = na
    var float lastSwingLow  = na
    var int   bias = 0
    ph = ta.pivothigh(high, swingLen, swingLen)
    pl = ta.pivotlow(low, swingLen, swingLen)
    if not na(ph)
        lastSwingHigh := ph
    if not na(pl)
        lastSwingLow := pl
    if not na(lastSwingHigh) and close > lastSwingHigh
        bias := 1
    if not na(lastSwingLow) and close < lastSwingLow
        bias := -1
    bias

h4Bias = request.security(syminfo.tickerid, "240", h4StructureBias(h4SwingLookback), lookahead=barmerge.lookahead_off)
h4BosBull = h4Bias == 1
h4BosBear = h4Bias == -1

// 方向组合：EMA 或 BOS/CHoCH 任一支持即可（OR）
allowedLong  = h4EmaBull or h4BosBull
allowedShort = h4EmaBear or h4BosBear
```

- [ ] **Step 2: 离线编译校验**

`pine_check({ source: <文件完整内容> })` —— 期望 `success: true`，无报错。
（这一步最容易出的错是 `request.security` 里传函数调用的语法问题，报错信息会
直接指出行号。）

- [ ] **Step 3: 图表验证方向输出**

追加一行临时调试代码到文件末尾（下一个任务开始前会被 Task 9 的正式可视化覆盖，
现在只是验证用）：

```pinescript
plot(h4Bias, "DEBUG h4Bias", display=display.data_window)
```

`pine_set_source` → `pine_smart_compile` → `pine_get_errors`（期望空）。用
`chart_scroll_to_date` 跳到最近日期，`capture_screenshot({ region: "chart" })`
配合 Data Window 面板肉眼确认 `h4Bias` 在明显的多头/空头行情段分别显示 1 / -1，
盘整段显示 0。确认无误后删除这行临时调试代码（正式可视化留给 Task 9）。

- [ ] **Step 4: 提交**

```bash
cd TradingBot
git add pine/gold-m30-ict-strategy.pine
git commit -m "feat(pine): H4方向判断(EMA50 OR BOS/CHoCH)"
```

---

## Task 3: M30 回调前置区域

**Files:**
- Modify: `TradingBot/pine/gold-m30-ict-strategy.pine`（追加到文件末尾）

**Interfaces:**
- Consumes: `emaFast`（Task 1）, `atrVal`（Task 1）, `pullbackAtrMult`（Task 1）,
  `bullStack`, `bearStack`（Task 1）
- Produces: `pullbackTol, inLongPullbackZone, inShortPullbackZone`

- [ ] **Step 1: 追加回调区域代码**

```pinescript

// ============================================================
// Task 3: M30 回调前置区域（价格回到 EMA20 附近才进入评分区）
// ============================================================

pullbackTol = pullbackAtrMult * atrVal
inLongPullbackZone  = bullStack and math.abs(close - emaFast) <= pullbackTol
inShortPullbackZone = bearStack and math.abs(close - emaFast) <= pullbackTol
```

- [ ] **Step 2: 离线编译校验**

`pine_check({ source: <文件完整内容> })` —— 期望 `success: true`。

- [ ] **Step 3: 提交**

```bash
cd TradingBot
git add pine/gold-m30-ict-strategy.pine
git commit -m "feat(pine): M30回调前置区域(EMA20容差)"
```

---

## Task 4: ICT 流动性扫荡评分

**Files:**
- Modify: `TradingBot/pine/gold-m30-ict-strategy.pine`（追加到文件末尾）

**Interfaces:**
- Consumes: `atrVal`（Task 1）
- Produces: `sweepLookback, sweepScoreMult, bullSweep, bearSweep,
  bullSweepScore, bearSweepScore`

- [ ] **Step 1: 追加流动性扫荡评分代码**

```pinescript

// ============================================================
// Task 4: ICT 因子① 流动性扫荡评分（0-100）
// ============================================================

sweepLookback  = input.int(10, "扫荡摆动点回看根数", group="ICT")
sweepScoreMult = input.float(200.0, "扫荡评分系数", group="ICT")

sweepSwingHigh = ta.pivothigh(high, sweepLookback, sweepLookback)
sweepSwingLow  = ta.pivotlow(low, sweepLookback, sweepLookback)

var float lastSwingHighPx = na
var float lastSwingLowPx  = na
if not na(sweepSwingHigh)
    lastSwingHighPx := sweepSwingHigh
if not na(sweepSwingLow)
    lastSwingLowPx := sweepSwingLow

// 触发：影线刺穿摆动点，但收盘收回点内侧
bullSweep = not na(lastSwingLowPx) and low < lastSwingLowPx and close > lastSwingLowPx
bearSweep = not na(lastSwingHighPx) and high > lastSwingHighPx and close < lastSwingHighPx

bullSweepDepth = bullSweep ? (lastSwingLowPx - low) : 0.0
bearSweepDepth = bearSweep ? (high - lastSwingHighPx) : 0.0

bullSweepScore = bullSweep ? math.min(100.0, (bullSweepDepth / atrVal) * sweepScoreMult) : 0.0
bearSweepScore = bearSweep ? math.min(100.0, (bearSweepDepth / atrVal) * sweepScoreMult) : 0.0
```

- [ ] **Step 2: 离线编译校验**

`pine_check({ source: <文件完整内容> })` —— 期望 `success: true`。

- [ ] **Step 3: 图表验证评分行为**

临时追加：
```pinescript
plot(bullSweepScore, "DEBUG bullSweepScore", display=display.data_window)
plot(bearSweepScore, "DEBUG bearSweepScore", display=display.data_window)
```
`pine_set_source` → `pine_smart_compile` → `pine_get_errors`（期望空）。找图上一根
明显插针后收回的 K 线，用 `chart_scroll_to_date` 跳过去，核对 Data Window 里对应
方向的评分是 0-100 之间的非零值，非扫荡的普通 K 线评分应为 0。确认后删除这两行
临时调试代码。

- [ ] **Step 4: 提交**

```bash
cd TradingBot
git add pine/gold-m30-ict-strategy.pine
git commit -m "feat(pine): ICT流动性扫荡评分"
```

---

## Task 5: ICT 订单块回踩评分

**Files:**
- Modify: `TradingBot/pine/gold-m30-ict-strategy.pine`（追加到文件末尾）

**Interfaces:**
- Consumes: `atrVal`（Task 1）
- Produces: `obLookback, obImpulseAtrMult, obImpulseScoreDiv, obDecayBars,
  obDecayPct, obMaxAgeBars, bullObHigh, bullObLow, bullObTap, bearObTap,
  bullObScore, bearObScore`

（`obLookback` 目前只作为文档化输入保留，冲量判定实际用的是相邻两根 K 线的差值，
不需要遍历回看窗口——先留着这个输入方便以后想改成"窗口内最大冲量"时用，不占实现
复杂度。）

- [ ] **Step 1: 追加订单块评分代码**

```pinescript

// ============================================================
// Task 5: ICT 因子② 订单块回踩评分（0-100，只跟踪最近一个有效OB）
// ============================================================

obLookback        = input.int(20, "OB回看窗口(预留)", group="ICT")
obImpulseAtrMult  = input.float(2.0, "OB冲量阈值 (x ATR)", group="ICT")
obImpulseScoreDiv = input.float(25.0, "OB评分系数", group="ICT")
obDecayBars       = input.int(5, "OB衰减步长(根)", group="ICT")
obDecayPct        = input.float(10.0, "OB每步衰减%", group="ICT")
obMaxAgeBars      = input.int(50, "OB最大有效期(根)", group="ICT")

var float bullObHigh     = na
var float bullObLow      = na
var int   bullObBar      = na
var float bullObImpulse  = na

var float bearObHigh     = na
var float bearObLow      = na
var int   bearObBar      = na
var float bearObImpulse  = na

impulseThreshold = obImpulseAtrMult * atrVal
barMove = close - close[1]

// 强势上冲：标记冲量前最后一根反向(阴)K线为多头订单块
if barMove >= impulseThreshold and close[1] < open[1]
    bullObHigh    := high[1]
    bullObLow     := low[1]
    bullObBar     := bar_index[1]
    bullObImpulse := barMove

// 强势下冲：标记冲量前最后一根反向(阳)K线为空头订单块
if -barMove >= impulseThreshold and close[1] > open[1]
    bearObHigh    := high[1]
    bearObLow     := low[1]
    bearObBar     := bar_index[1]
    bearObImpulse := -barMove

bullObAge = na(bullObBar) ? na : bar_index - bullObBar
bearObAge = na(bearObBar) ? na : bar_index - bearObBar

bullObValid = not na(bullObBar) and bullObAge <= obMaxAgeBars
bearObValid = not na(bearObBar) and bearObAge <= obMaxAgeBars

bullObTap = bullObValid and low <= bullObHigh and high >= bullObLow
bearObTap = bearObValid and high >= bearObLow and low <= bearObHigh

bullObDecaySteps = bullObValid ? math.floor(bullObAge / obDecayBars) : 0.0
bearObDecaySteps = bearObValid ? math.floor(bearObAge / obDecayBars) : 0.0

bullObBaseScore = bullObValid ? math.min(100.0, (bullObImpulse / atrVal) * obImpulseScoreDiv) : 0.0
bearObBaseScore = bearObValid ? math.min(100.0, (bearObImpulse / atrVal) * obImpulseScoreDiv) : 0.0

bullObDecayFactor = math.max(0.0, 1.0 - (bullObDecaySteps * obDecayPct / 100.0))
bearObDecayFactor = math.max(0.0, 1.0 - (bearObDecaySteps * obDecayPct / 100.0))

bullObScore = bullObTap ? bullObBaseScore * bullObDecayFactor : 0.0
bearObScore = bearObTap ? bearObBaseScore * bearObDecayFactor : 0.0
```

- [ ] **Step 2: 离线编译校验**

`pine_check({ source: <文件完整内容> })` —— 期望 `success: true`。

- [ ] **Step 3: 图表验证订单块区域**

临时追加：
```pinescript
plot(bullObValid ? bullObHigh : na, "DEBUG bullObHigh", display=display.data_window)
plot(bullObValid ? bullObLow  : na, "DEBUG bullObLow", display=display.data_window)
plot(bullObScore, "DEBUG bullObScore", display=display.data_window)
plot(bearObScore, "DEBUG bearObScore", display=display.data_window)
```
`pine_set_source` → `pine_smart_compile` → `pine_get_errors`（期望空）。找一段明显
的单根大阳线冲量（涨幅 ≥ 2×ATR），核对冲量前一根阴线的高低点被记录为
`bullObHigh`/`bullObLow`；价格后续回踩到这个区间时 `bullObScore` 应为非零值。
确认后删除临时调试代码。

- [ ] **Step 4: 提交**

```bash
cd TradingBot
git add pine/gold-m30-ict-strategy.pine
git commit -m "feat(pine): ICT订单块回踩评分"
```

---

## Task 6: ICT 公允价值缺口（FVG）回补评分

**Files:**
- Modify: `TradingBot/pine/gold-m30-ict-strategy.pine`（追加到文件末尾）

**Interfaces:**
- Consumes: `atrVal`（Task 1）
- Produces: `fvgScoreMult, bullFvgTouch, bearFvgTouch, bullFvgScore, bearFvgScore`

- [ ] **Step 1: 追加 FVG 评分代码**

```pinescript

// ============================================================
// Task 6: ICT 因子③ 公允价值缺口(FVG)回补评分（0-100，只跟踪最近一个未回补缺口）
// ============================================================

fvgScoreMult = input.float(150.0, "FVG评分系数", group="ICT")

var float bullFvgTop    = na
var float bullFvgBottom = na
var bool  bullFvgFilled = true

var float bearFvgTop    = na
var float bearFvgBottom = na
var bool  bearFvgFilled = true

// 看涨缺口：K1(2根前).high < K3(当前).low
newBullGapTop    = low
newBullGapBottom = high[2]
if newBullGapTop > newBullGapBottom
    bullFvgTop    := newBullGapTop
    bullFvgBottom := newBullGapBottom
    bullFvgFilled := false

// 看跌缺口：K1(2根前).low > K3(当前).high
newBearGapBottom = high
newBearGapTop    = low[2]
if newBearGapBottom < newBearGapTop
    bearFvgTop    := newBearGapTop
    bearFvgBottom := newBearGapBottom
    bearFvgFilled := false

// 价格完全穿越缺口视为已回补，停止评分
if not bullFvgFilled and not na(bullFvgBottom) and low <= bullFvgBottom
    bullFvgFilled := true
if not bearFvgFilled and not na(bearFvgTop) and high >= bearFvgTop
    bearFvgFilled := true

bullFvgTouch = not bullFvgFilled and not na(bullFvgTop) and low <= bullFvgTop and low >= bullFvgBottom
bearFvgTouch = not bearFvgFilled and not na(bearFvgBottom) and high >= bearFvgBottom and high <= bearFvgTop

bullFvgSize = na(bullFvgTop) ? 0.0 : (bullFvgTop - bullFvgBottom)
bearFvgSize = na(bearFvgTop) ? 0.0 : (bearFvgTop - bearFvgBottom)

bullFvgScore = bullFvgTouch ? math.min(100.0, (bullFvgSize / atrVal) * fvgScoreMult) : 0.0
bearFvgScore = bearFvgTouch ? math.min(100.0, (bearFvgSize / atrVal) * fvgScoreMult) : 0.0
```

- [ ] **Step 2: 离线编译校验**

`pine_check({ source: <文件完整内容> })` —— 期望 `success: true`。

- [ ] **Step 3: 图表验证缺口检测**

临时追加：
```pinescript
plot(na(bullFvgTop) ? na : bullFvgTop, "DEBUG bullFvgTop", display=display.data_window)
plot(na(bullFvgBottom) ? na : bullFvgBottom, "DEBUG bullFvgBottom", display=display.data_window)
plot(bullFvgScore, "DEBUG bullFvgScore", display=display.data_window)
```
`pine_set_source` → `pine_smart_compile` → `pine_get_errors`（期望空）。找一段 3
根 K 线里有明显跳空（K1 高点和 K3 低点之间有价格真空）的行情，核对
`bullFvgTop`/`bullFvgBottom` 记录了正确的缺口区间；价格回补进入缺口时
`bullFvgScore` 非零，完全穿越缺口后归零（`bullFvgFilled` 变 true，不再评分）。
确认后删除临时调试代码。

- [ ] **Step 4: 提交**

```bash
cd TradingBot
git add pine/gold-m30-ict-strategy.pine
git commit -m "feat(pine): ICT公允价值缺口(FVG)回补评分"
```

---

## Task 7: 日内熔断风控

**Files:**
- Modify: `TradingBot/pine/gold-m30-ict-strategy.pine`（追加到文件末尾）

**Interfaces:**
- Consumes: 无（只依赖 Pine 内置的 `strategy.equity` / `strategy.closedtrades`，
  这两个在没有任何交易时分别返回 `initial_capital` 和 `0`，编译和运行都不受影响）
- Produces: `dailyLossPct, maxConsecLosses, dayStartEquity, consecLosses,
  circuitBreakerActive`

- [ ] **Step 1: 追加日内熔断代码**

```pinescript

// ============================================================
// Task 7: 日内熔断风控
// ============================================================

dailyLossPct    = input.float(3.0, "单日最大亏损% (熔断)", group="Risk Guard")
maxConsecLosses = input.int(3, "单日最大连续亏损笔数 (熔断)", group="Risk Guard")

var float dayStartEquity  = na
var int   consecLosses    = 0
var int   lastClosedCount = 0

isNewTradingDay = dayofmonth != dayofmonth[1] or month != month[1] or year != year[1]
if isNewTradingDay or na(dayStartEquity)
    dayStartEquity := strategy.equity
    consecLosses := 0

if strategy.closedtrades > lastClosedCount
    lastTradeProfit = strategy.closedtrades.profit(strategy.closedtrades - 1)
    consecLosses := lastTradeProfit < 0 ? consecLosses + 1 : 0
    lastClosedCount := strategy.closedtrades

dailyLossHit  = (strategy.equity - dayStartEquity) <= -(dayStartEquity * dailyLossPct / 100.0)
consecLossHit = consecLosses >= maxConsecLosses
circuitBreakerActive = dailyLossHit or consecLossHit
```

- [ ] **Step 2: 离线编译校验**

`pine_check({ source: <文件完整内容> })` —— 期望 `success: true`。这一步因为还没
有任何 `strategy.entry`，`circuitBreakerActive` 恒为 `false`，属于预期行为，
Task 8 接上下单逻辑后才能真正观察到熔断触发。

- [ ] **Step 3: 提交**

```bash
cd TradingBot
git add pine/gold-m30-ict-strategy.pine
git commit -m "feat(pine): 日内熔断风控(单日亏损%+连续亏损笔数)"
```

---

## Task 8: 入场判定 + 下单 + 止盈止损 + 仓位管理

**Files:**
- Modify: `TradingBot/pine/gold-m30-ict-strategy.pine`（追加到文件末尾）

**Interfaces:**
- Consumes: `allowedLong, allowedShort`（Task 2）; `inLongPullbackZone,
  inShortPullbackZone`（Task 3）; `bullSweepScore, bearSweepScore`（Task 4）;
  `bullObScore, bearObScore`（Task 5）; `bullFvgScore, bearFvgScore`（Task 6）;
  `circuitBreakerActive`（Task 7）; `atrVal, slAtrMult, tpAtrMult, riskPct`
  （Task 1）
- Produces: `singleThreshold, comboThreshold, longSignal, shortSignal,
  slDistanceLong, slDistanceShort, tpDistanceLong, tpDistanceShort, qtyLong,
  qtyShort`

- [ ] **Step 1: 追加入场判定 + 下单代码**

```pinescript

// ============================================================
// Task 8: 入场判定(三因子评分) + 下单 + 止盈止损 + 仓位管理
// ============================================================

singleThreshold = input.float(80.0, "单因子直接入场阈值", group="Entry")
comboThreshold  = input.float(120.0, "共振(求和)入场阈值", group="Entry")

longMaxScore = math.max(bullSweepScore, math.max(bullObScore, bullFvgScore))
longSumScore = bullSweepScore + bullObScore + bullFvgScore
shortMaxScore = math.max(bearSweepScore, math.max(bearObScore, bearFvgScore))
shortSumScore = bearSweepScore + bearObScore + bearFvgScore

longSignal  = allowedLong  and inLongPullbackZone  and (longMaxScore >= singleThreshold or longSumScore >= comboThreshold)
shortSignal = allowedShort and inShortPullbackZone and (shortMaxScore >= singleThreshold or shortSumScore >= comboThreshold)

// ATR 止盈止损
slDistanceLong  = slAtrMult * atrVal
slDistanceShort = slAtrMult * atrVal
tpDistanceLong  = tpAtrMult * atrVal
tpDistanceShort = tpAtrMult * atrVal

// 按权益风险% 反推手数
riskAmount = strategy.equity * (riskPct / 100.0)
qtyLong  = slDistanceLong  > 0 ? riskAmount / slDistanceLong  : 0.0
qtyShort = slDistanceShort > 0 ? riskAmount / slDistanceShort : 0.0

// 下单：同一时间只持一笔仓位；反向信号触发时 strategy.entry 默认先平仓再反手
if longSignal and not circuitBreakerActive and strategy.position_size <= 0
    strategy.entry("Long", strategy.long, qty=qtyLong)
    strategy.exit("Long Exit", from_entry="Long", stop=close - slDistanceLong, limit=close + tpDistanceLong)

if shortSignal and not circuitBreakerActive and strategy.position_size >= 0
    strategy.entry("Short", strategy.short, qty=qtyShort)
    strategy.exit("Short Exit", from_entry="Short", stop=close + slDistanceShort, limit=close - tpDistanceShort)
```

- [ ] **Step 2: 离线编译校验**

`pine_check({ source: <文件完整内容> })` —— 期望 `success: true`。

- [ ] **Step 3: 图表上跑回测，核对交易行为**

1. `chart_set_symbol({ symbol: "OANDA:XAUUSD" })`（或 Task 1 确认过的代码）
2. `chart_set_timeframe({ timeframe: "30" })`
3. `pine_set_source({ source: <文件完整内容> })`
4. `pine_smart_compile()`
5. `pine_get_errors()` —— 期望空数组
6. `capture_screenshot({ region: "strategy_tester", wait_for_render: true })`
   —— 确认 Strategy Tester 面板出现且有交易记录（Total Trades > 0）；如果是 0
   笔交易，检查 `longSignal`/`shortSignal` 的三个前置条件（方向/回调区/评分）是
   不是同时满足过——这通常是三因子阈值设得偏高，属于预期的下一步调参范围，不是
   代码 bug，除非编译报错或者 Total Trades 报错。
7. `data_get_pine_tables()`（此时还没有 Task 9 的调试面板，返回空是正常的，先跳
   过，Task 9 会补上再做完整核对）

- [ ] **Step 4: 提交**

```bash
cd TradingBot
git add pine/gold-m30-ict-strategy.pine
git commit -m "feat(pine): 三因子入场判定+ATR止盈止损+权益风险仓位管理"
```

---

## Task 9: 可视化收尾 + 端到端验证 + 保存到 TradingView 云端

**Files:**
- Modify: `TradingBot/pine/gold-m30-ict-strategy.pine`（追加到文件末尾）

**Interfaces:**
- Consumes: 前 8 个任务产出的全部变量（`allowedLong/allowedShort`,
  `longSignal/shortSignal`, `circuitBreakerActive`, `bullSweepScore` 等六个评分
  变量, `singleThreshold`, `comboThreshold`）
- Produces: 无新逻辑变量，只有绘图/表格的展示层代码

- [ ] **Step 1: 追加可视化代码**

```pinescript

// ============================================================
// Task 9: 图表可视化
// ============================================================

// H4 方向背景色
bgcolor(allowedLong and not allowedShort ? color.new(color.green, 90) : allowedShort and not allowedLong ? color.new(color.red, 90) : na, title="H4 Direction")

// 熔断状态背景色
bgcolor(circuitBreakerActive ? color.new(color.gray, 60) : na, title="Circuit Breaker")

// 入场箭头
plotshape(longSignal, title="Long Entry", style=shape.triangleup, location=location.belowbar, color=color.green, size=size.small)
plotshape(shortSignal, title="Short Entry", style=shape.triangledown, location=location.abovebar, color=color.red, size=size.small)

// 触发因子标签
longTriggerLabel = bullSweepScore >= singleThreshold ? "扫荡" : bullObScore >= singleThreshold ? "订单块" : bullFvgScore >= singleThreshold ? "FVG" : longSumScore >= comboThreshold ? "共振" : ""
shortTriggerLabel = bearSweepScore >= singleThreshold ? "扫荡" : bearObScore >= singleThreshold ? "订单块" : bearFvgScore >= singleThreshold ? "FVG" : shortSumScore >= comboThreshold ? "共振" : ""

if longSignal
    label.new(bar_index, low - atrVal, longTriggerLabel, style=label.style_label_up, color=color.green, textcolor=color.white, size=size.tiny)
if shortSignal
    label.new(bar_index, high + atrVal, shortTriggerLabel, style=label.style_label_down, color=color.red, textcolor=color.white, size=size.tiny)

// 三因子实时评分调试面板
var table debugTable = table.new(position.top_right, 2, 4, bgcolor=color.new(color.black, 70), border_width=1)
if barstate.islast
    table.cell(debugTable, 0, 0, "因子", text_color=color.white)
    table.cell(debugTable, 1, 0, "多/空评分", text_color=color.white)
    table.cell(debugTable, 0, 1, "扫荡", text_color=color.white)
    table.cell(debugTable, 1, 1, str.tostring(bullSweepScore, "#.#") + " / " + str.tostring(bearSweepScore, "#.#"), text_color=color.yellow)
    table.cell(debugTable, 0, 2, "订单块", text_color=color.white)
    table.cell(debugTable, 1, 2, str.tostring(bullObScore, "#.#") + " / " + str.tostring(bearObScore, "#.#"), text_color=color.yellow)
    table.cell(debugTable, 0, 3, "FVG", text_color=color.white)
    table.cell(debugTable, 1, 3, str.tostring(bullFvgScore, "#.#") + " / " + str.tostring(bearFvgScore, "#.#"), text_color=color.yellow)
```

- [ ] **Step 2: 离线编译校验**

`pine_check({ source: <文件完整内容> })` —— 期望 `success: true`。

- [ ] **Step 3: 端到端验证（对应设计文档第十二节）**

1. `chart_set_symbol({ symbol: "OANDA:XAUUSD" })`
2. `chart_set_timeframe({ timeframe: "30" })`
3. `pine_set_source({ source: <文件完整内容> })`
4. `pine_smart_compile()`
5. `pine_get_errors()` —— 期望空数组
6. `capture_screenshot({ region: "chart", wait_for_render: true })` —— 确认能看到
   EMA 线、H4 方向背景色、入场箭头、触发因子标签
7. `data_get_pine_tables()` —— 确认返回调试面板的四行评分数据，格式形如
   `"12.3 / 0.0"`
8. `capture_screenshot({ region: "strategy_tester" })` —— 读回测报告：胜率、
   盈亏比、最大回撤、净利润、交易笔数，人工判断这版策略数据是否值得继续（这是
   给用户看的结果，不是这次实现任务本身的通过/失败标准）
9. 抽样核对熔断：在回测交易记录里找一个熔断应该触发的场景（同一天已经有 3 笔
   亏损，或者当日浮亏超过 3%），确认那一天之后没有新开仓——可以结合
   `capture_screenshot({ region: "chart" })` 看灰色熔断背景色出现的日期，和
   Strategy Tester 交易列表里最后一笔亏损单的时间对比
10. `pine_save()` —— 保存到 TradingView 云端账号，脚本名沿用
    `Gold M30 Trend+ICT Strategy`（Task 1 里 `strategy()` 声明的名字），方便后续
    用 `pine_open({ name: "Gold M30 Trend+ICT Strategy" })` 找回

- [ ] **Step 4: 提交**

```bash
cd TradingBot
git add pine/gold-m30-ict-strategy.pine
git commit -m "feat(pine): 图表可视化收尾 + 端到端回测验证"
```

---

## 完成后交付物

- `TradingBot/pine/gold-m30-ict-strategy.pine`：完整策略脚本，已保存到用户
  TradingView 云端账号
- 一份 Strategy Tester 回测报告截图（Task 9 Step 3.8 产出），供用户判断数据好
  不好、要不要往 MetaTrader EA 方向推进（那是设计文档里明确标注的下一个独立项目，
  不在本计划范围内）
