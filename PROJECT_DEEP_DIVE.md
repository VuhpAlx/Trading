# PROJECT DEEP DIVE — Quant Trading Terminal

> Tài liệu chi tiết toàn bộ cấu trúc, logic xử lý, và UI của hệ thống.  
> Cập nhật: 2026-06-10

---

## 1. TỔNG QUAN HỆ THỐNG

Đây là một terminal giao dịch crypto real-time chạy trên local machine. Backend Python stream dữ liệu từ Binance WebSocket, tính toán indicator kỹ thuật, chạy engine ML tự học, và đẩy kết quả lên browser qua WebSocket. Không có database — toàn bộ state lưu trong RAM (dict + DataFrame) và file JSONL trên disk.

**Stack:**
- Python 3.13 + FastAPI + uvicorn
- pandas / pandas-ta (indicator)
- websockets + httpx (I/O)
- Lightweight Charts (JS, CDN từ unpkg)
- Vanilla JS (no framework)

**Lệnh chạy:**
```
uvicorn main:app --host 127.0.0.1 --port 8000 --reload
```

**Symbols:** BTCUSDT, ETHUSDT, BNBUSDT, PAXGUSDT  
**Timeframes:** 1m, 5m, 15m, 30m, 1h

---

## 2. CẤU TRÚC FILE

```
Trading2/
├── main.py                  — FastAPI app, WebSocket server, REST endpoints
├── config.py                — Constants: SYMBOLS, TIMEFRAMES, cache sizes
├── data_layer.py            — Binance REST bootstrap + WebSocket stream + cache
├── indicator_layer.py       — Tính toán 15+ indicator trên DataFrame
├── signal_engine.py         — AdaptiveScorer + TradeSimulator + AdvancedSignalEngine
├── simulator_manager.py     — ManualTradeSimulator (lệnh thủ công của user)
├── ai_advisor.py            — Phân tích rule-based RR + entry quality
├── static/index.html        — Toàn bộ frontend (single file)
│
├── ml_training_data_*.jsonl — Log dự đoán + kết quả (1 dòng JSON/prediction)
├── trade_history_*.jsonl    — Log lịch sử auto-trade (1 dòng JSON/trade)
│
├── trade_engine.py          — [UNUSED] TradeEngine mới hơn (dataclass, pending orders)
├── prediction_engine.py     — [UNUSED] PredictionEngine EMA-smoothed
├── training_engine.py       — [UNUSED] AdaptiveTrainingEngine buffered
├── position_manager.py      — [DEAD] không import ở đâu
├── core_trading_system.py   — [DEAD] có app=FastAPI() riêng, không dùng
└── BK/                      — Backup các file cũ, bỏ qua
```

---

## 3. CONFIG (config.py)

```python
SYMBOLS        = ["BTCUSDT", "ETHUSDT", "BNBUSDT", "PAXGUSDT"]
TIMEFRAMES     = ["1m", "5m", "15m", "30m", "1h"]
HISTORY_LIMIT  = 1000   # Số nến fetch khi bootstrap
MAX_CACHE_SIZE = 1000   # Giữ tối đa 1000 nến trong RAM
INDICATOR_WINDOW = 260  # Chỉ tính indicator trên 260 nến cuối (SMA200 cần 200+)
```

Tổng số engine được khởi tạo: 4 symbols × 5 timeframes = **20 engines**.

---

## 4. DATA LAYER (data_layer.py)

### 4.1 Cấu trúc cache

```python
cache: dict = {
    "BTCUSDT": {
        "1m":  pd.DataFrame([...]),  # timestamp, open, high, low, close, volume
        "5m":  pd.DataFrame([...]),
        "15m": pd.DataFrame([...]),
        "30m": pd.DataFrame([...]),
        "1h":  pd.DataFrame([...]),
    },
    "ETHUSDT": { ... },
    ...
}
```

### 4.2 Bootstrap (async)

`bootstrap(symbol, interval)` — gọi Binance REST API:
```
GET https://api.binance.com/api/v3/klines?symbol=BTCUSDT&interval=1m&limit=1000
```
- Dùng `httpx.AsyncClient` (non-blocking, đã fix từ bug requests.get cũ)
- Parse 12-column response → giữ 6 cột: timestamp, open, high, low, close, volume
- `timestamp` convert sang `pd.Timestamp UTC`

`bootstrap_all()` — chạy tất cả 20 combinations concurrently qua `asyncio.gather()`

### 4.3 WebSocket Stream

Kết nối single combined stream Binance:
```
wss://stream.binance.com:9443/stream?streams=btcusdt@kline_1m/btcusdt@kline_5m/.../paxgusdt@kline_1h
```
- Tổng 20 streams trong 1 connection
- Auto-reconnect sau 5 giây nếu ngắt

### 4.4 _update_cache logic

