# CLAUDE.md — Production Quant Trading Terminal

## Project Overview
Real-time cryptocurrency trading terminal. Python FastAPI backend streams Binance WebSocket data, computes technical indicators, runs an adaptive ML signal engine, and pushes results to a browser-based chart via WebSocket.

**Stack:** Python 3.13, FastAPI, pandas/pandas-ta, websockets, Lightweight Charts (JS)  
**Run:** `uvicorn main:app --host 127.0.0.1 --port 8000 --reload`  
**Symbols:** BTCUSDT, ETHUSDT, BNBUSDT, PAXGUSDT  
**Timeframes:** 1m, 5m, 15m, 30m, 1h

---

## Architecture (Data Flow)

```
Binance WS Stream
      │
      ▼
data_layer.py  (DataLayer)
  ├─ bootstrap()       — REST API, loads 5000 candles per symbol/TF
  ├─ ws_loop()         — async WebSocket consumer, auto-reconnect
  └─ _update_cache()   — updates in-memory DataFrame cache, fires callback

      │ on_tick_callback
      ▼
main.py  (on_market_tick)
  ├─ ManualTradeSimulator.update_tick()    — TP/SL check for manual trades
  ├─ indicator_layer.apply_indicators()    — compute full indicator stack
  ├─ mtf_context build (1m, 5m, 15m)      — multi-timeframe context
  └─ engine.generate_signal()             — signal + ML scoring

      │
      ▼
signal_engine.py  (AdvancedSignalEngine)
  ├─ AdaptiveScorer     — ML weight adjustment, win-rate tracking
  ├─ TradeSimulator     — auto-trade simulation with TP/SL, risk sizing
  └─ generate_signal()  — returns ui_state dict for WebSocket broadcast

      │ broadcast_to_symbol (WebSocket)
      ▼
static/index.html  (Lightweight Charts + vanilla JS)
  ├─ FULL_LOAD message    — initial 200-candle history on subscribe
  ├─ TICK message         — real-time candle/signal updates
  └─ MANUAL_TRADE_CLOSED  — notification when manual trade hits TP/SL
```

---

## File Map

| File | Role | Status |
|------|------|--------|
| `main.py` | FastAPI app, WS endpoint, bootstrap, routes | **Active** |
| `data_layer.py` | Binance REST + WS, DataFrame cache | **Active** |
| `indicator_layer.py` | pandas-ta indicators (EMA, MACD, RSI, BB, ATR, VWAP) | **Active** |
| `signal_engine.py` | AdaptiveScorer, TradeSimulator, AdvancedSignalEngine | **Active** |
| `simulator_manager.py` | ManualTradeSimulator (manual trade TP/SL logic) | **Active** |
| `config.py` | SYMBOLS, TIMEFRAMES, HISTORY_LIMIT=5000, MAX_CACHE_SIZE=5000 | **Active** |
| `ai_advisor.py` | Rule-based trade analysis (entry/RR check) | **Active** |
| `trade_engine.py` | Newer TradeEngine with pending orders + dataclass | **UNUSED — not integrated** |
| `prediction_engine.py` | PredictionEngine with EMA smoothing | **UNUSED — not integrated** |
| `training_engine.py` | AdaptiveTrainingEngine (buffered weight updates) | **UNUSED — not integrated** |
| `position_manager.py` | Basic position state tracker | **UNUSED — dead code** |
| `core_trading_system.py` | Old orchestrator + duplicate FastAPI app | **UNUSED — dead code** |
| `static/index.html` | Single-file frontend, chart + panel | **Active** |
| `BK/` | Backup copies of old files | Ignore |

---

## Known Bugs (Must Fix Before Adding Features)

### CRITICAL — Runtime Crashes

**BUG-001** `main.py:34-38` — `engine` is fetched but no None-guard before `.manual_sim.update_tick()`.  
If an engine key is missing, this throws `AttributeError: 'NoneType' has no attribute 'manual_sim'`.  
Fix: add `if engine is None: return` right after line 34.

**BUG-002** `main.py:211-225` — `/trade/analyze` POST endpoint uses `trade_input: dict` as a function parameter.  
FastAPI cannot parse a raw `dict` from a POST body without `Body(...)` or a Pydantic model.  
Fix: create a `TradeRequest(BaseModel)` Pydantic class.

**BUG-003** `main.py:228-232` — `/trade/open-manual` same issue: `trade_data: dict` won't parse from POST body.  
Fix: same Pydantic model approach.

