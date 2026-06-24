# -*- coding: utf-8 -*-
# ai_advisor.py — Phân tích rule-based cho lệnh tay của user.
# Bổ sung: cảnh báo theo CẤU TRÚC (S/R) và theo KÝ QUỸ/THANH LÝ (lot/margin).

import lot_sizing
from config import LEVERAGE


def analyze_user_trade(entry, tp, sl, position, current_indicators,
                       lot=None, symbol="BTCUSDT", capital=100.0, structure=None):
    ema21 = current_indicators.get('EMA_21', entry)
    bb_upper = current_indicators.get('BB_U', entry * 1.05)
    bb_lower = current_indicators.get('BB_L', entry * 0.95)

    risk = abs(entry - sl)
    reward = abs(tp - entry)
    rr_ratio = reward / risk if risk > 0 else 0

    suggestions = []

    # --- 1) Entry quality theo BB / EMA ---
    if position == "LONG":
        if entry > bb_upper:
            suggestions.append("⚠️ Entry ở vùng Quá Mua (ngoài BB Upper), rủi ro đu đỉnh cao.")
        if entry > ema21 * 1.02:
            suggestions.append(f"💡 Entry hơi xa EMA21. Cân nhắc chờ Pullback về {ema21:.2f}.")
    elif position == "SHORT":
        if entry < bb_lower:
            suggestions.append("⚠️ Entry ở vùng Quá Bán (ngoài BB Lower), rủi ro bán đáy cao.")

    # --- 2) Cảnh báo theo CẤU TRÚC (S/R) ---
    if structure:
        ns = structure.get("nearest_support")
        nr = structure.get("nearest_resistance")
        if position == "LONG" and nr and tp > nr:
            suggestions.append(f"🚧 TP {tp:.2f} nằm SAU kháng cự {nr:.2f} — giá dễ quay đầu trước khi tới TP.")
        if position == "LONG" and nr and abs(entry - nr) / entry < 0.003:
            suggestions.append(f"⚠️ Entry sát kháng cự {nr:.2f} — không nên mua đuổi vào vùng cản.")
        if position == "SHORT" and ns and tp < ns:
            suggestions.append(f"🚧 TP {tp:.2f} nằm SAU hỗ trợ {ns:.2f} — giá dễ bật lại trước khi tới TP.")
        if position == "SHORT" and ns and abs(entry - ns) / entry < 0.003:
            suggestions.append(f"⚠️ Entry sát hỗ trợ {ns:.2f} — không nên bán đuổi vào vùng đỡ.")

    # --- 3) Cảnh báo KÝ QUỸ / THANH LÝ (lot/margin kiểu Exness) ---
    margin = None
    liq = None
    if lot:
        lot = lot_sizing.normalize_lot(lot)
        margin = lot_sizing.margin_required(lot, symbol, entry, LEVERAGE)
        liq = lot_sizing.liquidation_price(entry, lot, symbol, capital, margin, position)
        if margin > capital:
            suggestions.append(f"❌ Ký quỹ cần {margin:.2f}$ > vốn {capital:.2f}$ — không đủ để mở lot {lot}.")
        elif margin > capital * 0.5:
            suggestions.append(f"🔥 Ký quỹ {margin:.2f}$ chiếm >50% vốn — đòn bẩy quá nặng, dễ cháy.")
        # SL có nằm sau giá thanh lý không? (SL phải đóng TRƯỚC khi cháy)
        if position == "LONG" and sl < liq:
            suggestions.append(f"💥 Giá thanh lý {liq:.2f} nằm TRÊN SL {sl:.2f} — sẽ CHÁY trước khi chạm SL! Giảm lot.")
        if position == "SHORT" and sl > liq:
            suggestions.append(f"💥 Giá thanh lý {liq:.2f} nằm DƯỚI SL {sl:.2f} — sẽ CHÁY trước khi chạm SL! Giảm lot.")

    # --- 4) R:R ---
    if rr_ratio < 1.5:
        suggestions.append(f"❌ R:R quá thấp ({rr_ratio:.2f} < 1.5). Sau phí dễ lỗ — không nên vào.")
    elif rr_ratio > 4:
        suggestions.append("🚩 TP khá xa biến động trung bình — cân nhắc chốt lời từng phần.")

    return {
        "rr": round(rr_ratio, 2),
        "suggestions": suggestions,
        "is_valid": rr_ratio >= 1.0,
        "margin_usd": round(margin, 2) if margin is not None else None,
        "liq_price": round(liq, 4) if liq is not None else None,
    }