Mỗi message WebSocket có 2 case:
1. **Candle đang hình thành** (cùng timestamp): in-place update 4 giá trị (high, low, close, volume) trực tiếp vào DataFrame cell bằng `df.at[last_idx, col]` — **không allocate memory mới**
2. **Candle mới** (timestamp khác): `pd.concat([df.iloc[1:], new_row])` — bỏ nến đầu, thêm nến mới, giữ kích thước cố định 1000 nến

Sau cả 2 case: gọi `on_tick_callback(sym, interval, is_closed, tick_data)`

`tick_data` dict được gửi lên WebSocket frontend:
```python
{
    "time":  int(k['t'] / 1000),  # Unix seconds
    "open":  float,
    "high":  float,
    "low":   float,
    "close": float,
}
```

---

## 5. INDICATOR LAYER (indicator_layer.py)

### 5.1 Caching (MTF optimization)

`apply_indicators_cached(df, symbol, interval)`:
- Cache key: `(symbol, interval)`
- Cache value: `(last_timestamp, result_df)`
- Nếu `last_timestamp` giống cache → return cached ngay, **không recompute**
- Mục đích: MTF context (5m, 15m, 1h) không cần recompute mỗi tick mà chỉ recompute khi nến mới

### 5.2 apply_indicators (static method)

Luôn dùng **260 nến cuối** để tránh tính trên 1000 nến toàn bộ:
```python
df = df.tail(INDICATOR_WINDOW).copy().reset_index(drop=True)
```

**Stack indicator được tính:**

| Group | Indicators | Thư viện |
|-------|-----------|---------|
| Trend | EMA_9, EMA_21, EMA_50, SMA_200 | pandas-ta |
| MACD | MACD line, Histogram (MACD_H), Signal (MACD_S) | pandas-ta |
| Momentum | RSI_14, STOCH_K, STOCH_D | pandas-ta |
| Volatility | BB_L, BB_M, BB_U (20,2σ), ATR_14 | pandas-ta |
| Trend Strength | ADX, DI_PLUS, DI_MINUS (length=14) | pandas-ta |
| Volume | VOL_MA (SMA20), OBV, VWAP | pandas-ta |

**Lưu ý pandas-ta naming:**
- MACD histogram column → `MACDh_*` → mapped thành `MACD_H`
- BB bands: col[0]=lower, col[1]=mid, col[2]=upper
- ADX result: col[0]=ADX, col[1]=DI+, col[2]=DI-

Kết thúc: `.bfill().ffill().fillna(0.0)` để tránh NaN

---

## 6. SIGNAL ENGINE (signal_engine.py)

Gồm 3 class: `AdaptiveScorer`, `TradeSimulator`, `AdvancedSignalEngine`

---

### 6.1 AdaptiveScorer — ML tự học

**Khái niệm:** Theo dõi 8 "indicators" như các agent độc lập, mỗi indicator có 1 weight. Khi prediction đúng → tăng weight của indicator đã vote đúng. Khi sai → giảm.

**8 signal keys:** `EMA, MACD, RSI, BB, VWAP, VOLUME, STOCH, MTF`

**Khởi tạo weights:**
```python
weights = {k: 1.0 for k in SIGNAL_KEYS}
weights['MTF'] = 1.5   # MTF được prior cao hơn
```

**Bounds:** `[0.2, 3.0]` — không để indicator nào bị tắt hoàn toàn hoặc dominate tuyệt đối

**Weight normalization khi output:**
```python
total = sum(weights.values())
return {k: round(v / total, 4) for k, v in weights.items()}
```

#### Load history từ JSONL

Khi khởi tạo, đọc **500 dòng cuối** của `ml_training_data_{symbol}_{tf}.jsonl`:
- Mỗi dòng: parse result (WIN/LOSS/PARTIAL), raw_signals, direction
- Gọi `_apply_result_to_stats()` để build running stats
- Gọi `_adjust_weights()` với `lr=0.01` (thấp hơn live: 0.05) để replay học nhẹ
- Build `recent_results` list (15 entries hiển thị trên tracker)
- Tính lại `ema_winrate` và kiểm tra `reverse_mode`

#### _adjust_weights logic

```python
for ind, val in raw_sigs.items():
    is_correct = (val > 0 and "BULL" in direction) or (val < 0 and "BEAR" in direction)
    if is_correct:
        weights[ind] *= (1 + lr * factor)
    weights[ind] = clamp(0.2, 3.0)
```

`factor` = `{"WIN": 1.0, "PARTIAL": 0.2, "LOSS": -0.8}`

#### Prediction evaluation

`register_prediction(target_time, payload)`:
- target_time = `c_time + tf_seconds * 5` (ví dụ 1m: cộng thêm 5 phút)
- Lưu vào `pending_predictions[target_time]`

`evaluate_and_learn(current_time_sec, current_close)`:
- Scan tất cả pending predictions có `target_time <= current_time`
- So sánh `current_close` với range min/max và direction
- **WIN**: đúng hướng VÀ trong range
- **PARTIAL**: đúng hướng nhưng ngoài range
- **LOSS**: sai hướng
- Gọi `_adjust_weights()` với `lr=0.05`
- Update `ema_winrate` (alpha=0.15)
- Ghi vào JSONL log

