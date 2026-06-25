# strategy_lab.py — Thử nghiệm TÍN HIỆU VÀO LỆNH mới để tìm EDGE
# =====================================================================
# Tách bạch: GIỮ NGUYÊN execution (TradeSimulator: phí/lot/margin/thanh lý/
# slippage/cooldown) — chỉ THAY logic alpha (entry trigger + SL/TP).
# Mỗi coin = 1 trader (unified), trigger 15m, bias gộp 1h/4h/1d (tuỳ chọn).
#
# So sánh nhiều chiến lược coherent với benchmark Buy&Hold + "đứng im".
#   • breakout  : phá đỉnh/đáy Donchian N nến THUẬN bias (trend-following).
#   • momentum  : MACD_H đổi dấu THUẬN bias (động lượng mới).
#   • meanrev   : RSI/BB cực trị (đảo chiều) — tuỳ chọn bỏ cổng bias.
# SL theo ATR (rộng hơn 0.8×ATR cũ); TP cố định R-multiple HOẶC trailing ATR.
#
# Dùng:  python backtest/strategy_lab.py
#        python backtest/strategy_lab.py --symbols BTCUSDT --only breakout_trail
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

import backtest as bt
import fetch_data
import htf_bias
from config import SYMBOLS

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("SignalEngine").setLevel(logging.WARNING)
logger = logging.getLogger("strategy_lab")

RUN_DIR = bt.RUN_DIR + "_lab"
RESULTS_DIR = bt.RESULTS_DIR

TRIGGER = "15m"
BIAS_TFS = ["1h", "4h", "1d"]

# Các chiến lược thử nghiệm
STRATS = {
    "breakout_2R":    dict(entry="breakout", bias=True,  n=20, sl_atr=1.5, tp="fixedR", rr=2.0),
    "breakout_3R":    dict(entry="breakout", bias=True,  n=20, sl_atr=1.5, tp="fixedR", rr=3.0),
    "breakout_trail": dict(entry="breakout", bias=True,  n=20, sl_atr=1.5, tp="trail",  trail_atr=3.0),
    "momentum_2R":    dict(entry="momentum", bias=True,  sl_atr=1.5, tp="fixedR", rr=2.0),
    "meanrev_bias":   dict(entry="meanrev",  bias=True,  sl_atr=1.2, tp="fixedR", rr=1.5),
    "meanrev_noBias": dict(entry="meanrev",  bias=False, sl_atr=1.2, tp="fixedR", rr=1.5),
    # --- SWING (thiết kế cho WR cao + kỳ vọng dương, phí Exness) ---
    # pullback thuận trend: mua nhịp chỉnh trong uptrend (RSI hồi), TP 1.5R
    "pullback_trend": dict(entry="pullback", bias=True,  sl_atr=1.5, tp="fixedR", rr=1.5),
    # mean-reversion CHỈ khi đi ngang (ADX<20), chốt nhanh ở BB giữa → WR cao
    "regime_meanrev": dict(entry="regime_meanrev", bias=False, sl_atr=1.5, tp="bbmid"),
}


def _safe(v, d=0.0):
    try:
        x = float(v)
        return x if x == x else d
    except Exception:
        return d


