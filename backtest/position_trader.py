# position_trader.py — Position trading theo CHẾ ĐỘ XU HƯỚNG khung NGÀY (1d)
# =====================================================================
# Triết lý NGƯỢC HẲN bot intraday cũ:
#   • KHÔNG SL/TP từng lệnh, KHÔNG vào/ra liên tục.
#   • Chỉ bám trend khung 1d: trend lên → GIỮ LONG; trend xuống → FLAT (hoặc SHORT).
#   • Đổi trạng thái CHỈ khi trend 1d lật → vài lệnh/năm → phí ~0 → ôm trọn sóng.
# Mục tiêu: kiểm tra giả thuyết cuối của TA — "bộ lọc trend 1d có ĐÁNH BẠI
# buy-and-hold không?" (B&H = luôn ôm 100%, hứng trọn cả sóng giảm).
#
# Sizing: theo NOTIONAL = exposure × vốn (mặc định 1.0 = full, KHÔNG đòn bẩy)
# → so sánh công bằng với B&H. Phí taker+slippage 2 chiều mỗi lần đổi trạng thái.
#
# Dùng:  python backtest/position_trader.py
# =====================================================================
import os
import sys
import json
import argparse
import logging

import numpy as np

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import backtest as bt
import fetch_data
import htf_bias
import lot_sizing
from config import SYMBOLS

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger("position_trader")

RESULTS_DIR = bt.RESULTS_DIR
TRIGGER = "1d"          # quyết định trên nến NGÀY
WARMUP = 250            # cần ≥200 cho SMA200 ổn định
TAKER = 0.0010          # phí 1 chiều
SLIP = 0.0002           # slippage 1 chiều

# Chế độ thử nghiệm
STRATS = {
    # ema: LONG khi EMA21>EMA50 (1d); bias: dùng htf_bias.compute_bias(1d)
    "ema_longonly":   dict(regime="ema",  allow_short=False),
    "ema_longshort":  dict(regime="ema",  allow_short=True),
    "bias_longonly":  dict(regime="bias", allow_short=False),
    "bias_longshort": dict(regime="bias", allow_short=True),
}


