# fetch_data.py — Tải klines lịch sử Binance về cache (cho backtest)
# =====================================================================
# Tải toàn bộ SYMBOLS × ALL_TIMEFRAMES với độ dài đủ để backtest 3 tháng
# (kèm warmup cho indicator/HTF bias). Cache ra file .pkl trong CACHE_DIR
# để chạy lại tức thì, không gọi lại API.
#
# Dùng:
#   python backtest/fetch_data.py
# =====================================================================
import os
import sys
import time
import pickle
import logging

import httpx
import pandas as pd

# Cho phép import config từ project root khi chạy trực tiếp
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from config import SYMBOLS, ALL_TIMEFRAMES  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)  # bớt log từng request
logger = logging.getLogger("fetch_data")

BINANCE_KLINES = "https://api.binance.com/api/v3/klines"

# Cache nằm ở scratchpad (không làm bẩn repo). Có thể override bằng env.
CACHE_DIR = os.environ.get(
    "BACKTEST_CACHE_DIR",
    r"C:\Users\vuhp\AppData\Local\Temp\claude\D--Tools-Trading2"
    r"\5717359c-bea7-4510-8dfd-de4a640339ec\scratchpad\bt_data",
)

# Số NGÀY lịch sử cần tải cho mỗi khung. Khung GIAO DỊCH = ~95 ngày
# (90 ngày eval + warmup nhỏ). Khung BỐI CẢNH (4h/1d) cần dài hơn để
# SMA200/bias khung lớn hợp lệ ngay từ đầu cửa sổ eval.
FETCH_DAYS = {
    "1m": 95, "5m": 95, "15m": 95, "30m": 95,
    # Khung cao kéo dài để backtest ~2 năm (validate edge qua nhiều chế độ TT).
    "1h": 760, "4h": 820, "1d": 1100,
}

_TF_MS = {
    "1m": 60_000, "5m": 300_000, "15m": 900_000, "30m": 1_800_000,
    "1h": 3_600_000, "4h": 14_400_000, "1d": 86_400_000,
}


def _now_ms() -> int:
    return int(time.time() * 1000)


def fetch_klines(symbol: str, interval: str, days: int) -> pd.DataFrame:
    """Tải klines [now-days, now], phân trang 1000 nến/lần."""
    end = _now_ms()
    start = end - days * 86_400_000
    step = _TF_MS[interval] * 1000  # 1000 nến mỗi request
    rows = []
    cur = start
    with httpx.Client(timeout=30.0) as client:
        while cur < end:
            params = {
                "symbol": symbol, "interval": interval,
                "startTime": cur, "endTime": end, "limit": 1000,
            }
            for attempt in range(5):
                try:
                    res = client.get(BINANCE_KLINES, params=params)
                    if res.status_code == 429:  # rate limit
                        wait = 2 ** attempt
                        logger.warning(f"429 rate-limit {symbol}/{interval}, chờ {wait}s")
                        time.sleep(wait)
                        continue
                    res.raise_for_status()
                    batch = res.json()
                    break
                except Exception as e:
                    if attempt == 4:
                        raise
                    time.sleep(1 + attempt)
            else:
                batch = []

            if not batch:
                break
            rows.extend(batch)
            last_open = batch[-1][0]
            nxt = last_open + _TF_MS[interval]
            if nxt <= cur:        # không tiến → thoát tránh vòng lặp vô hạn
                break
            cur = nxt
            if len(batch) < 1000:  # hết dữ liệu
                break
            time.sleep(0.12)       # nhẹ tay với API

    if not rows:
        return pd.DataFrame(columns=["timestamp", "open", "high", "low", "close", "volume"])

    df = pd.DataFrame(rows, columns=[
        "timestamp", "open", "high", "low", "close", "volume",
        "ct", "qav", "nt", "tb", "tq", "i",
    ])
    df = df[["timestamp", "open", "high", "low", "close", "volume"]].copy()
    df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
    for col in ["open", "high", "low", "close", "volume"]:
        df[col] = df[col].astype(float)
    # Bỏ trùng (do biên trang) + sắp xếp
    df = df.drop_duplicates(subset="timestamp").sort_values("timestamp").reset_index(drop=True)
    return df


def cache_path(symbol: str, interval: str) -> str:
    return os.path.join(CACHE_DIR, f"{symbol}_{interval}.pkl")


def fetch_all(force: bool = False) -> dict:
    """Tải/đọc cache toàn bộ. Trả về dict[symbol][tf] = DataFrame."""
    os.makedirs(CACHE_DIR, exist_ok=True)
    out: dict = {}
    for sym in SYMBOLS:
        out[sym] = {}
        for tf in ALL_TIMEFRAMES:
            p = cache_path(sym, tf)
            if (not force) and os.path.exists(p):
                with open(p, "rb") as f:
                    df = pickle.load(f)
                logger.info(f"[cache] {sym}/{tf}: {len(df)} nến")
            else:
                df = fetch_klines(sym, tf, FETCH_DAYS[tf])
                with open(p, "wb") as f:
                    pickle.dump(df, f)
                logger.info(f"[fetch] {sym}/{tf}: {len(df)} nến "
                            f"({df['timestamp'].min()} → {df['timestamp'].max()})")
            out[sym][tf] = df
    return out


if __name__ == "__main__":
    force = "--force" in sys.argv
    fetch_all(force=force)
    logger.info(f"Hoàn tất. Cache tại: {CACHE_DIR}")