#### EMA Winrate

```python
# Exponential Moving Average của win/loss
alpha = 0.15
ema_winrate = alpha * (100 if WIN else 0) + (1 - alpha) * ema_winrate
```
Half-life ~4 trades — phản ứng nhanh hơn simple winrate.

#### Reverse Mode

- Bật khi: `total >= 20` trades AND `winrate < 38%`
- Tắt khi: `winrate >= 47%`
- Hiệu quả: đảo ngược toàn bộ signal BUY↔SELL khi hệ thống "đoán ngược"

---

### 6.2 TradeSimulator — Auto trade

**Vòng đời trade:**
1. `open_position()` → state="LONG"/"SHORT", tính TP/SL
2. `process_tick()` mỗi tick → kiểm tra SL/TP hit
3. Khi close → log vào JSONL, cộng/trừ capital, cooldown 5 bars

**TP/SL sizing (risk-adjusted):**
```python
raw_sl  = atr * 1.5
max_sl  = price * 0.0025   # Hard cap 0.25%
min_sl  = price * 0.0005   # Hard floor 0.05%
sl_dist = clamp(min_sl, raw_sl, max_sl)
tp_dist = sl_dist * 1.8    # R:R = 1:1.8
```

**Position sizing:**
```python
pos_size   = (capital * risk_pct) / sl_dist   # risk_pct = 1.5%
volume_usd = pos_size * price
```

**TP/SL hit check (với buffer nhỏ tránh false trigger):**
```python
sl_buf = entry * 0.0002   # 0.02% buffer
# LONG:
if low  <= (sl - sl_buf)  → LOSS
if high >= tp             → WIN
# SHORT:
if high >= (sl + sl_buf)  → LOSS
if low  <= tp             → WIN
```

**Cooldown:** 5 bars sau khi đóng trade — tránh re-entry ngay lập tức.

**Lưu history:** 20 trades gần nhất trong RAM; unlimited trong JSONL.

---

### 6.3 AdvancedSignalEngine — Master engine

Mỗi instance: 1 symbol + 1 timeframe. Có 20 instances tổng cộng.

**State tracking:**
```python
smoothed_score:  float = 0.0    # EMA của raw score
confirm_counter: int   = 0      # Đếm liên tiếp cùng direction
current_dir:     str   = "HOLD"
last_candle_time:       = None  # Chống re-run trên cùng 1 nến
hold_counter:    int   = 0      # Đếm bars không có trade (cho adaptive threshold)
```

#### generate_signal() — Flow chính

**Bước 1: TP/SL check** (mỗi tick)
```python
closed_trade = self.trade_sim.process_tick(c_price, c_high, c_low)
if closed_trade:
    self.scorer.learn_from_real_trade(...)
```

**Bước 2: Extract indicators** từ row cuối DataFrame

**Bước 3: Logic chỉ chạy trên CANDLE MỚI** (`last_candle_time != c_time`)

Nếu là nến mới:

3a. **Evaluate past predictions:**
```python
self.scorer.evaluate_and_learn(c_time, float(curr['close']))
```

3b. **MTF Score** (5m, 15m, 1h):
```python
for tf in ["5m", "15m", "1h"]:
    w = 1.5 if adx > 25 else 1.0   # Trend market → higher weight
    mtf_score += (1.0 if ema9 > ema21 else -1.0) * w
mtf_score /= total_weight
```

3c. **8 Signal Scores** (mỗi cái trong [-1, +1]):

| Signal | Logic |
|--------|-------|
| **EMA** | `ema_cross * (0.55 + abs(slope_norm) * 0.45)` — cross direction + slope momentum |
| **MACD** | `macd_norm + macd_cross_bonus` — histogram normalized + fresh cross bonus ±0.5 |
| **RSI** | `(rsi - 50) / 20` — linear: 50→0, 70→+1, 30→-1 |
| **BB** | Nếu BB width < 0.2% → 0. Ngược lại: `(0.5 - band_position) * 2` — giá gần lower → bullish |
| **VWAP** | `+1 nếu price > vwap, -1 nếu price < vwap` |
| **VOLUME** | Chỉ tính khi `vol_ratio > 1.2`: directional surge `vol_dir * (vol_ratio - 1) * 0.8` |
| **STOCH** | Zones: K<25 & D<25 → +0.8/+0.5; K>75 & D>75 → -0.8/-0.5; cross zones → ±0.45 |
| **MTF** | Kết quả tổng hợp 3 timeframes từ bước 3b |

3d. **Weighted score:**
```python
raw_score = sum(raw_sigs[k] * weights[k] for k in raw_sigs)
smoothed_score = 0.35 * raw_score + 0.65 * smoothed_score   # EMA smoothing
```
Lý do EMA: tránh flip signal quá nhanh, cần 2+ bars confirm.

3e. **Regime Detection:**
```python
if bb_width < 0.0025:             → "SQUEEZE"
elif adx > 25 and |DI+-DI-| > 5: → "TREND"
elif adx < 18:                    → "RANGE"
else:                             → "NEUTRAL"
```

