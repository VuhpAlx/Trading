# PROJECT DEEP DIVE — Quant Trading Terminal

> Tài liệu chi tiết toàn bộ cấu trúc, logic xử lý, và UI của hệ thống.  
> Cập nhật: 2026-06-25 (v6 — Backtest harness `backtest/` + nghiên cứu edge (mục 17–23);
> fix sizing theo % rủi ro + Regime Badge 1d, mục 24)

---

## 0. CẬP NHẬT LỚN v5 (2026-06-24)

Đại tu lõi đọc thị trường để khắc phục win-rate thấp (~34.8% baseline) do 3 lỗi gốc:
nhiễu MTF, thiếu S/R, phí ăn hết edge. Triết lý mới: **Top-Down 3 lớp**.

**Lớp 1 — BIAS (cổng cứng, `htf_bias.py`):** Xác định xu hướng khung lớn theo
`HTF_MAP` (vd 1m→[15m,1h], 1h→[4h,1d]). BULL→chỉ cho BUY; BEAR→chỉ SELL;
NEUTRAL→đứng ngoài. Thay cho MTF "1/8 phiếu bầu" cũ.

**Lớp 2 — STRUCTURE (`market_structure.py`):** Tính Hỗ trợ/Kháng cự (swing fractal),
Pivot sàn (từ nến khung lớn), số tròn tâm lý. SL đặt sau mốc cấu trúc, TP tại mốc kế tiếp.

**Lớp 3 — TRIGGER (confluence trong `signal_engine.py`):** Đếm yếu tố cùng hướng
(EMA, MACD, momentum, volume, vị trí giá). Cần `MIN_CONFLUENCE` (3) + **R:R sau phí ≥
`MIN_RR_AFTER_FEES` (1.5)** mới vào lệnh → ít lệnh, chất hơn. `reverse_mode` không còn
dùng để đảo lệnh.

**Mô hình LOT/Margin (`lot_sizing.py`)** thay sizing `risk_pct` cũ: lot bắt đầu 0.01,
bot tự **scale lot động** theo confluence+bias (chặn bởi `RISK_CAP_PCT`), mô phỏng
**margin + đòn bẩy (x100) + thanh lý** kiểu Exness. 1 lot = 1 coin (`CONTRACT_SIZE`).
Áp dụng cho cả auto bot và lệnh tay.

**Stream thêm 4h + 1d** (khung bối cảnh) để bias top-down đúng cho khung 15m/30m/1h.

**UI:** thêm card "🧭 Phân tích quyết định" (bias từng khung, S/R, reasons[], R:R),
hiển thị Lot/Margin/Equity/Margin level/Giá thanh lý, ô nhập lot cho lệnh tay, vẽ
đường S/R/Pivot/Liquidation lên chart, tooltip giáo dục.

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
├── config.py                — Constants: SYMBOLS, TIMEFRAMES, HTF_MAP, lot/margin params
├── data_layer.py            — Binance REST bootstrap + WebSocket stream + cache (gồm 4h/1d)
├── indicator_layer.py       — Tính toán 15+ indicator trên DataFrame
├── market_structure.py      — [MỚI v5] S/R, pivot sàn, swing fractal, số tròn
├── htf_bias.py              — [MỚI v5] Bias top-down khung lớn (compute/aggregate)
├── lot_sizing.py            — [MỚI v5] Toán lot/margin/đòn bẩy/thanh lý kiểu Exness
├── signal_engine.py         — AdaptiveScorer + TradeSimulator(lot) + AdvancedSignalEngine(top-down)
├── simulator_manager.py     — ManualTradeSimulator (lệnh tay, lot/margin/thanh lý)
├── prediction_engine.py     — PredictionEngine (range EMA-smoothed, hướng theo bias)
├── ai_advisor.py            — Rule-based RR + entry quality + cảnh báo margin/S/R
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
SYMBOLS            = ["BTCUSDT", "ETHUSDT", "BNBUSDT", "PAXGUSDT"]
TIMEFRAMES         = ["1m", "5m", "15m", "30m", "1h"]   # khung GIAO DỊCH (có engine)
CONTEXT_TIMEFRAMES = ["4h", "1d"]                        # khung BỐI CẢNH (chỉ bias)
ALL_TIMEFRAMES     = TIMEFRAMES + CONTEXT_TIMEFRAMES     # tất cả khung stream
HTF_MAP = {  # mỗi khung giao dịch → 2 khung lớn hơn làm bias top-down
    "1m":["15m","1h"], "5m":["1h","4h"], "15m":["1h","4h"],
    "30m":["4h","1d"], "1h":["4h","1d"],
}
MIN_RR_AFTER_FEES  = 1.5    # R:R tối thiểu sau phí mới vào lệnh
MIN_CONFLUENCE     = 3      # số yếu tố tối thiểu cùng hướng
STRUCTURE_LOOKBACK = 200; SWING_STRENGTH = 3
HISTORY_LIMIT = 1000; MAX_CACHE_SIZE = 1000; INDICATOR_WINDOW = 260

# --- Mô hình LOT/Margin (Exness) ---
CONTRACT_SIZE = {"BTCUSDT":1.0, "ETHUSDT":1.0, "BNBUSDT":1.0, "PAXGUSDT":1.0}  # 1 lot=1 coin
LEVERAGE = 100
MIN_LOT=0.01; LOT_STEP=0.01; MAX_LOT=1.0
LOT_BASE=0.01; LOT_MAX_DYNAMIC=0.10          # dải lot động của bot
MAINTENANCE_MARGIN=0.005; STOP_OUT_LEVEL=0.50  # thanh lý khi margin level ≤ 50%
RISK_CAP_PCT=0.03                              # trần rủi ro/lệnh chặn lot
```

Engine giao dịch: 4 symbols × 5 TIMEFRAMES = **20 engines**.
Stream Binance: 4 × 7 (ALL_TIMEFRAMES) = **28 streams** (20 giao dịch + 8 bối cảnh 4h/1d).

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
> [v5] Lời gọi callback được bọc try/except + log (`exc_info`). Một tick lỗi KHÔNG
> làm sập `ws_loop` (trước đây sẽ bị hiểu nhầm là mất kết nối → reconnect 5s liên tục
> → frontend đứng giá). Lỗi dữ liệu thật sẽ in `on_tick_callback error ...` ra console.

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

#### Load history từ JSONL — [v5.1] chỉ LIFETIME, không đổ vào tracker

Khi khởi tạo, đọc **500 dòng cuối** của `ml_training_data_{symbol}_{tf}.jsonl`:
- **Tương thích 2 schema**: `result`|`final_result`, `prediction.direction`|`predict_direction`.
- Gọi `_apply_result_to_stats()` (LIFETIME) + `_adjust_weights()` (lr=0.01) replay nhẹ.
- **KHÔNG** điền `recent_results` → tracker Forward Testing chỉ hiển thị **phiên hiện tại**
  (tránh lẫn dữ liệu nhiều ngày). `recent_results` bắt đầu rỗng mỗi phiên.

#### _adjust_weights logic

```python
for ind, val in raw_sigs.items():
    is_correct = (val > 0 and "BULL" in direction) or (val < 0 and "BEAR" in direction)
    if is_correct:
        weights[ind] *= (1 + lr * factor)
    weights[ind] = clamp(0.2, 3.0)
