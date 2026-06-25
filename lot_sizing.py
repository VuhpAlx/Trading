# lot_sizing.py — Toán khối lượng theo LOT + ký quỹ + thanh lý (kiểu Exness)
# =====================================================================
# Module DÙNG CHUNG cho cả bot tự động (TradeSimulator) và lệnh tay
# (ManualTradeSimulator) để đảm bảo cách tính lot/margin/PnL nhất quán.
#
# KIẾN THỨC NỀN (cho người mới):
#   • LOT       : đơn vị khối lượng. 0.01 lot = "micro lot" (nhỏ nhất).
#   • CONTRACT  : 1 lot = bao nhiêu coin. Crypto Exness thường = 1 coin.
#   • NOTIONAL  : giá trị thật của vị thế = lot × contract × giá (USDT).
#   • MARGIN    : tiền ký quỹ bị "khóa" = notional / đòn bẩy.
#   • ĐÒN BẨY   : cho phép mở vị thế lớn hơn vốn nhiều lần. Lãi/lỗ tính
#                 trên NOTIONAL chứ không phải trên margin → khuếch đại.
#   • THANH LÝ  : khi lỗ ăn gần hết margin (equity ≤ margin × stop-out),
#                 sàn tự đóng lệnh để bảo vệ. "Cháy" một phần/toàn bộ.
# =====================================================================

from config import (
    CONTRACT_SIZE, DEFAULT_CONTRACT_SIZE, LEVERAGE,
    MIN_LOT, LOT_STEP, MAX_LOT, LOT_BASE, LOT_MAX_DYNAMIC,
    MAINTENANCE_MARGIN, STOP_OUT_LEVEL, RISK_CAP_PCT,
)


def get_contract_size(symbol: str) -> float:
    """1 lot = bao nhiêu coin cơ sở của symbol này."""
    return CONTRACT_SIZE.get(symbol, DEFAULT_CONTRACT_SIZE)


def normalize_lot(lot: float) -> float:
    """Làm tròn lot về bội số LOT_STEP và kẹp trong [MIN_LOT, MAX_LOT]."""
    if lot is None or lot <= 0:
        return MIN_LOT
    steps = round(lot / LOT_STEP)
    lot = steps * LOT_STEP
    lot = max(MIN_LOT, min(MAX_LOT, lot))
    # Tránh sai số dấu phẩy động (vd 0.060000000001)
    return round(lot, 2)


def compute_dynamic_lot(confluence: float, bias_strength: float,
                        min_confluence: int = 3, max_confluence: int = 6) -> float:
    """
    Lot ĐỘNG cho bot: tín hiệu càng mạnh → lot càng lớn (ăn nhiều hơn).

    confluence    : số yếu tố kỹ thuật cùng hướng (entry quality).
    bias_strength : độ mạnh xu hướng khung lớn 0..1.

    Nội suy tuyến tính LOT_BASE → LOT_MAX_DYNAMIC theo điểm tổng hợp.
    """
    # Chuẩn hóa confluence về 0..1 trong dải [min_confluence, max_confluence]
    span = max(1, max_confluence - min_confluence)
    conf_norm = max(0.0, min(1.0, (confluence - min_confluence) / span))
    # Kết hợp: 60% chất lượng entry + 40% độ mạnh trend khung lớn
    strength = 0.6 * conf_norm + 0.4 * max(0.0, min(1.0, bias_strength))
    lot = LOT_BASE + (LOT_MAX_DYNAMIC - LOT_BASE) * strength
    return normalize_lot(lot)