def decide_entry(cfg, row, prev, hist_high, hist_low, bias_dir):
    """Trả ('BUY'|'SELL'|None, conf_strength 0..1). hist_high/low = đỉnh/đáy N nến TRƯỚC."""
    close = _safe(row["close"])
    ema9, ema21, ema50 = _safe(row.get("EMA_9"), close), _safe(row.get("EMA_21"), close), _safe(row.get("EMA_50"), close)
    macd_h, macd_h_p = _safe(row.get("MACD_H")), _safe(prev.get("MACD_H"))
    rsi = _safe(row.get("RSI_14"), 50.0)
    bb_l, bb_u = _safe(row.get("BB_L"), close * 0.98), _safe(row.get("BB_U"), close * 1.02)

    allow_buy = (bias_dir == "BULL") if cfg["bias"] else True
    allow_sell = (bias_dir == "BEAR") if cfg["bias"] else True

    if cfg["entry"] == "breakout":
        if allow_buy and close > hist_high:
            return "BUY", 0.7
        if allow_sell and close < hist_low:
            return "SELL", 0.7

    elif cfg["entry"] == "momentum":
        cross_up = macd_h > 0 and macd_h_p <= 0
        cross_dn = macd_h < 0 and macd_h_p >= 0
        if allow_buy and cross_up and ema9 > ema21:
            return "BUY", 0.6
        if allow_sell and cross_dn and ema9 < ema21:
            return "SELL", 0.6

    elif cfg["entry"] == "meanrev":
        # Đảo chiều ở cực trị: quá bán gần dải dưới → BUY; quá mua gần dải trên → SELL
        if allow_buy and rsi < 32 and close <= bb_l * 1.002:
            return "BUY", 0.6
        if allow_sell and rsi > 68 and close >= bb_u * 0.998:
            return "SELL", 0.6

    elif cfg["entry"] == "pullback":
        # Mua NHỊP CHỈNH trong uptrend (EMA xếp tầng tăng, giá hồi về EMA21, RSI quay lên)
        rsi_p = _safe(prev.get("RSI_14"), rsi)
        if allow_buy and ema9 > ema21 > ema50 and close > ema50 \
                and rsi < 48 and rsi > rsi_p and close <= ema21 * 1.006:
            return "BUY", 0.6
        if allow_sell and ema9 < ema21 < ema50 and close < ema50 \
                and rsi > 52 and rsi < rsi_p and close >= ema21 * 0.994:
            return "SELL", 0.6

    elif cfg["entry"] == "regime_meanrev":
        # CHỈ đánh đảo chiều khi thị trường ĐI NGANG (ADX thấp) — nơi mean-rev WR cao.
        adx = _safe(row.get("ADX"), 25.0)
        if adx < 20:
            if rsi < 35 and close <= bb_l * 1.003:
                return "BUY", 0.6
            if rsi > 65 and close >= bb_u * 0.997:
                return "SELL", 0.6

    return None, 0.0


