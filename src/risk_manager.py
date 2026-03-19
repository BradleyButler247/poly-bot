"""
Risk Manager — hard circuit breakers only. No edge floor.
The AI decides whether a trade has positive EV. This module
just enforces the absolute limits to prevent catastrophic loss.
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
        self._open_positions: dict[str, float] = {}

    def check_daily_loss_ok(self) -> bool:
        self._maybe_reset_daily_loss()
        ok = self._daily_loss < self.config.max_daily_loss_usdc
        if not ok:
            log.warning(f"Daily loss limit: ${self._daily_loss:.2f} / ${self.config.max_daily_loss_usdc:.2f}")
        return ok

    def approve_trade(self, trade: dict) -> Tuple[bool, str]:
        if self.emergency_stopped:
            return False, "Emergency stop is active"
        if not self.check_daily_loss_ok():
            return False, "Daily loss limit reached"

        size = float(trade.get("usdc_size", 0))
        if size <= 0:
            return False, "Trade size is zero or negative"

        # Hard cap
        trade["usdc_size"] = min(size, self.config.max_trade_usdc)

        # Per-market position limit
        market_id = trade.get("market_id", "unknown")
        existing = self._open_positions.get(market_id, 0.0)
        remaining = self.config.max_position_usdc - existing
        if remaining <= 0:
            return False, f"Max position reached for market {market_id}"

        trade["usdc_size"] = round(min(trade["usdc_size"], remaining), 2)
        return True, "ok"

    def record_open_position(self, market_id: str, usdc_size: float):
        self._open_positions[market_id] = self._open_positions.get(market_id, 0.0) + usdc_size

    def record_trade_result(self, market_id: str, pnl: float):
        self._maybe_reset_daily_loss()
        if pnl < 0:
            self._daily_loss += abs(pnl)
        self._open_positions.pop(market_id, None)

    def trigger_emergency_stop(self, reason: str):
        log.critical(f"EMERGENCY STOP: {reason}")
        self.emergency_stopped = True
        self.audit.log_emergency_stop(reason)

    def clear_emergency_stop(self):
        self.emergency_stopped = False
        log.info("Emergency stop cleared")

    def _maybe_reset_daily_loss(self):
        today = date.today()
        if today != self._loss_date:
            log.info(f"New day — resetting daily loss (was ${self._daily_loss:.2f})")
            self._daily_loss = 0.0
            self._loss_date = today