```

`factor` = `{"WIN": 1.0, "FLAT": 0.0, "LOSS": -0.8}` (PARTIAL: legacy)

#### Prediction evaluation — [v5.1] ĐÚNG HƯỚNG + ngưỡng (Forward Testing)

`register_prediction(target_time, payload)`:
- target_time = `c_time + tf_seconds * FORWARD_TEST_BARS` (mặc định 5 nến).
- payload gồm `start_price`, `atr_snapshot`, `target`, `target_zone`, `direction`...

`evaluate_and_learn(current_time_sec, current_close)`:
- Scan pending có `target_time <= current_time`. `move = current_close - start_price`;
  ngưỡng `thr = atr_snapshot × FORWARD_FLAT_ATR_MULT` (0.2×ATR).
- **WIN**: đi đúng hướng ≥ thr · **LOSS**: đi ngược ≥ thr · **FLAT**: |move| < thr (đi ngang, KHÔNG tính hit-rate).
- Bỏ `hit_range` làm cổng WIN (chỉ log tham khảo) → hết WIN-giả do dải EMA rộng.
- Cập nhật `session_stats` (phiên) + `stats` (lifetime). `recent_results` lưu **epoch `ts`** + `target`.
- Buffer learning, batch `_adjust_weights()` mỗi 20 (tránh overfit nhiễu).

#### Session vs Lifetime hit-rate
- `get_session_winrate()` = WIN/(WIN+LOSS) **phiên hiện tại** → hiển thị ở card Forward Testing.
- `get_winrate()` = hit-rate **lifetime** (cả lịch sử) → card Signal Engine.

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

**Class constants:**
```python
MAKER_FEE = 0.0000   # Limit order (không dùng mặc định)
TAKER_FEE = 0.0010   # 0.1% market taker fee mỗi chiều (Binance VIP0)
SLIPPAGE  = 0.0002   # 0.02% market impact mỗi fill
```

**Vòng đời trade:**
1. `open_position()` → tính actual_entry với slippage, tính TP/SL từ actual_entry, store entry_fee
2. `process_tick()` được gọi từ `main.py` mỗi tick (không còn trong generate_signal)
3. Khi close → tính exit với slippage, trừ entry_fee + exit_fee, log net_profit, cooldown 5 bars

**Entry (Market order):**
```python
actual_entry = price * (1 + mult * SLIPPAGE)
# LONG:  actual_entry = price * 1.0002 (mua cao hơn)
# SHORT: actual_entry = price * 0.9998 (bán thấp hơn)
entry_fee = volume_usd * TAKER_FEE
```

**[v5] SL/TP truyền từ ngoài (theo CẤU TRÚC), không còn ATR thuần.**
`generate_signal` tính `sl`/`tp` từ `market_structure` (SL sau hỗ trợ/kháng cự gần
nhất, TP tại mốc kế tiếp) rồi truyền vào `open_position(..., sl, tp, confluence, bias_strength)`.
Lệnh chỉ mở nếu **R:R sau phí ≥ MIN_RR_AFTER_FEES**.

**[v5] Khối lượng theo LOT ĐỘNG (thay `risk_pct` cũ):**
```python
dyn_lot = lot_sizing.compute_dynamic_lot(confluence, bias_strength)  # 0.01..0.10
cap_lot = lot_sizing.lot_from_risk_cap(capital, sl_dist, symbol, entry)  # trần rủi ro 3%
lot     = min(dyn_lot, cap_lot)
margin  = lot * contract * entry / LEVERAGE      # ký quỹ; bỏ lệnh nếu > vốn
liq     = lot_sizing.liquidation_price(...)      # giá thanh lý
```
`process_tick`: PnL = `pnl_usd` (lot-based) − phí 2 chiều; **kiểm tra THANH LÝ trước
TP/SL** (giá chạm `liq_price` hoặc margin level ≤ 50% → đóng `LIQUIDATED`).

**TP/SL hit check:**
```python
sl_buf = entry * 0.0002   # 0.02% buffer tránh false trigger
# LONG:
if low  <= (sl - sl_buf)  → LOSS; exit_p = sl * (1 - SLIPPAGE)
if high >= tp             → WIN;  exit_p = tp * (1 - SLIPPAGE)
# SHORT:
if high >= (sl + sl_buf)  → LOSS; exit_p = sl * (1 + SLIPPAGE)
if low  <= tp             → WIN;  exit_p = tp * (1 + SLIPPAGE)
```

**PnL calculation (net of fees):**
```python
exit_fee    = exit_p * size * TAKER_FEE
gross_profit = (exit_p - actual_entry) * size * mult
net_profit   = gross_profit - entry_fee - exit_fee
final_pnl    = (net_profit / (actual_entry * size)) * 100
```

**Live P&L (intra-tick, còn đang mở):**
```python
gross_live   = (current_price - entry) * size * mult
exit_fee_est = current_price * size * TAKER_FEE
net_live     = gross_live - entry_fee - exit_fee_est  # estimate, cập nhật mỗi tick
```

**Cooldown:** 5 bars sau khi đóng trade — tránh re-entry ngay lập tức.

**Lưu history:** 20 trades gần nhất trong RAM; unlimited trong JSONL.
**JSONL thêm field:** `fees_usd` (entry_fee + exit_fee tổng cộng).

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

3b. **[v5] LỚP 1 — BIAS top-down (cổng cứng):**
```python
htf_tfs = HTF_MAP[interval]                      # vd 1m → ["15m","1h"]
per_tf  = [htf_bias.compute_bias(mtf_context[tf]) for tf in htf_tfs]
bias_agg = htf_bias.aggregate_bias(per_tf)       # BULL / BEAR / NEUTRAL + strength
# BULL → allowed=["BUY"]; BEAR → ["SELL"]; NEUTRAL → [] (đứng ngoài)
```
`mtf_score` (cho breakdown/learning) = `bias_agg["score"]`. MTF không còn quyết định trực tiếp.

3c. **[v5] LỚP 2 — CẤU TRÚC:** `structure = market_structure.analyze(df, pivot_src=khung lớn nhất)`
→ `nearest_support`, `nearest_resistance`, pivots, distances.

3d. **8 Signal Scores** vẫn được tính (cho UI breakdown + scorer learning) — công thức
EMA/MACD/RSI/BB/VWAP/VOLUME/STOCH như cũ; MTF = `bias_agg.score`. `smoothed_score`
chỉ dùng cho hiển thị, KHÔNG còn là ngưỡng vào lệnh.

3e. **[v5] LỚP 3 — CONFLUENCE (`_confluence(side, ctx)`):** đếm yếu tố cùng hướng:
trend nội khung (EMA stack + giá vs EMA50), MACD, momentum (RSI hồi/Stoch),
volume xác nhận, **vị trí giá** (thưởng gần hỗ trợ cho BUY / kháng cự cho SELL;
**phạt −1 nếu mua sát kháng cự / bán sát hỗ trợ**). Trả `(count, reasons[])`.

3f. **Regime Detection:** (không đổi) SQUEEZE / TREND / RANGE / NEUTRAL.

3g. **[v5] QUYẾT ĐỊNH (cổng bias + confluence + R:R):**
```python
# chỉ xét hướng allowed theo bias; cần confluence ≥ MIN_CONFLUENCE (3)
sl, tp = _structure_sl_tp(side, price, atr, structure)   # SL sau S/R, TP tại mốc kế
rr = _rr_after_fees(side, price, sl, tp)                  # đã trừ phí+slippage khứ hồi
if rr < MIN_RR_AFTER_FEES:  bỏ lệnh (ghi reason)
else: cần 2 nến confirm → trade_sim.open_position(side, price, sl, tp, confluence, bias_strength)
# reverse_mode KHÔNG còn đảo lệnh. reasons[] mô tả từng yếu tố (giáo dục).
```

3h. **Prediction generation (hướng theo BIAS):**
```python
pe_dir = "BULL" if p_dir == "BULLISH" else "BEAR"
pred_result = self.predictor.predict(
    close_price, ema9, atr, bb_upper, bb_lower,
    momentum_score=smoothed_score, direction=pe_dir
)
# [v5.1] confidence tính lại trong engine: 0.30 + 0.40*bias_strength
#        + 0.15*(confluence/6) + 0.15*min(1,adx/30)  (thay công thức ATR cũ ~0.94)
# target = giá ± FORWARD_TARGET_ATR_MULT*ATR (0.6×ATR); target_zone = target ± 0.2×ATR
# Register để evaluate sau FORWARD_TEST_BARS nến:
scorer.register_prediction(c_time + tf_secs * FORWARD_TEST_BARS,
    {..., "target": target, "target_zone": zone, "start_price": c_price, "atr_snapshot": atr})
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

