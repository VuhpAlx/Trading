# funding_research.py — Proof-of-concept: FUNDING RATE có sức dự đoán không?
# =====================================================================
# Giả thuyết (đã có tài liệu): funding rate = phí mà long trả short (perp).
# Funding DƯƠNG cao → long đông/đòn bẩy quá mức → dễ ĐẢO XUỐNG (contrarian short).
# Funding ÂM sâu → short đông → dễ BẬT LÊN (contrarian long).
#
# Bước 1 (rẻ nhất, quyết định nhất): KIỂM SỨC DỰ ĐOÁN — chia funding thành
#   ngũ phân vị, đo lợi suất TƯƠNG LAI trung bình mỗi nhóm. Nếu nhóm funding
#   cao có lợi suất tương lai ÂM (và ngược lại) → CÓ edge → đáng xây tiếp.
# Bước 2: backtest chiến lược contrarian đơn giản (có phí) so với Buy&Hold.
#
# KHÔNG lookahead: tại thời điểm funding t, chỉ dùng giá tại/ trước t để vào,
# đo return về phía trước.
#
# Dùng:  python backtest/funding_research.py
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

import backtest as bt
import fetch_data

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger("funding")

FUNDING_URL = "https://fapi.binance.com/fapi/v1/fundingRate"
SYMBOLS = ["BTCUSDT", "ETHUSDT", "BNBUSDT"]   # PAXG không có perp funding
HORIZONS_H = [8, 24, 72]    # đo lợi suất sau 8h / 24h / 72h
COST = 0.0006               # phí khứ hồi (Exness crypto ước lượng)


def fetch_funding(symbol: str, days: int = 760) -> pd.DataFrame:
    end = int(time.time() * 1000)
    start = end - days * 86_400_000
    rows = []
    cur = start
    with httpx.Client(timeout=30.0) as c:
        while cur < end:
            r = c.get(FUNDING_URL, params={"symbol": symbol, "startTime": cur,
                                           "endTime": end, "limit": 1000})
            r.raise_for_status()
            batch = r.json()
            if not batch:
                break
            rows.extend(batch)
            last = batch[-1]["fundingTime"]
            if last <= cur:
                break
            cur = last + 1
            if len(batch) < 1000:
                break
            time.sleep(0.1)
    df = pd.DataFrame(rows)
    df["fundingTime"] = pd.to_datetime(df["fundingTime"], unit="ms", utc=True)
    df["fundingRate"] = df["fundingRate"].astype(float)
    return df.drop_duplicates("fundingTime").sort_values("fundingTime").reset_index(drop=True)


def analyze(symbol, price_df):
    fund = fetch_funding(symbol)
    logger.info(f"{symbol}: {len(fund)} bản ghi funding "
                f"({fund['fundingTime'].min().date()} → {fund['fundingTime'].max().date()})")

    ts = pd.DatetimeIndex(price_df["timestamp"]).asi8
    closes = price_df["close"].to_numpy()
    ft = pd.DatetimeIndex(fund["fundingTime"]).asi8

    # index giá tại/ngay trước mỗi funding event (không lookahead)
    idx = np.searchsorted(ts, ft, side="right") - 1
    valid = idx >= 0
    fund = fund[valid].reset_index(drop=True)
    idx = idx[valid]

    rates = fund["fundingRate"].to_numpy()
    out = {"rates": rates}
    for h in HORIZONS_H:
        fwd_idx = idx + h
        ok = fwd_idx < len(closes)
        fr = np.full(len(idx), np.nan)
        fr[ok] = closes[fwd_idx[ok]] / closes[idx[ok]] - 1.0
        out[f"fwd_{h}h"] = fr
    return fund, idx, closes, out


