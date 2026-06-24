# simulator_manager.py — Lệnh TAY (manual) theo mô hình LOT/margin kiểu Exness
from datetime import datetime
from typing import List

import lot_sizing
from config import LEVERAGE


class ManualTradeSimulator:
    # Market order costs
    TAKER_FEE = 0.0010   # 0.1% per side
    SLIPPAGE  = 0.0002   # 0.02% market impact per fill

    def __init__(self, symbol: str = "BTCUSDT", capital: float = 100.0):
        self.symbol = symbol
        self.capital = capital
        self.active_trades: List[dict] = []
        self.history: List[dict] = []

    def set_capital(self, value: float):
        self.capital = max(1.0, float(value))

    def open_trade(self, trade_data: dict):
        """
        trade_data: {entry, tp, sl, position, lot}. lot mặc định 0.01 (micro lot).
        Tính margin + giá thanh lý để hiển thị & mô phỏng cháy lệnh.
        """
        t = trade_data.copy()
        entry = float(t.get('entry', 0) or 0)
        lot = lot_sizing.normalize_lot(float(t.get('lot', lot_sizing.MIN_LOT) or lot_sizing.MIN_LOT))
        direction = t.get('position', 'LONG')

        margin = lot_sizing.margin_required(lot, self.symbol, entry, LEVERAGE)
        liq = lot_sizing.liquidation_price(entry, lot, self.symbol, self.capital, margin, direction)

        t['lot'] = lot
        t['leverage'] = LEVERAGE
        t['contract_size'] = lot_sizing.get_contract_size(self.symbol)
        t['margin_usd'] = round(margin, 4)
        t['liq_price'] = round(liq, 4)
        t['notional'] = round(lot_sizing.notional_value(lot, self.symbol, entry), 2)
        t['status'] = 'OPEN'
        t['entry_time'] = datetime.now().isoformat()
        t['pnl'] = 0.0
        t['profit_usd'] = 0.0
        self.active_trades.append(t)

    def update_tick(self, current_price: float) -> List[dict]:
        """Kiểm tra TP/SL/THANH LÝ cho mọi lệnh tay. Trả list lệnh đóng trong tick."""
        closed = []
        still_active = []

        for trade in self.active_trades:
            tp    = float(trade.get('tp', 0))
            sl    = float(trade.get('sl', 0))
            pos   = trade.get('position', 'LONG')
            entry = float(trade.get('entry', current_price))
            lot   = float(trade.get('lot', lot_sizing.MIN_LOT))
            margin = float(trade.get('margin_usd', 0.0))
            liq   = float(trade.get('liq_price', 0.0))

            # Live PnL (lot-based) để hiển thị
            gross_live = lot_sizing.pnl_usd(entry, current_price, lot, self.symbol, pos)
            fee_round = lot_sizing.notional_value(lot, self.symbol, current_price) * self.TAKER_FEE * 2
            trade['profit_usd'] = round(gross_live - fee_round, 2)
            trade['pnl'] = round((trade['profit_usd'] / (margin + 1e-9)) * 100, 2)

            equity = self.capital + (gross_live - fee_round)

            hit_tp = (pos == 'LONG'  and current_price >= tp) or \
                     (pos == 'SHORT' and current_price <= tp)
            hit_sl = (pos == 'LONG'  and current_price <= sl) or \
                     (pos == 'SHORT' and current_price >= sl)
            hit_liq = (pos == 'LONG'  and current_price <= liq) or \
                      (pos == 'SHORT' and current_price >= liq) or \
                      lot_sizing.check_liquidation(equity, margin)

            if hit_tp or hit_sl or hit_liq:
                ct = trade.copy()
                ct['status']    = 'CLOSED'
                ct['exit_time'] = datetime.now().isoformat()
                ct['result']    = 'LIQUIDATED' if hit_liq and not hit_tp else ('WIN' if hit_tp else 'LOSS')

                mult = 1 if pos == 'LONG' else -1
                ref_price = liq if (hit_liq and not hit_tp and not hit_sl) else current_price
                exit_actual = ref_price * (1 - mult * self.SLIPPAGE)
                ct['exit_price'] = round(exit_actual, 4)

                gross = lot_sizing.pnl_usd(entry, exit_actual, lot, self.symbol, pos)
                fees = lot_sizing.notional_value(lot, self.symbol, exit_actual) * self.TAKER_FEE * 2
                net = gross - fees
                ct['profit_usd'] = round(net, 2)
                ct['fees_usd'] = round(fees, 4)
                ct['pnl'] = round((net / (margin + 1e-9)) * 100, 2)
                self.capital = max(0.0, self.capital + net)
                ct['capital_after'] = round(self.capital, 2)

                self.history.insert(0, ct)
                closed.append(ct)
            else:
                still_active.append(trade)

        self.active_trades = still_active
        self.history = self.history[:50]
        return closed
