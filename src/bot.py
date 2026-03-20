"""
Polymarket AI Trading Bot — main orchestration loop

Speed strategy:
- All markets analysed in parallel using asyncio.gather
- Tier 2 API: 450k tokens/min — 20 markets simultaneously is well within budget
- Balance fetched once per cycle and tracked optimistically across parallel trades
- Orders serialised 3 at a time to avoid CLOB race conditions
"""

import asyncio
import logging
import os
import signal
import sys
from datetime import datetime

os.makedirs("logs", exist_ok=True)

from .config import Config
from .market_fetcher import MarketFetcher
from .ai_analyst import AIAnalyst
from .trader import Trader
from .risk_manager import RiskManager
from .audit_log import AuditLog
from .resolution_checker import ResolutionChecker

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("logs/bot.log"),
    ],
)
log = logging.getLogger("bot")

ANALYSIS_CONCURRENCY = int(os.getenv("ANALYSIS_CONCURRENCY", "20"))
ORDER_CONCURRENCY = int(os.getenv("ORDER_CONCURRENCY", "3"))


async def _notify(event: str, data: dict):
    try:
        from .api_server import broadcast
        await broadcast(event, data)
    except Exception:
        pass


class PolymarketBot:
    def __init__(self):
        self.config = Config()
        self.audit = AuditLog()
        self.risk = RiskManager(self.config, self.audit)
        self.fetcher = MarketFetcher(self.config)
        self.analyst = AIAnalyst(self.config, self.audit)
        self.trader = Trader(self.config, self.risk, self.audit)
        self.resolver = ResolutionChecker(self.config, self.audit, self.risk)
        self._running = False
        self._analysis_sem = None
        self._order_sem = None
        self._balance_lock = None

    async def run(self):
        log.info("=== Polymarket AI Bot starting ===")
        self._running = True
        self._analysis_sem = asyncio.Semaphore(ANALYSIS_CONCURRENCY)
        self._order_sem = asyncio.Semaphore(ORDER_CONCURRENCY)
        self._balance_lock = asyncio.Lock()

        loop = asyncio.get_event_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, self._stop)

        while self._running:
            try:
                await self._cycle()
            except Exception as e:
                log.error(f"Cycle error: {e}", exc_info=True)
                self.audit.log_error(str(e))

            if self._running:
                log.info(f"Sleeping {self.config.cycle_interval_seconds}s")
                await asyncio.sleep(self.config.cycle_interval_seconds)

        log.info("Bot stopped cleanly.")

    async def _cycle(self):
        cycle_id = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
        log.info(f"--- Cycle {cycle_id} ---")

        if self.risk.emergency_stopped:
            log.warning("Emergency stop active — skipping")
            return
        if not self.risk.check_daily_loss_ok():
            log.warning("Daily loss limit hit — skipping")
            return

        # Fetch markets, check resolutions, update strategy — all at once
        results = await asyncio.gather(
            self.fetcher.get_candidate_markets(),
            self.resolver.check_resolutions(),
            self.analyst.maybe_update_strategy(),
            return_exceptions=True,
        )
        markets = results[0] if not isinstance(results[0], Exception) else []

        # Fetch live wallet balance — cap trades to what we actually have
        self._available_balance = await self.trader.fetch_balance()
        if self._available_balance is not None:
            log.info(f"Available balance: ${self._available_balance:.2f} USDC")
        else:
            log.warning("Could not fetch balance — using config MAX_TRADE_USDC as cap")

        if not markets:
            log.warning("No candidate markets this cycle")
            return

        log.info(f"Evaluating {len(markets)} markets in parallel")
        await _notify("cycle_start", {"cycle_id": cycle_id, "market_count": len(markets)})

        # Analyse all markets simultaneously — Tier 2 handles the throughput
        tasks = [self._evaluate_market(market, cycle_id) for market in markets]
        await asyncio.gather(*tasks, return_exceptions=True)

        await _notify("cycle_end", {"cycle_id": cycle_id})

    async def _evaluate_market(self, market: dict, cycle_id: str):
        market_id = market["id"]
        question = market["question"]

        try:
            async with self._analysis_sem:
                log.info(f"  Analysing: {question[:70]}")
                analysis = await self.analyst.analyse(market)
        except Exception as e:
            log.error(f"Analysis failed for {market_id}: {e}")
            return

        self.audit.log_analysis(cycle_id, market_id, question, analysis)

        if not analysis["should_trade"]:
            log.info(f"    → Skip ({analysis.get('confidence','?')} conf): {analysis['reasoning'][:70]}")
            return

        trade = analysis["trade"]
        trade["market_id"] = market_id

        # Cap trade size to available balance under a lock (parallel trades share balance)
        async with self._balance_lock:
            balance = self._available_balance
            if balance is not None:
                usdc_size = float(trade.get("usdc_size", self.config.max_trade_usdc))
                # Never spend more than 80% of remaining balance on a single trade
                max_allowed = min(self.config.max_trade_usdc, balance * 0.8)
                if max_allowed < 1.0:
                    log.info(f"    → Skip (insufficient balance: ${balance:.2f} USDC)")
                    return
                trade["usdc_size"] = round(min(usdc_size, max_allowed), 2)
                # Deduct optimistically so concurrent trades don't over-commit
                self._available_balance = balance - trade["usdc_size"]

        allowed, reason = self.risk.approve_trade(trade)
        if not allowed:
            log.info(f"    → Risk block: {reason}")
            self.audit.log_risk_block(cycle_id, market_id, trade, reason)
            return

        log.info(
            f"    → TRADE {trade['outcome']} @ {trade['price']:.3f} "
            f"${trade['usdc_size']:.2f} | edge={analysis.get('edge',0):.3f} "
            f"conf={analysis.get('confidence')} tags={analysis.get('strategy_tags',[])} "
        )

        # Serialise order placement — avoid hammering CLOB simultaneously
        async with self._order_sem:
            result = await self.trader.place_order(market, trade)

        # If order failed, return the balance
        if not result.get("success"):
            async with self._balance_lock:
                if self._available_balance is not None:
                    self._available_balance += trade.get("usdc_size", 0)

        self.audit.log_trade(cycle_id, market_id, question, trade, result, analysis)

        await _notify("trade", {
            "question": question,
            "outcome": trade["outcome"],
            "price": trade["price"],
            "usdc_size": trade["usdc_size"],
            "success": result.get("success"),
            "order_id": result.get("order_id"),
        })

        if result.get("success"):
            self.resolver.record_open_trade(
                market_id=market_id,
                question=question,
                order_id=result.get("order_id", ""),
                outcome_traded=trade["outcome"],
                price_paid=float(trade["price"]),
                usdc_size=float(trade["usdc_size"]),
                your_probability=float(analysis.get("your_probability", 0.5)),
                strategy_tags=analysis.get("strategy_tags", []),
                token_id=result.get("token_id", ""),
            )

    def _stop(self):
        log.info("Shutdown signal — stopping after this cycle")
        self._running = False


async def main():
    bot = PolymarketBot()
    await bot.run()


if __name__ == "__main__":
    asyncio.run(main())
