# simulator_manager.py
from datetime import datetime
from typing import List


class ManualTradeSimulator:
    def __init__(self):
        self.active_trades: List[dict] = []
        self.history: List[dict] = []

    def open_trade(self, trade_data: dict):
        t = trade_data.copy()
        t['status'] = 'OPEN'
        t['entry_time'] = datetime.now().isoformat()
        self.active_trades.append(t)

    def update_tick(self, current_price: float) -> List[dict]:
        """Check TP/SL. Returns list of ALL trades closed this tick."""
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
                ct['status']     = 'CLOSED'
                ct['exit_price'] = current_price
                ct['exit_time']  = datetime.now().isoformat()
                ct['result']     = 'WIN' if hit_tp else 'LOSS'
                mult             = 1 if pos == 'LONG' else -1
                ct['pnl']        = round(((current_price - entry) / (entry + 1e-9)) * 100 * mult, 2)
                self.history.insert(0, ct)
                closed.append(ct)
            else:
                still_active.append(trade)

        self.active_trades = still_active
        self.history = self.history[:50]
        return closed