def main():
    logger.info("Đọc giá 1h từ cache...")
    data = fetch_data.fetch_all(False)

    # ===== BƯỚC 1: SỨC DỰ ĐOÁN (gộp 3 coin) =====
    print("\n" + "=" * 86)
    print("BƯỚC 1 — SỨC DỰ ĐOÁN CỦA FUNDING (gộp BTC/ETH/BNB, ~2 năm)")
    print("Chia funding thành 5 nhóm (Q1=âm nhất → Q5=dương nhất).")
    print("Nếu CÓ edge contrarian: Q5 (long đông) → fwd return ÂM; Q1 → fwd DƯƠNG.\n")

    pooled = {f"fwd_{h}h": [] for h in HORIZONS_H}
    pooled_rates = []
    per_symbol = {}
    for s in SYMBOLS:
        fund, idx, closes, out = analyze(s, bt.compute_indicators_full(data[s]["1h"]))
        per_symbol[s] = (fund, idx, closes, out)
        pooled_rates.append(out["rates"])
        for h in HORIZONS_H:
            pooled[f"fwd_{h}h"].append(out[f"fwd_{h}h"])

    rates = np.concatenate(pooled_rates)
    # ngũ phân vị theo funding
    qs = np.nanpercentile(rates, [20, 40, 60, 80])
    bucket = np.digitize(rates, qs)   # 0..4

    print(f"{'Nhóm funding':<26}{'fwd 8h':>12}{'fwd 24h':>12}{'fwd 72h':>12}{'N':>9}")
    print("-" * 86)
    labels = ["Q1 (âm nhất)", "Q2", "Q3 (≈0)", "Q4", "Q5 (dương nhất)"]
    for b in range(5):
        mask = bucket == b
        cells = []
        for h in HORIZONS_H:
            fr = np.concatenate(pooled[f"fwd_{h}h"])[mask]
            cells.append(np.nanmean(fr) * 100)
        n = int(mask.sum())
        print(f"{labels[b]:<26}" + "".join(f"{c:>+11.3f}%" for c in cells) + f"{n:>9}")
    print("-" * 86)
    # tương quan funding vs fwd return
    for h in HORIZONS_H:
        fr = np.concatenate(pooled[f"fwd_{h}h"])
        m = ~np.isnan(fr)
        corr = np.corrcoef(rates[m], fr[m])[0, 1]
        print(f"  corr(funding, fwd_{h}h) = {corr:+.4f}   (âm = có edge contrarian)")
    print("=" * 86)

    # ===== BƯỚC 2: chiến lược contrarian đơn giản (ngũ phân vị cực trị) =====
    # Q5 → SHORT, Q1 → LONG, giữ tới funding kế (8h), full-allocation, có phí.
    print("\n" + "=" * 70)
    print("BƯỚC 2 — Chiến lược contrarian (Q5→SHORT, Q1→LONG, giữ 8h)")
    print(f"{'SYMBOL':<10}{'Trades':>8}{'WR%':>7}{'Ret%':>9}{'B&H%':>9}")
    print("-" * 70)
    for s in SYMBOLS:
        fund, idx, closes, out = per_symbol[s]
        r = out["rates"]
        q = np.nanpercentile(r, [20, 80])
        cap = 100.0; wins = tot = 0
        for k in range(len(r)):
            fi = idx[k]
            if fi + 8 >= len(closes):
                continue
            side = 0
            if r[k] >= q[1]: side = -1     # funding cao → short
            elif r[k] <= q[0]: side = +1   # funding âm → long
            if side == 0:
                continue
            entry = closes[fi]; exitp = closes[fi + 8]
            gross = side * (exitp / entry - 1)
            net = gross - COST
            cap *= (1 + net)
            tot += 1
            if net > 0: wins += 1
        bh = (closes[-1] / closes[idx[0]] - 1) * 100
        print(f"{s:<10}{tot:>8}{(wins/tot*100 if tot else 0):>6.1f}%"
              f"{cap-100:>+8.1f}%{bh:>+8.1f}%")
    print("=" * 70)


if __name__ == "__main__":
    main()
