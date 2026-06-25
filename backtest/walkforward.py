# walkforward.py — Kiểm định ĐỘ BỀN của chiến lược swing (chống overfit)
# =====================================================================
# (A) ĐỘ NHẠY THEO PHÍ: chạy lại ở nhiều mức phí Exness (vàng→crypto→standard)
#     để biết kết quả phụ thuộc phí thế nào (user chưa chắc phí thật).
# (B) TÍNH NHẤT QUÁN QUA THỜI GIAN: chia ~2 năm thành 6 cửa sổ, xem chiến lược
#     dương ĐỀU hay chỉ ăn may 1 giai đoạn (out-of-sample qua từng đoạn).
#
# Tái dùng NGUYÊN run_strategy + decide_entry của strategy_lab (không lặp logic).
#
# Dùng:  python backtest/walkforward.py
# =====================================================================
import os
import sys
import json
import shutil
import logging

import pandas as pd

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import backtest as bt
import fetch_data
import lot_sizing
import strategy_lab as lab
from config import SYMBOLS

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("SignalEngine").setLevel(logging.WARNING)
logger = logging.getLogger("walkforward")

# Cấu hình: swing trên 1h, bias 4h/1d
lab.TRIGGER = "1h"
lab.BIAS_TFS = ["4h", "1d"]
TEST = ["pullback_regime", "pullback_trend", "breakout_3R"]
COSTS = [0.0003, 0.0006, 0.0010]   # vàng/FX · crypto · standard
N_WINDOWS = 6
RISK_PCT = 0.01   # rủi ro/lệnh (khớp lot_sizing.RISK_CAP_PCT bên dưới) — để quy R


def avg_r_multiple(trade_log):
    """Kỳ vọng/lệnh tính theo R: lãi/lỗ mỗi lệnh (% vốn) chia rủi ro/lệnh.
    R>0 nghĩa kỳ vọng dương dù WR thấp (thắng to bù thua nhỏ)."""
    rs, prev_cap = [], 100.0
    for _ts, cap, _r in trade_log:
        trade_ret = (cap / prev_cap - 1.0) if prev_cap > 0 else 0.0
        rs.append(trade_ret / RISK_PCT)
        prev_cap = cap
    return (sum(rs) / len(rs)) if rs else 0.0


def equity_at(trade_log, t):
    """Vốn tại thời điểm t (vốn phẳng giữa các lần đóng lệnh; bắt đầu 100)."""
    eq = 100.0
    for ts, cap, _ in trade_log:
        if pd.Timestamp(ts) <= t:
            eq = cap
        else:
            break
    return eq


def window_metrics(trade_log, ws, we):
    e0 = equity_at(trade_log, ws)
    e1 = equity_at(trade_log, we)
    ret = (e1 / e0 - 1) * 100 if e0 > 0 else 0.0
    win = sum(1 for ts, _, r in trade_log if ws < pd.Timestamp(ts) <= we and r == "WIN")
    tot = sum(1 for ts, _, r in trade_log if ws < pd.Timestamp(ts) <= we and r in ("WIN", "LOSS"))
    return ret, win, tot


