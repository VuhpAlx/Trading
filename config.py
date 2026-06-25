# config.py — Cấu hình toàn hệ thống
# =====================================================================
# Mọi hằng số cấu hình tập trung tại đây để dễ tinh chỉnh.

# --- Symbols & khung giao dịch ---------------------------------------
SYMBOLS = ["BTCUSDT", "ETHUSDT", "BNBUSDT", "PAXGUSDT"]

# Khung GIAO DỊCH — mỗi khung có 1 engine ra lệnh riêng.
TIMEFRAMES = ["1m", "5m", "15m", "30m", "1h"]

# Khung BỐI CẢNH (context) — chỉ stream để làm "bias" top-down,
# KHÔNG tạo engine giao dịch. Cần cho phân tích khung lớn của 15m/30m/1h.
CONTEXT_TIMEFRAMES = ["4h", "1d"]

# Tất cả khung cần fetch/stream từ Binance (giao dịch + bối cảnh).
ALL_TIMEFRAMES = TIMEFRAMES + CONTEXT_TIMEFRAMES

# --- Top-Down Multi-Timeframe ----------------------------------------
# HTF_MAP: với mỗi khung giao dịch, danh sách 2 khung LỚN HƠN dùng để
# xác định xu hướng chủ đạo (bias). Nguyên tắc trader: "chỉ giao dịch
# THUẬN chiều khung lớn". Khung nhỏ chỉ để canh điểm vào (timing).
HTF_MAP = {
    "1m":  ["15m", "1h"],
    "5m":  ["1h",  "4h"],
    "15m": ["1h",  "4h"],
    "30m": ["4h",  "1d"],
    "1h":  ["4h",  "1d"],
}

# --- Bộ lọc chất lượng tín hiệu (ít lệnh hơn nhưng chất hơn) ----------
MIN_RR_AFTER_FEES = 1.5   # R:R tối thiểu (sau phí) mới cho vào lệnh
MIN_CONFLUENCE    = 3      # Số yếu tố tối thiểu cùng hướng (xem signal_engine)
STRUCTURE_LOOKBACK = 200   # Số nến quét để tìm swing high/low (S/R)
SWING_STRENGTH     = 3      # Fractal: 1 đỉnh/đáy phải cao/thấp hơn N nến 2 bên

# --- Chiến lược BREAKOUT SWING cho VÀNG (PAXG proxy) ------------------
# Bằng chứng backtest (PROJECT_DEEP_DIVE mục 20 & 25): phá kênh Donchian
# THUẬN trend khung lớn trên VÀNG (1h, bias 4h/1d) ĐÁNH BẠI buy-and-hold
# (+97% vs +72%, MaxDD chỉ ~12%) NẾU phí khứ hồi ≤ ~0.03% — mức spread vàng
# Exness Standard thường đạt. WR thấp (~33%) nhưng ăn xa (TP 3R) → kỳ vọng
# DƯƠNG. Symbol nằm trong set này dùng entry BREAKOUT thay cho confluence;
# các symbol khác giữ nguyên logic cũ (bias + confluence + cấu trúc).
# ⚠️ Khuyến nghị dùng ở khung 1h (đã kiểm chứng). Khung nhỏ hơn = nhiễu/phí.
BREAKOUT_SYMBOLS    = {"PAXGUSDT"}
BREAKOUT_DONCHIAN_N = 20     # Số nến tính kênh giá (đỉnh/đáy) để xác định phá vỡ
BREAKOUT_SL_ATR     = 1.5    # SL cách entry 1.5 × ATR
BREAKOUT_RR         = 3.0    # TP = 3R (chốt xa, ôm trend — bù cho WR thấp)
# Trailing cho kế hoạch THAM KHẢO breakout_trail (chỉ HIỂN THỊ, bot KHÔNG đánh).
# Bot tự động vẫn dùng 3R (rủi ro/lãi xác định). breakout_trail = ôm trend, thoát
# khi giá đảo BREAKOUT_TRAIL_ATR × ATR từ đỉnh/đáy thuận lợi — để user tự quyết
# trên tài khoản thật nếu muốn ôm trend lâu hơn (backtest +95.7%, ít nhạy phí).
BREAKOUT_TRAIL_ATR  = 3.0