3f. **Adaptive Threshold:**
```python
base_thresh = 0.33 if regime == "TREND" else 0.40
# Suy giảm dần nếu không có trade quá lâu (tối đa 0.12)
decay = min(0.12, max(0, (hold_counter - 20) * 0.007))
current_thresh = base_thresh - decay
```

3g. **Entry Decision** (cần 2 bars liên tiếp confirm):
```python
if bb_width > 0.001 and cooldown == 0:
    if smoothed_score > thresh and rsi < 72:  dir_cand = "BUY"
    elif smoothed_score < -thresh and rsi > 28: dir_cand = "SELL"
    
    if reverse_mode: flip BUY↔SELL
    
    if dir_cand == current_dir:  confirm_counter += 1
    else:                        confirm_counter = 1; current_dir = dir_cand
    
    if confirm_counter >= 2:
        action = dir_cand
        trade_sim.open_position(...)
```

3h. **Prediction generation:**
```python
if regime == "TREND":
    p_dir = "BULLISH" if di_plus > di_minus else "BEARISH"
else:
    p_dir = "BULLISH" if slope > 0 else "BEARISH"

mid_price = c_price + (slope * 5)
range = mid_price ± (atr * 1.5)
confidence = 1 - (atr / price) * 10   # clamp [0.1, 0.95]

# Register để evaluate sau (tf_seconds * 5 giây)
scorer.register_prediction(c_time + tf_secs * 5, {...})
```

3i. **Update ui_state** và lưu `last_candle_time = c_time`

**Bước 4: Build output** (mỗi tick — luôn fresh price):
```python
res_out = ui_state.copy()
res_out["notifications"]  = trade_sim.get_notifications()   # flush queue
res_out["trade_history"]  = trade_sim.history[:10]
res_out["indicators"]     = { current values... }
res_out["trade"]          = { position_status, entry, tp, sl, pnl, capital... }
```

---

## 7. MANUAL TRADE SIMULATOR (simulator_manager.py)

Độc lập với TradeSimulator — xử lý lệnh do user đặt tay.

```python
class ManualTradeSimulator:
    active_trades: List[dict]   # Các lệnh đang mở
    history: List[dict]         # 50 lệnh đã đóng gần nhất
```

**open_trade(trade_data):**
- Nhận: `{entry, tp, sl, position: "LONG"/"SHORT"}`
- Thêm `status="OPEN"`, `entry_time=now`
- Push vào `active_trades`

**update_tick(current_price):**
- Gọi mỗi tick từ `main.py`
- Duyệt tất cả active_trades:
  - `hit_tp = (LONG && price >= tp) || (SHORT && price <= tp)`
  - `hit_sl = (LONG && price <= sl) || (SHORT && price >= sl)`
  - Nếu hit: tính PnL%, đưa vào history, loại khỏi active
- **Trả về LIST tất cả trades đóng** trong tick này (khác bản cũ chỉ trả 1)
- `history` giữ tối đa 50 entries

---

## 8. AI ADVISOR (ai_advisor.py)

Rule-based analysis — không có ML, chỉ logic đơn giản:

```python
def analyze_user_trade(entry, tp, sl, position, current_indicators):
```

**Checks:**
1. **Entry quality:**
   - LONG: nếu `entry > BB_U` → cảnh báo mua vùng overbought
   - LONG: nếu `entry > EMA21 * 1.02` → gợi ý chờ pullback
   - SHORT: nếu `entry < BB_L` → cảnh báo bán vùng oversold
2. **R:R ratio:**
   - `rr = |tp - entry| / |entry - sl|`
   - `rr < 1.5` → cảnh báo kèo xấu
   - `rr > 4` → cảnh báo TP quá xa
3. **Return:** `{rr, suggestions[], is_valid: rr >= 1.0}`

---

## 9. MAIN.PY — FastAPI App

### 9.1 Khởi tạo global

```python
indicator_layer = IndicatorLayer()   # shared, dùng chung cho mọi symbol
engines = {
    f"{sym}_{tf}": AdvancedSignalEngine(sym, tf)
    for sym in SYMBOLS for tf in TIMEFRAMES
}
# → 20 engines: BTCUSDT_1m, BTCUSDT_5m, ..., PAXGUSDT_1h
```

### 9.2 ConnectionManager

Quản lý WebSocket clients:
```python
active_connections: dict = {
    ws_object: {"symbol": "BTCUSDT", "interval": "1m"},
    ...
}
```

Không phân biệt user — tất cả client nhận broadcast theo `(symbol, interval)`.

### 9.3 on_market_tick callback

Được gọi từ `data_layer._update_cache()` mỗi khi có data mới từ Binance WS.

