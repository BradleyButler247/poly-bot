"""
Resolution Checker — monitors open positions each cycle for:
  1. Natural market resolution  → records full realised P&L
  2. Partial take-profit exits  → scales out in tiers as gains accumulate
  3. Full stop-loss exit        → closes entire position on large loss

Also provides:
  - get_open_market_ids() → called at cycle start to skip already-held markets
  - record_open_trade()   → stores token_id for exit orders
"""

import json
import logging
import os
from datetime import datetime, timezone
from typing import Optional

import aiohttp

from .config import Config
from .audit_log import AuditLog
from .risk_manager import RiskManager

log = logging.getLogger("resolver")

OPEN_TRADES_PATH = os.getenv("OPEN_TRADES_PATH", "logs/open_trades.jsonl")


class ResolutionChecker:
    def __init__(self, config: Config, audit: AuditLog, risk: RiskManager):
        self.config = config
        self.audit = audit
        self.risk = risk
        os.makedirs("logs", exist_ok=True)

    # ── Public interface ───────────────────────────────────────────────────

    def get_open_market_ids(self) -> set[str]:
        """
        Returns set of market IDs we currently hold.
        Called at cycle start to filter out markets before analysis.
        Prevents double-entering a position across cycles.
        """
        return {
            t["market_id"]
            for t in self._load_open_trades()
            if not t.get("resolved")
        }

    def record_open_trade(self, market_id: str, question: str, order_id: str,
                          outcome_traded: str, price_paid: float, usdc_size: float,
                          your_probability: float, strategy_tags: list,
                          token_id: str = ""):
        shares = round(usdc_size / price_paid, 4) if price_paid > 0 else 0
        record = {
            "market_id": market_id,
            "question": question,
            "order_id": order_id,
            "outcome_traded": outcome_traded,
            "price_paid": price_paid,
            "usdc_size": usdc_size,
            "shares_remaining": shares,
            "cost_basis_remaining": usdc_size,
            "your_probability": your_probability,
            "strategy_tags": strategy_tags,
            "token_id": token_id,
            "partial_exits": [],
            "resolved": False,
            "ts": datetime.now(timezone.utc).isoformat(),
        }
        with open(OPEN_TRADES_PATH, "a") as f:
            f.write(json.dumps(record) + "\n")
        log.info(f"Recorded open trade: {question[:60]} | {shares} shares @ {price_paid}")

    async def check_resolutions(self):
        open_trades = self._load_open_trades()
        if not open_trades:
            return

        log.info(f"Checking {len(open_trades)} open positions")
        updated = []

        for trade in open_trades:
            if trade.get("resolved"):
                updated.append(trade)
                continue

            # 1. Natural resolution
            resolution = await self._fetch_resolution(trade["market_id"])
            if resolution is not None:
                trade = await self._handle_resolution(trade, resolution)
                updated.append(trade)
                continue

            # 2. Partial take-profit + stop-loss
            current_price = await self._fetch_current_outcome_price(
                trade["market_id"], trade["outcome_traded"]
            )
            if current_price is not None:
                trade = await self._check_exit_triggers(trade, current_price)

            updated.append(trade)

        self._save_open_trades(updated)

    # ── Resolution ─────────────────────────────────────────────────────────

    async def _handle_resolution(self, trade: dict, market_resolved_yes: bool) -> dict:
        won = (trade["outcome_traded"] == "YES" and market_resolved_yes) or \
              (trade["outcome_traded"] == "NO" and not market_resolved_yes)

        price_paid = float(trade["price_paid"])
        cost_remaining = float(trade.get("cost_basis_remaining", trade["usdc_size"]))
        shares_remaining = float(trade.get("shares_remaining", trade["usdc_size"] / price_paid))

        if won:
            pnl = round(shares_remaining - cost_remaining, 4)
        else:
            pnl = round(-cost_remaining, 4)

        partial_pnl = sum(p.get("pnl", 0) for p in trade.get("partial_exits", []))
        total_pnl = round(pnl + partial_pnl, 4)

        log.info(
            f"Market resolved: {trade['question'][:60]} "
            f"→ {'WIN' if won else 'LOSS'} "
            f"final=${pnl:+.2f} partial=${partial_pnl:+.2f} total=${total_pnl:+.2f}"
        )

        self.audit.log_resolved_trade(
            order_id=trade["order_id"],
            market_id=trade["market_id"],
            question=trade["question"],
            outcome_traded=trade["outcome_traded"],
            price_paid=price_paid,
            usdc_size=float(trade["usdc_size"]),
            market_resolved_yes=market_resolved_yes,
            pnl=total_pnl,
            your_probability=float(trade.get("your_probability", 0.5)),
            strategy_tags=trade.get("strategy_tags", []),
            exit_type="resolution",
        )
        self.risk.record_trade_result(trade["market_id"], total_pnl)

        trade["resolved"] = True
        trade["pnl"] = total_pnl
        trade["won"] = won
        return trade

    # ── Exit triggers ───────────────────────────────────────────────────────

    async def _check_exit_triggers(self, trade: dict, current_price: float) -> dict:
        price_paid = float(trade["price_paid"])
        cost_remaining = float(trade.get("cost_basis_remaining", trade["usdc_size"]))
        shares_remaining = float(trade.get("shares_remaining", trade["usdc_size"] / price_paid))

        if shares_remaining <= 0 or cost_remaining <= 0:
            return trade

        current_value = shares_remaining * current_price
        unrealised_pct = ((current_value - cost_remaining) / cost_remaining) * 100

        # ── Stop-loss: full exit ──────────────────────────────────────────
        if unrealised_pct <= -self.config.stop_loss_pct:
            log.info(
                f"STOP-LOSS: {trade['question'][:50]} "
                f"| loss={unrealised_pct:.1f}% — selling all {shares_remaining:.2f} shares"
            )
            result = await self._place_sell(trade, shares_remaining, current_price)
            if result["success"]:
                sell_price = result["sell_price"]
                proceeds = shares_remaining * sell_price
                pnl = round(proceeds - cost_remaining, 4)
                partial_pnl = sum(p.get("pnl", 0) for p in trade.get("partial_exits", []))
                total_pnl = round(pnl + partial_pnl, 4)

                self.audit.log_resolved_trade(
                    order_id=trade["order_id"],
                    market_id=trade["market_id"],
                    question=trade["question"],
                    outcome_traded=trade["outcome_traded"],
                    price_paid=price_paid,
                    usdc_size=float(trade["usdc_size"]),
                    market_resolved_yes=None,
                    pnl=total_pnl,
                    your_probability=float(trade.get("your_probability", 0.5)),
                    strategy_tags=trade.get("strategy_tags", []) + ["stop_loss"],
                    exit_type="stop_loss",
                )
                self.risk.record_trade_result(trade["market_id"], total_pnl)
                trade["resolved"] = True
                trade["pnl"] = total_pnl
                trade["won"] = False
            return trade

        # ── Partial take-profit tiers ─────────────────────────────────────
        tiers = self.config.get_partial_exit_tiers()
        fired_tiers = {p["tier_gain_pct"] for p in trade.get("partial_exits", [])}

        for gain_pct, sell_pct in tiers:
            if gain_pct in fired_tiers:
                continue
            if unrealised_pct < gain_pct:
                continue

            shares_to_sell = round(shares_remaining * (sell_pct / 100), 4)
            if shares_to_sell < 0.01:
                continue

            log.info(
                f"PARTIAL EXIT +{gain_pct:.0f}%: {trade['question'][:50]} "
                f"| selling {sell_pct:.0f}% = {shares_to_sell:.2f} shares "
                f"(unrealised={unrealised_pct:.1f}%)"
            )

            result = await self._place_sell(trade, shares_to_sell, current_price)
            if result["success"]:
                sell_price = result["sell_price"]
                cost_of_sold = cost_remaining * (sell_pct / 100)
                proceeds = shares_to_sell * sell_price
                partial_pnl = round(proceeds - cost_of_sold, 4)

                trade.setdefault("partial_exits", []).append({
                    "tier_gain_pct": gain_pct,
                    "sell_pct": sell_pct,
                    "shares_sold": shares_to_sell,
                    "sell_price": sell_price,
                    "cost_of_sold": round(cost_of_sold, 4),
                    "proceeds": round(proceeds, 4),
                    "pnl": partial_pnl,
                    "ts": datetime.now(timezone.utc).isoformat(),
                })
                trade["shares_remaining"] = round(shares_remaining - shares_to_sell, 4)
                trade["cost_basis_remaining"] = round(cost_remaining - cost_of_sold, 4)

                shares_remaining = trade["shares_remaining"]
                cost_remaining = trade["cost_basis_remaining"]
                fired_tiers.add(gain_pct)

                # Log partial exit to resolved log so learning loop can see it
                self.audit.log_resolved_trade(
                    order_id=trade["order_id"],
                    market_id=trade["market_id"],
                    question=trade["question"],
                    outcome_traded=trade["outcome_traded"],
                    price_paid=float(trade["price_paid"]),
                    usdc_size=round(cost_of_sold, 4),
                    market_resolved_yes=None,
                    pnl=partial_pnl,
                    your_probability=float(trade.get("your_probability", 0.5)),
                    strategy_tags=trade.get("strategy_tags", []),
                    exit_type="partial_take_profit",
                )

                log.info(
                    f"  Partial exit done: pnl=${partial_pnl:+.2f} "
                    f"| remaining={shares_remaining:.2f} shares (basis=${cost_remaining:.2f})"
                )
            else:
                log.warning(f"  Partial exit failed: {result.get('error')}")

        return trade

    # ── Sell order ─────────────────────────────────────────────────────────

    async def _place_sell(self, trade: dict, shares: float, current_price: float) -> dict:
        token_id = trade.get("token_id")
        if not token_id:
            log.warning(f"No token_id on trade {trade.get('order_id')} — cannot sell")
            return {"success": False, "error": "no token_id"}

        sell_price = round(max(0.001, min(0.999, current_price * 0.98)), 3)

        try:
            from py_clob_client.client import ClobClient
            from py_clob_client.clob_types import OrderArgs
            from py_clob_client.constants import POLYGON
            import os as _os

            client = ClobClient(
                host=self.config.clob_host,
                chain_id=POLYGON,
                key=self.config.wallet_private_key,
                signature_type=1,
                funder=_os.getenv("WALLET_ADDRESS", ""),
            )
            creds = client.create_or_derive_api_creds()
            client.set_api_creds(creds)

            resp = client.create_and_post_order(OrderArgs(
                token_id=token_id,
                price=sell_price,
                size=round(shares, 2),
                side="SELL",
            ))
            success = resp.get("success", False) or resp.get("orderID") is not None
            log.info(f"Sell response: {resp}")
            return {"success": success, "sell_price": sell_price, "raw": resp}

        except Exception as e:
            log.error(f"Sell order exception: {e}", exc_info=True)
            return {"success": False, "error": str(e)}

    # ── Helpers ────────────────────────────────────────────────────────────

    async def _fetch_current_outcome_price(self, market_id: str, outcome: str) -> Optional[float]:
        try:
            url = f"{self.config.gamma_host}/markets/{market_id}"
            async with aiohttp.ClientSession() as session:
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=8)) as resp:
                    if resp.status != 200:
                        return None
                    data = await resp.json()
            for token in (data.get("tokens") or []):
                if str(token.get("outcome", "")).upper() == "YES":
                    yes_price = float(token.get("price", 0.5))
                    return yes_price if outcome == "YES" else 1.0 - yes_price
        except Exception as e:
            log.warning(f"Price fetch failed for {market_id}: {e}")
        return None

    async def _fetch_resolution(self, market_id: str) -> Optional[bool]:
        url = f"{self.config.gamma_host}/markets/{market_id}"
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                    resp.raise_for_status()
                    data = await resp.json()

            if not data.get("resolved") and (data.get("active", True) and not data.get("closed", False)):
                return None

            resolution_val = data.get("resolutionSource") or data.get("resolution")
            if resolution_val:
                if str(resolution_val).upper() in ("YES", "1", "TRUE"):
                    return True
                if str(resolution_val).upper() in ("NO", "0", "FALSE"):
                    return False

            for token in (data.get("tokens") or []):
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