def risk_based_lot(capital: float, sl_dist: float, symbol: str) -> float:
    """
    Lot THEO % RỦI RO — RAW (chưa kẹp sàn MIN_LOT, chưa làm tròn step).
    Khoản lỗ tại SL = sl_dist × lot × contract = RISK_CAP_PCT × vốn.
    Dùng để KIỂM TRA: nếu giá trị này < MIN_LOT nghĩa là lot nhỏ nhất giao
    dịch được (0.01) đã vượt hạn mức rủi ro → KHÔNG nên vào lệnh (vốn quá nhỏ
    cho symbol/SL này). Tránh lỗi cũ: normalize_lot kẹp sàn về 0.01 → âm thầm
    nhận rủi ro lớn hơn RISK_CAP_PCT rất nhiều (đặc biệt coin giá cao như BTC).
    """
    contract = get_contract_size(symbol)
    if sl_dist <= 0 or contract <= 0:
        return 0.0
    return (RISK_CAP_PCT * max(capital, 1.0)) / (sl_dist * contract)


def lot_from_risk_cap(capital: float, sl_dist: float, symbol: str, price: float) -> float:
    """
    Lot TỐI ĐA để khoản lỗ khi chạm SL không vượt RISK_CAP_PCT × vốn.
      lỗ_tại_SL = sl_dist × lot × contract  ≤  RISK_CAP_PCT × capital
    Đây là "phanh an toàn" để lot động không bao giờ rủi ro quá mức.
    """
    contract = get_contract_size(symbol)
    if sl_dist <= 0 or contract <= 0:
        return MAX_LOT
    max_loss = RISK_CAP_PCT * max(capital, 1.0)
    lot = max_loss / (sl_dist * contract)
    return normalize_lot(lot)


def margin_required(lot: float, symbol: str, price: float, leverage: float = LEVERAGE) -> float:
    """Ký quỹ cần để mở vị thế = notional / đòn bẩy."""
    contract = get_contract_size(symbol)
    notional = lot * contract * price
    return notional / max(leverage, 1.0)


def notional_value(lot: float, symbol: str, price: float) -> float:
    """Giá trị hợp đồng (USDT) = lot × contract × giá."""
    return lot * get_contract_size(symbol) * price


def pnl_usd(entry: float, exit_price: float, lot: float, symbol: str, direction: str) -> float:
    """
    Lãi/lỗ thực bằng USD theo lot (chưa trừ phí).
      LONG : (exit - entry) × lot × contract
      SHORT: (entry - exit) × lot × contract
    """
    contract = get_contract_size(symbol)
    mult = 1.0 if direction in ("LONG", "BUY") else -1.0
    return (exit_price - entry) * lot * contract * mult


def liquidation_price(entry: float, lot: float, symbol: str, equity: float,
                      used_margin: float, direction: str) -> float:
    """
    Giá thanh lý: mức giá mà tại đó equity rớt xuống = used_margin × STOP_OUT_LEVEL
    (sàn tự đóng lệnh). equity = số dư + lãi/lỗ đang mở.

      lỗ_cho_phép = equity - used_margin × STOP_OUT_LEVEL
      khoảng_giá  = lỗ_cho_phép / (lot × contract)

      LONG : liq = entry - khoảng_giá   (giá rớt → cháy)
      SHORT: liq = entry + khoảng_giá   (giá tăng → cháy)
    """
    contract = get_contract_size(symbol)
    denom = lot * contract
    if denom <= 0:
        return 0.0
    allowable_loss = equity - used_margin * STOP_OUT_LEVEL
    price_move = allowable_loss / denom
    if direction in ("LONG", "BUY"):
        return max(0.0, entry - price_move)
    return entry + price_move


def check_liquidation(equity: float, used_margin: float) -> bool:
    """
    True nếu vị thế bị thanh lý: margin level = equity/used_margin ≤ STOP_OUT_LEVEL.
    equity = vốn còn lại + lãi/lỗ đang mở (floating PnL).
    """
    if used_margin <= 0:
        return False
    margin_level = equity / used_margin
    return margin_level <= STOP_OUT_LEVEL


def margin_level_pct(equity: float, used_margin: float) -> float:
    """Margin level (%) để hiển thị. Càng cao càng an toàn; ≤50% → cháy."""
    if used_margin <= 0:
        return 0.0
    return round((equity / used_margin) * 100.0, 1)