def run_strategy(symbol, cfg, data, SimCls) -> dict:
    ind = bt.compute_indicators_full(data[symbol][TRIGGER])
    ind_ms = bt._ms(ind["timestamp"])
    close_ms = ind_ms + bt._TF_MS[TRIGGER]
    highs = ind["high"].to_numpy()
    lows = ind["low"].to_numpy()

    htf_ind, htf_close = {}, {}
    for h in BIAS_TFS:
        hi = bt.compute_indicators_full(data[symbol][h])
        htf_ind[h] = hi
        htf_close[h] = bt._ms(hi["timestamp"]) + bt._TF_MS[h]

    sim = SimCls(symbol, TRIGGER, capital=100.0)
    sim.trade_log_file = f"lab_{symbol}.jsonl"   # cô lập, không đụng file thật

    n = len(ind)
    N = cfg.get("n", 20)
    wins = losses = liq = 0
    cap_curve = [100.0]
    trade_log = []          # (exit_ts_iso, cap_after, result) — cho walk-forward
    first_ts = last_ts = None

    for i in range(bt.WARMUP_BARS, n):
        row = ind.iloc[i]
        prev = ind.iloc[i - 1]
        c_close, c_high, c_low = float(row["close"]), float(row["high"]), float(row["low"])
        atr = _safe(row.get("ATR_14"), c_close * 0.005) or (c_close * 0.005)

        # Giảm cooldown mỗi nến mới (live làm trong generate_signal; lab gọi
        # process_tick trực tiếp nên phải tự giảm — nếu không sẽ kẹt sau lệnh đầu).
        if sim.cooldown > 0:
            sim.cooldown -= 1

        # --- quản lý lệnh đang mở: TP/SL + trailing ---
        for _ in range(2):
            if sim.state == "NONE":
                break
            # trailing stop (nếu bật) trước khi check
            if cfg.get("tp") == "trail" and sim.trade:
                t = sim.trade
                if sim.state == "LONG":
                    t["sl"] = max(t["sl"], c_close - cfg["trail_atr"] * atr)
                else:
                    t["sl"] = min(t["sl"], c_close + cfg["trail_atr"] * atr)
            closed = sim.process_tick(c_close, c_high, c_low)
            if closed:
                r = closed["result"]
                if r == "WIN": wins += 1
                elif r == "LOSS": losses += 1
                else: liq += 1
                cap_curve.append(sim.capital)
                trade_log.append((str(row["timestamp"]), round(sim.capital, 4), r))
                break

        # --- xét vào lệnh mới khi đang rảnh ---
        if sim.state == "NONE" and sim.cooldown == 0:
            # bias từ HTF đã đóng (chống lookahead)
            if cfg["bias"]:
                per = []
                for h in BIAS_TFS:
                    j = int(np.searchsorted(htf_close[h], close_ms[i], side="right")) - 1
                    if j < 1:
                        continue
                    b = htf_bias.compute_bias(htf_ind[h].iloc[max(0, j - 250):j + 1])
                    per.append(b)
                bias_dir = htf_bias.aggregate_bias(per)["bias"] if per else "NEUTRAL"
            else:
                bias_dir = "ANY"

            lo_h = max(0, i - N)
            hist_high = float(highs[lo_h:i].max()) if i > lo_h else c_high
            hist_low = float(lows[lo_h:i].min()) if i > lo_h else c_low

            side, strength = decide_entry(cfg, row, prev, hist_high, hist_low, bias_dir)
            if side:
                mult = 1 if side == "BUY" else -1
                sl = c_close - mult * cfg["sl_atr"] * atr
                if cfg["tp"] == "fixedR":
                    tp = c_close + mult * cfg["sl_atr"] * atr * cfg["rr"]
                elif cfg["tp"] == "bbmid":
                    # Chốt nhanh ở dải BB giữa (mục tiêu mean-reversion) → WR cao
                    bbm = _safe(row.get("BB_M"), c_close)
                    tp = bbm
                    if (tp - c_close) * mult < 0.3 * atr:   # quá gần/sai hướng → fallback
                        tp = c_close + mult * atr * 1.0
                else:  # trailing: TP rất xa, thoát bằng trailing SL
                    tp = c_close + mult * atr * 100.0
                snap = {"EMA": 0.0}  # snapshot tối giản (không học weights ở lab)
                sim.open_position(
                    str(row["timestamp"]), side, c_close, round(sl, 4), round(tp, 4),
                    confluence=4.0, bias_strength=strength, indicators_snap=snap,
                )

        if first_ts is None:
            first_ts = row["timestamp"]
        last_ts = row["timestamp"]

    total = wins + losses
    # max drawdown trên đường vốn
    peak = cap_curve[0]; mdd = 0.0
    for c in cap_curve:
        peak = max(peak, c)
        mdd = max(mdd, (peak - c) / peak * 100 if peak > 0 else 0.0)

    # Buy & Hold cùng cửa sổ
    sub = ind.iloc[bt.WARMUP_BARS:]
    bh = (sub.iloc[-1]["close"] / sub.iloc[0]["close"] - 1) * 100

    return {
        "symbol": symbol, "trades": wins + losses + liq,
        "wins": wins, "losses": losses, "liq": liq,
        "winrate": round(wins / total * 100, 1) if total else 0.0,
        "final_capital": round(sim.capital, 2),
        "return_pct": round(sim.capital - 100.0, 2),
        "max_dd_pct": round(mdd, 2),
        "buyhold_pct": round(bh, 1),
        "trade_log": trade_log,
    }


