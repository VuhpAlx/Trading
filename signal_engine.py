import asyncio
import pandas as pd
import json
import logging
import os
from datetime import datetime
from typing import Dict, List, Optional

from simulator_manager import ManualTradeSimulator
from prediction_engine import PredictionEngine
import market_structure
import htf_bias
import lot_sizing
from config import (
    HTF_MAP, MIN_RR_AFTER_FEES, MIN_CONFLUENCE,
    STRUCTURE_LOOKBACK, SWING_STRENGTH, LEVERAGE,
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("SignalEngine")


# ==========================================
# 1. ADAPTIVE SCORER — Self-training ML
# ==========================================
class AdaptiveScorer:
    """
    Learns which indicators are predictive for this symbol/timeframe by
    comparing predictions against real outcomes and adjusting weights.
    Uses exponential smoothing so recent results matter more than old ones.

    NOTE (v5): reverse_mode KHÔNG còn được dùng để đảo lệnh trong quyết định
    (đã thay bằng cổng BIAS top-down). Vẫn giữ winrate/learning để hiển thị
    và tinh chỉnh trọng số các chỉ báo trong breakdown.
    """

    SIGNAL_KEYS = ['EMA', 'MACD', 'RSI', 'BB', 'VWAP', 'VOLUME', 'STOCH', 'MTF']

    def __init__(self, symbol: str, timeframe: str):
        self.symbol = symbol
        self.timeframe = timeframe
        self.weights: Dict[str, float] = {k: 1.0 for k in self.SIGNAL_KEYS}
        self.weights['MTF'] = 1.5   # Multi-timeframe starts with higher prior

        self.pending_predictions: Dict[int, dict] = {}
        self.recent_results: List[dict] = []
        self.ml_log_file = f"ml_training_data_{symbol}_{timeframe}.jsonl"

        # EMA-smoothed win stats (decay = 0.97 → half-life ~23 trades)
        self.ema_winrate: float = 50.0
        self.stats = {"win": 0, "loss": 0, "partial": 0, "total": 0}
        self.reverse_mode: bool = False

        # Batch learning buffer — weights only update every 20 evaluations
        # to learn from trend rather than noise of individual predictions
        self._learning_buffer: List[tuple] = []  # [(raw_sigs, direction, factor), ...]
        self._batch_size: int = 20

        self._load_history()

    # ---------- history ----------

    def _load_history(self):
        if not os.path.exists(self.ml_log_file):
            with open(self.ml_log_file, "w") as f:
                pass
            return
        try:
            with open(self.ml_log_file, "r", encoding="utf-8") as f:
                lines = f.readlines()

            for line in lines[-500:]:
                line = line.strip()
                if not line:
                    continue
                try:
                    data = json.loads(line)
                except json.JSONDecodeError:
                    continue

                try:
                    res       = data.get("result")
                    pred      = data.get("prediction") or {}
                    direction = pred.get("direction") or ""
                    raw_sigs  = data.get("raw_signals") or {}
                    if not raw_sigs or not res:
                        continue

                    self._apply_result_to_stats(res)
                    factor = self._result_factor(res)
                    self._adjust_weights(raw_sigs, direction, factor, lr=0.01)

                    # Build recent_results display entry
                    rng = pred.get("range") or {}
                    try:
                        rng_str = (
                            f"{float(rng.get('min', 0)):.2f} - "
                            f"{float(rng.get('max', 0)):.2f}"
                        )
                    except (TypeError, ValueError):
                        rng_str = "N/A"

                    t_str = data.get("time") or ""
                    if "T" in t_str:
                        try:
                            t_str = datetime.fromisoformat(t_str).strftime('%H:%M:%S')
                        except Exception:
                            pass

                    self.recent_results.insert(0, {
                        "time": t_str, "direction": direction, "range": rng_str,
                        "actual_price": data.get("actual_price") or 0,
                        "final_result": res,
                    })
                except Exception as e:
                    logger.debug(f"Skip malformed history entry: {e}")
                    continue

            self.recent_results = self.recent_results[:15]
            self._recalc_ema_winrate()
            self._check_reverse_mode()

        except Exception as e:
            logger.error(f"History load error {self.symbol}/{self.timeframe}: {e}")

    # ---------- weight helpers ----------

    def _result_factor(self, result: str) -> float:
        return {"WIN": 1.0, "PARTIAL": 0.2, "LOSS": -0.8}.get(result, 0.0)

    def _adjust_weights(self, raw_sigs: dict, direction: str, factor: float, lr: float = 0.05):
        for ind, val in raw_sigs.items():
            if ind not in self.weights:
                self.weights[ind] = 1.0
            is_correct = (val > 0 and "BULL" in direction.upper()) or \
                         (val < 0 and "BEAR" in direction.upper())
            if is_correct:
                self.weights[ind] *= (1 + lr * factor)
            self.weights[ind] = max(0.2, min(3.0, self.weights[ind]))

    def _apply_result_to_stats(self, result: str):
        self.stats["total"] += 1
        if result == "WIN":
            self.stats["win"] += 1
        elif result == "LOSS":
            self.stats["loss"] += 1
        elif result == "PARTIAL":
            self.stats["partial"] += 1

    def _recalc_ema_winrate(self):
        alpha = 0.15
        for r in reversed(self.recent_results):
            is_win = 1.0 if r.get("final_result") == "WIN" else 0.0
            self.ema_winrate = alpha * is_win * 100 + (1 - alpha) * self.ema_winrate

    def _check_reverse_mode(self):
        # Giữ lại để theo dõi winrate (chỉ hiển thị cảnh báo), KHÔNG đảo lệnh.
        total = self.stats["win"] + self.stats["loss"]
        if total >= 20:
            wr = self.get_winrate()
            if wr < 38.0:
                self.reverse_mode = True
            elif wr >= 47.0:
                self.reverse_mode = False

    # ---------- public ----------

    def get_weights(self) -> Dict[str, float]:
        total = sum(self.weights.values())
        return {k: round(v / total, 4) for k, v in self.weights.items()}

    def get_winrate(self) -> float:
        total = self.stats["win"] + self.stats["loss"]
        return round((self.stats["win"] / total) * 100, 1) if total > 0 else 0.0

    def get_ema_winrate(self) -> float:
        return round(self.ema_winrate, 1)

    def register_prediction(self, target_time: int, payload: dict):
        self.pending_predictions[target_time] = payload

    def evaluate_and_learn(self, current_time_sec: int, current_close: float):
        resolved = []
        for target_time, p in list(self.pending_predictions.items()):
            if current_time_sec < target_time:
                continue

            direction = p['direction']
            move = current_close - p['start_price']
            correct_dir = (direction.startswith("BULL") and move > 0) or \
                          (direction.startswith("BEAR") and move < 0)
            hit_range = p['range']['min'] <= current_close <= p['range']['max']

            result = "WIN" if (hit_range and correct_dir) else ("PARTIAL" if correct_dir else "LOSS")

            self._apply_result_to_stats(result)
            factor = self._result_factor(result)

            # Buffer result instead of updating weights immediately.
            self._learning_buffer.append((p['raw_signals'], direction, factor))

            # EMA winrate updates immediately (display only)
            alpha = 0.15
            is_win = 1.0 if result == "WIN" else 0.0
            self.ema_winrate = alpha * is_win * 100 + (1 - alpha) * self.ema_winrate
            self._check_reverse_mode()

            self.recent_results.insert(0, {
                "time": datetime.fromtimestamp(target_time).strftime('%H:%M:%S'),
                "direction": direction,
                "range": f"{p['range']['min']:.2f} - {p['range']['max']:.2f}",
                "actual_price": round(current_close, 2),
                "final_result": result,
            })
            self.recent_results = self.recent_results[:15]

            log_entry = {
                "time": datetime.fromtimestamp(target_time).isoformat(),
                "symbol": self.symbol, "timeframe": self.timeframe,
                "prediction": {"direction": direction, "range": p['range'],
                               "confidence": p.get('confidence', 0)},
                "signal": p.get("signal"), "score": p.get("score"),
                "raw_signals": p['raw_signals'],
                "indicators_snapshot": p.get("indicators_snapshot", {}),
                "actual_price": round(current_close, 4),
                "result": result,
            }
            self._async_write(self.ml_log_file, log_entry)

            resolved.append(target_time)

        for k in resolved:
            del self.pending_predictions[k]

        if len(self._learning_buffer) >= self._batch_size:
            for raw_sigs, direction, factor in self._learning_buffer:
                self._adjust_weights(raw_sigs, direction, factor, lr=0.05)
            self._learning_buffer.clear()
            logger.info(
                f"[{self.symbol}/{self.timeframe}] Batch weight update: "
                f"{self._batch_size} predictions processed"
            )

    # ---------- async file write ----------

    @staticmethod
    def _write_line_sync(path: str, line: str):
        try:
            with open(path, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except Exception:
            pass

    def _async_write(self, path: str, data: dict):
        line = json.dumps(data)
        try:
            loop = asyncio.get_running_loop()
            loop.run_in_executor(None, self._write_line_sync, path, line)
        except RuntimeError:
            self._write_line_sync(path, line)

    def learn_from_real_trade(self, snapshot: dict, direction: str, result: str):
        factor = 1.2 if result == "WIN" else -1.0
        bull_dir = direction == "LONG"
        for ind, val in snapshot.items():
            if ind not in self.weights:
                continue
            is_correct = (val > 0 and bull_dir) or (val < 0 and not bull_dir)
            if is_correct:
                self.weights[ind] *= (1 + 0.05 * factor)
            self.weights[ind] = max(0.2, min(3.0, self.weights[ind]))


# ==========================================
# 2. TRADE SIMULATOR — Auto execution (LOT-BASED, kiểu Exness)
# ==========================================
class TradeSimulator:
    # Market order costs — applied to both entry and exit fills
    TAKER_FEE = 0.0010   # 0.1% market taker fee per side (Binance/Exness ~VIP0)
    SLIPPAGE  = 0.0002   # 0.02% market impact per fill

    def __init__(self, symbol: str, timeframe: str, capital: float = 100.0):
        self.symbol = symbol
        self.timeframe = timeframe
        self.capital = capital          # số dư (balance)
        self.state = "NONE"
        self.trade: dict = {}
        self.history: List[dict] = []
        self.cooldown: int = 0
        self.just_opened: bool = False
        self.notifications: List[str] = []
        self.trade_log_file = f"trade_history_{symbol}_{timeframe}.jsonl"
        self._load_history()

    def _load_history(self):
        if not os.path.exists(self.trade_log_file):
            with open(self.trade_log_file, "w") as f:
                pass
            return
        try:
            with open(self.trade_log_file, "r", encoding="utf-8") as f:
                lines = f.readlines()
            for line in lines[-10:]:
                line = line.strip()
                if not line:
                    continue
                try:
                    data = json.loads(line)
                    self.history.insert(0, data)
                except json.JSONDecodeError:
                    pass
            if self.history:
                self.capital = self.history[0].get("capital_after", self.capital)
        except Exception:
            pass

    @staticmethod
    def _write_line_sync(path: str, line: str):
        try:
            with open(path, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except Exception:
            pass

    def _async_write(self, path: str, data: dict):
        line = json.dumps(data)
        try:
            loop = asyncio.get_running_loop()
            loop.run_in_executor(None, self._write_line_sync, path, line)
        except RuntimeError:
            self._write_line_sync(path, line)

    def set_capital(self, value: float):
        self.capital = max(1.0, float(value))

    def open_position(self, timestamp: str, side: str, price: float, sl: float, tp: float,
                      confluence: float, bias_strength: float, indicators_snap: dict) -> bool:
        """
        Mở vị thế với SL/TP đã tính theo CẤU TRÚC (truyền từ generate_signal) và
        khối lượng LOT ĐỘNG theo độ mạnh tín hiệu. Trả về True nếu mở thành công.
        """
        if self.state != "NONE" or self.cooldown > 0:
            return False

        direction = "LONG" if side == "BUY" else "SHORT"
        mult = 1 if direction == "LONG" else -1

        # Market fill có slippage
        actual_entry = price * (1 + mult * self.SLIPPAGE)
        sl_dist = abs(actual_entry - sl)
        if sl_dist <= 0:
            return False

        # --- LOT ĐỘNG: tín hiệu càng mạnh lot càng lớn, nhưng bị chặn rủi ro ---
        dyn_lot = lot_sizing.compute_dynamic_lot(confluence, bias_strength,
                                                 min_confluence=MIN_CONFLUENCE)
        cap_lot = lot_sizing.lot_from_risk_cap(self.capital, sl_dist, self.symbol, actual_entry)
        lot = min(dyn_lot, cap_lot)

        # --- Kiểm tra ký quỹ: hạ lot cho vừa free margin, bỏ lệnh nếu không đủ ---
        margin = lot_sizing.margin_required(lot, self.symbol, actual_entry, LEVERAGE)
        if margin > self.capital:
            # Hạ về lot tối đa mà margin ≤ vốn
            affordable = lot_sizing.normalize_lot(
                self.capital * LEVERAGE / (lot_sizing.get_contract_size(self.symbol) * actual_entry)
            )
            lot = min(lot, affordable)
            margin = lot_sizing.margin_required(lot, self.symbol, actual_entry, LEVERAGE)
            if margin > self.capital or lot < lot_sizing.MIN_LOT:
                self.notifications.append("⚠️ Không đủ ký quỹ để mở lệnh — bỏ qua.")
                return False

        notional = lot_sizing.notional_value(lot, self.symbol, actual_entry)
        entry_fee = notional * self.TAKER_FEE
        liq_price = lot_sizing.liquidation_price(
            actual_entry, lot, self.symbol, self.capital, margin, direction
        )

        self.state = direction
        self.trade = {
            "entry_time": timestamp,
            "entry": round(actual_entry, 4),
            "position": direction,
            "sl": round(sl, 4), "tp": round(tp, 4),
            "lot": lot,
            "contract_size": lot_sizing.get_contract_size(self.symbol),
            "leverage": LEVERAGE,
            "margin_usd": round(margin, 4),
            "notional": round(notional, 2),
            "liq_price": round(liq_price, 4),
            "entry_fee": entry_fee,
            "snapshot": indicators_snap,
            "pnl_pct": 0.0, "profit_usd": 0.0,
        }
        self.just_opened = True
        self.notifications.append(
            f"🟢 OPEN {direction} | Lot {lot:.2f} (x{LEVERAGE})\n"
            f"Entry: {actual_entry:.2f} | Margin: {margin:.2f}$\n"
            f"TP: {tp:.2f} | SL: {sl:.2f} | Liq: {liq_price:.2f}"
        )
        return True

    def process_tick(self, current_price: float, high: float, low: float) -> Optional[dict]:
        if self.state == "NONE":
            return None
        if self.just_opened:
            self.just_opened = False
            return None

        t    = self.trade
        mult = 1 if self.state == "LONG" else -1
        lot  = t['lot']

        # Live mark-to-market P&L (lot-based) — net of entry fee + est. exit fee
        gross_live   = lot_sizing.pnl_usd(t['entry'], current_price, lot, self.symbol, self.state)
        exit_fee_est = lot_sizing.notional_value(lot, self.symbol, current_price) * self.TAKER_FEE
        net_live     = gross_live - t['entry_fee'] - exit_fee_est
        t['profit_usd'] = round(net_live, 2)
        # PnL% so với margin (đòn bẩy) — sát cảm nhận "lời/lỗ trên ký quỹ" của Exness
        t['pnl_pct'] = round((net_live / (t['margin_usd'] + 1e-9)) * 100, 2)

        equity = self.capital + net_live

        is_closed = False
        res       = ""
        exit_p    = current_price

        # --- 1) THANH LÝ (liquidation) — ưu tiên kiểm tra trước TP/SL ---
        liq_hit = (self.state == "LONG"  and low  <= t['liq_price']) or \
                  (self.state == "SHORT" and high >= t['liq_price'])
        if liq_hit or lot_sizing.check_liquidation(equity, t['margin_usd']):
            is_closed = True
            res = "LIQUIDATED"
            exit_p = t['liq_price']

        # --- 2) SL / TP ---
        if not is_closed:
            sl_buf = t['entry'] * 0.0002
            if self.state == "LONG":
                if low <= (t['sl'] - sl_buf):
                    is_closed = True; res = "LOSS"; exit_p = t['sl'] * (1 - self.SLIPPAGE)
                elif high >= t['tp']:
                    is_closed = True; res = "WIN";  exit_p = t['tp'] * (1 - self.SLIPPAGE)
            else:
                if high >= (t['sl'] + sl_buf):
                    is_closed = True; res = "LOSS"; exit_p = t['sl'] * (1 + self.SLIPPAGE)
                elif low <= t['tp']:
                    is_closed = True; res = "WIN";  exit_p = t['tp'] * (1 + self.SLIPPAGE)

        if not is_closed:
            return None

        # Net profit (lot-based) sau cả 2 chiều phí
        exit_fee     = lot_sizing.notional_value(lot, self.symbol, exit_p) * self.TAKER_FEE
        gross_profit = lot_sizing.pnl_usd(t['entry'], exit_p, lot, self.symbol, self.state)
        net_profit   = gross_profit - t['entry_fee'] - exit_fee
        final_pnl    = (net_profit / (t['margin_usd'] + 1e-9)) * 100
        total_fees   = t['entry_fee'] + exit_fee

        cap_before    = self.capital
        self.capital  = max(0.0, self.capital + net_profit)

        log_entry = {
            "symbol": self.symbol, "timeframe": self.timeframe,
            "entry_time": t["entry_time"], "exit_time": datetime.now().isoformat(),
            "entry_price": t["entry"], "exit_price": round(exit_p, 4),
            "position": t["position"],
            "lot": lot, "leverage": t["leverage"],
            "contract_size": t["contract_size"], "margin_usd": t["margin_usd"],
            "liq_price": t["liq_price"],
            "capital_before": round(cap_before, 2), "capital_after": round(self.capital, 2),
            "fees_usd": round(total_fees, 4),
            "profit_usd": round(net_profit, 2), "pnl": round(final_pnl, 2), "result": res,
        }
        self._async_write(self.trade_log_file, log_entry)

        self.history.insert(0, log_entry)
        self.history = self.history[:20]

        icon = "💥" if res == "LIQUIDATED" else ("🔴" if res == "LOSS" else "🔵")
        self.notifications.append(
            f"{icon} CLOSED {res} | Lot {lot:.2f}\n"
            f"Net: {net_profit:.2f}$ ({final_pnl:.1f}% margin)\n"
            f"Fees: {total_fees:.3f}$"
        )

        closed_data = {"snapshot": t["snapshot"], "direction": self.state, "result": res}
        self.state    = "NONE"
        self.trade    = {}
        self.cooldown = 5
        return closed_data

    def get_notifications(self) -> List[str]:
        msgs = self.notifications.copy()
        self.notifications.clear()
        return msgs


# ==========================================
# 3. MASTER SIGNAL ENGINE — Top-Down + Cấu trúc
# ==========================================
class AdvancedSignalEngine:
    def __init__(self, symbol: str, interval: str):
        self.symbol   = symbol
        self.interval = interval
        self.scorer     = AdaptiveScorer(symbol, interval)
        self.trade_sim  = TradeSimulator(symbol, interval, capital=100.0)
        self.manual_sim = ManualTradeSimulator(symbol)
        self.predictor  = PredictionEngine(atr_multiplier=1.2, smooth_period=5, max_width_pct=0.02)

        self.smoothed_score: float = 0.0
        self.confirm_counter: int  = 0
        self.current_dir: str      = "HOLD"
        self.last_candle_time      = None
        self.hold_counter: int     = 0

        self.ui_state = {
            "action": "HOLD", "signal": "HOLD", "score": 0.0,
            "confidence": 0.0, "reason": "Initializing...",
            "regime": "NEUTRAL",
            "htf_bias": {"bias": "NEUTRAL", "strength": 0.0, "per_tf": []},
            "structure": {},
            "reasons": [],
            "prediction": {
                "direction": "UNKNOWN", "mid_price": 0.0,
                "range": {"min": 0.0, "max": 0.0},
            },
            "indicators": {}, "raw_signals": {},
            "weights": self.scorer.get_weights(),
            "winrate": 0.0, "tracker": [], "notifications": [],
        }

    def set_capital(self, value: float):
        self.trade_sim.set_capital(value)
        self.manual_sim.set_capital(value)

    # ---------- helpers ----------

    @staticmethod
    def _safe(value, default: float = 0.0) -> float:
        try:
            v = float(value)
            return v if pd.notna(v) and not (v != v) else default
        except Exception:
            return default

    @staticmethod
    def _tf_to_sec(tf: str) -> int:
        unit = tf[-1]
        val  = int(tf[:-1])
        if unit == 'h':
            return val * 3600
        if unit == 'd':
            return val * 86400
        return val * 60

    @staticmethod
    def _detect_regime(adx: float, bb_width: float, di_plus: float, di_minus: float) -> str:
        if bb_width < 0.0025:
            return "SQUEEZE"
        if adx > 25 and abs(di_plus - di_minus) > 5:
            return "TREND"
        if adx < 18:
            return "RANGE"
        return "NEUTRAL"

    # ---------- main ----------

    def generate_signal(self, df: pd.DataFrame, mtf_context: dict) -> dict:
        """
        mtf_context: dict {tf: df_indicators} cho các khung trong HTF_MAP[interval]
        (khung lớn hơn) + có thể chứa các khung khác để vẽ pivot.
        """
        if df is None or len(df) < 5:
            return self.ui_state

        curr  = df.iloc[-1]
        prev  = df.iloc[-2]
        c_price = self._safe(curr.get('close'))
        c_high  = self._safe(curr.get('high'),  c_price)
        c_low   = self._safe(curr.get('low'),   c_price)
        c_vol   = self._safe(curr.get('volume'), 0)
        c_time  = int(curr['timestamp'].timestamp())
        c_time_str = datetime.fromtimestamp(c_time).strftime('%H:%M:%S')

        # --- Extract indicators ---
        ema9   = self._safe(curr.get('EMA_9'),   c_price)
        ema21  = self._safe(curr.get('EMA_21'),  c_price)
        ema50  = self._safe(curr.get('EMA_50'),  c_price)
        ema9_p = self._safe(prev.get('EMA_9'),   ema9)

        rsi      = self._safe(curr.get('RSI_14'), 50.0)
        rsi_p    = self._safe(prev.get('RSI_14'), rsi)
        macd_h   = self._safe(curr.get('MACD_H'), 0.0)
        macd_h_p = self._safe(prev.get('MACD_H'), 0.0)

        bb_u  = self._safe(curr.get('BB_U'), c_price * 1.02)
        bb_l  = self._safe(curr.get('BB_L'), c_price * 0.98)
        atr   = self._safe(curr.get('ATR_14'), c_price * 0.002)
        vwap  = self._safe(curr.get('VWAP'),  c_price)

        vol_ma  = self._safe(curr.get('VOL_MA'), max(c_vol, 1.0))
        adx     = self._safe(curr.get('ADX'),      20.0)
        di_plus = self._safe(curr.get('DI_PLUS'),  20.0)
        di_minus= self._safe(curr.get('DI_MINUS'), 20.0)
        stoch_k = self._safe(curr.get('STOCH_K'), 50.0)
        stoch_d = self._safe(curr.get('STOCH_D'), 50.0)
        stoch_k_p = self._safe(prev.get('STOCH_K'), stoch_k)

        slope      = ema9 - ema9_p
        bb_width   = (bb_u - bb_l) / (c_price + 1e-9)
        vol_ratio  = c_vol / (vol_ma + 1e-9)
        price_chg  = c_price - self._safe(prev.get('close'), c_price)

        # ====================================================
        # Chỉ chạy logic đầy đủ khi NẾN MỚI mở
        # ====================================================
        if self.last_candle_time != c_time:
            self.scorer.evaluate_and_learn(c_time, float(curr['close']))

            if self.trade_sim.cooldown > 0:
                self.trade_sim.cooldown -= 1

            # ============ LỚP 1: BIAS TOP-DOWN (khung lớn) ============
            htf_tfs = HTF_MAP.get(self.interval, [])
            per_tf_bias = []
            for tf in htf_tfs:
                df_tf = mtf_context.get(tf)
                b = htf_bias.compute_bias(df_tf)
                b["timeframe"] = tf
                per_tf_bias.append(b)
            bias_agg = htf_bias.aggregate_bias(per_tf_bias)
            bias_dir = bias_agg["bias"]        # BULL / BEAR / NEUTRAL
            bias_strength = bias_agg["strength"]

            # MTF raw signal (giữ cho breakdown + learning, không còn quyết định)
            mtf_score = bias_agg["score"]

            # ============ LỚP 2: CẤU TRÚC (S/R, pivot) ============
            # Nguồn pivot: khung lớn nhất có trong context (vd 1d > 4h > 1h...)
            pivot_src = None
            for tf in reversed(htf_tfs):
                if mtf_context.get(tf) is not None:
                    pivot_src = mtf_context.get(tf)
                    break
            structure = market_structure.analyze(
                df, htf_df_for_pivot=pivot_src,
                strength=SWING_STRENGTH, lookback=STRUCTURE_LOOKBACK,
            )

            # ============ 8 RAW SIGNALS (breakdown + learning) ============
            ema_cross = 1.0 if ema9 > ema21 else -1.0
            slope_norm = slope / (atr + 1e-9) * 3
            ema_score = ema_cross * min(1.0, 0.55 + abs(slope_norm) * 0.45)

            macd_norm = max(-1.0, min(1.0, macd_h / (atr * 0.3 + 1e-9)))
            macd_cross = 0.0
            if macd_h > 0 and macd_h_p <= 0:
                macd_cross = 0.5
            elif macd_h < 0 and macd_h_p >= 0:
                macd_cross = -0.5
            macd_score = max(-1.0, min(1.0, macd_norm + macd_cross))

            rsi_score = max(-1.0, min(1.0, (rsi - 50) / 20))

            if bb_width < 0.002:
                bb_score = 0.0
            else:
                band_pos = (c_price - bb_l) / (bb_u - bb_l + 1e-9)
                bb_score = max(-1.0, min(1.0, (0.5 - band_pos) * 2))

            vwap_score = 1.0 if c_price > vwap else -1.0

            if vol_ratio > 1.2:
                vol_dir = 1.0 if price_chg > 0 else -1.0
                vol_score = vol_dir * min(1.0, (vol_ratio - 1.0) * 0.8)
            else:
                vol_score = 0.0

            if stoch_k < 25 and stoch_d < 25:
                stoch_score = 0.8 if stoch_k > stoch_k_p else 0.5
            elif stoch_k > 75 and stoch_d > 75:
                stoch_score = -0.8 if stoch_k < stoch_k_p else -0.5
            elif stoch_k > stoch_d and stoch_k < 50:
                stoch_score = 0.45
            elif stoch_k < stoch_d and stoch_k > 50:
                stoch_score = -0.45
            else:
                stoch_score = 0.0

            raw_sigs = {
                'EMA':    round(ema_score,   3), 'MACD':   round(macd_score,  3),
                'RSI':    round(rsi_score,   3), 'BB':     round(bb_score,    3),
                'VWAP':   round(vwap_score,  3), 'VOLUME': round(vol_score,   3),
                'STOCH':  round(stoch_score, 3), 'MTF':    round(mtf_score,   3),
            }

            weights   = self.scorer.get_weights()
            raw_score = sum(raw_sigs[k] * weights.get(k, 1.0 / len(raw_sigs)) for k in raw_sigs)
            self.smoothed_score = (0.35 * raw_score) + (0.65 * self.smoothed_score)

            regime = self._detect_regime(adx, bb_width, di_plus, di_minus)

            # ============ LỚP 3: CONFLUENCE (chất lượng entry) ============
            ctx = {
                "ema9": ema9, "ema21": ema21, "ema50": ema50, "price": c_price,
                "macd_h": macd_h, "macd_h_p": macd_h_p,
                "rsi": rsi, "rsi_p": rsi_p,
                "stoch_k": stoch_k, "stoch_d": stoch_d,
                "vol_ratio": vol_ratio, "price_chg": price_chg,
                "structure": structure, "atr": atr,
            }
            buy_conf,  buy_reasons  = self._confluence("BUY",  ctx)
            sell_conf, sell_reasons = self._confluence("SELL", ctx)

            # ============ QUYẾT ĐỊNH (cổng BIAS + confluence + R:R) ============
            action = "HOLD"
            reasons: List[str] = []
            reasons.append(self._bias_reason(bias_agg))

            # Hướng được phép theo bias
            if bias_dir == "BULL":
                allowed = ["BUY"]
            elif bias_dir == "BEAR":
                allowed = ["SELL"]
            else:
                allowed = []   # NEUTRAL: mặc định đứng ngoài
            req_conf = MIN_CONFLUENCE

            # Chọn ứng viên trong hướng được phép
            cand = None
            if "BUY" in allowed and buy_conf >= req_conf:
                cand = ("BUY", buy_conf, buy_reasons)
            elif "SELL" in allowed and sell_conf >= req_conf:
                cand = ("SELL", sell_conf, sell_reasons)

            if bias_dir == "NEUTRAL":
                reasons.append("⏸️ Khung lớn trung lập → ưu tiên đứng ngoài")

            sl = tp = None
            rr = 0.0
            if cand and self.trade_sim.cooldown == 0 and self.trade_sim.state == "NONE":
                side, conf, side_reasons = cand
                reasons.extend(side_reasons)

                sl, tp = self._structure_sl_tp(side, c_price, atr, structure)
                rr = self._rr_after_fees(side, c_price, sl, tp)
                reasons.append(f"🎯 TP {tp:.2f} / SL {sl:.2f} → R:R {rr:.2f}")

                if rr < MIN_RR_AFTER_FEES:
                    reasons.append(f"❌ R:R {rr:.2f} < {MIN_RR_AFTER_FEES} (sau phí) → BỎ lệnh")
                else:
                    # Xác nhận 2 nến liên tiếp cùng hướng (giảm nhiễu)
                    if side == self.current_dir:
                        self.confirm_counter += 1
                    else:
                        self.confirm_counter = 1
                        self.current_dir = side

                    if self.confirm_counter >= 2:
                        opened = self.trade_sim.open_position(
                            c_time_str, side, c_price, sl, tp,
                            confluence=conf, bias_strength=bias_strength,
                            indicators_snap=raw_sigs,
                        )
                        if opened:
                            action = side
                            reasons.append(f"✅ VÀO LỆNH {side} (lot động theo tín hiệu)")
                    else:
                        reasons.append(f"⏳ Chờ xác nhận nến 2 ({self.confirm_counter}/2)")
            else:
                self.current_dir = "HOLD"
                self.confirm_counter = 0
                if cand is None and bias_dir != "NEUTRAL":
                    need = buy_conf if bias_dir == "BULL" else sell_conf
                    reasons.append(f"👀 Confluence {need}/{req_conf} chưa đủ — chờ điểm vào đẹp")
                if self.trade_sim.cooldown > 0:
                    reasons.append(f"⏳ Cooldown {self.trade_sim.cooldown} nến")
                if self.trade_sim.state != "NONE":
                    reasons.append("📌 Đang có lệnh mở — không vào thêm")

            self.hold_counter = self.hold_counter + 1 if self.trade_sim.state == "NONE" else 0

            # ============ DỰ ĐOÁN (hướng theo bias) ============
            if bias_dir == "BULL":
                p_dir = "BULLISH"
            elif bias_dir == "BEAR":
                p_dir = "BEARISH"
            else:
                p_dir = "BULLISH" if slope > 0 else "BEARISH"

            pe_dir = "BULL" if p_dir == "BULLISH" else "BEAR"
            pred_result = self.predictor.predict(
                close_price=c_price, ema9=ema9, atr=atr,
                bb_upper=bb_u, bb_lower=bb_l,
                momentum_score=self.smoothed_score, direction=pe_dir,
            )
            conf_pred = pred_result["confidence"]
            pred = {"direction": p_dir, "mid_price": pred_result["mid_price"],
                    "range": pred_result["range"]}

            indicators_snap = {
                "EMA_9": round(ema9, 4), "EMA_21": round(ema21, 4),
                "RSI": round(rsi, 2),    "MACD": round(macd_h, 6),
                "ATR": round(atr, 4),    "BB_U": round(bb_u, 4), "BB_L": round(bb_l, 4),
                "ADX": round(adx, 1),    "DI_PLUS": round(di_plus, 1), "DI_MINUS": round(di_minus, 1),
                "STOCH_K": round(stoch_k, 1), "STOCH_D": round(stoch_d, 1),
                "VWAP": round(vwap, 4),  "VOL_RATIO": round(vol_ratio, 2),
            }

            self.scorer.register_prediction(
                c_time + (self._tf_to_sec(self.interval) * 5),
                {
                    "direction": p_dir, "range": pred['range'], "confidence": conf_pred,
                    "start_price": c_price, "raw_signals": raw_sigs,
                    "indicators_snapshot": indicators_snap, "atr_snapshot": atr,
                    "signal": action, "score": self.smoothed_score,
                },
            )

            # Score hiển thị: confluence có dấu, chuẩn hóa về [-1,1]
            disp_conf = buy_conf if buy_conf >= sell_conf else -sell_conf
            disp_score = round(max(-1.0, min(1.0, disp_conf / 6.0)), 3)

            # Tracker
            pending_list = [
                {
                    "time": datetime.fromtimestamp(k).strftime('%H:%M:%S'),
                    "direction": v['direction'],
                    "range": f"{v['range']['min']:.2f} - {v['range']['max']:.2f}",
                    "actual_price": 0.0, "final_result": "WAITING",
                }
                for k, v in list(self.scorer.pending_predictions.items())
            ]
            combined_tracker = sorted(
                pending_list + self.scorer.recent_results[:15],
                key=lambda x: x["time"], reverse=True,
            )

            self.ui_state.update({
                "action": action, "signal": action,
                "score": disp_score, "confidence": round(conf_pred, 3),
                "prediction": pred, "indicators": indicators_snap,
                "raw_signals": raw_sigs, "reason": (reasons[-1] if reasons else "Monitoring..."),
                "reasons": reasons,
                "htf_bias": {"bias": bias_dir, "strength": bias_strength,
                             "score": bias_agg["score"], "per_tf": per_tf_bias},
                "structure": structure,
                "weights": weights,
                "winrate": self.scorer.get_winrate(),
                "ema_winrate": self.scorer.get_ema_winrate(),
                "tracker": combined_tracker,
                "regime": regime,
            })
            self.last_candle_time = c_time

        # ============ OUTPUT (always fresh prices) ============
        res_out = self.ui_state.copy()
        res_out["notifications"]  = self.trade_sim.get_notifications()
        res_out["trade_history"]  = self.trade_sim.history[:10]

        res_out["indicators"] = {
            "EMA_9": round(ema9, 4),   "EMA_21": round(ema21, 4),
            "RSI": round(rsi, 2),      "MACD": round(macd_h, 6),
            "ATR": round(atr, 4),      "BB_U": round(bb_u, 4), "BB_L": round(bb_l, 4),
            "ADX": round(adx, 1),      "DI_PLUS": round(di_plus, 1), "DI_MINUS": round(di_minus, 1),
            "STOCH_K": round(stoch_k, 1), "STOCH_D": round(stoch_d, 1),
            "VWAP": round(vwap, 4),    "VOL_RATIO": round(vol_ratio, 2),
        }

        t = self.trade_sim.trade
        is_open = self.trade_sim.state != "NONE"
        equity = round(self.trade_sim.capital + (t.get('profit_usd', 0.0) if is_open else 0.0), 2)
        m_level = lot_sizing.margin_level_pct(equity, t.get('margin_usd', 0.0)) if is_open else 0.0
        res_out["trade"] = {
            "position_status": "OPEN" if is_open else "NONE",
            "entry":       round(t.get('entry',      0.0), 4) if is_open else None,
            "tp":          round(t.get('tp',          0.0), 4) if is_open else None,
            "sl":          round(t.get('sl',          0.0), 4) if is_open else None,
            "lot":         t.get('lot', 0.0) if is_open else None,
            "leverage":    t.get('leverage', LEVERAGE),
            "margin_usd":  round(t.get('margin_usd', 0.0), 2) if is_open else None,
            "liq_price":   round(t.get('liq_price', 0.0), 4) if is_open else None,
            "margin_level": m_level,
            "equity":      equity,
            "pnl":         round(t.get('pnl_pct',    0.0), 2),
            "profit_usd":  round(t.get('profit_usd', 0.0), 2),
            "capital":     round(self.trade_sim.capital, 2),
        }

        return res_out

    # ---------- confluence & structure helpers ----------

    def _confluence(self, side: str, c: dict):
        """
        Đếm số yếu tố kỹ thuật ỦNG HỘ một hướng. Trả về (count, reasons[]).
        Mỗi yếu tố +1 nếu thuận hướng; vị trí giá xấu (mua sát kháng cự /
        bán sát hỗ trợ) bị PHẠT -1 để tránh đu đỉnh/bắt đáy.
        """
        is_buy = (side == "BUY")
        cnt = 0
        reasons: List[str] = []
        st = c["structure"]

        # 1) Trend nội khung: EMA stack + giá vs EMA50
        if is_buy and c["ema9"] > c["ema21"] and c["price"] > c["ema50"]:
            cnt += 1; reasons.append("EMA khung vào lệnh thuận TĂNG ✓")
        elif (not is_buy) and c["ema9"] < c["ema21"] and c["price"] < c["ema50"]:
            cnt += 1; reasons.append("EMA khung vào lệnh thuận GIẢM ✓")

        # 2) MACD
        if is_buy and c["macd_h"] > 0 and c["macd_h"] >= c["macd_h_p"]:
            cnt += 1; reasons.append("MACD dương & đang mạnh lên ✓")
        elif (not is_buy) and c["macd_h"] < 0 and c["macd_h"] <= c["macd_h_p"]:
            cnt += 1; reasons.append("MACD âm & đang yếu đi ✓")

        # 3) Momentum (RSI hồi + Stoch)
        if is_buy and 40 <= c["rsi"] <= 68 and c["rsi"] >= c["rsi_p"]:
            cnt += 1; reasons.append(f"RSI {c['rsi']:.0f} hồi lên (chưa quá mua) ✓")
        elif (not is_buy) and 32 <= c["rsi"] <= 60 and c["rsi"] <= c["rsi_p"]:
            cnt += 1; reasons.append(f"RSI {c['rsi']:.0f} yếu xuống (chưa quá bán) ✓")

        # 4) Volume xác nhận
        if c["vol_ratio"] > 1.2 and ((is_buy and c["price_chg"] > 0) or
                                     ((not is_buy) and c["price_chg"] < 0)):
            cnt += 1; reasons.append(f"Volume xác nhận (x{c['vol_ratio']:.1f}) ✓")

        # 5) VỊ TRÍ GIÁ so với cấu trúc (quan trọng nhất)
        ds = st.get("dist_to_support_pct")
        dr = st.get("dist_to_resistance_pct")
        if is_buy:
            if ds is not None and ds <= 0.4:
                cnt += 1
                reasons.append(f"Giá sát HỖ TRỢ {st.get('nearest_support')} (mua giá tốt) ✓")
            if dr is not None and dr <= 0.25:
                cnt -= 1
                reasons.append(f"⚠️ Giá sát KHÁNG CỰ {st.get('nearest_resistance')} (rủi ro đu đỉnh) ✗")
        else:
            if dr is not None and dr <= 0.4:
                cnt += 1
                reasons.append(f"Giá sát KHÁNG CỰ {st.get('nearest_resistance')} (bán giá tốt) ✓")
            if ds is not None and ds <= 0.25:
                cnt -= 1
                reasons.append(f"⚠️ Giá sát HỖ TRỢ {st.get('nearest_support')} (rủi ro bắt đáy) ✗")

        return cnt, reasons

    def _structure_sl_tp(self, side: str, price: float, atr: float, st: dict):
        """
        SL đặt PHÍA SAU mốc cấu trúc gần nhất (đệm bằng ATR); TP tại mốc
        cấu trúc kế tiếp. Nếu thiếu mốc → fallback theo ATR (mặc định 2R).
        """
        buf = atr * 0.5
        min_dist = atr * 0.8   # tránh SL quá sát gây nhiễu
        ns = st.get("nearest_support")
        nr = st.get("nearest_resistance")

        if side == "BUY":
            sl = (ns - buf) if ns else (price - atr * 1.5)
            if (price - sl) < min_dist:
                sl = price - min_dist
            tp = nr if nr else (price + (price - sl) * 2.0)
            # Đảm bảo TP có khoảng thở (không sát giá)
            if (tp - price) < min_dist:
                tp = price + (price - sl) * 2.0
        else:
            sl = (nr + buf) if nr else (price + atr * 1.5)
            if (sl - price) < min_dist:
                sl = price + min_dist
            tp = ns if ns else (price - (sl - price) * 2.0)
            if (price - tp) < min_dist:
                tp = price - (sl - price) * 2.0

        return round(sl, 4), round(tp, 4)

    def _rr_after_fees(self, side: str, price: float, sl: float, tp: float) -> float:
        """
        R:R đã tính chi phí khứ hồi (phí taker 2 chiều + slippage 2 chiều).
        Đây là bộ lọc cốt lõi chống 'phí ăn hết lời'.
        """
        fee_cost = price * (2 * TradeSimulator.TAKER_FEE + 2 * TradeSimulator.SLIPPAGE)
        if side == "BUY":
            reward = (tp - price) - fee_cost
            risk   = (price - sl) + fee_cost
        else:
            reward = (price - tp) - fee_cost
            risk   = (sl - price) + fee_cost
        if risk <= 0:
            return 0.0
        return round(reward / risk, 2)

    @staticmethod
    def _bias_reason(bias_agg: dict) -> str:
        b = bias_agg["bias"]
        icon = {"BULL": "🟢", "BEAR": "🔴", "NEUTRAL": "⚪"}.get(b, "⚪")
        tfs = ", ".join(
            f"{x.get('timeframe','?')}:{x['bias']}" for x in bias_agg.get("per_tf", [])
        )
        return f"{icon} Bias khung lớn: {b} (sức mạnh {bias_agg['strength']:.2f}) [{tfs}]"