**Class constants:**
```python
TAKER_FEE = 0.0010   # 0.1% per side
SLIPPAGE  = 0.0002   # 0.02% market impact
```

```python
class ManualTradeSimulator:
    active_trades: List[dict]   # Các lệnh đang mở
    history: List[dict]         # 50 lệnh đã đóng gần nhất
```

**[v5]** Khởi tạo với `symbol` + `capital`. PnL tính theo LOT (USD thực), có margin & thanh lý.

**open_trade(trade_data):**
- Nhận: `{entry, tp, sl, position, lot}` (lot mặc định 0.01, step 0.01)
- Tính `margin_usd`, `liq_price` (qua `lot_sizing`), lưu kèm `leverage`, `contract_size`, `notional`
- Thêm `status="OPEN"`, `entry_time=now`; push vào `active_trades`

**update_tick(current_price):**
- Duyệt active_trades; live PnL = `lot_sizing.pnl_usd(...)` − phí 2 chiều (USD thực theo lot)
- `hit_tp`/`hit_sl` như cũ + **`hit_liq`**: giá chạm `liq_price` hoặc `check_liquidation(equity, margin)`
- result = `LIQUIDATED` (nếu cháy) / `WIN` / `LOSS`; exit có slippage; cập nhật `capital`
- **Trả LIST tất cả trades đóng** trong tick; `history` giữ 50 entries

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
4. Build mtf_context theo HTF_MAP[interval] (top-down):
   for tf in HTF_MAP[interval]:   # vd 1m→[15m,1h], 1h→[4h,1d]
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

#### Card 11: Forward Testing Tracker — [v5.1]
- Header **hit-rate phiên**: `{W}/{W+L} ({%}) · FLAT {n}` (từ `session_winrate`/`session_stats`).
- Chỉ hiển thị dự đoán **phiên hiện tại**; mỗi dòng có **NGÀY+giờ** (`MM-DD HH:MM`, format từ epoch `ts`).
- 5 cột: Thời gian(ngày+giờ) | Hướng | Target | Thực tế | KQ (WIN xanh/LOSS đỏ/FLAT xám/WAITING cam).
- Sort theo `ts` (epoch số) — không vỡ thứ tự khi qua ngày.
- Chart: **vùng target dự kiến** (1 đường target + 2 đường mờ zone) thay đường chéo `predSeries` cũ.

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
  ├─ ManualTradeSimulator.update_tick(price)            ← ALWAYS
  │      → broadcast MANUAL_TRADE_CLOSED nếu có
  │
  ├─ trade_sim.process_tick(price, high, low)           ← ALWAYS (lightweight)
  │      → nếu closed: scorer.learn_from_real_trade()
  │
  ├─[is_closed=False] Intra-tick path ──────────────────────────────────┐
  │      Broadcast TICK với cached ui_state + fresh trade/pnl          │
  │      return ←───────────────────────────────────────────────────┘
  │
  ├─[is_closed=True] Full pipeline ────────────────────────────────────┐
  │   indicator_layer.apply_indicators(df_raw)                        │
  │   mtf_context build: apply_indicators_cached(...)                 │
  │   AdvancedSignalEngine.generate_signal(df_ind, mtf)               │
  │      ├─ evaluate_and_learn() → buffer; batch _adjust_weights @20  │
  │      ├─ Compute 8 signal scores                                   │
  │      ├─ Weighted sum → smoothed_score                             │
  │      ├─ Regime detect → threshold adapt                           │
  │      ├─ Entry decision (2-bar confirm)                            │
  │      ├─ Prediction register                                       │
  │      └─ Build ui_state                                            │
  └─ broadcast_to_symbol(TICK message) ←──────────────────────────────┘
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

### ml_training_data_{SYMBOL}_{TF}.jsonl — [v5.1]

Ghi mỗi khi prediction được evaluate (async via `run_in_executor`, non-blocking):
```json
{
    "time": "2026-06-24T12:34:56", "ts": 1782304496,
    "symbol": "BTCUSDT", "timeframe": "1m",
    "prediction": {"direction": "BULLISH", "target": 50180.0,
                   "range": {"min": 49800, "max": 50200}, "confidence": 0.72},
    "signal": "BUY", "score": 0.42,
    "raw_signals": {"EMA": 0.7, "MACD": -0.2, ...},
    "indicators_snapshot": {"EMA_9": 50100, "RSI": 58.3, ...},
    "start_price": 50000.0, "actual_price": 50150.0,
    "threshold": 30.0, "hit_range": true,
    "result": "WIN"
}
```
> [v5.1] Thêm `ts` (epoch), `prediction.target`, `start_price`, `threshold`, `hit_range`.
> `result` ∈ {WIN, LOSS, **FLAT**}. Tiêu chí: đúng hướng ≥ `threshold` (=0.2×ATR) → WIN.
> File schema CŨ (trước v5.1) đã được archive thành `*.jsonl.old` (forward-test tính lại sạch).

### trade_history_{SYMBOL}_{TF}.jsonl

Ghi mỗi khi auto trade đóng (async via `run_in_executor`, non-blocking):
```json
{
    "symbol": "BTCUSDT", "timeframe": "1m",
    "entry_time": "12:34:56", "exit_time": "2026-06-10T12:39:00",
    "entry_price": 50000.0, "exit_price": 50090.0,
    "position": "LONG",
    "capital_before": 100.00, "capital_after": 100.24,
    "lot": 0.03, "leverage": 100, "contract_size": 1.0,
    "margin_usd": 24.0, "liq_price": 49500.0,
    "fees_usd": 0.12, "profit_usd": 0.24, "pnl": 1.0, "result": "WIN|LOSS|LIQUIDATED"
}
```
> [v5] `pnl` giờ là % trên MARGIN (đòn bẩy), không phải % giá. Thêm `lot/leverage/margin_usd/liq_price`.

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
| BUG-006 | signal_engine.py:57-58 | Key access không safe khi load history — old JSONL schema khác crash | `.get()` + `or {}` + try/except bao quanh toàn bộ per-line block — **ĐÃ FIX** |
| BUG-007 | simulator_manager.py | Cũ chỉ trả 1 trade, bỏ sót nếu 2 trade đóng cùng tick | Return list — **ĐÃ FIX** |

