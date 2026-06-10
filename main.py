import asyncio
import json
import logging
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel
import uvicorn

from ai_advisor import analyze_user_trade
from data_layer import data_manager
from indicator_layer import IndicatorLayer
from signal_engine import AdvancedSignalEngine
from config import SYMBOLS, TIMEFRAMES

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("MainApp")

# Shared indicator layer (cached per symbol/interval)
indicator_layer = IndicatorLayer()

# One engine per symbol/timeframe combination
engines = {
    f"{sym}_{tf}": AdvancedSignalEngine(sym, tf)
    for sym in SYMBOLS
    for tf in TIMEFRAMES
}


# ==========================================
# WEBSOCKET CONNECTION MANAGER
# ==========================================
class ConnectionManager:
    def __init__(self):
        self.active_connections: dict = {}

    async def connect(self, ws: WebSocket):
        await ws.accept()
        self.active_connections[ws] = {"symbol": "BTCUSDT", "interval": "1m"}

    def disconnect(self, ws: WebSocket):
        self.active_connections.pop(ws, None)

    def update_subscription(self, ws: WebSocket, symbol: str, interval: str):
        if ws in self.active_connections:
            self.active_connections[ws]["symbol"] = symbol
            self.active_connections[ws]["interval"] = interval

    async def broadcast_to_symbol(self, message: str, symbol: str, interval: str):
        for ws, pref in list(self.active_connections.items()):
            if pref["symbol"] == symbol and pref["interval"] == interval:
                try:
                    await ws.send_text(message)
                except Exception:
                    self.disconnect(ws)


connection_manager = ConnectionManager()


# ==========================================
# MARKET TICK HANDLER
# ==========================================
async def on_market_tick(symbol: str, interval: str, is_closed: bool, tick_data: dict):
    engine = engines.get(f"{symbol}_{interval}")
    if engine is None:
        return

    current_price = tick_data['close']

    # Check manual TP/SL
    closed_manual_trades = engine.manual_sim.update_tick(current_price)
    for ct in closed_manual_trades:
        await connection_manager.broadcast_to_symbol(
            json.dumps({"type": "MANUAL_TRADE_CLOSED", "data": ct}),
            symbol, interval,
        )

    # Get cached raw data
    df_raw = data_manager.cache.get(symbol, {}).get(interval)
    if df_raw is None or df_raw.empty:
        return

    # Compute indicators for current interval (always fresh — close price changes)
    df_ind = indicator_layer.apply_indicators(df_raw)

    # MTF context — FIXED: use 5m, 15m, 1h (matches signal_engine expectation)
    mtf_context = {}
    for tf in ("5m", "15m", "1h"):
        df_tf = data_manager.cache.get(symbol, {}).get(tf)
        if df_tf is not None and not df_tf.empty:
            # Use cached version — only recomputes when candle closes
            mtf_context[tf] = indicator_layer.apply_indicators_cached(df_tf, symbol, tf)

    # Generate signal
    analysis = engine.generate_signal(df_ind, mtf_context)
    analysis["manual_active"]  = engine.manual_sim.active_trades
    analysis["manual_history"] = engine.manual_sim.history

    message = json.dumps({
        "type":   "TICK",
        "symbol": symbol,
        "candle": tick_data,
        "signal": analysis,
    })
    await connection_manager.broadcast_to_symbol(message, symbol, interval)


data_manager.on_tick_callback = on_market_tick


# ==========================================
# FASTAPI LIFESPAN
# ==========================================
@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("[SYSTEM] Starting Quant Trading Terminal...")
    # Bootstrap all symbols/timeframes concurrently (async, non-blocking)
    await data_manager.bootstrap_all()
    # Start Binance WS stream in background
    asyncio.create_task(data_manager.ws_loop())
    yield
    logger.info("[SYSTEM] Shutting down.")


app = FastAPI(lifespan=lifespan)
app.mount("/static", StaticFiles(directory="static"), name="static")


@app.get("/")
async def root():
    return FileResponse("static/index.html")


# ==========================================
# WEBSOCKET ENDPOINT
# ==========================================
@app.websocket("/ws/frontend")
async def websocket_endpoint(ws: WebSocket):
    await connection_manager.connect(ws)
    try:
        while True:
            data = await ws.receive_text()
            req  = json.loads(data)

            if req.get("action") == "subscribe":
                symbol   = req['symbol']
                interval = req['interval']
                capital  = req.get("capital")

                connection_manager.update_subscription(ws, symbol, interval)

                engine = engines.get(f"{symbol}_{interval}")
                if engine and capital is not None:
                    try:
                        engine.set_capital(float(capital))
                    except (ValueError, TypeError):
                        logger.warning(f"Invalid capital: {capital}")

                # Bootstrap on-demand if symbol not in cache
                if symbol not in data_manager.cache:
                    await data_manager.bootstrap(symbol, interval)

                # Send 200-candle history for chart initialisation
                df_raw = data_manager.cache.get(symbol, {}).get(interval)
                if df_raw is not None and not df_raw.empty:
                    df_ind = indicator_layer.apply_indicators(df_raw)
                    tail   = df_ind.tail(200)

                    history_data = [
                        {
                            "time":   int(row['timestamp'].timestamp()),
                            "open":   row['open'],  "high": row['high'],
                            "low":    row['low'],   "close": row['close'],
                            "EMA_9":  row.get('EMA_9',  0) or 0,
                            "EMA_21": row.get('EMA_21', 0) or 0,
                        }
                        for _, row in tail.iterrows()
                    ]
                    history_data.sort(key=lambda x: x['time'])

                    await ws.send_text(json.dumps({
                        "type": "FULL_LOAD", "symbol": symbol, "data": history_data,
                    }))

    except WebSocketDisconnect:
        connection_manager.disconnect(ws)
    except Exception as e:
        logger.error(f"WebSocket error: {e}")
        connection_manager.disconnect(ws)


# ==========================================
# REST ENDPOINTS
# ==========================================
class TradeRequest(BaseModel):
    entry:    float
    tp:       float
    sl:       float
    position: str


class ManualTradeData(BaseModel):
    entry:    float
    tp:       float
    sl:       float
    position: str


@app.post("/trade/analyze")
async def get_trade_analysis(symbol: str, interval: str, body: TradeRequest):
    engine = engines.get(f"{symbol}_{interval}")
    if engine is None:
        return {"error": "Engine not found", "rr": 0, "suggestions": [], "is_valid": False}

    current_inds = engine.ui_state.get('indicators', {})
    result = analyze_user_trade(
        entry=body.entry, tp=body.tp, sl=body.sl,
        position=body.position, current_indicators=current_inds,
    )
    return result


@app.post("/trade/open-manual")
async def open_manual_trade(symbol: str, interval: str, body: ManualTradeData):
    engine = engines.get(f"{symbol}_{interval}")
    if engine is None:
        return {"status": "error", "message": "Engine not found"}
    engine.manual_sim.open_trade(body.model_dump())
    return {"status": "success"}


if __name__ == "__main__":
    uvicorn.run("main:app", host="127.0.0.1", port=8000, reload=True)