**BUG-004** `main.py:54` vs `signal_engine.py:369` — MTF context mismatch.  
`main.py` builds mtf_context with `("1m", "5m", "15m")`, but `generate_signal()` iterates `["5m", "15m", "1h"]`.  
Result: `1h` context is always missing; `1m` is built but never consumed.  
Fix: align both to `["5m", "15m", "1h"]`.

### HIGH — Logic Errors

**BUG-005** `signal_engine.py:366` — `evaluate_and_learn(c_time, float(prev['close']))` passes **prev** candle close as the "actual price".  
The prediction target time is `c_time + tf_seconds*5` in the future, so when that time arrives, the actual price used is the previous candle's close, not the current close. This systematically under-evaluates results.  
Fix: use `float(curr['close'])`.

**BUG-006** `signal_engine.py:57-58` — `AdaptiveScorer._load_and_train_from_history()` accesses `data["prediction"]["range"]["min"]` without checking if the key structure exists. Old JSONL entries with a different schema will crash on startup.  
Fix: use `.get()` chaining with fallbacks.

**BUG-007** `simulator_manager.py:update_tick()` — returns only the **first** trade that closes in a tick. If two manual trades hit TP/SL at the same price tick, only one is returned and the other is silently dropped from notification. The `main.py` caller only handles a single return value.  
Fix: return a list; update caller.

### MEDIUM — Performance & Code Quality

**BUG-008** `data_layer.py:18-31` — `bootstrap()` is **synchronous** (`requests.get()`), called from the `lifespan` async context on startup. With 4 symbols × 5 timeframes = 20 REST calls, this blocks the event loop for several seconds.  
Fix: use `httpx.AsyncClient` in an async `bootstrap()`.

**BUG-009** `indicator_layer.py:78` — `apply_indicators()` does `df.copy()` + full index re-set + VWAP calculation on the entire 5000-candle DataFrame **every tick**.  
With 20 active streams, this is 20 full-DataFrame copies/computations per second.  
Fix: cache indicator results; only recompute the last candle incrementally for updating ticks.

**BUG-010** `data_layer.py:70` — `pd.concat([df, pd.DataFrame([new_row])], ignore_index=True).tail(MAX_CACHE_SIZE)` on every new candle.  
This allocates a new DataFrame every minute per symbol/TF. Use `collections.deque` or pre-allocated buffer.

**BUG-011** `signal_engine.py:141-144` — File I/O (`open(ml_log_file, "a")`) is called synchronously inside the async tick loop. This blocks the event loop on disk write.  
Fix: use `asyncio.get_event_loop().run_in_executor()` or `aiofiles`.

**BUG-012** `main.py:185` — `df_ind.iterrows()` used to build 200-candle history payload. For 200 rows it's acceptable but 5× slower than `.to_dict('records')`.  
Fix: use `df_ind[['timestamp','open','high','low','close','EMA_9','EMA_21']].to_dict('records')`.

### FRONTEND — Lag Sources

**BUG-013** `static/index.html` — `innerHTML` is fully rebuilt on every WebSocket `TICK` message for:
- `manual-history-body` (manual trade table)
- `weights-container` (indicator weight bars)
- `trade-history-body` (auto trade history)
- `tracker-body` (prediction tracker)

This causes DOM thrashing/reflow 60+ times per minute. Fix: diff and update only changed rows, or use `requestAnimationFrame` throttling.

**BUG-014** `static/index.html` — No `requestAnimationFrame` batching on WebSocket messages. Each message triggers synchronous DOM updates, potentially stacking if messages arrive faster than 16ms.

**BUG-015** `static/index.html:62` — `<option value="PAXGUSDT" selected` has excessive whitespace/corruption in the HTML attribute. Minor cosmetic issue but signals copy-paste damage.

---

## Unused / Dead Code to Remove

- `core_trading_system.py` — has its own `app = FastAPI()` which conflicts; never imported by main.py
- `position_manager.py` — never imported anywhere  
- `trade_engine.py`, `prediction_engine.py`, `training_engine.py` — newer, better implementations but not wired up

**Decision required:** Either integrate the newer engines (`trade_engine.py` has proper dataclasses, pending orders) or delete them. Don't leave dead code.

---

## Improvement Roadmap

### Phase 1 — Stability & Bug Fixes (do first)
1. Fix BUG-001, 002, 003 (None guards, Pydantic models)
2. Fix BUG-004 (MTF alignment)
3. Fix BUG-005 (evaluate_and_learn uses wrong price)
4. Fix BUG-006 (safe key access in history loader)