### Performance
| Bug | Mô tả | Trạng thái |
|-----|-------|-----------|
| BUG-008 | Bootstrap sync với requests.get | **ĐÃ FIX** (httpx async) |
| BUG-009 | Full 5000-row indicator recompute mỗi tick | **ĐÃ CẢI THIỆN** (INDICATOR_WINDOW=260 + cached MTF) |
| BUG-010 | pd.concat allocate DataFrame mỗi nến mới | **ĐÃ FIX** — `to_numpy(copy=True)` trên (N,5) array, shift+assign qua pandas API. Tương thích pandas 3.x CoW (không còn dùng direct numpy view). |
| BUG-011 | Sync `open(file,"a")` block event loop trong async tick | **ĐÃ FIX** — `asyncio.get_running_loop().run_in_executor(None, write_func)` trong cả `AdaptiveScorer._async_write` và `TradeSimulator._async_write` |
| BUG-012 | iterrows() cho 200-candle history | **ĐÃ FIX** — `[int(ts.timestamp()) for ts in tail['timestamp']]` thay `astype('int64')//10^9`. pandas 3.x đổi dtype sang `datetime64[ms,UTC]` nên `astype int64` cho ms không phải ns — timestamp bị sai hoàn toàn. |

### Frontend
| Bug | Mô tả | Trạng thái |
|-----|-------|-----------|
| BUG-013 | innerHTML rebuild toàn bộ table mỗi tick | **ĐÃ CẢI THIỆN** (setHTML với diff check) |
| BUG-014 | Không có RAF batching | **ĐÃ FIX** (scheduleRender + RAF) |
| BUG-015 | HTML attribute whitespace trên PAXGUSDT option | **ĐÃ FIX** (code hiện tại đã clean) |

---

## 14. DEAD CODE — ĐÃ DỌN DẸP

Tất cả dead code đã được xử lý:

| File | Kết quả | Lý do |
|------|---------|-------|
| `position_manager.py` | **ĐÃ XÓA** | Không import ở đâu |
| `core_trading_system.py` | **ĐÃ XÓA** | Có `app = FastAPI()` riêng → conflict, mock data, hoàn toàn lỗi thời |
| `training_engine.py` | **ĐÃ XÓA** | AdaptiveScorer hiện tại mạnh hơn (8 indicators, reverse mode, EMA winrate) |
| `trade_engine.py` | **ĐÃ XÓA** | Pending orders hay nhưng integration cần refactor lớn; TradeSimulator hiện tại ổn định |
| `prediction_engine.py` | **ĐÃ INTEGRATE** | EMA-smoothed prediction, BB-adjusted ranges, momentum factor — tốt hơn linear extrapolation |

**Kết quả sau dọn dẹp — file Python active:**
```
ai_advisor.py        indicator_layer.py   prediction_engine.py  simulator_manager.py
config.py            main.py              signal_engine.py
data_layer.py        
```

---

## 15. ROADMAP (từ CLAUDE.md)

### Phase 1 — Stability ✅ HOÀN THÀNH
- Fix BUG-001..004 (None guards, Pydantic, MTF align) ✅
- Fix BUG-005 (evaluate_and_learn dùng curr close) ✅
- Fix BUG-006 (safe key access + try/except per-line) ✅
- Fix BUG-007 (simulator_manager trả list) ✅

### Phase 2 — Performance ✅ HOÀN THÀNH
- Indicator cache (MTF) ✅ — `apply_indicators_cached`
- Async bootstrap ✅ — `httpx.AsyncClient`
- Async file I/O ✅ — `run_in_executor` (không cần aiofiles)
- Frontend RAF throttle ✅ — `scheduleRender`
- In-place DataFrame rotation ✅ — numpy shift thay pd.concat (BUG-010)
- Vectorized FULL_LOAD history ✅ — tolist()+zip thay iterrows (BUG-012)

### Phase 3 — Features (còn lại)
- ~~Integrate `trade_engine.py`~~ → Đã xóa (không tích hợp)
- ~~Integrate `prediction_engine.py`~~ → ✅ ĐÃ INTEGRATE
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

---

## 17. BACKTEST HARNESS (backtest/) — [MỚI]

Mục tiêu: đo win-rate & đầu ra của logic hiện tại trên dữ liệu lịch sử thay vì
chờ stream live. **Tái dùng CHÍNH `AdvancedSignalEngine` + `TradeSimulator`** (không
viết lại logic) → kết quả phản ánh đúng logic production.

### File
```
backtest/
├── fetch_data.py   — Tải klines Binance (phân trang 1000), cache .pkl ở scratchpad
├── backtest.py     — Replay nến qua engine; tổng hợp metric; ghi results/*.json
├── diagnose.py     — Đếm "phễu" quyết định (bias/confluence/RR) để biết gate nào chặn
└── results/        — backtest_<tag>.json + run_<tag>.log (deliverable, trong repo)
```

### Cơ chế khớp live (backtest.py)
- **Thứ tự mỗi nến đóng** y như `main.on_market_tick`: `trade_sim.process_tick(close,
  high,low)` TRƯỚC (gọi 2 lần để "tiêu" cờ `just_opened` của lệnh mở nến trước, khớp
  hành vi intra-candle live) → rồi `generate_signal(df_win, mtf_context)`.
- **Cô lập I/O**: `chdir` vào RUN_DIR tạm; mọi `ml_training_data_*`/`trade_history_*`
  ghi ở đó, KHÔNG đụng file live. Weights khởi tạo MẶC ĐỊNH (reproducible).
- **Chống lookahead MTF**: bias khung lớn chỉ dùng nến HTF **đã đóng** tại thời điểm
  nến giao dịch đóng (`searchsorted` trên close-time HTF). Live thấy nến HTF đang
  hình thành — backtest không được phép.
- **Tốc độ**: indicator precompute **vectorized 1 lần/khung** (`compute_indicators_full`,
  bỏ `tail(260)`) rồi slice cửa sổ WINDOW=320. VWAP anchored theo ngày — lệch nhẹ so
  với live (chỉ ảnh hưởng vwap_score hiển thị/learning, KHÔNG ảnh hưởng quyết định).

### Cấu hình mặc định
- `EVAL_DAYS=90` (~3 tháng), `WARMUP_BARS=300` (không tính metric), `WINDOW=320`.
- `FETCH_DAYS`: khung giao dịch ~95d; 4h=220d, 1d=420d (đủ SMA200 cho bias từ đầu).

### Metric xuất ra mỗi (symbol, TF)
- **Forward-test (hướng):** pred_win/loss/flat, pred_winrate = WIN/(WIN+LOSS).
- **Trade (auto bot):** trades_total, trade_winrate, liquidated, final_capital, return_pct, max_drawdown_pct.
- **Phân bố action:** BUY/SELL/HOLD.