def main():
    global TRIGGER, BIAS_TFS
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbols", default=",".join(SYMBOLS))
    ap.add_argument("--only", default="", help="chỉ chạy 1 strategy theo tên")
    ap.add_argument("--risk", type=float, default=0.01, help="rủi ro mỗi lệnh theo %% vốn (0.01=1%%)")
    ap.add_argument("--trigger", default=TRIGGER, help="khung trigger (15m/30m/1h/4h)")
    ap.add_argument("--bias", default=",".join(BIAS_TFS), help="khung bias, vd 4h,1d")
    ap.add_argument("--cost", type=float, default=0.0006,
                    help="phí KHỨ HỒI/lệnh (fraction). Binance spot~0.0024; Exness vàng~0.0003, crypto~0.0006")
    args = ap.parse_args()
    TRIGGER = args.trigger
    BIAS_TFS = [t.strip() for t in args.bias.split(",") if t.strip()]
    symbols = [s.strip() for s in args.symbols.split(",") if s.strip()]
    strats = {k: v for k, v in STRATS.items() if (not args.only or k == args.only)}

    logger.info("Tải/đọc data cache...")
    data = fetch_data.fetch_all(force=False)

    if os.path.exists(RUN_DIR):
        shutil.rmtree(RUN_DIR, ignore_errors=True)
    os.makedirs(RUN_DIR, exist_ok=True)
    os.makedirs(RESULTS_DIR, exist_ok=True)
    os.chdir(RUN_DIR)

    # --- FIX SIZING (cô lập): chuẩn hóa rủi ro theo % vốn cho MỌI coin ---
    # Bỏ sàn MIN_LOT (cho lot phân số) để BTC giá cao không bị phơi nhiễm 7× vốn.
    # Khi đó lot = RISK_CAP_PCT × vốn / (sl_dist × contract) → mỗi lệnh rủi ro
    # ĐỒNG ĐỀU % vốn bất kể giá coin → so sánh chiến lược mới công bằng.
    import lot_sizing
    lot_sizing.MIN_LOT = 1e-9
    lot_sizing.LOT_STEP = 1e-9
    lot_sizing.MAX_LOT = 1e9
    lot_sizing.RISK_CAP_PCT = args.risk
    # Ép dynamic-lot lớn để open_position luôn lấy lot theo risk-cap (min(dyn,cap)=cap)
    lot_sizing.LOT_BASE = 1e6
    lot_sizing.LOT_MAX_DYNAMIC = 1e6
    # normalize_lot gốc kết thúc bằng round(lot,2) → lot phân số nhỏ (vd BTC ~0.0007)
    # bị làm tròn về 0.00 → không trade. Thay bằng identity để giữ lot risk-based thật.
    lot_sizing.normalize_lot = lambda lot: max(0.0, float(lot)) if lot else 0.0
    from signal_engine import TradeSimulator
    # Phí theo Exness (thấp hơn Binance): TAKER mỗi chiều = cost/2, slippage gộp vào
    TradeSimulator.TAKER_FEE = args.cost / 2.0
    TradeSimulator.SLIPPAGE = 0.00005
    logger.info(f"Sizing: rủi ro {args.risk*100:.1f}%/lệnh, lot phân số. "
                f"Phí khứ hồi {args.cost*100:.3f}% (TAKER {TradeSimulator.TAKER_FEE*100:.3f}%/chiều)")

    all_results = {}
    t0 = time.time()
    for name, cfg in strats.items():
        logger.info(f"▶ Strategy '{name}'  {cfg}")
        rows = []
        for sym in symbols:
            r = run_strategy(sym, cfg, data, TradeSimulator)
            rows.append(r)
            logger.info(f"   {sym}: {r['trades']} lệnh, WR {r['winrate']}%, "
                        f"${r['final_capital']} (ret {r['return_pct']:+}, B&H {r['buyhold_pct']:+})")
        all_results[name] = rows

    # Bảng so sánh
    print("\n" + "=" * 104)
    print(f"STRATEGY LAB | trigger={TRIGGER} bias={BIAS_TFS} | vốn $100/coin | cửa sổ ~90 ngày")
    print(f"{'STRATEGY':<16}{'SYMBOL':<10}{'Trades':>7}{'WR%':>6}{'Final$':>9}{'Ret%':>8}{'B&H%':>8}{'MaxDD%':>8}{'vs B&H':>9}")
    print("-" * 104)
    for name, rows in all_results.items():
        tot_ret = sum(r["return_pct"] for r in rows)
        for r in rows:
            edge = r["return_pct"] - r["buyhold_pct"]
            print(f"{name:<16}{r['symbol']:<10}{r['trades']:>7}{r['winrate']:>6}"
                  f"{r['final_capital']:>9}{r['return_pct']:>8.1f}{r['buyhold_pct']:>8.1f}"
                  f"{r['max_dd_pct']:>8.1f}{edge:>+9.1f}")
        print(f"{'  → tổng ret':<26}{'':>7}{'':>6}{'':>9}{tot_ret:>8.1f}")
        print("-" * 104)
    print("=" * 104)

    out = os.path.join(RESULTS_DIR, "strategy_lab.json")
    with open(out, "w", encoding="utf-8") as f:
        json.dump({"trigger": TRIGGER, "bias": BIAS_TFS, "strats": STRATS,
                  "results": all_results, "elapsed_sec": round(time.time() - t0, 1)},
                 f, ensure_ascii=False, indent=2)
    logger.info(f"Kết quả: {out}  ({round(time.time()-t0,1)}s)")


if __name__ == "__main__":
    main()
