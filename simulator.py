"""
simulator.py — Portfolio simulation: $100 capital, 2% per trade
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional


INITIAL_CAPITAL = 100.0
TRADE_PCT       = 0.02      # 2% of current capital per trade
MIN_CONFIDENCE  = 65        # minimum confidence to enter a trade
ENTRY_AFTER_N   = 4         # consistent snapshots required before entering


@dataclass
class Trade:
    id:           int
    market:       str
    direction:    str          # "UP" or "DOWN"
    entry_price:  float        # token price at entry (0-1)
    shares:       float        # usdc_bet / entry_price
    bet_size:     float        # USDC committed
    entry_time:   str
    # filled at close
    exit_price:   Optional[float] = None
    pnl:          Optional[float] = None
    status:       str = "OPEN"  # OPEN | WIN | LOSS | CANCELLED

    def mark_to_market(self, current_price: float) -> float:
        """Current value of position."""
        return round(self.shares * current_price, 4)

    def unrealized_pnl(self, current_price: float) -> float:
        return round(self.mark_to_market(current_price) - self.bet_size, 4)

    def close(self, won: bool, exit_price: float) -> float:
        """
        Resolve trade.
        If won=True:  shares * 1.0  (token resolves to $1)
        If won=False: shares * 0.0  (token resolves to $0)
        Returns realized P&L.
        """
        self.exit_price = exit_price
        if won:
            proceeds   = self.shares * 1.0
            self.pnl   = round(proceeds - self.bet_size, 4)
            self.status = "WIN"
        else:
            self.pnl   = round(-self.bet_size, 4)
            self.status = "LOSS"
        return self.pnl

    def to_dict(self) -> dict:
        return {
            "id":           self.id,
            "market":       self.market,
            "direction":    self.direction,
            "entry_price":  self.entry_price,
            "shares":       round(self.shares, 4),
            "bet_size":     self.bet_size,
            "entry_time":   self.entry_time,
            "exit_price":   self.exit_price,
            "pnl":          self.pnl,
            "status":       self.status,
        }


class Portfolio:
    def __init__(self, initial_capital: float = INITIAL_CAPITAL, trade_pct: float = TRADE_PCT,
                 db=None):
        self.initial_capital  = initial_capital
        self.capital          = initial_capital
        self.trade_pct        = trade_pct
        self.active_trade: Optional[Trade] = None
        self.closed_trades: list[Trade] = []
        self.pnl_history: list[float]   = [0.0]
        self._trade_counter = 0
        self._db = db  # módulo db para persistencia (opcional)
        # For entry confirmation
        self._signal_streak: dict = {"label": None, "count": 0}

    def restore(self, saved: dict) -> None:
        """Restaura el estado desde la DB al arrancar el servidor."""
        self.capital         = saved["capital"]
        self.initial_capital = saved["initial_capital"]
        self.pnl_history     = saved["pnl_history"]
        self._trade_counter  = saved["trade_counter"]
        self.closed_trades   = saved["closed_trades"]

    # ── Entry logic ────────────────────────────────────────────────────────────

    def consider_entry(self, signal: dict, market_question: str, up_price: float, down_price: float) -> bool:
        """
        Track signal streak. Enter trade when signal is consistent
        for ENTRY_AFTER_N snapshots and confidence >= MIN_CONFIDENCE.
        Returns True if a trade was entered.
        """
        if self.active_trade is not None:
            return False
        if self.capital < 1.0:
            return False

        label = signal["label"]
        conf  = signal["confidence"]

        # Only act on directional signals
        if label not in ("UP", "DOWN", "STRONG UP", "STRONG DOWN"):
            self._signal_streak = {"label": None, "count": 0}
            return False

        # Normalize to UP/DOWN
        direction = "UP" if "UP" in label else "DOWN"

        # Track streak
        if self._signal_streak["label"] == direction:
            self._signal_streak["count"] += 1
        else:
            self._signal_streak = {"label": direction, "count": 1}

        if self._signal_streak["count"] < ENTRY_AFTER_N:
            return False
        if conf < MIN_CONFIDENCE:
            return False

        # Enter
        bet_size    = round(self.capital * self.trade_pct, 2)
        entry_price = up_price if direction == "UP" else down_price
        if entry_price <= 0.01:
            return False

        shares = round(bet_size / entry_price, 4)
        self._trade_counter += 1
        self.active_trade = Trade(
            id           = self._trade_counter,
            market       = market_question,
            direction    = direction,
            entry_price  = entry_price,
            shares       = shares,
            bet_size     = bet_size,
            entry_time   = datetime.utcnow().strftime("%H:%M:%S"),
        )
        self._signal_streak = {"label": None, "count": 0}
        # Persistir trade abierto
        if self._db:
            self._db.save_trade(self.active_trade)
        return True

    # ── Mark-to-market ─────────────────────────────────────────────────────────

    def current_price_for_trade(self, up_price: float, down_price: float) -> float:
        if not self.active_trade:
            return 0.0
        return up_price if self.active_trade.direction == "UP" else down_price

    def get_unrealized(self, up_price: float, down_price: float) -> float:
        if not self.active_trade:
            return 0.0
        cp = self.current_price_for_trade(up_price, down_price)
        return self.active_trade.unrealized_pnl(cp)

    # ── Close trade ────────────────────────────────────────────────────────────

    def close_trade(self, up_price: float, down_price: float, force_winner: Optional[bool] = None) -> Optional[Trade]:
        """
        Close active trade.
        If force_winner is given, use it directly.
        Otherwise determine winner from final prices (>0.5 wins).
        """
        if not self.active_trade:
            return None

        trade = self.active_trade

        if force_winner is not None:
            won = force_winner
        else:
            # Determine from price
            if trade.direction == "UP":
                won = up_price >= 0.5
            else:
                won = down_price >= 0.5

        exit_price = 1.0 if won else 0.0
        pnl        = trade.close(won, exit_price)
        self.capital = round(self.capital + trade.bet_size + pnl, 4)  # return stake + profit
        self.closed_trades.append(trade)
        self.active_trade = None
        self._signal_streak = {"label": None, "count": 0}

        # Record cumulative P&L snapshot
        total_pnl = round(self.capital - self.initial_capital, 4)
        self.pnl_history.append(round(total_pnl, 4))

        # Persistir trade cerrado + estado del portafolio
        if self._db:
            self._db.save_trade(trade)
            self._db.save_portfolio_state(
                self.capital, self.initial_capital,
                self.pnl_history, self._trade_counter,
            )

        return trade

    def cancel_active_trade(self):
        """Cancel without resolving (market ended without clear resolution)."""
        if not self.active_trade:
            return
        self.active_trade.status = "CANCELLED"
        self.closed_trades.append(self.active_trade)
        if self._db:
            self._db.save_trade(self.active_trade)
            self._db.save_portfolio_state(
                self.capital, self.initial_capital,
                self.pnl_history, self._trade_counter,
            )
        self.active_trade = None

    # ── Stats ──────────────────────────────────────────────────────────────────

    def stats(self, up_price: float = 0.5, down_price: float = 0.5) -> dict:
        closed   = self.closed_trades
        wins     = [t for t in closed if t.status == "WIN"]
        losses   = [t for t in closed if t.status == "LOSS"]
        n_closed = len(wins) + len(losses)
        win_rate = round(len(wins) / n_closed * 100, 1) if n_closed else 0.0

        realized_pnl   = sum(t.pnl for t in closed if t.pnl is not None)
        unrealized_pnl = self.get_unrealized(up_price, down_price)
        total_pnl      = round(realized_pnl + unrealized_pnl, 4)
        equity         = round(self.capital + (self.active_trade.bet_size if self.active_trade else 0) + unrealized_pnl, 4)

        active = None
        if self.active_trade:
            t  = self.active_trade
            cp = self.current_price_for_trade(up_price, down_price)
            active = {
                **t.to_dict(),
                "current_price":  round(cp, 4),
                "mark_to_market": t.mark_to_market(cp),
                "unrealized_pnl": t.unrealized_pnl(cp),
            }

        return {
            "initial_capital": self.initial_capital,
            "capital":         round(self.capital, 4),
            "equity":          equity,
            "realized_pnl":    round(realized_pnl, 4),
            "unrealized_pnl":  round(unrealized_pnl, 4),
            "total_pnl":       total_pnl,
            "total_pnl_pct":   round(total_pnl / self.initial_capital * 100, 2),
            "total_trades":    len(closed),
            "wins":            len(wins),
            "losses":          len(losses),
            "cancelled":       len([t for t in closed if t.status == "CANCELLED"]),
            "win_rate":        win_rate,
            "best_trade":      round(max((t.pnl for t in closed if t.pnl), default=0), 4),
            "worst_trade":     round(min((t.pnl for t in closed if t.pnl), default=0), 4),
            "avg_pnl":         round(realized_pnl / n_closed, 4) if n_closed else 0,
            "pnl_history":     self.pnl_history[-50:],
            "active_trade":    active,
            "trade_log":       [t.to_dict() for t in reversed(closed[-20:])],
            "signal_streak":   self._signal_streak,
        }