### Phase 2 — Performance (do second)
1. **Indicator Cache**: Cache `apply_indicators()` result keyed by `(symbol, interval, last_candle_time)`. Only recompute when last_candle_time changes. Reduces 20 full computations/second to ~1 per changed candle.
2. **Async Bootstrap**: Replace `requests.get()` with `httpx.AsyncClient`.
3. **Async File I/O**: Replace synchronous `open()` writes with `aiofiles.open()`.
4. **Frontend RAF Throttle**: Wrap all DOM updates in `requestAnimationFrame`. Batch WS messages in a queue, drain at 60fps.

### Phase 3 — Features (after stability)
1. **Integrate `trade_engine.py`**: Replace current `TradeSimulator` with the cleaner `TradeEngine` (has pending orders, proper dataclass, leverage support).
2. **Integrate `prediction_engine.py`**: The smoothed EMA-based predictor is better than the linear slope extrapolation currently used.
3. **Alert System**: Sound/browser notification on signal BUY/SELL.
4. **Multi-symbol Dashboard**: Show all 4 symbols simultaneously (mini-charts grid).
5. **PnL Chart**: Track capital curve over time as a separate chart series.

---

## WebSocket Protocol

### Server → Client

```json
// Initial data load on subscribe
{"type": "FULL_LOAD", "symbol": "BTCUSDT", "data": [{
  "time": 1234567890, "open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0,
  "EMA_9": 1.0, "EMA_21": 1.0
}]}

// Real-time tick update
{"type": "TICK", "symbol": "BTCUSDT", "candle": {
  "time": 1234567890, "open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0
}, "signal": {
  "action": "BUY|SELL|HOLD", "score": 0.42, "confidence": 0.85,
  "prediction": {"direction": "BULLISH", "mid_price": 50000, "range": {"min": 49800, "max": 50200}},
  "indicators": {"EMA_9": 0, "EMA_21": 0, "RSI": 0, "MACD": 0, "ATR": 0, "BB_U": 0, "BB_L": 0},
  "weights": {"EMA": 0.17, "MACD": 0.17, ...},
  "trade": {"position_status": "OPEN|NONE", "entry": 0, "tp": 0, "sl": 0, "pnl": 0, "profit_usd": 0, "capital": 0},
  "trade_history": [...],
  "manual_active": [...], "manual_history": [...],
  "notifications": ["string", ...],
  "tracker": [{"time": "", "direction": "", "range": "", "actual_price": 0, "final_result": "WIN|LOSS|WAITING"}],
  "winrate": 65.0, "reason": "Trend Confirmed"
}}

// Manual trade closed notification
{"type": "MANUAL_TRADE_CLOSED", "data": {"result": "WIN|LOSS", "exit_price": 0, "pnl": 0}}
```

### Client → Server

```json
{"action": "subscribe", "symbol": "BTCUSDT", "interval": "1m", "capital": 100}
```

---

## REST Endpoints

| Method | Path | Description |
|--------|------|-------------|
| GET | `/` | Serve static/index.html |
| POST | `/trade/analyze?symbol=&interval=` | AI rule-based trade analysis (returns RR + suggestions) |
| POST | `/trade/open-manual?symbol=&interval=` | Open a manual simulated trade |

---

## ML / Adaptive Learning

- **AdaptiveScorer** (`signal_engine.py`) learns indicator weights from prediction outcomes.
- Log files: `ml_training_data_{SYMBOL}_{TF}.jsonl` — one JSON line per evaluated prediction
- Trade files: `trade_history_{SYMBOL}_{TF}.jsonl` — one JSON line per closed auto trade
- **Reverse Mode**: activates automatically when win-rate drops below 40% (inverts all signals). Deactivates when win-rate recovers above 45%.
- Weights are bounded: `[0.2, 2.5]` per indicator, normalized to sum to 1.0.

---

## Development Notes

- Encoding: source files use UTF-8. Comments in Vietnamese are fine, keep them.
- pandas-ta column names: MACD histogram = `MACD_H` (mapped from `MACDh_*`), BB = `BB_U`/`BB_L`.
- `AdvancedSignalEngine` has both `set_capital()` (passed from subscribe) and hardcoded `capital=100.0` in `TradeSimulator.__init__`. The `set_capital` call in `main.py` is correct.
- `ManualTradeSimulator` is separate from `TradeSimulator`. Manual trades are user-placed; auto trades are engine-generated.
- Time is always UTC in the backend; `tzOffsetSeconds` correction is applied in the frontend to display local time.