# --- PHÍ GIAO DỊCH theo SÀN/symbol (Exness) — ƯỚC LƯỢNG, PHẢI hiệu chỉnh! ----
# Chi phí khứ hồi mỗi lệnh ≈ 2×TAKER_FEE + 2×SLIPPAGE (xấp xỉ spread). Chiến lược
# breakout vàng CHỈ có lãi khi phí khứ hồi ≤ ~0.03% (PROJECT_DEEP_DIVE mục 25).
#   • VÀNG (PAXG/XAUUSD) Exness Standard: spread rẻ → ~0.03% khứ hồi.
#   • CRYPTO Exness Standard: spread RỘNG hơn → ~0.10–0.12% khứ hồi.
# ⚠️ Đây là ƯỚC LƯỢNG. Mở MT5/Exness Terminal, xem cột Spread của symbol, lấy
#    (spread ÷ giá) làm phí khứ hồi THẬT rồi sửa lại các số dưới cho khớp tài khoản.
TAKER_FEE_BY_SYMBOL = {
    "PAXGUSDT": 0.00010,   # vàng: ~0.03% khứ hồi (đã gồm slippage) — vùng breakout có lãi
    "BTCUSDT":  0.00050,   # crypto Standard: ~0.11% khứ hồi
    "ETHUSDT":  0.00050,
    "BNBUSDT":  0.00060,
}
DEFAULT_TAKER_FEE = 0.00050   # symbol không có trong map
SLIPPAGE_PER_SIDE = 0.00005   # trượt giá mỗi chiều (thấp hơn Binance giả định cũ)

# --- Forward Testing (kiểm chứng dự đoán hướng) ----------------------
# Sau mỗi nến đóng, hệ thống ghi 1 dự đoán hướng và kiểm chứng sau N nến:
#   WIN  = giá đi ĐÚNG hướng ít nhất FLAT_ATR_MULT × ATR
#   LOSS = giá đi NGƯỢC hướng ít nhất FLAT_ATR_MULT × ATR
#   FLAT = đi ngang trong dải nhiễu (không tính vào hit-rate)
FORWARD_TEST_BARS       = 5     # Nhìn trước bao nhiêu nến để kiểm chứng
FORWARD_FLAT_ATR_MULT   = 0.2   # Ngưỡng "đi ngang" = 0.2 × ATR (lọc nhiễu)
FORWARD_TARGET_ATR_MULT = 0.6   # Target dự kiến = giá ± 0.6 × ATR (vẽ vùng + tham chiếu)

# --- Dữ liệu / cache -------------------------------------------------
HISTORY_LIMIT  = 1000      # Số nến fetch khi bootstrap
MAX_CACHE_SIZE = 1000      # Giữ tối đa 1000 nến trong RAM
INDICATOR_WINDOW = 260     # Tính indicator trên 260 nến cuối (SMA200 cần 200+)

# =====================================================================
# MÔ HÌNH KHỐI LƯỢNG THEO LOT (kiểu sàn Exness)
# =====================================================================
# Lot là đơn vị khối lượng chuẩn của sàn CFD/forex.
#   Khối lượng coin   = lot × CONTRACT_SIZE
#   Giá trị hợp đồng  = lot × CONTRACT_SIZE × giá  (notional, tính bằng USDT)
#   Ký quỹ (margin)   = notional / LEVERAGE
# Ví dụ: BTC giá 80,000, lot 0.01, contract 1.0, đòn bẩy 100
#   notional = 0.01 × 1 × 80,000 = 800$ ; margin = 800 / 100 = 8$.
#   Giá chạy 1% (800$) → lãi/lỗ = 8$ (đòn bẩy khuếch đại theo margin).

# 1 lot = bao nhiêu coin cơ sở. Exness crypto thường 1 lot = 1 coin.
CONTRACT_SIZE = {
    "BTCUSDT": 1.0,
    "ETHUSDT": 1.0,
    "BNBUSDT": 1.0,
    "PAXGUSDT": 1.0,
}
DEFAULT_CONTRACT_SIZE = 1.0   # Dùng khi symbol không có trong map

# Đòn bẩy mặc định (có thể nâng cấp per-symbol sau).
LEVERAGE = 100

# Giới hạn lot
MIN_LOT  = 0.01   # Lot nhỏ nhất (micro lot)
LOT_STEP = 0.01   # Bước nhảy lot
MAX_LOT  = 1.0    # Trần an toàn tuyệt đối

# Dải lot ĐỘNG cho bot tự động — scale theo độ mạnh tín hiệu.
# Tín hiệu yếu → LOT_BASE; tín hiệu rất mạnh → LOT_MAX_DYNAMIC.
LOT_BASE        = 0.01
LOT_MAX_DYNAMIC = 0.10

# --- Margin / thanh lý (liquidation) kiểu Exness ---------------------
MAINTENANCE_MARGIN = 0.005   # 0.5% — ký quỹ duy trì tối thiểu
STOP_OUT_LEVEL     = 0.50    # Margin level ≤ 50% → sàn tự thanh lý lệnh

# Trần rủi ro mỗi lệnh: lot động vẫn bị chặn để khoảng lỗ tới SL
# không vượt RISK_CAP_PCT × vốn (an toàn vốn).
RISK_CAP_PCT = 0.03   # 3% vốn / lệnh
