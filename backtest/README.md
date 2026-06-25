# Backtest Harness — Hướng dẫn

Bộ công cụ kiểm chứng chiến lược trên dữ liệu lịch sử, **tái dùng chính logic
production** (`AdvancedSignalEngine`, `TradeSimulator`) để đo đúng hành vi thật.
Cô lập hoàn toàn: không đụng file `.jsonl` live, không sửa `config.py`.

## Cài đặt
```
pip install scikit-learn      # chỉ cần cho ml_research.py
```
Đã có sẵn: numpy, pandas, pandas-ta, httpx.

## Quy trình chuẩn (luôn chạy từ thư mục gốc dự án)
```bash
export PYTHONIOENCODING=utf-8        # tránh lỗi font tiếng Việt trên Windows
python backtest/fetch_data.py        # tải & cache klines (chạy 1 lần; --force để tải lại)
```
Data cache (.pkl) nằm ở scratchpad, KHÔNG vào repo. Kết quả JSON + log vào `backtest/results/`.

## Các script

| Script | Mục đích | Lệnh ví dụ |
|--------|----------|-----------|
| `fetch_data.py` | Tải klines Binance (1m→1d), cache .pkl | `python backtest/fetch_data.py` |
| `backtest.py` | Replay per-(symbol,TF) qua engine production | `python backtest/backtest.py --tfs 5m,15m,30m,1h` |
| `backtest_symbol.py` | 1 trader / 1 coin (unified MTF), nới gate | `python backtest/backtest_symbol.py --trigger 15m --bias 1h,4h,1d --min-conf 2` |
| `diagnose.py` | Đếm "phễu" quyết định (bias/confluence/RR) | `python backtest/diagnose.py BTCUSDT 1h` |
| `strategy_lab.py` | Thử tín hiệu MỚI (breakout/momentum/pullback/meanrev), sizing risk-%, phí tuỳ chỉnh | `python backtest/strategy_lab.py --trigger 1h --bias 4h,1d --cost 0.0006` |
| `position_trader.py` | Position trading theo trend 1d (full-allocation) | `python backtest/position_trader.py` |
| `walkforward.py` | Độ nhạy phí + nhất quán qua 6 cửa sổ (chống overfit) | `python backtest/walkforward.py` |
| `funding_research.py` | Kiểm sức dự đoán funding rate | `python backtest/funding_research.py` |
| `orderflow_research.py` | Kiểm sức dự đoán order-flow (taker imbalance) | `python backtest/orderflow_research.py` |
| `ml_research.py` | ML đa đặc trưng + walk-forward (cần sklearn) | `python backtest/ml_research.py` |

## Nguyên tắc thiết kế (đọc trước khi sửa)
- **Chống lookahead:** bias khung lớn chỉ dùng nến HTF ĐÃ ĐÓNG tại thời điểm quyết định
  (`searchsorted` trên close-time). ML: purge H giờ cuối train.
- **Phí là tham số sống/chết:** `--cost` (khứ hồi). Binance spot ~0.0024; Exness vàng ~0.0003,
  crypto ~0.0006, standard ~0.001. Kết quả RẤT nhạy với phí — luôn đặt đúng phí tài khoản bạn.
- **Sizing công bằng:** strategy_lab patch CÔ LẬP sang risk-% (lot phân số) để so sánh chéo coin.
- **Bi quan trong nến:** `process_tick` kiểm SL trước TP khi cả hai trong 1 nến (thận trọng).

## Thêm chiến lược mới (trong strategy_lab.py)
1. Thêm 1 nhánh vào `decide_entry(...)` trả `("BUY"|"SELL"|None, strength)`.
2. Thêm 1 mục vào dict `STRATS` với `entry/bias/sl_atr/tp(fixedR|trail|bbmid)/rr`.
3. Chạy `--only <tên>` để test nhanh 1 chiến lược, hoặc bỏ `--only` để chạy hết.
4. **LUÔN** xác thực bằng `walkforward.py` trước khi tin — lãi in-sample dễ là overfit.

## Đọc kết quả
- **pred_winrate / directional accuracy**: < ~52% và AUC ≈ 0.5 → không có sức dự đoán.
- **trade_winrate vs ngưỡng hoà vốn của RR**: WR cần ≥ 1/(1+RR). VD RR 2 → cần ≥33%.
- **vs B&H**: dương = thắng buy-and-hold. Nhưng coi chừng "thắng" do ít phơi nhiễm trong downtrend.
- **MaxDD**: drawdown lớn = rủi ro cao dù lãi dương.
- **+đoạn (walk-forward)**: số cửa sổ dương / tổng. Lãi tập trung 1-2 đoạn = lumpy, không bền.

## Tóm tắt phát hiện (chi tiết: PROJECT_DEEP_DIVE.md mục 17–23)
Kiểm chứng đầy đủ (TA nhiều biến thể + funding + order-flow + ML phi tuyến, walk-forward
nghiêm ngặt, ~2 năm): **không tìm thấy edge bền khai thác được từ data free trên major
crypto.** Giá trị thật: (1) sizing theo % rủi ro (đã fix ở `lot_sizing.py`), (2) bộ lọc
trend 1d để QUẢN TRỊ RỦI RO (Regime Badge trong terminal), (3) chính harness này để tự
kiểm ý tưởng tương lai một cách trung thực.
