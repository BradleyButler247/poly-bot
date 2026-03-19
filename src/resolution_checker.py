"""
Resolution Checker — periodically polls Polymarket to find
which markets we traded have now resolved, then records the
actual P&L so the learning loop can use real outcomes.
"""

import json
import logging
import os
import aiohttp
from typing import Optional

from .config import Config
from .audit_log import AuditLog
from .risk_manager import RiskManager

log = logging.getLogger("resolver")

# Tracks open positions we need to monitor: order_id → trade metadata
OPEN_TRADES_PATH = os.getenv("OPEN_TRADES_PATH", "logs/open_trades.jsonl")


class ResolutionChecker:
    def __init__(self, config: Config, audit: AuditLog, risk: RiskManager):
        self.config = config
        self.audit = audit
        self.risk = risk
        os.makedirs("logs", exist_ok=True)

    def record_open_trade(self, market_id: str, question: str, order_id: str,
                          outcome_traded: str, price_paid: float, usdc_size: float,
                          your_probability: float, strategy_tags: list):
        """Write a pending trade so we can check its resolution later."""
        record = {
            "market_id": market_id,
            "question": question,
            "order_id": order_id,
            "outcome_traded": outcome_traded,
            "price_paid": price_paid,
            "usdc_size": usdc_size,
            "your_probability": your_probability,
            "strategy_tags": strategy_tags,
            "resolved": False,
        }
        with open(OPEN_TRADES_PATH, "a") as f:
            f.write(json.dumps(record) + "\n")

    async def check_resolutions(self):
        """
        For each unresolved open trade, check if the market has resolved.
        If it has, compute P&L and log to the learning system.
        """
        open_trades = self._load_open_trades()
        if not open_trades:
            return

        log.info(f"Checking resolution for {len(open_trades)} open trades")
        updated = []

        for trade in open_trades:
            if trade.get("resolved"):
                updated.append(trade)
                continue

            resolution = await self._fetch_resolution(trade["market_id"])
            if resolution is None:
                updated.append(trade)
                continue

            # Market resolved
            market_resolved_yes = resolution
            won = (trade["outcome_traded"] == "YES" and market_resolved_yes) or \
                  (trade["outcome_traded"] == "NO" and not market_resolved_yes)

            price_paid = float(trade["price_paid"])
            usdc_size = float(trade["usdc_size"])

            if won:
                # Payout = shares * $1. Shares = usdc_size / price_paid.
                # Net P&L = (1 - price_paid) / price_paid * usdc_size
                pnl = round((1.0 - price_paid) / price_paid * usdc_size, 4)
            else:
                pnl = -usdc_size

            log.info(
                f"Market resolved: {trade['question'][:60]}... "
                f"→ {'WIN' if won else 'LOSS'} ${pnl:+.2f}"
            )

            self.audit.log_resolved_trade(
                order_id=trade["order_id"],
                market_id=trade["market_id"],
                question=trade["question"],
                outcome_traded=trade["outcome_traded"],
                price_paid=price_paid,
                usdc_size=usdc_size,
                market_resolved_yes=market_resolved_yes,
                pnl=pnl,
                your_probability=float(trade.get("your_probability", 0.5)),
                strategy_tags=trade.get("strategy_tags", []),
            )

            self.risk.record_trade_result(trade["market_id"], pnl)

            trade["resolved"] = True
            trade["pnl"] = pnl
            trade["won"] = won
            updated.append(trade)

        self._save_open_trades(updated)

    async def _fetch_resolution(self, market_id: str) -> Optional[bool]:
        """
        Returns True if market resolved YES, False if NO, None if still open.
        """
        url = f"{self.config.gamma_host}/markets/{market_id}"
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                    resp.raise_for_status()
                    data = await resp.json()

            active = data.get("active", True)
            closed = data.get("closed", False)
            resolved = data.get("resolved", False)

            if not resolved and (active and not closed):
                return None  # Still open

            # Check resolution value
            resolution_val = data.get("resolutionSource") or data.get("resolution")
            if resolution_val:
                if str(resolution_val).upper() in ("YES", "1", "TRUE"):
                    return True
                if str(resolution_val).upper() in ("NO", "0", "FALSE"):
                    return False

            # Fallback: check outcome prices (resolved YES → YES price = 1.0)
            tokens = data.get("tokens") or []
            for token in tokens:
                if str(token.get("outcome", "")).upper() == "YES":
                    price = float(token.get("price", 0.5))
                    if price >= 0.99:
                        return True
                    if price <= 0.01:
                        return False

            return None

        except Exception as e:
            log.warning(f"Resolution check failed for {market_id}: {e}")
            return None

    def _load_open_trades(self) -> list[dict]:
        trades = []
        try:
            with open(OPEN_TRADES_PATH) as f:
                for line in f:
                    line = line.strip()
                    if line:
                        try:
                            t = json.loads(line)
                            if not t.get("resolved"):
                                trades.append(t)
                        except json.JSONDecodeError:
                            pass
        except FileNotFoundError:
            pass
        return trades

    def _save_open_trades(self, trades: list[dict]):
        with open(OPEN_TRADES_PATH, "w") as f:
            for t in trades:
                f.write(json.dumps(t) + "\n")
