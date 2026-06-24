# market_structure.py — Lớp CẤU TRÚC THỊ TRƯỜNG (Hỗ trợ/Kháng cự/Pivot)
# =====================================================================
# Đây là lớp mà hệ thống cũ HOÀN TOÀN THIẾU. Trader chuyên nghiệp ra
# quyết định dựa trên "giá đang ở đâu so với các vùng quan trọng", không
# chỉ dựa vào chỉ báo. Module này tìm các MỐC GIÁ mà thị trường hay phản
# ứng:
#   • SWING HIGH/LOW : đỉnh/đáy cục bộ (fractal) — nơi giá từng đảo chiều.
#   • PIVOT POINTS   : mốc tính theo công thức từ nến khung-lớn liền trước
#                       (pivot sàn cổ điển) — dân day-trade dùng rất nhiều.
#   • ROUND NUMBERS  : mốc số tròn (tâm lý) — vd 80,000 / 3,000.
#
# Nguyên tắc dùng:
#   - MUA gần HỖ TRỢ (giá rẻ, rủi ro thấp), không mua sát KHÁNG CỰ.
#   - BÁN gần KHÁNG CỰ, không bán sát HỖ TRỢ.
#   - Đặt SL phía sau mốc cấu trúc; đặt TP tại mốc kế tiếp.
# =====================================================================

import pandas as pd
from typing import List, Dict, Optional


def floor_pivots(prev_high: float, prev_low: float, prev_close: float) -> Dict[str, float]:
    """
    Pivot sàn cổ điển (Classic Floor Pivots) tính từ nến KHUNG-LỚN-HƠN
    liền trước (vd nến ngày trước cho giao dịch trong ngày).
      P  = (H + L + C) / 3              -- điểm pivot trung tâm
      R1 = 2P - L ;  S1 = 2P - H        -- kháng cự / hỗ trợ cấp 1
      R2 = P + (H-L) ; S2 = P - (H-L)
      R3 = H + 2(P-L) ; S3 = L - 2(H-P)
    """
    p = (prev_high + prev_low + prev_close) / 3.0
    rng = prev_high - prev_low
    return {
        "P":  round(p, 4),
        "R1": round(2 * p - prev_low, 4),
        "S1": round(2 * p - prev_high, 4),
        "R2": round(p + rng, 4),
        "S2": round(p - rng, 4),
        "R3": round(prev_high + 2 * (p - prev_low), 4),
        "S3": round(prev_low - 2 * (prev_high - p), 4),
    }


def swing_levels(df: pd.DataFrame, strength: int = 3, lookback: int = 200) -> Dict[str, List[float]]:
    """
    Tìm swing high/low kiểu FRACTAL: 1 đỉnh là nến có high cao hơn `strength`
    nến mỗi bên; 1 đáy là nến có low thấp hơn `strength` nến mỗi bên.
    Trả về 2 danh sách giá (đỉnh = kháng cự tiềm năng, đáy = hỗ trợ tiềm năng).
    """
    if df is None or len(df) < (2 * strength + 1):
        return {"highs": [], "lows": []}

    sub = df.tail(lookback).reset_index(drop=True)
    highs_arr = sub["high"].to_numpy()
    lows_arr = sub["low"].to_numpy()
    n = len(sub)

    swing_highs: List[float] = []
    swing_lows: List[float] = []

    for i in range(strength, n - strength):
        window_h = highs_arr[i - strength:i + strength + 1]
        window_l = lows_arr[i - strength:i + strength + 1]
        if highs_arr[i] == window_h.max() and (window_h.argmax() == strength):
            swing_highs.append(round(float(highs_arr[i]), 4))
        if lows_arr[i] == window_l.min() and (window_l.argmin() == strength):
            swing_lows.append(round(float(lows_arr[i]), 4))

    return {"highs": swing_highs, "lows": swing_lows}


