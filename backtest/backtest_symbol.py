# backtest_symbol.py — MỘT trader / MỘT đồng tiền (unified multi-timeframe)
# =====================================================================
# Khác biệt cốt lõi với backtest.py (per-TF):
#   • Mỗi SYMBOL chỉ chạy 1 engine duy nhất ở khung TRIGGER (mặc định 15m).
#   • Tất cả khung phục vụ 1 quyết định: bias = gộp 1h/4h/1d (top-down),
#     setup/structure + confluence = khung trigger. 1 vị thế / 1 coin.
#   • Khung nhỏ (1m/5m) KHÔNG tự trade riêng — chỉ để xem chart/bối cảnh.
#
# Tái dùng NGUYÊN generate_signal (đo đúng logic). Chỉ:
#   1) nối mtf_context = các khung bias (override HTF_MAP cho khung trigger),
#   2) nới cổng vào lệnh (MIN_CONFLUENCE / MIN_RR_AFTER_FEES) — monkeypatch
#      CÔ LẬP trong process backtest, KHÔNG đụng config/live.
#
# Dùng:
#   python backtest/backtest_symbol.py                       # 15m, conf2, rr1.5
#   python backtest/backtest_symbol.py --trigger 5m --bias 30m,1h,4h --min-conf 2 --min-rr 2.0
# =====================================================================
import os
import sys
import json
import time
import shutil
import argparse
import logging

import numpy as np

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import backtest as bt          # tái dùng compute_indicators_full, _ms, _collect_metrics, ...
import fetch_data              # noqa: E402
from config import SYMBOLS     # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("SignalEngine").setLevel(logging.WARNING)
logger = logging.getLogger("backtest_symbol")

RUN_DIR = bt.RUN_DIR + "_sym"
RESULTS_DIR = bt.RESULTS_DIR


