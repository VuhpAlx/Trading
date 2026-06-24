# indicator_layer.py
import pandas as pd
import pandas_ta as ta
import logging
from config import INDICATOR_WINDOW

logger = logging.getLogger("IndicatorLayer")


class IndicatorLayer:
    def __init__(self):
        # Cache: (symbol, interval) → (last_timestamp, result_df)
        # Recompute only when candle time changes (new candle opened)
        self._cache: dict = {}

    def apply_indicators_cached(self, df: pd.DataFrame, symbol: str, interval: str) -> pd.DataFrame:
        """Return cached indicators if candle time unchanged (MTF optimisation)."""
        if df is None or df.empty:
            return df
        last_ts = df.iloc[-1]['timestamp']
        cache_key = (symbol, interval)
        cached = self._cache.get(cache_key)
        if cached is not None and cached[0] == last_ts:
            return cached[1]
        result = self.apply_indicators(df)
        self._cache[cache_key] = (last_ts, result)
        return result

    @staticmethod
    def apply_indicators(df: pd.DataFrame) -> pd.DataFrame:
        """Compute full indicator stack. Only uses last INDICATOR_WINDOW rows for speed."""
        if df is None or df.empty:
            return df

        df = df.tail(INDICATOR_WINDOW).copy().reset_index(drop=True)
        for col in ['open', 'high', 'low', 'close', 'volume']:
            if col in df.columns:
                df[col] = df[col].astype(float)

        # 1. Trend (EMA)
        try:
            df['EMA_9']  = ta.ema(df['close'], length=9)
            df['EMA_21'] = ta.ema(df['close'], length=21)
            df['EMA_50'] = ta.ema(df['close'], length=50)
            df['SMA_200'] = ta.sma(df['close'], length=200)
        except Exception as e:
            logger.warning(f"Trend error: {e}")

        # 2. MACD
        try:
            macd = ta.macd(df['close'])
            if macd is not None and not macd.empty:
                df['MACD']   = macd.iloc[:, 0]   # MACD line
                df['MACD_H'] = macd.iloc[:, 1]   # Histogram
                df['MACD_S'] = macd.iloc[:, 2]   # Signal line
        except Exception as e:
            logger.warning(f"MACD error: {e}")

        # 3. Momentum
        try:
            df['RSI']    = ta.rsi(df['close'], length=14)
            df['RSI_14'] = df['RSI']
            stoch = ta.stoch(df['high'], df['low'], df['close'])
            if stoch is not None and not stoch.empty:
                df['STOCH_K'] = stoch.iloc[:, 0]
                df['STOCH_D'] = stoch.iloc[:, 1]
        except Exception as e:
            logger.warning(f"Momentum error: {e}")

        # 4. Volatility (BB + ATR)
        try:
            bb = ta.bbands(df['close'], length=20, std=2)
            if bb is not None and not bb.empty:
                df['BB_L'] = bb.iloc[:, 0]
                df['BB_M'] = bb.iloc[:, 1]
                df['BB_U'] = bb.iloc[:, 2]
            df['ATR']    = ta.atr(df['high'], df['low'], df['close'], length=14)
            df['ATR_14'] = df['ATR']
        except Exception as e:
            logger.warning(f"Volatility error: {e}")

        # 5. Trend Strength (ADX + DI)
        try:
            adx_res = ta.adx(df['high'], df['low'], df['close'], length=14)
            if adx_res is not None and not adx_res.empty:
                df['ADX']      = adx_res.iloc[:, 0]
                df['DI_PLUS']  = adx_res.iloc[:, 1]
                df['DI_MINUS'] = adx_res.iloc[:, 2]
        except Exception as e:
            logger.warning(f"ADX error: {e}")

        # 6. Volume
        try:
            df['VOL_MA'] = ta.sma(df['volume'], length=20)
            df['OBV']    = ta.obv(df['close'], df['volume'])
            df2 = df.set_index('timestamp')
            vwap = ta.vwap(df2['high'], df2['low'], df2['close'], df2['volume'])
            if vwap is not None:
                df['VWAP'] = vwap.values
        except Exception as e:
            logger.warning(f"Volume error: {e}")

        # Only fill NaN for numeric columns — avoids corrupting the timestamp column in pandas 2.x.
        num_cols = df.select_dtypes(include='number').columns
        df[num_cols] = df[num_cols].bfill().ffill().fillna(0.0)
        return df
