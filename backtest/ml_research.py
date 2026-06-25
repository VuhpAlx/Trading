# ml_research.py — ML đa đặc trưng + WALK-FORWARD (phép thử edge cuối cùng)
# =====================================================================
# Ghép TA + order-flow + funding thành 1 mô hình phi tuyến (HistGradientBoosting),
# kiểm bằng WALK-FORWARD nghiêm ngặt (train quá khứ → test tương lai, có PURGE
# để chống rò rỉ nhãn) trên gộp BTC/ETH/BNB ~2 năm, khung 1h.
#
# Mục tiêu: out-of-sample directional accuracy có > 50% ổn định không? Nếu ~50%
# → xác nhận KHÔNG edge. Nếu >~53% bền + chiến lược dương sau phí → có gì đó.
#
# CHỐNG LOOKAHEAD:
#  • Feature tại bar t chỉ dùng dữ liệu ≤ t.
#  • Nhãn = dấu lợi suất t→t+H. Khi train, BỎ (purge) H bar cuối của cửa sổ train
#    (nhãn chưa hiện thực hoá / trùng test).
#  • Chuẩn hoá feature bằng thống kê TRAIN.
#
# Dùng:  python backtest/ml_research.py
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
logger = logging.getLogger("ml")

SYMBOLS = ["BTCUSDT", "ETHUSDT", "BNBUSDT"]
H = 24            # chân trời dự đoán (giờ)
COST = 0.0006     # phí khứ hồi mỗi lần đổi vị thế (Exness crypto)
FUNDING_URL = "https://fapi.binance.com/fapi/v1/fundingRate"
KLINES = "https://api.binance.com/api/v3/klines"


def _fetch_taker(symbol, days=760):
    end = int(time.time() * 1000); start = end - days * 86_400_000
    rows = []; cur = start
    with httpx.Client(timeout=30.0) as c:
        while cur < end:
            r = c.get(KLINES, params={"symbol": symbol, "interval": "1h",
                                      "startTime": cur, "endTime": end, "limit": 1000})
            r.raise_for_status(); b = r.json()
            if not b: break
            rows.extend(b); last = b[-1][0]
            if last + 3_600_000 <= cur: break
            cur = last + 3_600_000
            if len(b) < 1000: break
            time.sleep(0.1)
    df = pd.DataFrame(rows, columns=["t","o","h","l","c","v","ct","qav","n","tbb","tbq","ig"])
    df["timestamp"] = pd.to_datetime(df["t"], unit="ms", utc=True)
    df["taker_ratio"] = df["tbb"].astype(float) / (df["v"].astype(float) + 1e-9)
    return df[["timestamp", "taker_ratio"]]


def _fetch_funding(symbol, days=760):
    end = int(time.time() * 1000); start = end - days * 86_400_000
    rows = []; cur = start
    with httpx.Client(timeout=30.0) as c:
        while cur < end:
            r = c.get(FUNDING_URL, params={"symbol": symbol, "startTime": cur,
                                           "endTime": end, "limit": 1000})
            r.raise_for_status(); b = r.json()
            if not b: break
            rows.extend(b); last = b[-1]["fundingTime"]
            if last <= cur: break
            cur = last + 1
            if len(b) < 1000: break
            time.sleep(0.1)
    df = pd.DataFrame(rows)
    df["timestamp"] = pd.to_datetime(df["fundingTime"], unit="ms", utc=True)
    df["funding"] = df["fundingRate"].astype(float)
    return df[["timestamp", "funding"]]


def build_features(symbol, data):
    ind = bt.compute_indicators_full(data[symbol]["1h"]).copy()
    c = ind["close"]
    atr = ind["ATR_14"].replace(0, np.nan)

    feat = pd.DataFrame({"timestamp": ind["timestamp"]})
    feat["ret1"]  = c.pct_change(1)
    feat["ret4"]  = c.pct_change(4)
    feat["ret24"] = c.pct_change(24)
    feat["atr_pct"] = atr / c
    feat["rsi"] = ind["RSI_14"]
    feat["macd_n"] = ind["MACD_H"] / (atr + 1e-9)
    feat["adx"] = ind["ADX"]
    feat["di_diff"] = ind["DI_PLUS"] - ind["DI_MINUS"]
    feat["ema9_d"]  = (c - ind["EMA_9"]) / c
    feat["ema50_d"] = (c - ind["EMA_50"]) / c
    feat["bb_pos"] = (c - ind["BB_L"]) / (ind["BB_U"] - ind["BB_L"] + 1e-9)
    feat["vol_ratio"] = ind["volume"] / (ind["VOL_MA"] + 1e-9)
    feat["stoch_k"] = ind["STOCH_K"]

    # order-flow
    tk = _fetch_taker(symbol)
    feat = feat.merge(tk, on="timestamp", how="left")
    feat["taker_ratio"] = feat["taker_ratio"].ffill().fillna(0.5)
    feat["taker_ratio_s"] = feat["taker_ratio"].rolling(6, min_periods=1).mean()

    # funding (8h) → ffill về 1h (causal: dùng funding gần nhất ĐÃ công bố)
    fd = _fetch_funding(symbol).sort_values("timestamp")
    feat = pd.merge_asof(feat.sort_values("timestamp"), fd, on="timestamp", direction="backward")
    feat["funding"] = feat["funding"].fillna(0.0)
    feat["funding_s"] = feat["funding"].rolling(3, min_periods=1).mean()

    # nhãn: dấu lợi suất H giờ tới + lợi suất 1 bar tới (cho chiến lược)
    feat["fwd_ret"] = c.shift(-H).values / c.values - 1.0
    feat["target"] = (feat["fwd_ret"] > 0).astype(int)
    feat["next_ret_H"] = feat["fwd_ret"]   # dùng cho strategy (giữ H giờ)
    feat["symbol"] = symbol
    return feat


