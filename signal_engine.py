import asyncio
import pandas as pd
import json
import logging
import os
from datetime import datetime
from typing import Dict, List, Optional

from simulator_manager import ManualTradeSimulator
from prediction_engine import PredictionEngine

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
            self._adjust_weights(p['raw_signals'], direction, factor, lr=0.05)

            # EMA update for live winrate
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
# 2. TRADE SIMULATOR — Auto execution
# ==========================================
class TradeSimulator:
    def __init__(self, symbol: str, timeframe: str, capital: float = 100.0, risk_pct: float = 0.015):
        self.symbol = symbol
        self.timeframe = timeframe
        self.capital = capital
        self.risk_pct = risk_pct
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

    def open_position(self, timestamp: str, side: str, price: float, atr: float, indicators_snap: dict):
        if self.state != "NONE" or self.cooldown > 0:
            return

        self.state = "LONG" if side == "BUY" else "SHORT"

        # Risk-adjusted TP/SL sizing
        raw_sl  = atr * 1.5
        max_sl  = price * 0.0025   # Hard cap: 0.25% max
        min_sl  = price * 0.0005   # Hard floor: 0.05% min
        sl_dist = max(min_sl, min(raw_sl, max_sl))
        tp_dist = sl_dist * 1.8    # R:R = 1:1.8

        if self.state == "LONG":
            sl = price - sl_dist
            tp = price + tp_dist
        else:
            sl = price + sl_dist
            tp = price - tp_dist

        pos_size  = (self.capital * self.risk_pct) / sl_dist
        volume_usd = pos_size * price

        self.trade = {
            "entry_time": timestamp, "entry": price, "position": self.state,
            "sl": round(sl, 4), "tp": round(tp, 4),
            "size": pos_size, "volume_usd": volume_usd,
            "snapshot": indicators_snap, "pnl_pct": 0.0, "profit_usd": 0.0,
        }
        self.just_opened = True
        self.notifications.append(
            f"🟢 OPEN {self.state}\nEntry: {price:.2f} | Vol: {volume_usd:.2f}$\nTP: {tp:.2f} | SL: {sl:.2f}"
        )

    def process_tick(self, current_price: float, high: float, low: float) -> Optional[dict]:
        if self.state == "NONE":
            return None
        if self.just_opened:
            self.just_opened = False
            return None

        t = self.trade
        mult = 1 if self.state == "LONG" else -1
        t['pnl_pct']    = ((current_price - t['entry']) / t['entry']) * 100 * mult
        t['profit_usd'] = (current_price - t['entry']) * t['size'] * mult

        sl_buf   = t['entry'] * 0.0002
        is_closed = False
        res       = ""

        if self.state == "LONG":
            if low  <= (t['sl'] - sl_buf):  is_closed = True; res = "LOSS"; exit_p = t['sl']
            elif high >= t['tp']:            is_closed = True; res = "WIN";  exit_p = t['tp']
        else:
            if high >= (t['sl'] + sl_buf):  is_closed = True; res = "LOSS"; exit_p = t['sl']
            elif low  <= t['tp']:            is_closed = True; res = "WIN";  exit_p = t['tp']

        if not is_closed:
            return None

        final_profit = (exit_p - t['entry']) * t['size'] * mult
        final_pnl    = ((exit_p - t['entry']) / t['entry']) * 100 * mult
        cap_before   = self.capital
        self.capital += final_profit

        log_entry = {
            "symbol": self.symbol, "timeframe": self.timeframe,
            "entry_time": t["entry_time"], "exit_time": datetime.now().isoformat(),
            "entry_price": t["entry"], "exit_price": round(exit_p, 4),
            "position": t["position"],
            "capital_before": round(cap_before, 2), "capital_after": round(self.capital, 2),
            "risk_pct": self.risk_pct, "position_size": round(t["size"], 6),
            "profit_usd": round(final_profit, 2), "pnl": round(final_pnl, 2), "result": res,
        }
        self._async_write(self.trade_log_file, log_entry)

        self.history.insert(0, log_entry)
        self.history = self.history[:20]

        icon = "🔴" if res == "LOSS" else "🔵"
        self.notifications.append(
            f"{icon} CLOSED {res}\nPnL: {final_pnl:.2f}%  Profit: {final_profit:.2f}$"
        )

        closed_data = {"snapshot": t["snapshot"], "direction": self.state, "result": res}
        self.state   = "NONE"
        self.trade   = {}
        self.cooldown = 5
        return closed_data

    def get_notifications(self) -> List[str]:
        msgs = self.notifications.copy()
        self.notifications.clear()
        return msgs


