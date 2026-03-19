"""
Risk Manager — enforces hard limits before any trade is allowed.

This is the last line of defence before real money moves.
"""

import logging
from datetime import date
from typing import Tuple

from .config import Config
from .audit_log import AuditLog

log = logging.getLogger("risk_manager")


class RiskManager:
    def __init__(self, config: Config, audit: AuditLog):
        self.config = config
        self.audit = audit
        self.emergency_stopped = False
        self._daily_loss: float = 0.0
        self._loss_date: date = date.today()
        self._open_positions: dict[str, float] = {}  # market_id → USDC exposure

    # ── Public API ──────────────────────────────────────────────────────────

    def check_daily_loss_ok(self) -> bool:
        self._maybe_reset_daily_loss()
        ok = self._daily_loss < self.config.max_daily_loss_usdc
        if not ok:
            log.warning(
                f"Daily loss limit reached: ${self._daily_loss:.2f} / "
                f"${self.config.max_daily_loss_usdc:.2f}"
            )
        return ok

    def approve_trade(self, trade: dict) -> Tuple[bool, str]:
        """
        Returns (approved, reason).
        Adjusts trade['usdc_size'] to comply with limits if needed.
        """
        if self.emergency_stopped:
            return False, "Emergency stop is active"

        if not self.check_daily_loss_ok():
            return False, "Daily loss limit reached"

        # Cap trade size to max_trade_usdc
        ideal_size = float(trade.get("usdc_size", 0))
        if ideal_size <= 0:
            return False, "Trade size is zero or negative"

        capped_size = min(ideal_size, self.config.max_trade_usdc)

        # Check position limit per market
        market_id = trade.get("market_id", "unknown")
        existing_exposure = self._open_positions.get(market_id, 0.0)
        remaining_capacity = self.config.max_position_usdc - existing_exposure
        if remaining_capacity <= 0:
            return False, f"Max position already reached for market {market_id}"

        final_size = min(capped_size, remaining_capacity)
        trade["usdc_size"] = round(final_size, 2)

        log.info(
            f"Trade approved: ${ideal_size:.2f} → ${final_size:.2f} "
            f"(cap={self.config.max_trade_usdc}, capacity={remaining_capacity:.2f})"
        )
        return True, "ok"

    def apply_kelly_sizing(self, trade: dict, analysis: dict) -> dict:
        """
        Adjust trade size using fractional Kelly criterion.
        Kelly formula for binary market: f = (p*(b+1) - 1) / b
          where p = your estimated probability of winning
                b = net odds (payout ratio = 1/price - 1)
        """
        p = analysis.get("your_probability", 0.5)
        price = float(trade.get("price", 0.5))
        if price <= 0 or price >= 1:
            return trade

        b = (1.0 / price) - 1.0  # net odds
        kelly_f = (p * (b + 1) - 1) / b
        kelly_f = max(0.0, kelly_f)  # never negative

        # Apply fractional Kelly
        fraction = self.config.kelly_fraction
        bankroll = self._estimate_bankroll()
        kelly_size = bankroll * kelly_f * fraction

        trade["usdc_size"] = round(min(kelly_size, self.config.max_trade_usdc), 2)
        log.info(f"Kelly sizing: f={kelly_f:.4f}, fraction={fraction}, size=${trade['usdc_size']:.2f}")
        return trade

    def record_trade_result(self, market_id: str, pnl: float):
        """Call this when a position closes to update daily loss tracking."""
        self._maybe_reset_daily_loss()
        if pnl < 0:
            self._daily_loss += abs(pnl)
            log.info(f"Recorded loss ${abs(pnl):.2f}. Daily loss: ${self._daily_loss:.2f}")

        if market_id in self._open_positions:
            del self._open_positions[market_id]

    def record_open_position(self, market_id: str, usdc_size: float):
        self._open_positions[market_id] = (
            self._open_positions.get(market_id, 0.0) + usdc_size
        )

    def trigger_emergency_stop(self, reason: str):
        log.critical(f"EMERGENCY STOP TRIGGERED: {reason}")
        self.emergency_stopped = True
        self.audit.log_emergency_stop(reason)

    def clear_emergency_stop(self):
        log.info("Emergency stop cleared manually")
        self.emergency_stopped = False

    # ── Private ─────────────────────────────────────────────────────────────

    def _maybe_reset_daily_loss(self):
        today = date.today()
        if today != self._loss_date:
            log.info(f"New day — resetting daily loss tracker (was ${self._daily_loss:.2f})")
            self._daily_loss = 0.0
            self._loss_date = today

    def _estimate_bankroll(self) -> float:
        """
        Conservative bankroll estimate for Kelly sizing.
        Uses max_daily_loss as a proxy — replace with live balance fetch if desired.
        """
        return self.config.max_daily_loss_usdc * 4