### ⚠️ BUG đã gặp khi dựng (đã fix)
`_ms()` ban đầu chia `asi8 // 1_000_000`. Nhưng pandas 3.x giữ dtype `datetime64[ms,UTC]`
nên `.asi8` ĐÃ là mili-giây → chia thêm làm sai đơn vị → `searchsorted` luôn trả 0 →
**mtf_context rỗng → bias NEUTRAL 100% → 0 lệnh**. Fix: `as_unit("ms").asi8` (ép ms mọi
version, không chia). Bài học: cùng lỗi class với BUG-012 (pandas 3.x resolution = ms).

### Lệnh chạy
```
python backtest/backtest.py --tfs 5m,15m,30m,1h --tag fast
python backtest/backtest.py --tfs 1m --tag 1m          # nặng (~chục phút)
python backtest/diagnose.py BTCUSDT 1h                  # phễu quyết định
python backtest/backtest_symbol.py --trigger 15m --bias 1h,4h,1d --min-conf 2 --min-rr 1.5
```

### backtest_symbol.py — UNIFIED 1 trader / 1 coin [MỚI]
Sửa lỗi thiết kế "20 engine độc lập cùng trade": mỗi SYMBOL chỉ 1 engine ở khung
TRIGGER (15m), bias gộp 1h/4h/1d, 1 vị thế/coin. Tái dùng nguyên `generate_signal`,
chỉ nối lại mtf_context + monkeypatch CÔ LẬP `MIN_CONFLUENCE`/`MIN_RR_AFTER_FEES`/
`HTF_MAP[trigger]` trong process backtest (KHÔNG đụng config/live).

### 📊 KẾT QUẢ BACKTEST 3 THÁNG (21/03–24/06/2026, vốn $100/đơn vị)

**Baseline (per-TF, 20 engine — logic gốc):** Mọi khung 1m cháy nặng
(BTC −95.7%, PAXG −72.9%, ETH −43.5%, BNB −10.4%). 5m: BTC −41%, PAXG −33%.
30m/1h gần hòa vốn NHƯNG chỉ vì gần như không vào lệnh (1–9 lệnh/3 tháng).

**Unified per-symbol (sweep nhiều cấu hình):**

| Cấu hình | BTC | ETH | BNB | PAXG |
|---|---|---|---|---|
| 15m c2 rr1.5 | $35.7 (−64%) | $100.3 | $100.1 | $88.0 |
| 15m c2 rr1.0 | $40.2 (−60%) | $100.7 | $97.9 | $88.5 |
| 30m c2 rr1.5 | $92.7 | **$106.1 (+6%)** | $99.9 | $92.8 |
| 15m c3 rr2.5 | $100 (0 lệnh) | $98.5 | $100 | $93.8 |

### ⚠️ KẾT LUẬN CỐT LÕI (3 phát hiện)
1. **Dự đoán hướng = tung đồng xu (47–50.5%)** trên MỌI khung/coin — thống kê chắc
   (2k–25k mẫu/khung). Logic top-down KHÔNG dự đoán hướng tốt hơn ngẫu nhiên.
2. **Trade WR luôn DƯỚI ngưỡng hòa vốn của RR** (rr1.5→cần ~40%, đạt 9–31%;
   rr1.0→cần 50%, đạt 20–43%). → Kỳ vọng âm. TP hiếm chạm trước SL.
3. **Không cấu hình nào có lãi ổn định.** Chỉnh trigger/confluence/RR chỉ đổi MỨC
   lỗ (hoặc ngừng giao dịch). KHÔNG có vùng tham số nào tạo ra edge. Tăng số lệnh =
   tăng lỗ. Khung cao (30m) "ít tệ nhất" chỉ vì ít lệnh → ít phí.

**→ Hệ thống thiếu EDGE thực. Việc cần làm KHÔNG phải tinh chỉnh tham số mà là
THIẾT KẾ LẠI tín hiệu vào lệnh (nguồn alpha).** Kiến trúc unified-per-symbol là nền
tảng đúng để xây tín hiệu mới lên trên.

**Caveat phương pháp:** `process_tick` kiểm SL trước TP khi cả hai trong cùng 1 nến
(bi quan trong nến) → có thể hạ WR thật chút ít; nhưng khoảng cách tới ngưỡng hòa
vốn quá lớn nên không đổi kết luận.

### 18. STRATEGY LAB (backtest/strategy_lab.py) — TÌM EDGE [MỚI]

Tách *execution* (giữ nguyên `TradeSimulator`) khỏi *alpha* (thay entry/SL/TP). Thử
breakout / momentum / meanrev × bias on/off × fixedR/trailing.

**🔑 Phát hiện 1 — BUG SIZING (phải fix ở live):** `MIN_LOT=0.01` tạo phơi nhiễm chênh
**100×** giữa coin: 0.01 lot BTC = $700 notional = **7× vốn $100**; BNB = $6 = 0.06×.
`lot_from_risk_cap` (3%) bị `normalize_lot` kẹp sàn MIN_LOT → vô hiệu cho coin giá cao
→ BTC cháy (~−95%) mọi chiến lược dù WR 39–49%. **Fix: sizing theo % rủi ro/vốn, lot
phân số (bỏ sàn).** Lab patch CÔ LẬP: `lot_sizing.MIN_LOT/LOT_STEP→~0`, `RISK_CAP_PCT
=risk`, `LOT_BASE/LOT_MAX_DYNAMIC` lớn → mỗi lệnh rủi ro đồng đều 1% vốn.

**🔑 Phát hiện 2 — TẦN SUẤT là kẻ giết người:** notional ≈ 1.3× vốn → phí khứ hồi
≈ 0.32% vốn/lệnh. ~120 lệnh/3 tháng (trigger 15m) = ~38% phí + kỳ vọng âm → −50%↓.

**🔑 Phát hiện 3 — BUG sizing thứ 3:** `normalize_lot` kết thúc `round(lot,2)` → lot
phân số nhỏ (BTC ~0.0007) làm tròn về 0 → không trade. Lab patch `normalize_lot`=identity.

**🔑 Phát hiện 4 — KHÔNG CÓ EDGE (kiểm chứng 2 NĂM, 2024-06→2026-06):**
Ban đầu test 3 tháng (1h) thấy momentum/meanrev "thắng B&H cả 4 coin" → NHƯNG đó là
**ảo giác do chế độ thị trường** (cửa sổ giảm thưởng cho việc ít phơi nhiễm). Test lại
trên 2 NĂM (1h, ~18k nến):

| Strategy (1h, 2 năm, risk 1%) | Tổng ret | vs B&H |
|---|---|---|
| meanrev_bias | −136% | THUA B&H |
| momentum_2R | −143% | THUA B&H |
| breakout_3R | −153% | THUA B&H |

**Bằng chứng dứt khoát:** PAXG (vàng) B&H **+72%** (sóng tăng sạch) — mọi chiến lược ra
**−52%..−97%** (thua B&H 122–169 điểm). Trend-following biến +72% thành −73% = KHÔNG
có edge, chỉ có phí + whipsaw. ETH "thắng B&H" chỉ vì ETH sập −57% và bot ít phơi nhiễm.