# ==========================================
# 3. MASTER SIGNAL ENGINE
# ==========================================
class AdvancedSignalEngine:
    def __init__(self, symbol: str, interval: str):
        self.symbol   = symbol
        self.interval = interval
        self.scorer     = AdaptiveScorer(symbol, interval)
        self.trade_sim  = TradeSimulator(symbol, interval, capital=100.0)
        self.manual_sim = ManualTradeSimulator()
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
        return val * (3600 if unit == 'h' else 60)

    @staticmethod
    def _detect_regime(adx: float, bb_width: float, di_plus: float, di_minus: float) -> str:
        if bb_width < 0.0025:
            return "SQUEEZE"
        if adx > 25 and abs(di_plus - di_minus) > 5:
            return "TREND"
        if adx < 18:
            return "RANGE"
        return "NEUTRAL"

    @staticmethod
    def _compute_reason(action: str, regime: str, rsi: float, adx: float,
                        bb_width: float, cooldown: int, reverse: bool) -> str:
        if action != "HOLD":
            return f"✅ Signal Confirmed [{regime}]"
        if reverse:
            return "⚡ REVERSE MODE ACTIVE"
        if cooldown > 0:
            return f"⏳ Cooldown ({cooldown} bars)"
        if bb_width < 0.0025:
            return "🔸 Squeeze — Awaiting Breakout"
        if adx < 15:
            return "😴 Weak Trend (ADX low)"
        if rsi >= 72:
            return "🔴 RSI Overbought — Wait Pullback"
        if rsi <= 28:
            return "🟢 RSI Oversold — Wait Bounce"
        return "👀 Monitoring..."

    # ---------- main ----------

    def generate_signal(self, df: pd.DataFrame, mtf_context: dict) -> dict:
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

        # --- Auto-trade TP/SL check ---
        closed_trade = self.trade_sim.process_tick(c_price, c_high, c_low)
        if closed_trade:
            self.scorer.learn_from_real_trade(
                closed_trade["snapshot"], closed_trade["direction"], closed_trade["result"]
            )

        # --- Extract indicators ---
        ema9   = self._safe(curr.get('EMA_9'),   c_price)
        ema21  = self._safe(curr.get('EMA_21'),  c_price)
        ema9_p = self._safe(prev.get('EMA_9'),   ema9)

        rsi      = self._safe(curr.get('RSI_14'), 50.0)
        macd_h   = self._safe(curr.get('MACD_H'), 0.0)
        macd_h_p = self._safe(prev.get('MACD_H'), 0.0)
        macd_s   = self._safe(curr.get('MACD_S'), 0.0)

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
        # Only run full scoring logic on NEW candle opening
        # ====================================================
        if self.last_candle_time != c_time:
            # BUG-005 fix: use current close (not prev)
            self.scorer.evaluate_and_learn(c_time, float(curr['close']))

            # MTF Score — aligned to signal_engine expected TFs (5m, 15m, 1h)
            mtf_score = 0.0
            mtf_weight_total = 0.0
            for tf in ["5m", "15m", "1h"]:
                df_tf = mtf_context.get(tf)
                if df_tf is not None and len(df_tf) > 0:
                    mc     = df_tf.iloc[-1]
                    me9    = self._safe(mc.get('EMA_9'),  c_price)
                    me21   = self._safe(mc.get('EMA_21'), c_price)
                    madx   = self._safe(mc.get('ADX'),    20.0)
                    w      = 1.5 if madx > 25 else 1.0
                    mtf_score        += (1.0 if me9 > me21 else -1.0) * w
                    mtf_weight_total += w
            mtf_score = mtf_score / max(mtf_weight_total, 1.0)

            if self.trade_sim.cooldown > 0:
                self.trade_sim.cooldown -= 1

            # ============ SIGNAL SCORING (8 dimensions) ============

            # 1. EMA: cross direction weighted by slope momentum
            ema_cross = 1.0 if ema9 > ema21 else -1.0
            slope_norm = slope / (atr + 1e-9) * 3
            ema_score = ema_cross * min(1.0, 0.55 + abs(slope_norm) * 0.45)

            # 2. MACD: histogram normalised + crossover bonus
            macd_norm = max(-1.0, min(1.0, macd_h / (atr * 0.3 + 1e-9)))
            macd_cross = 0.0
            if macd_h > 0 and macd_h_p <= 0:
                macd_cross = 0.5    # Fresh bullish cross
            elif macd_h < 0 and macd_h_p >= 0:
                macd_cross = -0.5   # Fresh bearish cross
            macd_score = max(-1.0, min(1.0, macd_norm + macd_cross))

            # 3. RSI: normalised momentum
            rsi_score = max(-1.0, min(1.0, (rsi - 50) / 20))

            # 4. BB: position within bands (avoids trading in squeeze)
            if bb_width < 0.002:
                bb_score = 0.0
            else:
                band_pos = (c_price - bb_l) / (bb_u - bb_l + 1e-9)
                bb_score = max(-1.0, min(1.0, (0.5 - band_pos) * 2))

            # 5. VWAP
            vwap_score = 1.0 if c_price > vwap else -1.0

            # 6. Volume: directional surge
            if vol_ratio > 1.2:
                vol_dir = 1.0 if price_chg > 0 else -1.0
                vol_score = vol_dir * min(1.0, (vol_ratio - 1.0) * 0.8)
            else:
                vol_score = 0.0

            # 7. Stochastic: zone + cross
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
                'EMA':    round(ema_score,   3),
                'MACD':   round(macd_score,  3),
                'RSI':    round(rsi_score,   3),
                'BB':     round(bb_score,    3),
                'VWAP':   round(vwap_score,  3),
                'VOLUME': round(vol_score,   3),
                'STOCH':  round(stoch_score, 3),
                'MTF':    round(mtf_score,   3),
            }

            weights   = self.scorer.get_weights()
            raw_score = sum(raw_sigs[k] * weights.get(k, 1.0 / len(raw_sigs)) for k in raw_sigs)
            self.smoothed_score = (0.35 * raw_score) + (0.65 * self.smoothed_score)

            # Regime detection
            regime = self._detect_regime(adx, bb_width, di_plus, di_minus)

            # Adaptive threshold: lower in strong trends, higher in ranging
            self.hold_counter = self.hold_counter + 1 if self.trade_sim.state == "NONE" else 0
            decay        = min(0.12, max(0, (self.hold_counter - 20) * 0.007))
            base_thresh  = 0.33 if regime == "TREND" else 0.40
            current_thresh = base_thresh - decay

            # ============ ENTRY DECISION ============
            action = "HOLD"
            if bb_width > 0.001 and self.trade_sim.cooldown == 0:
                dir_cand = "HOLD"
                if self.smoothed_score > current_thresh and rsi < 72:
                    dir_cand = "BUY"
                elif self.smoothed_score < -current_thresh and rsi > 28:
                    dir_cand = "SELL"

                if self.scorer.reverse_mode and dir_cand != "HOLD":
                    dir_cand = "SELL" if dir_cand == "BUY" else "BUY"

                if dir_cand == self.current_dir and dir_cand != "HOLD":
                    self.confirm_counter += 1
                else:
                    self.confirm_counter = 1
                    self.current_dir = dir_cand

                if self.confirm_counter >= 2:
                    action = dir_cand
                    if action != "HOLD":
                        self.trade_sim.open_position(c_time_str, action, c_price, atr, raw_sigs)

            # ============ PREDICTION (EMA-smoothed PredictionEngine) ============
            if regime == "TREND":
                p_dir = "BULLISH" if di_plus > di_minus else "BEARISH"
            else:
                p_dir = "BULLISH" if slope > 0 else "BEARISH"

            if self.scorer.reverse_mode:
                p_dir = "BEARISH" if p_dir == "BULLISH" else "BULLISH"

            # Map to PredictionEngine direction format (BULL/BEAR/SIDEWAY)
            pe_dir = "BULL" if p_dir == "BULLISH" else "BEAR"
            pred_result = self.predictor.predict(
                close_price=c_price,
                ema9=ema9,
                atr=atr,
                bb_upper=bb_u,
                bb_lower=bb_l,
                momentum_score=self.smoothed_score,
                direction=pe_dir,
            )
            conf = pred_result["confidence"]
            pred = {
                "direction": p_dir,
                "mid_price": pred_result["mid_price"],
                "range": pred_result["range"],
            }

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
                    "direction": p_dir, "range": pred['range'], "confidence": conf,
                    "start_price": c_price, "raw_signals": raw_sigs,
                    "indicators_snapshot": indicators_snap, "atr_snapshot": atr,
                    "signal": action, "score": self.smoothed_score,
                },
            )

            reason = self._compute_reason(
                action, regime, rsi, adx, bb_width,
                self.trade_sim.cooldown, self.scorer.reverse_mode
            )

            # Tracker: pending + recent
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
                "score": round(self.smoothed_score, 3), "confidence": round(conf, 3),
                "prediction": pred, "indicators": indicators_snap,
                "raw_signals": raw_sigs, "reason": reason,
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
        res_out["trade"] = {
            "position_status": "OPEN" if self.trade_sim.state != "NONE" else "NONE",
            "entry":       round(t.get('entry',      0.0), 4) if self.trade_sim.state != "NONE" else None,
            "tp":          round(t.get('tp',          0.0), 4) if self.trade_sim.state != "NONE" else None,
            "sl":          round(t.get('sl',          0.0), 4) if self.trade_sim.state != "NONE" else None,
            "pnl":         round(t.get('pnl_pct',    0.0), 2),
            "profit_usd":  round(t.get('profit_usd', 0.0), 2),
            "position_size": round(t.get('size',     0.0), 6),
            "capital":     round(self.trade_sim.capital, 2),
        }

        return res_out