def main():
    logger.info("Tải/đọc data...")
    data = fetch_data.fetch_all(False)

    run_dir = bt.RUN_DIR + "_wf"
    if os.path.exists(run_dir):
        shutil.rmtree(run_dir, ignore_errors=True)
    os.makedirs(run_dir, exist_ok=True)
    os.chdir(run_dir)

    # Patch sizing y như strategy_lab (risk-based, lot phân số)
    lot_sizing.MIN_LOT = 1e-9
    lot_sizing.LOT_STEP = 1e-9
    lot_sizing.MAX_LOT = 1e9
    lot_sizing.RISK_CAP_PCT = 0.01
    lot_sizing.LOT_BASE = 1e6
    lot_sizing.LOT_MAX_DYNAMIC = 1e6
    lot_sizing.normalize_lot = lambda lot: max(0.0, float(lot)) if lot else 0.0
    from signal_engine import TradeSimulator

    # Cửa sổ thời gian chung (theo BTC 1h sau warmup)
    ind_btc = bt.compute_indicators_full(data["BTCUSDT"]["1h"]).iloc[bt.WARMUP_BARS:]
    t_start = pd.Timestamp(ind_btc.iloc[0]["timestamp"])
    t_end = pd.Timestamp(ind_btc.iloc[-1]["timestamp"])
    edges = [t_start + (t_end - t_start) * k / N_WINDOWS for k in range(N_WINDOWS + 1)]

    # ===== (A) ĐỘ NHẠY THEO PHÍ =====
    print("\n" + "=" * 78)
    print("(A) ĐỘ NHẠY THEO PHÍ — lãi TB/coin qua ~2 năm (1h)")
    print(f"{'Strategy':<16}{'phí 0.03%':>12}{'phí 0.06%':>12}{'phí 0.10%':>12}")
    print("-" * 78)
    logs_at_006 = {}
    res_at_006 = {}            # name -> {sym: full result dict} ở phí 0.06%
    fee_sensitivity = {}       # name -> {cost: avg_ret}
    for name in TEST:
        cfg = lab.STRATS[name]
        row_out = []
        for cost in COSTS:
            TradeSimulator.TAKER_FEE = cost / 2.0
            TradeSimulator.SLIPPAGE = 0.00005
            rets = []
            logs = {}
            results = {}
            for sym in SYMBOLS:
                r = lab.run_strategy(sym, cfg, data, TradeSimulator)
                rets.append(r["return_pct"])
                logs[sym] = r["trade_log"]
                results[sym] = r
            row_out.append(sum(rets) / len(rets))
            if abs(cost - 0.0006) < 1e-9:
                logs_at_006[name] = logs
                res_at_006[name] = results
        fee_sensitivity[name] = {f"{c*100:.2f}%": round(v, 1) for c, v in zip(COSTS, row_out)}
        print(f"{name:<16}{row_out[0]:>11.1f}%{row_out[1]:>11.1f}%{row_out[2]:>11.1f}%")
    print("=" * 78)

    # ===== KỲ VỌNG/LỆNH (R-multiple) + WR + DD ở phí 0.06% =====
    print("\n" + "=" * 78)
    print("KỲ VỌNG (phí 0.06%) — gộp 4 coin | R-multiple TB = kỳ vọng/lệnh theo R")
    print(f"{'Strategy':<16}{'Trades':>8}{'WR%':>8}{'avg R':>9}{'MaxDD%':>9}{'Ret%TB':>9}")
    print("-" * 78)
    expectancy = {}
    for name in TEST:
        rs = res_at_006[name]
        trades = sum(rs[s]["trades"] for s in SYMBOLS)
        wins = sum(rs[s]["wins"] for s in SYMBOLS)
        losses = sum(rs[s]["losses"] for s in SYMBOLS)
        wr = round(wins / (wins + losses) * 100, 1) if (wins + losses) else 0.0
        all_r = []
        for s in SYMBOLS:
            all_r.append(avg_r_multiple(logs_at_006[name][s]))
        avg_r = round(sum(all_r) / len(all_r), 3)
        maxdd = round(sum(rs[s]["max_dd_pct"] for s in SYMBOLS) / len(SYMBOLS), 1)
        avg_ret = round(sum(rs[s]["return_pct"] for s in SYMBOLS) / len(SYMBOLS), 1)
        expectancy[name] = {"trades": trades, "winrate": wr, "avg_R": avg_r,
                            "avg_maxdd": maxdd, "avg_ret": avg_ret}
        print(f"{name:<16}{trades:>8}{wr:>8}{avg_r:>9.3f}{maxdd:>9.1f}{avg_ret:>9.1f}")
    print("=" * 78)

    # ===== (B) TÍNH NHẤT QUÁN QUA 6 CỬA SỔ (tại phí 0.06%) =====
    print("\n" + "=" * 100)
    print(f"(B) NHẤT QUÁN QUA {N_WINDOWS} CỬA SỔ (phí 0.06%, lãi TB/coin mỗi đoạn, WR gộp)")
    hdr = "".join([f"W{k+1}" .rjust(13) for k in range(N_WINDOWS)])
    print(f"{'Strategy':<16}{hdr}{'  +đoạn':>9}")
    print("  (cửa sổ: " + " | ".join(f"{edges[k].date()}→{edges[k+1].date()}" for k in range(N_WINDOWS)) + ")")
    print("-" * 100)
    consistency = {}
    for name in TEST:
        logs = logs_at_006[name]
        cells = []
        pos_windows = 0
        win_details = []
        for k in range(N_WINDOWS):
            ws, we = edges[k], edges[k + 1]
            srets, wins, tots = [], 0, 0
            for sym in SYMBOLS:
                ret, w, t = window_metrics(logs[sym], ws, we)
                srets.append(ret); wins += w; tots += t
            avg = sum(srets) / len(srets)
            if avg > 0:
                pos_windows += 1
            wr = round(wins / tots * 100) if tots else 0
            cells.append(f"{avg:+.1f}%/{wr}%")
            win_details.append({"avg_ret": round(avg, 1), "wr": wr})
        consistency[name] = {"pos_windows": pos_windows, "n_windows": N_WINDOWS,
                             "windows": win_details}
        print(f"{name:<16}" + "".join(c.rjust(13) for c in cells) + f"{pos_windows}/{N_WINDOWS}".rjust(9))
    print("=" * 100)
    print("Mỗi ô = lãi TB/coin trong đoạn / WR gộp. Cột cuối = số đoạn dương / tổng.")

    # ===== TIÊU CHÍ GO / NO-GO cho 'pullback_regime' =====
    print("\n" + "=" * 78)
    print("GO/NO-GO — 'pullback_regime' (mục tiêu kỳ-vọng-dương để wire live)")
    print("-" * 78)
    decision = {}
    for name in TEST:
        e = expectancy[name]
        c = consistency[name]
        per_sym_006 = res_at_006[name]
        worst_sym_ret = min(per_sym_006[s]["return_pct"] for s in SYMBOLS)
        checks = {
            "expectancy_R_>=0.3": e["avg_R"] >= 0.3,
            "winrate_>=40": e["winrate"] >= 40.0,
            "maxdd_<25": e["avg_maxdd"] < 25.0,
            "pos_windows_>=4of6": c["pos_windows"] >= 4,
            "no_symbol_<-20%": worst_sym_ret >= -20.0,
        }
        passed = all(checks.values())
        decision[name] = {"pass": passed, "checks": checks,
                          "worst_symbol_ret": round(worst_sym_ret, 1)}
        if name == "pullback_regime":
            print(f"{name}: {'✅ GO' if passed else '❌ NO-GO'}")
            for k, v in checks.items():
                print(f"   {'✓' if v else '✗'} {k}")
    print("=" * 78)

    out = {
        "config": {"trigger": lab.TRIGGER, "bias_tfs": lab.BIAS_TFS,
                   "risk_pct": RISK_PCT, "costs": COSTS, "n_windows": N_WINDOWS,
                   "tested": TEST},
        "fee_sensitivity": fee_sensitivity,
        "expectancy_at_006": expectancy,
        "consistency_at_006": consistency,
        "go_no_go": decision,
    }
    out_path = os.path.join(bt.RESULTS_DIR, "walkforward_pullback_regime.json")
    os.makedirs(bt.RESULTS_DIR, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    logger.info(f"Kết quả walk-forward: {out_path}")


if __name__ == "__main__":
    main()