def cluster_levels(levels: List[float], tol_pct: float = 0.0015) -> List[float]:
    """
    Gộp các mốc nằm sát nhau (trong khoảng tol_pct) thành 1 "vùng" (lấy
    trung bình). Giúp loại bớt nhiễu — nhiều đỉnh sát nhau = 1 vùng kháng cự.
    """
    if not levels:
        return []
    ordered = sorted(levels)
    clusters: List[List[float]] = [[ordered[0]]]
    for lv in ordered[1:]:
        last_group = clusters[-1]
        anchor = last_group[0]
        if abs(lv - anchor) / (anchor + 1e-9) <= tol_pct:
            last_group.append(lv)
        else:
            clusters.append([lv])
    return [round(sum(g) / len(g), 4) for g in clusters]


def round_numbers(price: float, count: int = 2) -> List[float]:
    """
    Mốc số tròn tâm lý quanh giá hiện tại. Bước làm tròn tỉ lệ với độ lớn
    giá (vd ~1% của giá) để hợp lý cho mọi mức giá (BTC vs BNB).
    """
    if price <= 0:
        return []
    # Bậc độ lớn: chọn step "đẹp" gần ~0.5-1% giá
    magnitude = 10 ** (len(str(int(price))) - 2) if price >= 100 else 1
    step = magnitude
    base = round(price / step) * step
    levels = []
    for k in range(-count, count + 1):
        lv = base + k * step
        if lv > 0:
            levels.append(round(lv, 4))
    return levels


def analyze(df: pd.DataFrame,
            htf_df_for_pivot: Optional[pd.DataFrame] = None,
            strength: int = 3,
            lookback: int = 200) -> Dict:
    """
    Phân tích cấu trúc tổng hợp quanh giá hiện tại.

    Trả về:
      supports/resistances   : danh sách mốc (đã gộp vùng) dưới/trên giá.
      pivots                 : dict pivot sàn (P, R1..R3, S1..S3).
      nearest_support        : hỗ trợ gần nhất NGAY DƯỚI giá (hoặc None).
      nearest_resistance     : kháng cự gần nhất NGAY TRÊN giá (hoặc None).
      dist_to_support_pct    : % khoảng cách tới hỗ trợ gần nhất.
      dist_to_resistance_pct : % khoảng cách tới kháng cự gần nhất.
    """
    empty = {
        "supports": [], "resistances": [], "pivots": {},
        "nearest_support": None, "nearest_resistance": None,
        "dist_to_support_pct": None, "dist_to_resistance_pct": None,
        "all_levels": [],
    }
    if df is None or df.empty:
        return empty

    price = float(df.iloc[-1]["close"])
    if price <= 0:
        return empty

    # 1) Swing levels từ khung hiện tại
    sw = swing_levels(df, strength=strength, lookback=lookback)
    raw_levels = list(sw["highs"]) + list(sw["lows"])

    # 2) Pivot từ nến khung-lớn-hơn liền trước (nếu có) — nếu không, dùng
    #    nến trước của chính khung này như fallback.
    pivots: Dict[str, float] = {}
    pivot_src = htf_df_for_pivot if (htf_df_for_pivot is not None and len(htf_df_for_pivot) >= 2) else df
    if pivot_src is not None and len(pivot_src) >= 2:
        prev = pivot_src.iloc[-2]
        pivots = floor_pivots(float(prev["high"]), float(prev["low"]), float(prev["close"]))
        raw_levels += list(pivots.values())

    # 3) Mốc số tròn tâm lý
    raw_levels += round_numbers(price)

    # Gộp vùng + phân loại trên/dưới giá
    clustered = cluster_levels(raw_levels)
    supports = sorted([lv for lv in clustered if lv < price], reverse=True)
    resistances = sorted([lv for lv in clustered if lv > price])

    nearest_support = supports[0] if supports else None
    nearest_resistance = resistances[0] if resistances else None

    dist_s = round((price - nearest_support) / price * 100, 3) if nearest_support else None
    dist_r = round((nearest_resistance - price) / price * 100, 3) if nearest_resistance else None

    return {
        "supports": supports[:5],
        "resistances": resistances[:5],
        "pivots": pivots,
        "nearest_support": nearest_support,
        "nearest_resistance": nearest_resistance,
        "dist_to_support_pct": dist_s,
        "dist_to_resistance_pct": dist_r,
        "all_levels": clustered,
    }