```
Binance tick
    ↓
data_layer._update_cache()
    ↓
on_market_tick(symbol, interval, is_closed, tick_data)
    ↓
1. engine.manual_sim.update_tick(current_price)
   └─ broadcast MANUAL_TRADE_CLOSED nếu có
2. data_manager.cache[symbol][interval] → df_raw
3. indicator_layer.apply_indicators(df_raw) → df_ind (always fresh)
4. Build mtf_context:
   for tf in ("5m", "15m", "1h"):
       indicator_layer.apply_indicators_cached(df_tf, symbol, tf)
5. engine.generate_signal(df_ind, mtf_context) → analysis
6. Merge manual_active + manual_history
7. broadcast_to_symbol(TICK message)
```

**Lưu ý quan trọng:** `apply_indicators(df_raw)` gọi cho current TF **luôn fresh** (không cache) vì close price thay đổi mỗi sub-second. Nhưng MTF context dùng `apply_indicators_cached` vì 5m/15m/1h nến thay đổi chậm hơn.

### 9.4 Lifespan

```python
@asynccontextmanager
async def lifespan(app):
    await data_manager.bootstrap_all()    # 20 REST calls concurrent
    asyncio.create_task(data_manager.ws_loop())  # background task
    yield
    logger.info("Shutting down.")
```

### 9.5 WebSocket Endpoint `/ws/frontend`

**Subscribe flow:**
1. Client gửi: `{"action": "subscribe", "symbol": "BTCUSDT", "interval": "1m", "capital": 100}`
2. Server update subscription mapping
3. Set capital trên engine
4. Bootstrap nếu symbol chưa có trong cache
5. Gửi **FULL_LOAD**: 200 nến cuối với OHLC + EMA_9 + EMA_21

**FULL_LOAD format:**
```json
{
    "type": "FULL_LOAD",
    "symbol": "BTCUSDT",
    "data": [
        {"time": 1234567890, "open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0,
         "EMA_9": 1.0, "EMA_21": 1.0},
        ...
    ]
}
```

**TICK format** (broadcast từ on_market_tick):
```json
{
    "type": "TICK",
    "symbol": "BTCUSDT",
    "candle": {"time": ..., "open": ..., "high": ..., "low": ..., "close": ...},
    "signal": {
        "action": "BUY|SELL|HOLD",
        "score": 0.42,
        "confidence": 0.85,
        "regime": "TREND|RANGE|SQUEEZE|NEUTRAL",
        "reason": "string mô tả trạng thái",
        "prediction": {
            "direction": "BULLISH|BEARISH",
            "mid_price": 50000.0,
            "range": {"min": 49800.0, "max": 50200.0}
        },
        "indicators": {
            "EMA_9": 0, "EMA_21": 0, "RSI": 0, "MACD": 0, "ATR": 0,
            "BB_U": 0, "BB_L": 0, "ADX": 0, "DI_PLUS": 0, "DI_MINUS": 0,
            "STOCH_K": 0, "STOCH_D": 0, "VWAP": 0, "VOL_RATIO": 0
        },
        "raw_signals": {"EMA": 0.7, "MACD": -0.3, ...},   // -1 đến +1
        "weights": {"EMA": 0.17, "MACD": 0.15, ...},       // sum=1
        "trade": {
            "position_status": "OPEN|NONE",
            "entry": 50000.0, "tp": 50090.0, "sl": 49972.0,
            "pnl": 0.12, "profit_usd": 0.24,
            "position_size": 0.000012, "capital": 103.50
        },
        "trade_history": [...],           // 10 entries gần nhất
        "manual_active": [...],           // lệnh thủ công đang mở
        "manual_history": [...],          // lịch sử lệnh thủ công
        "notifications": ["string", ...], // toast messages
        "tracker": [...],                 // prediction tracker rows
        "winrate": 65.0,                  // simple winrate %
        "ema_winrate": 62.3               // EMA-smoothed winrate %
    }
}
```

### 9.6 REST Endpoints

**POST `/trade/analyze?symbol=&interval=`**
- Body: `TradeRequest {entry, tp, sl, position}`
- Lấy `current_indicators` từ `engine.ui_state`
- Gọi `analyze_user_trade()` → `{rr, suggestions, is_valid}`

**POST `/trade/open-manual?symbol=&interval=`**
- Body: `ManualTradeData {entry, tp, sl, position}`
- Gọi `engine.manual_sim.open_trade(body.model_dump())`
- Return `{"status": "success"}`

---

## 10. FRONTEND (static/index.html)

Single-file HTML + CSS + JS. Không framework — vanilla.

### 10.1 Layout

```
┌─────────────────────────────┬──────────────┐
│          LEFT (flex:3)       │ RIGHT (panel) │
│                              │   flex:1      │
│  [Symbol] [TF] [Capital] [WS]│   max:320px   │
│                              │               │
│  ┌────────────────────────┐  │  Cards stack  │
│  │  Lightweight Chart      │  │  (scroll)     │
│  │  Candlestick + EMA9/21  │  │               │
│  │  + Prediction line      │  │               │
│  │  + Price lines (TP/SL)  │  │               │
│  └────────────────────────┘  │               │
└─────────────────────────────┴──────────────┘
```

### 10.2 Chart (Lightweight Charts v4 từ unpkg CDN)