def run(symbol, cfg, data) -> dict:
    ind = bt.compute_indicators_full(data[symbol][TRIGGER])
    n = len(ind)
    cap = 100.0
    pos = "FLAT"          # trạng thái hiện tại
    entry = 0.0
    lot = 0.0
    trades = 0
    wins = losses = 0
    cap_curve = [cap]
    contract = lot_sizing.get_contract_size(symbol)

    # Chuẩn bị bias theo cửa sổ (nếu regime=bias) — tính trên slice tới nến i
    def get_regime(i):
        row = ind.iloc[i]
        if cfg["regime"] == "ema":
            e21 = float(row.get("EMA_21") or 0); e50 = float(row.get("EMA_50") or 0)
            if e21 > e50: return "LONG"
            return "SHORT" if cfg["allow_short"] else "FLAT"
        else:
            b = htf_bias.compute_bias(ind.iloc[max(0, i - 250):i + 1])
            if b["bias"] == "BULL": return "LONG"
            if b["bias"] == "BEAR": return "SHORT" if cfg["allow_short"] else "FLAT"
            return "FLAT"

    def close_pos(price):
        nonlocal cap, pos, entry, lot, wins, losses
        if pos == "FLAT" or lot <= 0:
            return
        gross = lot_sizing.pnl_usd(entry, price, lot, symbol, pos)
        fee = lot_sizing.notional_value(lot, symbol, price) * TAKER + \
              lot_sizing.notional_value(lot, symbol, price) * SLIP
        cap = max(0.0, cap + gross - fee)
        if gross - fee > 0: wins += 1
        else: losses += 1
        pos, entry, lot = "FLAT", 0.0, 0.0

    def open_pos(side, price):
        nonlocal cap, pos, entry, lot, trades
        # notional = exposure(1.0) × vốn ; lot = notional/(contract×price)
        notional = 1.0 * cap
        lot = notional / (contract * price)
        entry = price
        pos = side
        trades += 1
        fee = notional * TAKER + notional * SLIP   # phí mở
        cap = max(0.0, cap - fee)

    first_ts = last_ts = None
    for i in range(WARMUP, n):
        row = ind.iloc[i]
        price = float(row["close"])
        target = get_regime(i)

        if target != pos:
            close_pos(price)          # đóng trạng thái cũ (nếu có)
            if target in ("LONG", "SHORT"):
                open_pos(target, price)
        cap_curve.append(cap if pos == "FLAT" else cap + lot_sizing.pnl_usd(entry, price, lot, symbol, pos))

        if first_ts is None: first_ts = row["timestamp"]
        last_ts = row["timestamp"]

    # đóng cuối kỳ theo giá cuối
    close_pos(float(ind.iloc[-1]["close"]))

    sub = ind.iloc[WARMUP:]
    bh = (sub.iloc[-1]["close"] / sub.iloc[0]["close"] - 1) * 100
    peak = cap_curve[0]; mdd = 0.0
    for c in cap_curve:
        peak = max(peak, c); mdd = max(mdd, (peak - c) / peak * 100 if peak > 0 else 0)
    total = wins + losses
    return {
        "symbol": symbol, "trades": trades, "wins": wins, "losses": losses,
        "winrate": round(wins / total * 100, 1) if total else 0.0,
        "final_capital": round(cap, 2), "return_pct": round(cap - 100, 2),
        "buyhold_pct": round(bh, 1), "max_dd_pct": round(mdd, 2),
        "eval_from": str(first_ts), "eval_to": str(last_ts),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbols", default=",".join(SYMBOLS))
    args = ap.parse_args()
    symbols = [s.strip() for s in args.symbols.split(",") if s.strip()]

    logger.info("Tải/đọc data cache...")
    data = fetch_data.fetch_all(force=False)
    os.makedirs(RESULTS_DIR, exist_ok=True)

    all_res = {}
    for name, cfg in STRATS.items():
        rows = [run(s, cfg, data) for s in symbols]
        all_res[name] = rows

    print("\n" + "=" * 96)
    print(f"POSITION TRADER (trigger {TRIGGER}, full-allocation, no leverage) | so với Buy&Hold")
    print(f"{'STRATEGY':<16}{'SYMBOL':<10}{'Trades':>7}{'WR%':>6}{'Final$':>9}{'Ret%':>8}{'B&H%':>8}{'MaxDD%':>8}{'vs B&H':>9}")
    print("-" * 96)
    for name, rows in all_res.items():
        tot = sum(r["return_pct"] for r in rows)
        beat = sum(1 for r in rows if r["return_pct"] > r["buyhold_pct"])
        for r in rows:
            edge = r["return_pct"] - r["buyhold_pct"]
            print(f"{name:<16}{r['symbol']:<10}{r['trades']:>7}{r['winrate']:>6}"
                  f"{r['final_capital']:>9}{r['return_pct']:>8.1f}{r['buyhold_pct']:>8.1f}"
                  f"{r['max_dd_pct']:>8.1f}{edge:>+9.1f}")
        print(f"{'  → tổng ret '+str(round(tot,1))+'  | thắng B&H '+str(beat)+'/'+str(len(rows)):<60}")
        print("-" * 96)
    print(f"Cửa sổ: {all_res[list(all_res)[0]][0]['eval_from'][:10]} → "
          f"{all_res[list(all_res)[0]][0]['eval_to'][:10]}")
    print("=" * 96)

    out = os.path.join(RESULTS_DIR, "position_trader.json")
    with open(out, "w", encoding="utf-8") as f:
        json.dump({"trigger": TRIGGER, "strats": STRATS, "results": all_res}, f,
                  ensure_ascii=False, indent=2)
    logger.info(f"Kết quả: {out}")


if __name__ == "__main__":
    main()
