# orderflow_research.py — POC: ORDER-FLOW (taker buy/sell imbalance) có edge không?
# =====================================================================
# Binance klines có cột "taker buy base volume" = khối lượng khớp bởi lệnh
# MUA chủ động (market buy). taker_buy_ratio = takerBuyBase / volume:
#   >0.5 = phe MUA chủ động áp đảo; <0.5 = phe BÁN chủ động áp đảo.
# Đây là proxy order-flow MIỄN PHÍ (không cần L2 orderbook).
#
# Kiểm: gom ratio (làm mượt) theo ngũ phân vị → lợi suất TƯƠNG LAI.
#   • Nếu mua chủ động cao → giá tiếp tục lên (momentum) → Q5 fwd DƯƠNG.
#   • Hoặc → kiệt sức/đảo (exhaustion) → Q5 fwd ÂM.
# Tương quan ≈ 0 → không edge.
#
# Dùng:  python backtest/orderflow_research.py
# =====================================================================
import os
import sys
import time
import logging

import numpy as np
import pandas as pd
import httpx

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger("orderflow")

KLINES = "https://api.binance.com/api/v3/klines"
SYMBOLS = ["BTCUSDT", "ETHUSDT", "BNBUSDT"]
INTERVAL = "1h"
DAYS = 760
HORIZONS = [4, 24, 72]   # số nến 1h nhìn tới
SMOOTH = 6               # làm mượt ratio qua 6 nến (giảm nhiễu)


def fetch_with_taker(symbol):
    end = int(time.time() * 1000); start = end - DAYS * 86_400_000
    step = 3_600_000 * 1000
    rows = []; cur = start
    with httpx.Client(timeout=30.0) as c:
        while cur < end:
            r = c.get(KLINES, params={"symbol": symbol, "interval": INTERVAL,
                                      "startTime": cur, "endTime": end, "limit": 1000})
            r.raise_for_status(); batch = r.json()
            if not batch:
                break
            rows.extend(batch); last = batch[-1][0]
            if last + 3_600_000 <= cur:
                break
            cur = last + 3_600_000
            if len(batch) < 1000:
                break
            time.sleep(0.1)
    df = pd.DataFrame(rows, columns=["t", "o", "h", "l", "c", "v", "ct", "qav",
                                     "n", "tbb", "tbq", "ig"])
    df["close"] = df["c"].astype(float)
    df["volume"] = df["v"].astype(float)
    df["taker_buy"] = df["tbb"].astype(float)
    df = df.drop_duplicates("t").reset_index(drop=True)
    return df


def main():
    pooled_ratio = []
    pooled_fwd = {h: [] for h in HORIZONS}
    for s in SYMBOLS:
        df = fetch_with_taker(s)
        logger.info(f"{s}: {len(df)} nến 1h")
        vol = df["volume"].to_numpy()
        ratio = np.divide(df["taker_buy"].to_numpy(), vol, out=np.full(len(df), 0.5), where=vol > 0)
        # làm mượt
        ratio_s = pd.Series(ratio).rolling(SMOOTH, min_periods=1).mean().to_numpy()
        closes = df["close"].to_numpy()
        pooled_ratio.append(ratio_s)
        for h in HORIZONS:
            fr = np.full(len(df), np.nan)
            ok = np.arange(len(df)) + h < len(df)
            fr[ok] = closes[np.arange(len(df))[ok] + h] / closes[np.arange(len(df))[ok]] - 1
            pooled_fwd[h].append(fr)

    ratio = np.concatenate(pooled_ratio)
    qs = np.nanpercentile(ratio, [20, 40, 60, 80])
    bucket = np.digitize(ratio, qs)

    print("\n" + "=" * 86)
    print("ORDER-FLOW (taker-buy ratio làm mượt 6 nến) — gộp BTC/ETH/BNB, ~2 năm, khung 1h")
    print("Q1 = bán chủ động áp đảo → Q5 = mua chủ động áp đảo.")
    print(f"\n{'Nhóm order-flow':<24}{'fwd 4h':>12}{'fwd 24h':>12}{'fwd 72h':>12}{'N':>9}")
    print("-" * 86)
    labels = ["Q1 (bán mạnh)", "Q2", "Q3", "Q4", "Q5 (mua mạnh)"]
    for b in range(5):
        mask = bucket == b
        cells = [np.nanmean(np.concatenate(pooled_fwd[h])[mask]) * 100 for h in HORIZONS]
        print(f"{labels[b]:<24}" + "".join(f"{c:>+11.3f}%" for c in cells) + f"{int(mask.sum()):>9}")
    print("-" * 86)
    for h in HORIZONS:
        fr = np.concatenate(pooled_fwd[h]); m = ~np.isnan(fr)
        corr = np.corrcoef(ratio[m], fr[m])[0, 1]
        print(f"  corr(taker-buy ratio, fwd_{h}h) = {corr:+.4f}   "
              f"(|corr|>~0.05 mới đáng chú ý)")
    print("=" * 86)


if __name__ == "__main__":
    main()
