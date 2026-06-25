# backtest.py — Replay nến lịch sử qua CHÍNH engine live (đo đúng logic hiện tại)
# =====================================================================
# Triết lý: KHÔNG viết lại logic. Tải data 3 tháng, đưa từng nến đóng qua
# AdvancedSignalEngine.generate_signal + TradeSimulator.process_tick theo
# ĐÚNG thứ tự main.on_market_tick → đo được win-rate & đầu ra thực của logic.
#
# Cô lập: chạy trong thư mục tạm, mọi *.jsonl (weights/trade) ghi ở đó, KHÔNG
# đụng file live. Weights khởi tạo MẶC ĐỊNH (reproducible, đo bản thân logic).
#
# Chống lookahead: bias khung lớn chỉ dùng nến HTF ĐÃ ĐÓNG tại thời điểm
# nến giao dịch đóng (live thấy nến HTF đang hình thành — backtest không được).
#
# Tốc độ: indicator tính VECTORIZED 1 lần/khung rồi slice (thay vì gọi
# pandas-ta mỗi nến). VWAP anchored theo ngày — lệch nhẹ so với live (chỉ
# ảnh hưởng vwap_score hiển thị/learning, KHÔNG ảnh hưởng quyết định vào lệnh).
#
# Dùng:
#   python backtest/backtest.py --tfs 5m,15m,30m,1h
#   python backtest/backtest.py --tfs 1m            (nặng — chạy nền)
#   python backtest/backtest.py --symbols BTCUSDT,ETHUSDT --tfs 1h
# =====================================================================
import os
import sys
import json
import time
import shutil
import argparse
import logging

import numpy as np
import pandas as pd
import pandas_ta as ta

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

from config import SYMBOLS, TIMEFRAMES, HTF_MAP  # noqa: E402
import fetch_data  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("backtest")
logging.getLogger("SignalEngine").setLevel(logging.WARNING)  # bớt ồn

# Thư mục cô lập cho JSONL + kết quả (scratchpad)
RUN_DIR = os.environ.get(
    "BACKTEST_RUN_DIR",
    r"C:\Users\vuhp\AppData\Local\Temp\claude\D--Tools-Trading2"
    r"\5717359c-bea7-4510-8dfd-de4a640339ec\scratchpad\bt_run",
)
# Báo cáo kết quả lưu vào repo (deliverable)
RESULTS_DIR = os.path.join(PROJECT_ROOT, "backtest", "results")

WARMUP_BARS = 300        # số nến đầu để indicator/structure ổn định (không tính metric)
WINDOW = 320             # cửa sổ slice đưa vào engine (≥ STRUCTURE_LOOKBACK 200 + buffer)
EVAL_DAYS = 90           # cửa sổ đánh giá ~3 tháng

_TF_MS = {
    "1m": 60_000, "5m": 300_000, "15m": 900_000, "30m": 1_800_000,
    "1h": 3_600_000, "4h": 14_400_000, "1d": 86_400_000,
}