FEATURES = ["ret1","ret4","ret24","atr_pct","rsi","macd_n","adx","di_diff",
            "ema9_d","ema50_d","bb_pos","vol_ratio","stoch_k",
            "taker_ratio_s","funding","funding_s"]


def main():
    from sklearn.ensemble import HistGradientBoostingClassifier

    logger.info("Đọc giá + tải order-flow/funding, dựng feature...")
    data = fetch_data.fetch_all(False)
    parts = [build_features(s, data) for s in SYMBOLS]
    df = pd.concat(parts, ignore_index=True)
    df = df.dropna(subset=FEATURES + ["target", "fwd_ret"]).reset_index(drop=True)
    df = df.sort_values("timestamp").reset_index(drop=True)
    logger.info(f"Tổng mẫu: {len(df)} (3 coin gộp)")

    tmin, tmax = df["timestamp"].min(), df["timestamp"].max()
    # Walk-forward: train 12 tháng → test 1 tháng → lăn tới
    train_td = pd.Timedelta(days=365)
    test_td = pd.Timedelta(days=30)
    purge_td = pd.Timedelta(hours=H)   # bỏ H giờ cuối train (chống rò rỉ nhãn)

    oos_pred, oos_true, oos_proba, oos_fwd, oos_ts = [], [], [], [], []
    fold = 0
    start = tmin + train_td
    while start + test_td <= tmax:
        tr = df[(df["timestamp"] < start - purge_td)]
        te = df[(df["timestamp"] >= start) & (df["timestamp"] < start + test_td)]
        if len(tr) > 2000 and len(te) > 50:
            mu = tr[FEATURES].mean(); sd = tr[FEATURES].std().replace(0, 1)
            Xtr = ((tr[FEATURES] - mu) / sd).values
            Xte = ((te[FEATURES] - mu) / sd).values
            clf = HistGradientBoostingClassifier(max_iter=200, max_depth=4,
                                                 learning_rate=0.05, l2_regularization=1.0,
                                                 random_state=0)
            clf.fit(Xtr, tr["target"].values)
            p = clf.predict_proba(Xte)[:, 1]
            oos_proba.extend(p); oos_pred.extend((p > 0.5).astype(int))
            oos_true.extend(te["target"].values); oos_fwd.extend(te["fwd_ret"].values)
            oos_ts.extend(te["timestamp"].values)
            fold += 1
        start += test_td

    oos_proba = np.array(oos_proba); oos_pred = np.array(oos_pred)
    oos_true = np.array(oos_true); oos_fwd = np.array(oos_fwd)
    acc = (oos_pred == oos_true).mean() * 100
    base = max(oos_true.mean(), 1 - oos_true.mean()) * 100   # baseline đoán lớp đa số

    print("\n" + "=" * 78)
    print(f"ML WALK-FORWARD ({fold} folds, train 12th→test 1th, H={H}h)")
    print(f"  Mẫu OOS: {len(oos_true)}")
    print(f"  Directional accuracy OOS : {acc:.2f}%   (baseline đoán lớp đa số: {base:.2f}%)")
    try:
        from sklearn.metrics import roc_auc_score
        print(f"  AUC OOS                  : {roc_auc_score(oos_true, oos_proba):.4f}  (0.5 = vô dụng)")
    except Exception:
        pass

    # Chiến lược: mỗi 24h vào theo view, giữ H giờ, có phí. Lấy mẫu không chồng lấn.
    print("\n  Chiến lược (ngưỡng p): long nếu p>thr, short nếu p<1-thr, giữ 24h, phí 0.06%")
    print(f"  {'thr':>6}{'#lệnh':>8}{'WR%':>7}{'Ret TB/lệnh':>13}{'Tổng ret(nhân)':>16}")
    order = np.argsort(oos_ts)
    p_o = oos_proba[order]; f_o = oos_fwd[order]
    for thr in [0.52, 0.55, 0.58]:
        # lấy mẫu mỗi 24 phần tử để giảm chồng lấn (xấp xỉ không trùng kỳ giữ)
        eq = 1.0; n = 0; w = 0
        for k in range(0, len(p_o), H):
            p = p_o[k]; fr = f_o[k]
            side = 1 if p > thr else (-1 if p < 1 - thr else 0)
            if side == 0 or np.isnan(fr):
                continue
            net = side * fr - COST
            eq *= (1 + net); n += 1; w += 1 if net > 0 else 0
        wr = w / n * 100 if n else 0
        avg = (eq ** (1/n) - 1) * 100 if n else 0
        print(f"  {thr:>6}{n:>8}{wr:>6.1f}%{avg:>+12.3f}%{(eq-1)*100:>+15.1f}%")
    print("=" * 78)
    print("Đọc: accuracy ≈ baseline & AUC ≈ 0.5 → KHÔNG edge. Tổng ret dương bền qua nhiều thr → có thể có.")


if __name__ == "__main__":
    main()
