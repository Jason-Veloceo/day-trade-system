"""Risk gate.

Every entry signal goes through here BEFORE the executor places an order.
Hard invariants (paper-only, kill switch) cannot be relaxed by per-run config.
Per-run caps (max trades, max position value) are loaded from RiskCaps.
"""

from __future__ import annotations

from dataclasses import dataclass

from day_trade.config import Settings


@dataclass(frozen=True, slots=True)
class RiskCaps:
    """Per-run risk configuration. The /engine page lets the user adjust
    these before pressing Start; values fall back to .env defaults."""

    max_trades_per_run: int = 5
    max_position_value_usd: float = 5000.0
    max_position_qty: int = 25_000  # forex defaults to 25k base units; for stocks well above any sensible share count
    max_daily_loss_usd: float = 150.0


@dataclass(slots=True)
class RiskState:
    """Mutable per-run running totals."""

    trades_count: int = 0
    realized_pnl_usd: float = 0.0
    open_position_qty: int = 0
    kill_switch_on: bool = False


@dataclass(frozen=True, slots=True)
class RiskDecision:
    """Result of a risk check."""

    allowed: bool
    reasons: tuple[str, ...]


class RiskGate:
    def __init__(self, settings: Settings, caps: RiskCaps) -> None:
        self._settings = settings
        self.caps = caps
        self.state = RiskState()

    # --- mutators ---

    def record_open(self, qty: int) -> None:
        self.state.open_position_qty = qty

    def record_close(self, realized_pnl_usd: float) -> None:
        self.state.open_position_qty = 0
        self.state.realized_pnl_usd += realized_pnl_usd
        self.state.trades_count += 1

    def engage_kill_switch(self, reason: str) -> None:
        self.state.kill_switch_on = True

    # --- gate ---

    def can_enter(self, *, intended_qty: int, intended_price: float) -> RiskDecision:
        reasons: list[str] = []

        # ---- hard invariants ----
        if not self._settings.paper_trading_only:
            reasons.append("PAPER_TRADING_ONLY=false; engine refuses to operate")
        if self._settings.live_trading_enabled:
            reasons.append("LIVE_TRADING_ENABLED=true; engine refuses to operate")
        if self.state.kill_switch_on:
            reasons.append("kill switch is engaged")

        # ---- per-run caps ----
        if self.state.open_position_qty > 0:
            reasons.append(
                f"already have an open position ({self.state.open_position_qty}); "
                "POC is single-position-at-a-time"
            )
        if self.state.trades_count >= self.caps.max_trades_per_run:
            reasons.append(
                f"hit max trades per run ({self.state.trades_count} >= "
                f"{self.caps.max_trades_per_run})"
            )
        if self.state.realized_pnl_usd <= -abs(self.caps.max_daily_loss_usd):
            reasons.append(
                f"hit max daily loss ({self.state.realized_pnl_usd:.2f} <= "
                f"-{self.caps.max_daily_loss_usd:.2f}); engaging kill switch"
            )
            self.engage_kill_switch("max_daily_loss")

        position_value = intended_qty * intended_price
        if position_value > self.caps.max_position_value_usd:
            reasons.append(
                f"intended position value {position_value:.2f} > cap "
                f"{self.caps.max_position_value_usd:.2f}"
            )
        if intended_qty > self.caps.max_position_qty:
            reasons.append(
                f"intended position qty {intended_qty} > cap {self.caps.max_position_qty}"
            )
        if intended_qty <= 0:
            reasons.append(f"intended position qty {intended_qty} is not positive")

        return RiskDecision(allowed=not reasons, reasons=tuple(reasons))

    def can_exit(self) -> RiskDecision:
        # We always allow exits. The only thing that could block is a
        # disconnection / kill switch, which the engine handles separately.
        return RiskDecision(allowed=True, reasons=())