# ---------------------------------------------------------------------
# Indicator: replicate indicator_layer.apply_indicators NHƯNG trên TOÀN BỘ
# series (bỏ tail(260)) để precompute 1 lần. Tên cột giữ y hệt engine cần.
# ---------------------------------------------------------------------
def compute_indicators_full(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy().reset_index(drop=True)
    for col in ["open", "high", "low", "close", "volume"]:
        df[col] = df[col].astype(float)

    df["EMA_9"]  = ta.ema(df["close"], length=9)
    df["EMA_21"] = ta.ema(df["close"], length=21)
    df["EMA_50"] = ta.ema(df["close"], length=50)
    df["SMA_200"] = ta.sma(df["close"], length=200)

    macd = ta.macd(df["close"])
    if macd is not None and not macd.empty:
        df["MACD"]   = macd.iloc[:, 0]
        df["MACD_H"] = macd.iloc[:, 1]
        df["MACD_S"] = macd.iloc[:, 2]

    df["RSI"]    = ta.rsi(df["close"], length=14)
    df["RSI_14"] = df["RSI"]
    stoch = ta.stoch(df["high"], df["low"], df["close"])
    if stoch is not None and not stoch.empty:
        df["STOCH_K"] = stoch.iloc[:, 0]
        df["STOCH_D"] = stoch.iloc[:, 1]

    bb = ta.bbands(df["close"], length=20, std=2)
    if bb is not None and not bb.empty:
        df["BB_L"] = bb.iloc[:, 0]
        df["BB_M"] = bb.iloc[:, 1]
        df["BB_U"] = bb.iloc[:, 2]
    df["ATR"]    = ta.atr(df["high"], df["low"], df["close"], length=14)
    df["ATR_14"] = df["ATR"]

    adx_res = ta.adx(df["high"], df["low"], df["close"], length=14)
    if adx_res is not None and not adx_res.empty:
        df["ADX"]      = adx_res.iloc[:, 0]
        df["DI_PLUS"]  = adx_res.iloc[:, 1]
        df["DI_MINUS"] = adx_res.iloc[:, 2]

    df["VOL_MA"] = ta.sma(df["volume"], length=20)
    df["OBV"]    = ta.obv(df["close"], df["volume"])
    try:
        df2 = df.set_index("timestamp")
        vwap = ta.vwap(df2["high"], df2["low"], df2["close"], df2["volume"])
        if vwap is not None:
            df["VWAP"] = vwap.values
    except Exception:
        df["VWAP"] = df["close"]

    num_cols = df.select_dtypes(include="number").columns
    df[num_cols] = df[num_cols].bfill().ffill().fillna(0.0)
    return df


def _ms(ts_series) -> np.ndarray:
    """Series timestamp (datetime64 UTC) → epoch MILLISECONDS int64.

    Lưu ý: pandas 3.x giữ resolution 'ms' (datetime64[ms,UTC]) nên .asi8 đã là
    ms; resolution 'ns' thì .asi8 là ns. as_unit('ms') ép về ms cho mọi version
    → KHÔNG được chia thêm 1e6 (bug cũ làm sai đơn vị, MTF rỗng → bias NEUTRAL).
    """
    return pd.DatetimeIndex(ts_series).as_unit("ms").asi8


# ---------------------------------------------------------------------
# Chạy backtest cho 1 (symbol, tf)
# ---------------------------------------------------------------------
def backtest_one(symbol: str, tf: str, data: dict, EngineCls) -> dict:
    df_raw = data[symbol][tf]
    if df_raw is None or len(df_raw) < WARMUP_BARS + 50:
        logger.warning(f"{symbol}/{tf}: thiếu data ({0 if df_raw is None else len(df_raw)} nến) — bỏ qua")
        return {"symbol": symbol, "timeframe": tf, "error": "insufficient_data"}

    ind = compute_indicators_full(df_raw)
    ind_ms = _ms(ind["timestamp"])

    # Precompute indicators + close-time cho các khung HTF (chống lookahead)
    htf_tfs = HTF_MAP.get(tf, [])
    htf_ind = {}
    htf_close_ms = {}
    for h in htf_tfs:
        hdf = data[symbol].get(h)
        if hdf is None or hdf.empty:
            continue
        hind = compute_indicators_full(hdf)
        htf_ind[h] = hind
        htf_close_ms[h] = _ms(hind["timestamp"]) + _TF_MS[h]  # thời điểm ĐÓNG nến HTF

    tf_dur = _TF_MS[tf]
    close_ms = ind_ms + tf_dur  # thời điểm đóng của từng nến giao dịch

    # Engine live, capital 100, weights mặc định (file jsonl cô lập rỗng)
    engine = EngineCls(symbol, tf)
    engine.set_capital(100.0)

    n = len(ind)
    eval_start_idx = max(WARMUP_BARS, 0)
    actions = {"BUY": 0, "SELL": 0, "HOLD": 0}
    errors = 0
    first_eval_ts = None
    last_eval_ts = None

    for i in range(eval_start_idx, n):
        c_close = float(ind.iloc[i]["close"])
        c_high  = float(ind.iloc[i]["high"])
        c_low   = float(ind.iloc[i]["low"])

        # --- (1) TP/SL check như main.on_market_tick (process_tick TRƯỚC) ---
        # Gọi 2 lần: lần 1 có thể "tiêu" cờ just_opened của lệnh mở ở nến trước,
        # lần 2 mới thực sự kiểm tra high/low của nến hiện tại (khớp hành vi live
        # nơi just_opened bị tiêu ngay tick kế trong cùng nến).
        for _ in range(2):
            if engine.trade_sim.state == "NONE":
                break
            closed = engine.trade_sim.process_tick(c_close, c_high, c_low)
            if closed:
                engine.scorer.learn_from_real_trade(
                    closed["snapshot"], closed["direction"], closed["result"]
                )
                break

        # --- (2) slice cửa sổ + build mtf_context (chỉ nến HTF đã đóng) ---
        lo = max(0, i - WINDOW + 1)
        df_win = ind.iloc[lo:i + 1].reset_index(drop=True)

        mtf = {}
        for h in htf_tfs:
            if h not in htf_ind:
                continue
            j = int(np.searchsorted(htf_close_ms[h], close_ms[i], side="right")) - 1
            if j < 1:
                continue
            hlo = max(0, j - WINDOW + 1)
            mtf[h] = htf_ind[h].iloc[hlo:j + 1].reset_index(drop=True)

        # --- (3) chạy logic ---
        try:
            res = engine.generate_signal(df_win, mtf)
            act = res.get("action", "HOLD")
            actions[act] = actions.get(act, 0) + 1
        except Exception as e:
            errors += 1
            if errors <= 3:
                logger.warning(f"{symbol}/{tf} @i={i}: lỗi generate_signal: {e}")

        ts = ind.iloc[i]["timestamp"]
        if first_eval_ts is None:
            first_eval_ts = ts
        last_eval_ts = ts

    # --- Tổng hợp metric từ file JSONL cô lập ---
    metrics = _collect_metrics(symbol, tf)
    metrics.update({
        "symbol": symbol, "timeframe": tf,
        "candles_total": int(n),
        "candles_evaluated": int(n - eval_start_idx),
        "eval_from": str(first_eval_ts), "eval_to": str(last_eval_ts),
        "actions": actions,
        "final_capital": round(float(engine.trade_sim.capital), 2),
        "return_pct": round((float(engine.trade_sim.capital) - 100.0), 2),
        "errors": errors,
    })
    return metrics


def _read_jsonl(path: str) -> list:
    if not os.path.exists(path):
        return []
    out = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    return out


def _collect_metrics(symbol: str, tf: str) -> dict:
    # File nằm trong RUN_DIR (cwd đã chdir tới đây)
    preds = _read_jsonl(f"ml_training_data_{symbol}_{tf}.jsonl")
    trades = _read_jsonl(f"trade_history_{symbol}_{tf}.jsonl")

    # Forward-test (dự đoán hướng)
    p_win = sum(1 for p in preds if p.get("result") == "WIN")
    p_loss = sum(1 for p in preds if p.get("result") == "LOSS")
    p_flat = sum(1 for p in preds if p.get("result") == "FLAT")
    p_decided = p_win + p_loss
    pred_winrate = round(p_win / p_decided * 100, 1) if p_decided else 0.0

    # Trade (auto bot, lot-based)
    t_win = sum(1 for t in trades if t.get("result") == "WIN")
    t_loss = sum(1 for t in trades if t.get("result") == "LOSS")
    t_liq = sum(1 for t in trades if t.get("result") == "LIQUIDATED")
    t_decided = t_win + t_loss
    trade_winrate = round(t_win / t_decided * 100, 1) if t_decided else 0.0
    pnl_sum = round(sum(float(t.get("profit_usd", 0.0)) for t in trades), 2)

    # Max drawdown trên đường vốn
    caps = [float(t.get("capital_after", 100.0)) for t in trades]
    max_dd = 0.0
    if caps:
        peak = caps[0]
        for c in caps:
            peak = max(peak, c)
            dd = (peak - c) / peak * 100 if peak > 0 else 0.0
            max_dd = max(max_dd, dd)

    return {
        "pred_total": len(preds), "pred_win": p_win, "pred_loss": p_loss,
        "pred_flat": p_flat, "pred_winrate": pred_winrate,
        "trades_total": len(trades), "trade_win": t_win, "trade_loss": t_loss,
        "trade_liquidated": t_liq, "trade_winrate": trade_winrate,
        "pnl_usd_sum": pnl_sum, "max_drawdown_pct": round(max_dd, 2),
    }


def _print_summary(results: list):
    print("\n" + "=" * 110)
    print(f"{'SYMBOL/TF':<14}{'Pred WR':>9}{'(W/L/F)':>14}{'Trade WR':>10}"
          f"{'(W/L/Liq)':>13}{'Trades':>8}{'Final$':>10}{'MaxDD%':>9}{'BUY/SELL/HOLD':>18}")
    print("-" * 110)
    for r in results:
        name = r["symbol"] + "/" + r["timeframe"]
        if r.get("error"):
            print(f"{name:<14}  {r['error']}")
            continue
        a = r["actions"]
        plf = f"{r['pred_win']}/{r['pred_loss']}/{r['pred_flat']}"
        wll = f"{r['trade_win']}/{r['trade_loss']}/{r['trade_liquidated']}"
        bsh = f"{a['BUY']}/{a['SELL']}/{a['HOLD']}"
        print(f"{name:<14}"
              f"{r['pred_winrate']:>7.1f}% "
              f"{plf:>13}"
              f"{r['trade_winrate']:>9.1f}%"
              f"{wll:>13}"
              f"{r['trades_total']:>8}"
              f"{r['final_capital']:>10.2f}"
              f"{r['max_drawdown_pct']:>9.2f}"
              f"{bsh:>18}")
    print("=" * 110)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbols", default=",".join(SYMBOLS))
    ap.add_argument("--tfs", default="5m,15m,30m,1h",
                    help="khung giao dịch cần backtest, vd '5m,15m,30m,1h' hoặc '1m'")
    ap.add_argument("--force-fetch", action="store_true", help="tải lại data dù đã cache")
    ap.add_argument("--tag", default="", help="hậu tố tên file kết quả")
    args = ap.parse_args()

    symbols = [s.strip() for s in args.symbols.split(",") if s.strip()]
    tfs = [t.strip() for t in args.tfs.split(",") if t.strip() in TIMEFRAMES]
    if not tfs:
        logger.error("Không có khung hợp lệ. Chọn trong: " + ",".join(TIMEFRAMES))
        return

    logger.info(f"Tải/đọc data cache... (symbols={symbols}, cần cả HTF context)")
    data = fetch_data.fetch_all(force=args.force_fetch)

    # Cô lập file I/O: dọn RUN_DIR rồi chdir vào đó. Import engine SAU khi chdir
    # để mọi *.jsonl (do __init__ tạo/đọc) nằm trong thư mục cô lập.
    if os.path.exists(RUN_DIR):
        shutil.rmtree(RUN_DIR, ignore_errors=True)
    os.makedirs(RUN_DIR, exist_ok=True)
    os.makedirs(RESULTS_DIR, exist_ok=True)
    os.chdir(RUN_DIR)
    from signal_engine import AdvancedSignalEngine  # noqa: E402

    results = []
    t0 = time.time()
    for sym in symbols:
        for tf in tfs:
            logger.info(f"▶ Backtest {sym}/{tf} ...")
            ts0 = time.time()
            r = backtest_one(sym, tf, data, AdvancedSignalEngine)
            r["seconds"] = round(time.time() - ts0, 1)
            results.append(r)
            logger.info(f"  ✓ {sym}/{tf} xong trong {r['seconds']}s "
                        f"(pred WR {r.get('pred_winrate','?')}%, "
                        f"trades {r.get('trades_total','?')}, "
                        f"final ${r.get('final_capital','?')})")

    _print_summary(results)

    tag = ("_" + args.tag) if args.tag else ("_" + "-".join(tfs))
    out_path = os.path.join(RESULTS_DIR, f"backtest{tag}.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump({"results": results, "elapsed_sec": round(time.time() - t0, 1)},
                  f, ensure_ascii=False, indent=2)
    logger.info(f"Kết quả lưu: {out_path}  (tổng {round(time.time()-t0,1)}s)")


if __name__ == "__main__":
    main()