```javascript
const chart = createChart(container, {
    layout: { background: '#131722' (dark navy) },
    crosshair: CrosshairMode.Normal,
    timeScale: { timeVisible: true }
})

// Series:
candleSeries  — CandlestickSeries (upColor: #26a69a teal, downColor: #ef5350 red)
ema9Series    — LineSeries (color: #2962FF blue, width: 1.5)
ema21Series   — LineSeries (color: #FF6D00 orange, width: 1.5)
predSeries    — LineSeries (color: #B228FF purple, dashed, lineStyle: 2)
```

**Price lines trên chart:**
- Auto trade: `entryLine (gold)`, `tpLine (green)`, `slLine (red)` — tạo/xóa theo trạng thái
- Manual trade: `mEntryLine (blue)`, `mTpLine (green)`, `mSlLine (red)` — interactive

**Prediction line:** Vẽ từ `currentPrice` tại `localTime` đến `mid_price` tại `localTime + tf_seconds*5`

**Timezone:** `tzOff = new Date().getTimezoneOffset() * 60` — trừ vào tất cả timestamps để hiển thị giờ local

**Responsive:** `window.resize` → `chart.applyOptions({width: container.clientWidth})`

### 10.3 Click-to-set-price (Manual Trade)

```javascript
// 3 nút: Entry 🎯 / TP 🎯 / SL 🎯
// Click nút → activeClickMode = 'ENTRY'/'TP'/'SL'
// Click chart → chart.subscribeClick() → lấy price từ coordinate
//             → gán vào input → updateManualLines()
```

### 10.4 RAF Batching (Performance)

```javascript
let pendingTick = null;
let rafQueued   = false;

function scheduleRender(state) {
    pendingTick = state;           // overwrite, chỉ giữ tick mới nhất
    if (!rafQueued) {
        rafQueued = true;
        requestAnimationFrame(() => {
            if (pendingTick) renderTick(pendingTick);
            pendingTick = null;
            rafQueued   = false;
        });
    }
}
```
→ Nếu nhiều tick đến trong 1 frame 16ms, chỉ render tick cuối cùng. Giảm DOM thrash.

### 10.5 DOM Diff Helpers

```javascript
const _lastText = {};

function setText(id, text) {
    if (_lastText[id] === text) return;   // Skip nếu không đổi
    document.getElementById(id).textContent = text;
    _lastText[id] = text;
}
function setHTML(id, html) { ... }   // Tương tự nhưng innerHTML
function setCls(id, cls) { ... }
function setStyle(id, prop, val) { ... }
```
→ Tránh write DOM khi value không đổi.

### 10.6 Các Card trong Right Panel

#### Card 1: Live Price + Regime
- `#price` — giá hiện tại (lớn, vàng)
- `#regime-badge` — TREND / RANGE / SQUEEZE / NEUTRAL với màu tương ứng:
  - TREND: xanh dương `#2962FF`
  - RANGE: cam `#FFA500`
  - SQUEEZE: tím `#B228FF`
  - NEUTRAL: xám

#### Card 2: Signal Engine
- `#action-badge` — BUY (xanh) / SELL (đỏ) / HOLD (cam)
- `#winrate` — EMA win rate %, màu thay đổi: ≥50%=xanh, ≥40%=cam, <40%=đỏ
- `#score` — momentum score (-1..+1), progress bar màu theo sign
- `#confidence` — % confidence của prediction
- `#reason` — text mô tả trạng thái ("✅ Signal Confirmed [TREND]", "⏳ Cooldown...", etc.)
- `#conf-dots` — 8 chấm nhỏ bull/bear/neutral theo raw_signals

#### Card 3: Signal Breakdown
- 8 horizontal bar charts cho EMA, MACD, RSI, BB, VWAP, VOLUME, STOCH, MTF
- Mỗi bar: tên (trái) + bar fill giữa (-1 → 0 → +1) + value số (phải)
- Màu: val>0.05→xanh, val<-0.05→đỏ, neutral→xám

#### Card 4: Trend & Momentum
- `#adx-val` + ADX progress bar (xám<18, cam<25, xanh≥25)
- DI+ (xanh) / DI- (đỏ)
- Stoch K (vàng) / D (xám)
- Vol Ratio (màu theo threshold: >1.5=xanh, >1.2=cam, else=xám)

#### Card 5: Adaptive Weights
- 8 weight bars (vàng `#FCD535`) cho mỗi indicator
- Hiển thị % normalized (sum=100%)

#### Card 6: Prediction
- Direction: BULLISH (xanh) / BEARISH (đỏ)
- Target Mid price
- Range: `min – max`

#### Card 7: Auto Trade Bot
- Capital ($)
- Status: OPEN (xanh) / NONE
- Entry / TP / SL prices
- Live P&L: `+0.24$ (+0.12%)` — màu theo profit/loss

#### Card 8: Auto Trade History
- Table 5 cột: Pos | Entry | Exit | PnL% | Result
- Tối đa 10 rows
- WIN=xanh, LOSS=đỏ