**KẾT LUẬN CUỐI:** Sau khi thử đủ nguyên mẫu TA (breakout/momentum/meanrev × bias on/off
× fixedR/trailing × RR 1.5–3 × khung 15m/30m/1h × cửa sổ 3 tháng & 2 năm): **TA đơn giản
trên OHLC KHÔNG tạo edge dương sau phí qua trọn chu kỳ.** Khung intraday + SL/TP từng lệnh
không ôm được sóng dài và bị phí bào mòn. KHÔNG wire vào live. Hướng còn lại: (a) position
trader tần suất CỰC thấp (chỉ theo lật trend khung 1d, ôm lệnh nhiều tuần — CHƯA test),
(b) nguồn tín hiệu khác (order-flow/funding/on-chain/ML đa đặc trưng), (c) định vị lại
hệ thống là công cụ giáo dục/paper-trading. **Sizing theo % rủi ro + lot phân số là bug
THẬT phải fix ở live bất kể hướng nào.**

### 19. POSITION TRADER (backtest/position_trader.py) — HƯỚNG KHẢ THI ✅ [MỚI]

Kiểm chứng hướng (a): bỏ SL/TP & giao dịch dày → chỉ bám trend khung NGÀY (1d),
giữ lệnh hàng tuần/tháng, đổi trạng thái khi trend lật. Full-allocation (notional=vốn,
KHÔNG đòn bẩy) để so công bằng với B&H. Vài lệnh/năm → phí ~0.

**Kết quả (~2.3 năm, 2024-02→2026-06):**

| Strategy | Tổng ret | Điểm nhấn |
|---|---|---|
| ema_longonly (LONG khi EMA21>EMA50 1d, else CASH) | **+198%** | ETH −1% vs B&H −50% (né sập); BNB +103% vs +44% |
| bias_longonly | +171% | thắng B&H 3/4, BTC +21% vs +7% |
| ema_longshort | +285% | BNB +170% nhưng MaxDD tới 61% |

**Ý nghĩa:** kẻ giết bot cũ là TẦN SUẤT/PHÍ + SL hẹp, KHÔNG phải ý tưởng trend. Trend-
following tần suất thấp trên 1d ĐÁNH BẠI buy-and-hold — chủ yếu nhờ **né sụt giảm lớn**
(ETH) và **ôm trọn sóng** (BNB). Đây là "trend filter overlay" kinh điển, có cơ sở.

**Caveat trung thực:** (1) Max drawdown vẫn lớn (28–65%) — đây là chiến lược "tốt hơn
B&H", KHÔNG phải rủi ro thấp. (2) longonly an toàn hơn longshort (short rủi ro, DD cao
hơn) → khuyến nghị **ema_longonly**. (3) Mẫu 2.3 năm/1 chu kỳ — vẫn cần thêm chu kỳ.
(4) "Edge" phần lớn là QUẢN TRỊ RỦI RO (đứng ngoài downtrend), không phải alpha kỳ diệu.

**Khuyến nghị:** dùng ema_longonly như TÍN HIỆU PHÂN BỔ hằng ngày (giữ coin khi trend
1d lên / về USDT khi xuống), KHÔNG phải bot intraday. Có thể wire vào terminal như 1
"regime badge" mức symbol, check 1 lần/ngày.

### 20. PHÍ EXNESS thay đổi cục diện + chiến lược SWING [MỚI]

Toàn bộ kết luận "intraday luôn lỗ vì phí" dựa trên **phí Binance spot 0.24%/vòng**.
User trade **Exness** (vàng ~0.03%, crypto ~0.06%/vòng — thấp hơn 4–8×). `strategy_lab.py`
thêm `--cost` (patch `TradeSimulator.TAKER_FEE`). Chạy lại 1h, 2 năm, cost 0.06%:

| Strategy (1h, 2 năm, cost 0.06%) | Tổng ret | Ghi chú |
|---|---|---|
| breakout_3R | **+68%** | ETH +16.6 (B&H −57!), thắng B&H 3/4; bỏ lỡ sóng vàng |
| breakout_trail | +35% | |
| **pullback_trend** (swing, MỚI) | +8% | **WR 41–52%, MaxDD chỉ 2–12%**, ~1 lệnh/tháng, thắng B&H 3/4 |

**Phát hiện:** ở phí Exness, giao dịch tần suất vừa KHÔNG còn bị phí giết → có chiến lược
DƯƠNG qua 2 năm. **`pullback_trend`** (mua nhịp chỉnh trong uptrend, TP 1.5R) cho hồ sơ
RỦI RO TỐT NHẤT: WR ~50%, drawdown 2–12% (so 30–65% của các chiến lược khác), lãi đều
nhẹ. `breakout_3R` lãi cao hơn nhưng DD lớn hơn & nhiều lệnh hơn.

**Caveat:** (1) cost 0.06% là ƯỚC LƯỢNG — phải lấy phí THẬT từ tài khoản Exness của user
(kết quả rất nhạy với cost). (2) Lãi LŨY KẾ 2 năm, KHÔNG đều hàng tháng (đến theo sóng).
(3) Vàng (B&H +72%) là điểm yếu của pullback/meanrev — không ôm được trend dài. (4) In-sample,
cần WALK-FORWARD để loại overfit. Bug sizing (mục 18) vẫn phải fix ở live.

### 21. WALK-FORWARD (backtest/walkforward.py) — KIỂM ĐỊNH ĐỘ BỀN [MỚI]

**(A) Độ nhạy phí (lãi TB/coin, 2 năm 1h):**

| Strategy | phí 0.03% | phí 0.06% | phí 0.10% |
|---|---|---|---|
| pullback_trend | +2.9% | +2.0% | +0.8% |
| breakout_3R | +32.8% | +16.9% | **−0.8%** |

→ breakout_3R lãi cao nhưng **chết ở phí ≥0.10%** (chỉ sống ở tài khoản phí thấp: vàng/FX/raw).
pullback_trend bền với phí nhưng lãi cực nhỏ (≈ hòa vốn).

**(B) Nhất quán qua 6 cửa sổ (~4 tháng/đoạn, phí 0.06%):**

| Strategy | W1 | W2 | W3 | W4 | W5 | W6 | +đoạn |
|---|---|---|---|---|---|---|---|
| pullback_trend | −0.1 | −0.9 | +0.2 | −1.3 | **+3.4** | +0.9 | 3/6 |
| breakout_3R | +1.9 | −2.4 | −2.9 | **+12.1** | **+7.5** | +1.8 | 4/6 |

**KẾT LUẬN WALK-FORWARD (quan trọng nhất):**
- **KHÔNG có edge ổn định, độc lập chế độ.** Lãi của cả 2 đến từ 1–2 cửa sổ (W4/W5 — giai
  đoạn có trend), các đoạn khác hòa/âm. WR pullback dao động 33–85% theo đoạn (KHÔNG ổn định).
- **pullback_trend ≈ hòa vốn** (lãi trong nhiễu) nhưng rủi ro rất thấp (DD 2–12%) → "bảo toàn vốn".
- **breakout_3R = trend-follower thật**: lãi lũy kế dương NHƯNG **lumpy** (đứng yên/âm nhiều
  tháng, rồi 1 đợt trend bù) + **rất nhạy phí**.
- **→ MỤC TIÊU "lãi đều hàng tháng + WR cao ổn định" KHÔNG đạt được với TA đơn giản.** Lãi
  đến theo sóng trend, tập trung, không đều. Đây là trần của hướng TA — tinh chỉnh thêm =
  overfit vào mẫu 2 năm này.