def backtest_symbol(symbol, trigger_tf, bias_tfs, data, EngineCls) -> dict:
    df_raw = data[symbol][trigger_tf]
    if df_raw is None or len(df_raw) < bt.WARMUP_BARS + 50:
        return {"symbol": symbol, "timeframe": trigger_tf, "error": "insufficient_data"}

    ind = bt.compute_indicators_full(df_raw)
    ind_ms = bt._ms(ind["timestamp"])
    close_ms = ind_ms + bt._TF_MS[trigger_tf]

    # Precompute indicator + close-time cho các khung BIAS (chống lookahead)
    htf_ind, htf_close = {}, {}
    for h in bias_tfs:
        hdf = data[symbol].get(h)
        if hdf is None or hdf.empty:
            continue
        hi = bt.compute_indicators_full(hdf)
        htf_ind[h] = hi
        htf_close[h] = bt._ms(hi["timestamp"]) + bt._TF_MS[h]

    engine = EngineCls(symbol, trigger_tf)   # 1 engine duy nhất / coin
    engine.set_capital(100.0)

    n = len(ind)
    actions = {"BUY": 0, "SELL": 0, "HOLD": 0}
    errors = 0
    first_ts = last_ts = None

    for i in range(bt.WARMUP_BARS, n):
        c_close = float(ind.iloc[i]["close"])
        c_high = float(ind.iloc[i]["high"])
        c_low = float(ind.iloc[i]["low"])

        for _ in range(2):
            if engine.trade_sim.state == "NONE":
                break
            closed = engine.trade_sim.process_tick(c_close, c_high, c_low)
            if closed:
                engine.scorer.learn_from_real_trade(
                    closed["snapshot"], closed["direction"], closed["result"])
                break

        lo = max(0, i - bt.WINDOW + 1)
        df_win = ind.iloc[lo:i + 1].reset_index(drop=True)

        mtf = {}
        for h in bias_tfs:
            if h not in htf_ind:
                continue
            j = int(np.searchsorted(htf_close[h], close_ms[i], side="right")) - 1
            if j < 1:
                continue
            hlo = max(0, j - bt.WINDOW + 1)
            mtf[h] = htf_ind[h].iloc[hlo:j + 1].reset_index(drop=True)

        try:
            res = engine.generate_signal(df_win, mtf)
            actions[res.get("action", "HOLD")] = actions.get(res.get("action", "HOLD"), 0) + 1
        except Exception as e:
            errors += 1
            if errors <= 3:
                logger.warning(f"{symbol} @i={i}: {e}")

        ts = ind.iloc[i]["timestamp"]
        if first_ts is None:
            first_ts = ts
        last_ts = ts

    m = bt._collect_metrics(symbol, trigger_tf)
    m.update({
        "symbol": symbol, "trigger_tf": trigger_tf, "bias_tfs": bias_tfs,
        "candles_evaluated": int(n - bt.WARMUP_BARS),
        "eval_from": str(first_ts), "eval_to": str(last_ts),
        "actions": actions,
        "final_capital": round(float(engine.trade_sim.capital), 2),
        "return_pct": round(float(engine.trade_sim.capital) - 100.0, 2),
        "errors": errors,
    })
    return m


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbols", default=",".join(SYMBOLS))
    ap.add_argument("--trigger", default="15m")
    ap.add_argument("--bias", default="1h,4h,1d")
    ap.add_argument("--min-conf", type=int, default=2)
    ap.add_argument("--min-rr", type=float, default=1.5)
    ap.add_argument("--tag", default="")
    args = ap.parse_args()

    symbols = [s.strip() for s in args.symbols.split(",") if s.strip()]
    bias_tfs = [t.strip() for t in args.bias.split(",") if t.strip()]
    trigger = args.trigger

    logger.info("Tải/đọc data cache...")
    data = fetch_data.fetch_all(force=False)

    if os.path.exists(RUN_DIR):
        shutil.rmtree(RUN_DIR, ignore_errors=True)
    os.makedirs(RUN_DIR, exist_ok=True)
    os.makedirs(RESULTS_DIR, exist_ok=True)
    os.chdir(RUN_DIR)

    # --- Monkeypatch CÔ LẬP (chỉ trong process này) ---
    import signal_engine
    signal_engine.MIN_CONFLUENCE = args.min_conf
    signal_engine.MIN_RR_AFTER_FEES = args.min_rr
    signal_engine.HTF_MAP[trigger] = bias_tfs   # bias cho khung trigger = bias_tfs
    from signal_engine import AdvancedSignalEngine

    logger.info(f"Cấu hình: trigger={trigger}  bias={bias_tfs}  "
                f"MIN_CONFLUENCE={args.min_conf}  MIN_RR={args.min_rr}")

    results = []
    t0 = time.time()
    for sym in symbols:
        logger.info(f"▶ {sym} (trader đơn, trigger {trigger}) ...")
        ts0 = time.time()
        r = backtest_symbol(sym, trigger, bias_tfs, data, AdvancedSignalEngine)
        r["seconds"] = round(time.time() - ts0, 1)
        results.append(r)
        logger.info(f"  ✓ {sym}: trades {r.get('trades_total','?')}  "
                    f"WR {r.get('trade_winrate','?')}%  final ${r.get('final_capital','?')}  "
                    f"({r['seconds']}s)")

    # Summary
    print("\n" + "=" * 100)
    print(f"UNIFIED PER-SYMBOL  | trigger={trigger} bias={bias_tfs} "
          f"conf>={args.min_conf} rr>={args.min_rr}")
    print(f"{'SYMBOL':<11}{'Trades':>8}{'TradeWR':>9}{'W/L/Liq':>12}"
          f"{'Final$':>10}{'Ret%':>8}{'MaxDD%':>8}{'BUY/SELL/HOLD':>18}{'PredWR':>8}")
    print("-" * 100)
    for r in results:
        if r.get("error"):
            print(f"{r['symbol']:<11}  {r['error']}")
            continue
        a = r["actions"]
        wll = f"{r['trade_win']}/{r['trade_loss']}/{r['trade_liquidated']}"
        bsh = f"{a['BUY']}/{a['SELL']}/{a['HOLD']}"
        print(f"{r['symbol']:<11}{r['trades_total']:>8}{r['trade_winrate']:>8.1f}%"
              f"{wll:>12}{r['final_capital']:>10.2f}{r['return_pct']:>8.2f}"
              f"{r['max_drawdown_pct']:>8.2f}{bsh:>18}{r['pred_winrate']:>7.1f}%")
    print("=" * 100)

    tag = ("_" + args.tag) if args.tag else f"_{trigger}_conf{args.min_conf}_rr{args.min_rr}"
    out = os.path.join(RESULTS_DIR, f"backtest_symbol{tag}.json")
    with open(out, "w", encoding="utf-8") as f:
        json.dump({"config": {"trigger": trigger, "bias": bias_tfs,
                              "min_conf": args.min_conf, "min_rr": args.min_rr},
                  "results": results, "elapsed_sec": round(time.time() - t0, 1)},
                 f, ensure_ascii=False, indent=2)
    logger.info(f"Kết quả: {out}  ({round(time.time()-t0,1)}s)")


if __name__ == "__main__":
    main()