#### Card 9: Manual Simulator
- Select LONG/SHORT
- 3 inputs: Entry, TP, SL + nút 🎯 để click từ chart
- Live R:R display (≥1.5=xanh, <1.5=đỏ)
- Nút "AI Analyze" → POST `/trade/analyze` → hiển thị AI box
- Nút "Open Trade" → validate direction logic → POST `/trade/open-manual`
- Nút "✕ Clear" → xóa inputs + price lines

#### Card 10: Manual Trade History
- Active trades: highlighted màu xanh nhạt, "Live" pulse animation
- Closed trades: màu theo result
- 6 cột: Time | Pos | Entry | TP/SL | PnL | Result

#### Card 11: Forward Testing Tracker
- Prediction tracker: pending (WAITING cam) + recent results (WIN xanh / LOSS đỏ)
- 5 cột: Time | Dir | Range | Actual | Result

### 10.7 Toast Notifications

```javascript
function toast(msg, color='#FCD535') {
    // Tạo div.toast, append vào #toast-container (fixed bottom-right)
    // Tự xóa sau 6 giây (fade out)
}
```
- Màu: `🟢` → xanh, `🔴` → đỏ, default → vàng
- Auto trade open/close: trigger từ `p.notifications`
- Manual trade close: trigger từ `MANUAL_TRADE_CLOSED` WS message

### 10.8 WebSocket Client

```javascript
connectWS()  →  ws = new WebSocket(`ws://host/ws/frontend`)

ws.onopen:  setText('ws-status', '🟢 ONLINE') → requestData()
ws.onmessage:
    FULL_LOAD  → candleSeries.setData() + ema series setData()
    MANUAL_TRADE_CLOSED → toast()
    TICK → scheduleRender(res)  (RAF batched)
ws.onclose: setText('ws-status', '🔴 OFFLINE') → setTimeout(connectWS, 2500)
ws.onerror: ws.close()
```

`requestData()` gửi subscribe message mỗi khi:
- Symbol select change
- Timeframe select change
- Capital input change

---

## 11. DATA FLOW TOÀN BỘ

```
Binance WS (20 streams)
        │
        ▼
data_layer._update_cache(msg)
  • In-place update hoặc append+trim
        │
        ▼
on_market_tick(symbol, interval, is_closed, tick_data)
  │
  ├─ ManualTradeSimulator.update_tick(price)
  │      → broadcast MANUAL_TRADE_CLOSED nếu có
  │
  ├─ indicator_layer.apply_indicators(df_raw)           ← always fresh (current TF)
  │
  ├─ mtf_context build:
  │      indicator_layer.apply_indicators_cached(...)   ← cached (5m, 15m, 1h)
  │
  ├─ AdvancedSignalEngine.generate_signal(df_ind, mtf)
  │      ├─ process_tick() → check auto TP/SL
  │      ├─ evaluate_and_learn() → score past predictions
  │      ├─ Compute 8 signal scores
  │      ├─ Weighted sum → smoothed_score
  │      ├─ Regime detect → threshold adapt
  │      ├─ Entry decision (2-bar confirm)
  │      ├─ Prediction register
  │      └─ Build ui_state
  │
  └─ broadcast_to_symbol(TICK message)
             │
             ▼
       Browser WebSocket
             │
             ▼
       scheduleRender() [RAF]
             │
             ▼
       renderTick(res)
         • setText/setHTML helpers (diff-only)
         • chart update (candle, EMA lines)
         • All panel cards update