### 22. ALT-DATA RESEARCH (funding, order-flow) — đều ~0 edge [MỚI]

Hướng "tín hiệu giàu hơn". Kiểm SỨC DỰ ĐOÁN từng tín hiệu (gộp BTC/ETH/BNB, ~2 năm, 1h):

- **Funding rate** (`backtest/funding_research.py`): corr(funding, fwd return) = +0.001..+0.009
  (≈0, còn ngược dấu giả thuyết contrarian). Chiến lược contrarian Q1/Q5: WR 47–49%, đều lỗ.
  → **Funding đứng riêng KHÔNG có edge.**
- **Order-flow** (taker-buy ratio, `backtest/orderflow_research.py`): corr = +0.004..+0.021
  (≈0). Có momentum RẤT mờ (mua mạnh→tăng nhẹ ở 72h) nhưng biên ~0.08–0.2% < phí. Không tradeable riêng.

**Meta-kết luận (quan trọng):** TA (nhiều biến thể + walk-forward) + funding + order-flow —
TẤT CẢ tín hiệu free/đơn giản đều ~0 edge bền trên major crypto. Nhất quán với thị trường
hiệu quả: tín hiệu đơn giản trên tài sản thanh khoản lớn đã bị arbitrage hết. Edge thật
thường đòi hỏi: dữ liệu alt đắt/độc quyền, tốc độ (HFT), ML tinh vi trên feature giàu +
hạ tầng, hoặc thị trường ngách/kém thanh khoản. Setup retail + data free + phương pháp đơn
giản là nơi KHÓ tìm edge nhất.

**Còn lại:** ML đa đặc trưng (tương tác phi tuyến nhiều tín hiệu yếu) — cần cài scikit-learn
(môi trường hiện THIẾU). Rủi ro overfit cao, kỳ vọng thực tế thấp vì input đều ~0. Là phép
thử cuối cùng có cơ sở của hướng systematic.

### 23. ML ĐA ĐẶC TRƯNG + WALK-FORWARD — KHÔNG edge (KẾT LUẬN CUỐI) [MỚI]

`backtest/ml_research.py` (sklearn HistGradientBoosting). 16 feature (TA + order-flow +
funding), gộp BTC/ETH/BNB, 54.576 mẫu, nhãn = dấu lợi suất 24h. Walk-forward 13 fold
(train 12 tháng → test 1 tháng, PURGE H giờ chống rò rỉ), chuẩn hoá theo train.

**Kết quả OOS (28.080 mẫu):**
- Directional accuracy = **49.86%** (baseline lớp đa số 50.66% → mô hình CÒN TỆ HƠN đoán bừa).
- **AUC = 0.4982** (≈0.5 = vô dụng).
- Chiến lược mọi ngưỡng p (0.52/0.55/0.58): đều ÂM (−58% đến −73%), WR 44–49%.

**KẾT LUẬN CUỐI CÙNG CỦA TOÀN BỘ NGHIÊN CỨU:** Mọi hướng — TA (nhiều biến thể + walk-forward),
funding, order-flow, và ML phi tuyến đa đặc trưng (walk-forward nghiêm ngặt) — đều hội tụ:
**KHÔNG có edge bền, khai thác được trong dữ liệu free + phương pháp tiêu chuẩn trên major
crypto.** AUC 0.498 với GBM trên 54k mẫu/13 fold là bằng chứng gần như dứt khoát ở quy mô
retail. Lợi nhuận tự động từ tín hiệu đơn giản trên tài sản thanh khoản lớn = không có sẵn.

**Giá trị THẬT đã tạo ra (nên giữ & wire vào live):**
1. **Bug sizing** (mục 18): % rủi ro + lot phân số (bỏ MIN_LOT 0.01 & round(lot,2)) — fix thật.
2. **Regime filter 1d** (mục 19): công cụ QUẢN TRỊ RỦI RO (né downtrend lớn — ETH −50%→−1%).
3. **Backtest harness** (`backtest/`): để user tự kiểm mọi ý tưởng tương lai, chống overfit.
Định vị đúng của hệ thống: **trợ lý phân tích/giáo dục/quản trị rủi ro**, KHÔNG phải máy in tiền.

### 24. TRIỂN KHAI LIVE từ nghiên cứu (v6) [MỚI]

Ba giá trị thật của nghiên cứu đã được wire vào hệ thống live:

**(1) Fix bug sizing — `lot_sizing.py` + `signal_engine.py:open_position`:**
- Thêm `lot_sizing.risk_based_lot(capital, sl_dist, symbol)` — lot RAW theo % rủi ro
  (`RISK_CAP_PCT × vốn / (sl_dist × contract)`), KHÔNG kẹp sàn.
- `open_position` giờ: nếu `min(dyn_lot, risk_lot_raw) < MIN_LOT` → **BỎ lệnh + cảnh báo**
  thay vì âm thầm kẹp sàn về 0.01 (lỗi cũ khiến vốn nhỏ trên coin giá cao nhận rủi ro
  >> RISK_CAP_PCT). VD: $100 trên BTC (lot cần ~0.0015 < 0.01) → bỏ lệnh đúng; trên BNB
  (lot 0.05) → vào bình thường. Đây là hành vi QUẢN TRỊ RỦI RO đúng.

**(2) Regime Badge 1d — `main.py:compute_regime_1d` + `static/index.html`:**
- `compute_regime_1d(symbol)`: EMA21 vs EMA50 trên cache 1d → `UPTREND`/`CASH`.
  Gắn vào `analysis["regime_1d"]` ở CẢ 2 nhánh broadcast (intra-tick + nến đóng).
- Frontend: badge `#regime-1d-badge` trong card Live Price (🟢 UPTREND / ⚪ CASH), tooltip
  giải thích đây là công cụ quản trị rủi ro xem ~1 lần/ngày, KHÔNG phải tín hiệu intraday.
- → TICK message thêm field `regime_1d: {state, ema21, ema50}`.

**(3) Backtest harness** — `backtest/README.md` hướng dẫn đầy đủ (chạy, thêm chiến lược,
đọc kết quả, chống overfit). Để user tự kiểm mọi ý tưởng tương lai một cách trung thực.

### 25. THỬ NGHIỆM `pullback_regime` (cổng 1d cứng) — NO-GO [MỚI]

Bối cảnh: user lại hỏi liệu có cách nào đạt WR cao / app khả thi hơn cho Exness. Chốt
hướng (phiên này): mục tiêu = **kỳ vọng dương** (không chạy theo WR), backtest-validate
trước khi wire. Giả thuyết kết hợp: cổng REGIME khung NGÀY (1d) cứng (lấy edge né-downtrend
của `ema_longonly`) + entry `pullback` thuận trend trên 1h + **TP 2R**.

**Triển khai (CHỈ trong `backtest/`):**
- `strategy_lab.py`: thêm strat `pullback_regime` (`bias_tfs=["1d"]`, rr=2.0) + hỗ trợ
  `cfg["bias_tfs"]` để 1 strategy ép khung cổng riêng (độc lập `BIAS_TFS` toàn cục).
- `walkforward.py`: thêm vào `TEST`; thêm **kỳ vọng/lệnh theo R** (`avg_r_multiple`),
  bảng WR/avgR/MaxDD, và **tiêu chí GO/NO-GO**; lưu `results/walkforward_pullback_regime.json`.

