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
TEST = ["pullback_trend", "breakout_3R"]
COSTS = [0.0003, 0.0006, 0.0010]   # vàng/FX · crypto · standard
N_WINDOWS = 6


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
    for name in TEST:
        cfg = lab.STRATS[name]
        row_out = []
        for cost in COSTS:
            TradeSimulator.TAKER_FEE = cost / 2.0
            TradeSimulator.SLIPPAGE = 0.00005
            rets = []
            logs = {}
            for sym in SYMBOLS:
                r = lab.run_strategy(sym, cfg, data, TradeSimulator)
                rets.append(r["return_pct"])
                logs[sym] = r["trade_log"]
            row_out.append(sum(rets) / len(rets))
            if abs(cost - 0.0006) < 1e-9:
                logs_at_006[name] = logs
        print(f"{name:<16}{row_out[0]:>11.1f}%{row_out[1]:>11.1f}%{row_out[2]:>11.1f}%")
    print("=" * 78)

    # ===== (B) TÍNH NHẤT QUÁN QUA 6 CỬA SỔ (tại phí 0.06%) =====
    print("\n" + "=" * 100)
    print(f"(B) NHẤT QUÁN QUA {N_WINDOWS} CỬA SỔ (phí 0.06%, lãi TB/coin mỗi đoạn, WR gộp)")
    hdr = "".join([f"W{k+1}" .rjust(13) for k in range(N_WINDOWS)])
    print(f"{'Strategy':<16}{hdr}{'  +đoạn':>9}")
    print("  (cửa sổ: " + " | ".join(f"{edges[k].date()}→{edges[k+1].date()}" for k in range(N_WINDOWS)) + ")")
    print("-" * 100)
    for name in TEST:
        logs = logs_at_006[name]
        cells = []
        pos_windows = 0
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
        print(f"{name:<16}" + "".join(c.rjust(13) for c in cells) + f"{pos_windows}/{N_WINDOWS}".rjust(9))
    print("=" * 100)
    print("Mỗi ô = lãi TB/coin trong đoạn / WR gộp. Cột cuối = số đoạn dương / tổng.")


if __name__ == "__main__":
    main()