```

---

## 12. PERSISTENCE (File I/O)

### ml_training_data_{SYMBOL}_{TF}.jsonl

Ghi mỗi khi prediction được evaluate (synchronous `open(...,"a")`):
```json
{
    "time": "2026-06-10T12:34:56",
    "symbol": "BTCUSDT", "timeframe": "1m",
    "prediction": {"direction": "BULLISH", "range": {"min": 49800, "max": 50200}, "confidence": 0.75},
    "signal": "BUY", "score": 0.42,
    "raw_signals": {"EMA": 0.7, "MACD": -0.2, ...},
    "indicators_snapshot": {"EMA_9": 50100, "RSI": 58.3, ...},
    "actual_price": 50150.0,
    "result": "WIN"
}
```

### trade_history_{SYMBOL}_{TF}.jsonl

Ghi mỗi khi auto trade đóng:
```json
{
    "symbol": "BTCUSDT", "timeframe": "1m",
    "entry_time": "12:34:56", "exit_time": "2026-06-10T12:39:00",
    "entry_price": 50000.0, "exit_price": 50090.0,
    "position": "LONG",
    "capital_before": 100.00, "capital_after": 100.24,
    "risk_pct": 0.015, "position_size": 0.000012,
    "profit_usd": 0.24, "pnl": 0.18, "result": "WIN"
}
```

**Đọc lại khi khởi động:**
- `AdaptiveScorer._load_history()`: 500 dòng cuối → replay weights + winrate
- `TradeSimulator._load_history()`: 10 dòng cuối → restore capital + display history

---

## 13. KNOWN BUGS (từ CLAUDE.md)

### Critical (crash)
| Bug | File:Line | Mô tả | Fix |
|-----|-----------|-------|-----|
| BUG-001 | main.py:34-38 | engine None → AttributeError trước None-guard | `if engine is None: return` |
| BUG-002 | main.py:211-225 | `dict` param không parse được từ POST body | Pydantic model |
| BUG-003 | main.py:228-232 | Tương tự BUG-002 | Pydantic model |
| BUG-004 | main.py:54 vs signal_engine.py:369 | MTF mismatch 1m/5m/15m vs 5m/15m/1h | Align thành 5m/15m/1h |

**Note: BUG-001, 002, 003, 004 đã được FIX trong code hiện tại.**

### Logic errors
| Bug | File:Line | Mô tả | Fix |
|-----|-----------|-------|-----|
| BUG-005 | signal_engine.py:366 | evaluate dùng prev close thay vì curr close | `float(curr['close'])` — **ĐÃ FIX** |
| BUG-006 | signal_engine.py:57-58 | Key access không safe khi load history | `.get()` chaining |
| BUG-007 | simulator_manager.py | Cũ chỉ trả 1 trade, bỏ sót nếu 2 trade đóng cùng tick | Return list — **ĐÃ FIX** |

### Performance
| Bug | Mô tả | Trạng thái |
|-----|-------|-----------|
| BUG-008 | Bootstrap sync với requests.get | **ĐÃ FIX** (httpx async) |
| BUG-009 | Full 5000-row indicator recompute mỗi tick | **ĐÃ CẢI THIỆN** (INDICATOR_WINDOW=260 + cached MTF) |
| BUG-010 | pd.concat allocate DataFrame mỗi nến mới | Vẫn còn, có thể dùng deque |
| BUG-011 | Sync file I/O trong async loop | Vẫn còn |
| BUG-012 | iterrows() cho 200-candle history | Vẫn còn |

### Frontend
| Bug | Mô tả | Trạng thái |
|-----|-------|-----------|
| BUG-013 | innerHTML rebuild toàn bộ table mỗi tick | **ĐÃ CẢI THIỆN** (setHTML với diff check) |
| BUG-014 | Không có RAF batching | **ĐÃ FIX** (scheduleRender + RAF) |

---

## 14. UNUSED / DEAD CODE

| File | Lý do unused | Quyết định |
|------|-------------|-----------|
| `trade_engine.py` | Phiên bản mới hơn TradeSimulator với dataclass + pending orders — tốt hơn nhưng chưa wire up | Tích hợp hoặc xóa |
| `prediction_engine.py` | EMA-smoothed predictor tốt hơn extrapolation hiện tại | Tích hợp hoặc xóa |
| `training_engine.py` | Buffered weight updates | Tích hợp hoặc xóa |
| `position_manager.py` | Không import ở đâu | Xóa |
| `core_trading_system.py` | Có `app = FastAPI()` riêng → conflict; không dùng | Xóa |

---

## 15. ROADMAP (từ CLAUDE.md)

### Phase 1 — Stability ✅ (phần lớn đã làm)
- Fix BUG-001..004 (None guards, Pydantic, MTF align) ✅
- Fix BUG-005 (evaluate_and_learn dùng curr close) ✅
- Fix BUG-006 (safe key access) — còn thiếu

### Phase 2 — Performance (đang làm dần)
- Indicator cache (MTF) ✅ — `apply_indicators_cached`
- Async bootstrap ✅ — `httpx.AsyncClient`
- Async file I/O — chưa (cần `aiofiles`)
- Frontend RAF throttle ✅ — `scheduleRender`

### Phase 3 — Features
- Integrate `trade_engine.py` (pending orders, leverage)
- Integrate `prediction_engine.py` (EMA-smoothed predictor)
- Alert system (sound/browser notification)
- Multi-symbol dashboard (4 mini-charts)
- PnL Chart (capital curve)

---

## 16. KEY DESIGN DECISIONS

1. **20 independent engines** — mỗi symbol/TF có state riêng hoàn toàn. Không share learning giữa BTCUSDT_1m và BTCUSDT_5m.

2. **Score smoothing (EMA 0.35/0.65)** — bias mạnh về quá khứ để tránh flip-flop signal. Cần 2+ bars liên tiếp để confirm.

3. **Prediction target = 5 bars ahead** — đủ xa để có ý nghĩa, đủ gần để evaluate nhanh.

4. **Adaptive threshold với decay** — nếu market range lâu không có signal, threshold tự giảm để engine không "freeze".

5. **Separate auto vs manual simulation** — auto bot chạy hoàn toàn tự động dựa trên signal; manual là user đặt tay và theo dõi TP/SL tự động.

6. **Single-file frontend** — không build step, không dependency. Open browser → chạy ngay.

7. **RAF + DOM diff** — tránh 60 DOM rebuilds/minute. Chỉ cập nhật khi value thực sự thay đổi.
