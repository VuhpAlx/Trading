# htf_bias.py — Lớp BIAS top-down (xu hướng khung lớn)
# =====================================================================
# "The trend is your friend." Trước khi tìm điểm vào lệnh ở khung nhỏ,
# trader xác định XU HƯỚNG CHỦ ĐẠO ở khung lớn hơn, rồi CHỈ giao dịch
# thuận chiều đó. Module này biến nguyên tắc đó thành "cổng cứng":
#   • BULL  → chỉ cho phép lệnh BUY
#   • BEAR  → chỉ cho phép lệnh SELL
#   • NEUTRAL → thị trường lưỡng lự, hạn chế giao dịch (cần tín hiệu rất mạnh)
#
# Bias mỗi khung được chấm điểm từ 4 yếu tố kinh điển:
#   1. Xếp tầng EMA (EMA9 > EMA21 > EMA50 = tăng mạnh; ngược lại = giảm).
#   2. Giá so với EMA50 và SMA200 (trên = tăng, dưới = giảm).
#   3. ADX + DI (ADX cao = trend rõ; DI+ > DI- = tăng).
# =====================================================================

import pandas as pd
from typing import Dict, List, Optional


def _safe(value, default: float = 0.0) -> float:
    try:
        v = float(value)
        return v if v == v else default   # loại NaN
    except Exception:
        return default


def compute_bias(df_htf: Optional[pd.DataFrame]) -> Dict:
    """
    Tính bias của MỘT khung. Trả về:
      {bias: "BULL"/"BEAR"/"NEUTRAL", strength: 0..1, score: -1..1, notes: [...]}
    score > 0 thiên tăng, < 0 thiên giảm; strength = |score|.
    """
    if df_htf is None or df_htf.empty or len(df_htf) < 2:
        return {"bias": "NEUTRAL", "strength": 0.0, "score": 0.0, "notes": ["Thiếu dữ liệu"]}

    row = df_htf.iloc[-1]
    close = _safe(row.get("close"))
    ema9 = _safe(row.get("EMA_9"), close)
    ema21 = _safe(row.get("EMA_21"), close)
    ema50 = _safe(row.get("EMA_50"), close)
    sma200 = _safe(row.get("SMA_200"), close)
    adx = _safe(row.get("ADX"), 20.0)
    di_plus = _safe(row.get("DI_PLUS"), 20.0)
    di_minus = _safe(row.get("DI_MINUS"), 20.0)

    score = 0.0
    notes: List[str] = []

    # 1) Xếp tầng EMA (trọng số cao nhất)
    if ema9 > ema21 > ema50:
        score += 0.4
        notes.append("EMA xếp tầng TĂNG (9>21>50)")
    elif ema9 < ema21 < ema50:
        score -= 0.4
        notes.append("EMA xếp tầng GIẢM (9<21<50)")
    else:
        # Xếp tầng một phần
        if ema9 > ema21:
            score += 0.15
        else:
            score -= 0.15

    # 2) Giá vs EMA50
    if close > ema50:
        score += 0.15
    else:
        score -= 0.15

    # 3) Giá vs SMA200 (xu hướng dài hạn)
    if sma200 > 0:
        if close > sma200:
            score += 0.2
            notes.append("Giá trên SMA200 (dài hạn tăng)")
        else:
            score -= 0.2
            notes.append("Giá dưới SMA200 (dài hạn giảm)")

    # 4) ADX + DI (chỉ tính khi trend đủ rõ)
    if adx >= 20:
        if di_plus > di_minus:
            score += 0.25
            notes.append(f"ADX {adx:.0f} mạnh, DI+ dẫn (tăng)")
        else:
            score -= 0.25
            notes.append(f"ADX {adx:.0f} mạnh, DI- dẫn (giảm)")
    else:
        notes.append(f"ADX {adx:.0f} yếu — trend không rõ")

    score = max(-1.0, min(1.0, score))

    # Ngưỡng phân loại: |score| < 0.25 coi như lưỡng lự
    if score >= 0.25:
        bias = "BULL"
    elif score <= -0.25:
        bias = "BEAR"
    else:
        bias = "NEUTRAL"

    return {"bias": bias, "strength": round(abs(score), 3),
            "score": round(score, 3), "notes": notes}


def aggregate_bias(biases: List[Dict]) -> Dict:
    """
    Gộp bias của 2 khung lớn (theo HTF_MAP) thành 1 phán quyết tổng.
      • Hai khung cùng chiều → bias đó, strength = trung bình (có thưởng đồng thuận).
      • Mâu thuẫn (1 BULL, 1 BEAR) → NEUTRAL (không giao dịch khi khung lớn cãi nhau).
      • Có NEUTRAL → nghiêng theo khung còn lại nhưng strength giảm.
    """
    if not biases:
        return {"bias": "NEUTRAL", "strength": 0.0, "score": 0.0, "per_tf": [], "notes": []}

    avg_score = sum(b["score"] for b in biases) / len(biases)
    has_bull = any(b["bias"] == "BULL" for b in biases)
    has_bear = any(b["bias"] == "BEAR" for b in biases)

    if has_bull and has_bear:
        # Khung lớn mâu thuẫn → đứng ngoài
        final = "NEUTRAL"
        strength = 0.0
        note = "Khung lớn MÂU THUẪN → đứng ngoài"
    elif avg_score >= 0.2:
        final = "BULL"
        strength = min(1.0, abs(avg_score) * (1.2 if all(b["bias"] == "BULL" for b in biases) else 1.0))
        note = "Đồng thuận TĂNG" if all(b["bias"] == "BULL" for b in biases) else "Thiên TĂNG"
    elif avg_score <= -0.2:
        final = "BEAR"
        strength = min(1.0, abs(avg_score) * (1.2 if all(b["bias"] == "BEAR" for b in biases) else 1.0))
        note = "Đồng thuận GIẢM" if all(b["bias"] == "BEAR" for b in biases) else "Thiên GIẢM"
    else:
        final = "NEUTRAL"
        strength = 0.0
        note = "Khung lớn trung lập"

    return {
        "bias": final,
        "strength": round(strength, 3),
        "score": round(avg_score, 3),
        "per_tf": biases,
        "notes": [note],
    }
