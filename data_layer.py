# data_layer.py
import pandas as pd
import httpx
import websockets
import json
import asyncio
import logging
from config import SYMBOLS, TIMEFRAMES, HISTORY_LIMIT, MAX_CACHE_SIZE

logger = logging.getLogger("DataLayer")


class DataLayer:
    def __init__(self):
        self.cache: dict = {}
        self.on_tick_callback = None

    async def bootstrap(self, symbol: str, interval: str):
        if symbol not in self.cache:
            self.cache[symbol] = {}
        logger.info(f"Bootstrapping {symbol}/{interval}...")
        try:
            url = "https://api.binance.com/api/v3/klines"
            params = {"symbol": symbol, "interval": interval, "limit": HISTORY_LIMIT}
            async with httpx.AsyncClient(timeout=15.0) as client:
                res = await client.get(url, params=params)
                data = res.json()
            df = pd.DataFrame(data, columns=[
                'timestamp', 'open', 'high', 'low', 'close', 'volume',
                'ct', 'qav', 'nt', 'tb', 'tq', 'i'
            ])
            df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms', utc=True)
            for col in ['open', 'high', 'low', 'close', 'volume']:
                df[col] = df[col].astype(float)
            self.cache[symbol][interval] = (
                df[['timestamp', 'open', 'high', 'low', 'close', 'volume']]
                .reset_index(drop=True)
                .copy()
            )
        except Exception as e:
            logger.error(f"Bootstrap error {symbol}/{interval}: {e}")

    async def bootstrap_all(self):
        """Bootstrap all symbol/timeframe combinations concurrently."""
        tasks = [
            self.bootstrap(sym, tf)
            for sym in SYMBOLS
            for tf in TIMEFRAMES
        ]
        await asyncio.gather(*tasks)
        logger.info(f"Bootstrap complete: {len(SYMBOLS)} symbols × {len(TIMEFRAMES)} timeframes")

    async def ws_loop(self):
        streams = [f"{s.lower()}@kline_{t}" for s in SYMBOLS for t in TIMEFRAMES]
        ws_url = f"wss://stream.binance.com:9443/stream?streams={'/'.join(streams)}"
        while True:
            try:
                async with websockets.connect(
                    ws_url,
                    ping_interval=20,
                    ping_timeout=20,
                    close_timeout=10,
                    max_size=10_000_000,
                ) as ws:
                    logger.info("Binance WS Connected!")
                    while True:
                        msg = json.loads(await ws.recv())['data']
                        await self._update_cache(msg)
            except Exception as e:
                logger.error(f"WS Disconnected: {e}. Reconnecting in 5s...")
                await asyncio.sleep(5)

    async def _update_cache(self, msg: dict):
        k = msg['k']
        sym = msg['s']
        interval = k['i']

        if sym not in self.cache or interval not in self.cache[sym]:
            return

        df = self.cache[sym][interval]
        tick_time = pd.to_datetime(k['t'], unit='ms', utc=True)
        last_idx = df.index[-1]

        if df.at[last_idx, 'timestamp'] == tick_time:
            # In-place update — existing candle
            df.at[last_idx, 'high'] = float(k['h'])
            df.at[last_idx, 'low'] = float(k['l'])
            df.at[last_idx, 'close'] = float(k['c'])
            df.at[last_idx, 'volume'] = float(k['v'])
        else:
            # New candle: in-place rotation — no DataFrame allocation
            # Float columns: shift numpy backing array left by 1 (O(n) copy, no new object)
            for col, raw_val in (
                ('open',   k['o']),
                ('high',   k['h']),
                ('low',    k['l']),
                ('close',  k['c']),
                ('volume', k['v']),
            ):
                buf = df[col].values   # numpy float64 view of backing array
                buf[:-1] = buf[1:]     # numpy handles overlapping copy safely
                buf[-1]  = float(raw_val)

            # Timestamp column (DatetimeTZ): shift via pandas, set last cell
            df['timestamp'] = df['timestamp'].shift(periods=-1)
            df.at[df.index[-1], 'timestamp'] = tick_time

        if self.on_tick_callback:
            tick_data = {
                "time": int(k['t'] / 1000),
                "open": float(k['o']),
                "high": float(k['h']),
                "low": float(k['l']),
                "close": float(k['c']),
            }
            await self.on_tick_callback(sym, interval, bool(k['x']), tick_data)


data_manager = DataLayer()