**Kết quả walk-forward (2 năm 1h, 4 coin, phí 0.06%):**

| Strategy | Trades | WR% | avg R | MaxDD% | Ret%TB | +đoạn |
|---|---|---|---|---|---|---|
| **pullback_regime** | 71 | 35.2 | **−0.014** | 7.3 | −0.5 | 2/6 |
| pullback_trend | 98 | 46.9 | +0.099 | 5.7 | +2.0 | 3/6 |
| breakout_3R | 1133 | 29.1 | +0.068 | 23.8 | +16.9 | 4/6 |

Độ nhạy phí pullback_regime: +0.1% (0.03%) → −0.5% (0.06%) → −1.3% (0.10%).

**GO/NO-GO = ❌ NO-GO** (đạt 2/5): ✓MaxDD<25, ✓không coin nào <−20%; ✗avgR≥0.3, ✗WR≥40,
✗≥4/6 đoạn dương.

**Kết luận:** cổng 1d CỨNG **làm xấu hơn** `pullback_trend` (cắt 98→71 lệnh mà không lọc
được lệnh tốt hơn) → khẳng định lại: giá trị của regime 1d là **quản trị rủi ro** (DD chỉ
7.3%), KHÔNG phải nguồn alpha. Nhất quán toàn bộ nghiên cứu (mục 18–24). **KHÔNG wire vào
live.** `pullback_trend` vẫn là hồ sơ tốt nhất cho intraday-swing (≈hòa vốn, DD thấp), nhưng
không đạt "kỳ vọng dương rõ rệt". Hướng còn lại đúng đắn nhất: dùng **regime badge 1d**
(đã có ở live, mục 24) như tín hiệu PHÂN BỔ swing, không kỳ vọng bot intraday sinh lời.

### 26. WIRE LIVE: BREAKOUT SWING cho VÀNG + PHÍ THEO SYMBOL (v7) [MỚI]

Bối cảnh: user trade Exness Standard, muốn xem khả năng sinh lời nếu BÁM tín hiệu app (để
so với bot web), chọn dùng PAXG làm proxy vàng, và yêu cầu app tự đề xuất SL/TP hợp lý.
Bằng chứng (mục 20 & breakout riêng PAXG): breakout Donchian thuận trend trên VÀNG **+97.6%
vs B&H +72.2% (MaxDD ~12%)** ở phí 0.02%, ≈hòa ở 0.04%, thua ở 0.06% → CHỈ sống ở phí thấp.

**(1) Chiến lược BREAKOUT theo symbol — `config.py` + `signal_engine.py`:**
- `config.BREAKOUT_SYMBOLS={"PAXGUSDT"}`, `BREAKOUT_DONCHIAN_N=20`, `BREAKOUT_SL_ATR=1.5`,
  `BREAKOUT_RR=3.0`.
- `signal_engine`: 2 helper `_breakout_candidate(df, bias_dir, price)` (phá kênh Donchian N
  nến ĐÃ ĐÓNG thuận bias, chống lookahead) + `_breakout_sl_tp(side, price, atr)` (SL 1.5×ATR,
  TP 3R). Trong `generate_signal`, nhánh `is_breakout = symbol in BREAKOUT_SYMBOLS`: dùng
  entry breakout thay cho confluence, SL/TP breakout thay cho cấu trúc, **vào NGAY trên nến
  phá vỡ** (không chờ xác nhận 2 nến — phá vỡ là sự kiện 1 nến). Vẫn qua cổng `MIN_RR_AFTER_FEES`
  + sizing rủi ro + `open_position` cũ. Các symbol khác giữ nguyên logic confluence.
- `ui_state["strategy"]` = "Breakout Swing (vàng)" | "Confluence MTF" → frontend hiển thị.

**(2) PHÍ THEO SYMBOL (Exness) — `config.py` + `TradeSimulator`:**
- Lỗi nền tảng phát hiện khi test: `TradeSimulator` đang dùng phí Binance 0.24% khứ hồi →
  breakout vàng KHÔNG BAO GIỜ qua được cổng R:R (RR sụt còn 0.65). User trade Exness.
- `config.TAKER_FEE_BY_SYMBOL` (PAXG 0.0001 → ~0.03% khứ hồi; crypto 0.0005 → ~0.11%) +
  `DEFAULT_TAKER_FEE`, `SLIPPAGE_PER_SIDE=0.00005`. `TradeSimulator.__init__` set
  `self.TAKER_FEE/SLIPPAGE` theo symbol; `_rr_after_fees` đọc `self.trade_sim.TAKER_FEE`
  (trước đây đọc class-level). ⚠️ Đây là ƯỚC LƯỢNG — user PHẢI hiệu chỉnh theo spread thật.
- Sau fix: smoke test (PAXG, bias BULL ép, nến phá đỉnh) → BUY mở, R:R 2.40 (sau phí vàng),
  SL/TP đề xuất; trường hợp không phá vỡ → HOLD đúng.

**Định vị:** đây là chiến lược TREND-FOLLOWER thật (WR ~33%, ăn xa) — lãi **lumpy** theo
sóng, DD ~12–18%, **rất nhạy phí**. Chỉ có ý nghĩa khi phí khứ hồi thật của user ≤ ~0.03%
(vàng Exness Standard thường đạt). KHÔNG phải đảm bảo lãi — là công cụ để user quan sát &
so với bot web. Paper-trade: theo dõi winrate/PnL/lịch sử trong terminal như các symbol khác.

**(3) HAI KẾ HOẠCH thoát lệnh (v7.1) — bot 3R + trailing tham khảo:**
Theo yêu cầu user ("kết hợp với buy-and-hold"): so sánh trên PAXG cho thấy `breakout_trail`
(ôm trend, trailing 3×ATR thay vì chốt 3R) = **+95.7% vs B&H +72%, KHÔNG nhạy phí** (ít
vào/ra hơn → hợp tài khoản Standard) nhưng DD cao hơn (16% vs 12%) vì trả lại lãi mở khi
giá đảo. User chọn: **BOT vẫn đánh 3R** (rủi ro/lãi xác định, không muốn trả lại lãi khi
trend đảo), **trailing chỉ HIỂN THỊ tham khảo** để tự quyết trên tài khoản thật.
- `config.BREAKOUT_TRAIL_ATR=3.0`. `signal_engine`: khi breakout kích hoạt → `ui_state
  ["breakout_plans"]` = {side, entry, atr, bot_3R:{sl,tp,rr}, trail:{sl_init,trail_atr}}.
  Khi lệnh breakout đang mở → `res_out["breakout_trail_live"]` = {peak, trail_stop, bot_tp}
  với `peak` (đỉnh/đáy thuận lợi) cập nhật mỗi tick trong `process_tick` (`trade['peak']`).
- Frontend: card `#breakout-card` (🥇 Kế Hoạch Breakout) hiện 2 kế hoạch + trailing sống.
- ⚠️ Lưu ý số liệu backtest: lãi đã trừ phí+slippage nhưng **CHƯA trừ swap** (phí qua đêm).
  Trailing ôm lệnh lâu → swap ảnh hưởng nhiều hơn. MaxDD = sụt giảm đỉnh-đáy lớn nhất (%).
