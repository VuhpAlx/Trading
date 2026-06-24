# simulator_manager.py
from datetime import datetime
from typing import List


class ManualTradeSimulator:
    # Market order costs applied on exit
    TAKER_FEE = 0.0010   # 0.1% per side (Binance standard)
    SLIPPAGE  = 0.0002   # 0.02% market impact per fill

    def __init__(self):
        self.active_trades: List[dict] = []
        self.history: List[dict] = []

    def open_trade(self, trade_data: dict):
        t = trade_data.copy()
        t['status'] = 'OPEN'
        t['entry_time'] = datetime.now().isoformat()
        self.active_trades.append(t)

    def update_tick(self, current_price: float) -> List[dict]:
        """Check TP/SL for all active manual trades. Returns list of ALL trades closed this tick."""
        closed = []
        still_active = []

        for trade in self.active_trades:
            tp    = float(trade.get('tp', 0))
            sl    = float(trade.get('sl', 0))
            pos   = trade.get('position', 'LONG')
            entry = float(trade.get('entry', current_price))

            hit_tp = (pos == 'LONG'  and current_price >= tp) or \
                     (pos == 'SHORT' and current_price <= tp)
            hit_sl = (pos == 'LONG'  and current_price <= sl) or \
                     (pos == 'SHORT' and current_price >= sl)

            if hit_tp or hit_sl:
                ct = trade.copy()
                ct['status']    = 'CLOSED'
                ct['exit_time'] = datetime.now().isoformat()
                ct['result']    = 'WIN' if hit_tp else 'LOSS'

                mult = 1 if pos == 'LONG' else -1

                # Market exit: LONG sells lower than current price, SHORT covers higher
                # Formula: exit_actual = price * (1 - mult * SLIPPAGE)
                #   LONG  (mult=+1): exit = price * (1 - 0.0002) — sold slightly below
                #   SHORT (mult=-1): exit = price * (1 + 0.0002) — covered slightly above
                exit_actual = current_price * (1 - mult * self.SLIPPAGE)
                ct['exit_price'] = round(exit_actual, 4)

                # Gross % move from user's entry to slippage-adjusted exit
                price_move_pct = ((exit_actual - entry) / (entry + 1e-9)) * 100 * mult

                # Deduct round-trip taker fees: entry side + exit side = 2 × TAKER_FEE
                round_trip_fee_pct = self.TAKER_FEE * 2 * 100   # = 0.20%
                ct['pnl'] = round(price_move_pct - round_trip_fee_pct, 2)

                self.history.insert(0, ct)
                closed.append(ct)
            else:
                still_active.append(trade)

        self.active_trades = still_active
        self.history = self.history[:50]
        return closed
