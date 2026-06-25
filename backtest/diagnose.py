# diagnose.py — Tại sao bot không vào lệnh? Đếm "phễu" quyết định.
# Đọc res["reasons"] + res["htf_bias"] mỗi nến (KHÔNG lặp lại logic) để xem
# gate nào chặn: bias NEUTRAL / confluence thiếu / R:R thấp / chờ xác nhận.
import os, sys, collections
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import numpy as np, backtest as bt, fetch_data

RUN = bt.RUN_DIR + "_diag"
os.makedirs(RUN, exist_ok=True)
os.chdir(RUN)
from signal_engine import AdvancedSignalEngine

def diag(symbol, tf):
    data = fetch_data.fetch_all(force=False)
    ind = bt.compute_indicators_full(data[symbol][tf])
    ind_ms = bt._ms(ind["timestamp"])
    htf_tfs = __import__("config").HTF_MAP.get(tf, [])
    htf_ind, htf_close = {}, {}
    for h in htf_tfs:
        hi = bt.compute_indicators_full(data[symbol][h]); htf_ind[h]=hi
        htf_close[h] = bt._ms(hi["timestamp"]) + bt._TF_MS[h]
    close_ms = ind_ms + bt._TF_MS[tf]
    eng = AdvancedSignalEngine(symbol, tf); eng.set_capital(100.0)

    bias_c = collections.Counter(); funnel = collections.Counter()
    conf_hist = collections.Counter()   # max(buy_conf,sell_conf) khi bias non-neutral
    n=len(ind)
    for i in range(bt.WARMUP_BARS, n):
        lo=max(0,i-bt.WINDOW+1); dfw=ind.iloc[lo:i+1].reset_index(drop=True)
        mtf={}
        for h in htf_tfs:
            if h not in htf_ind: continue
            j=int(np.searchsorted(htf_close[h], close_ms[i], side="right"))-1
            if j<1: continue
            hlo=max(0,j-bt.WINDOW+1); mtf[h]=htf_ind[h].iloc[hlo:j+1].reset_index(drop=True)
        res=eng.generate_signal(dfw, mtf)
        b=res.get("htf_bias",{}).get("bias","?"); bias_c[b]+=1
        reasons=" || ".join(res.get("reasons",[]))
        if "VÀO LỆNH" in reasons: funnel["ENTERED"]+=1
        elif "Chờ xác nhận" in reasons: funnel["CONFIRM_WAIT"]+=1
        elif "BỎ lệnh" in reasons: funnel["RR_FAIL"]+=1
        elif "Confluence" in reasons and "chưa đủ" in reasons: funnel["CONFLUENCE_LOW"]+=1
        elif b=="NEUTRAL": funnel["BIAS_NEUTRAL"]+=1
        else: funnel["OTHER"]+=1
    print(f"\n=== {symbol}/{tf}  (đánh giá {n-bt.WARMUP_BARS} nến) ===")
    print("BIAS:", dict(bias_c))
    print("FUNNEL:", dict(funnel))

if __name__=="__main__":
    sym = sys.argv[1] if len(sys.argv)>1 else "BTCUSDT"
    tf  = sys.argv[2] if len(sys.argv)>2 else "1h"
    diag(sym, tf)
