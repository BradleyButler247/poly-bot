"""
Polymarket AI Trading Bot — main orchestration loop
"""

import asyncio
import logging
import signal
import sys
from datetime import datetime

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

    async def run(self):
        log.info("=== Polymarket AI Bot starting ===")
        self._running = True

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

        # Check if any open trades have resolved → feeds the learning loop
        await self.resolver.check_resolutions()

        # Every 10 cycles: Claude reviews its own performance and updates strategy notes
        await self.analyst.maybe_update_strategy()

        # Fetch and evaluate markets
        markets = await self.fetcher.get_candidate_markets()
        log.info(f"Evaluating {len(markets)} markets")

        for market in markets:
            if not self._running:
                break
            try:
                await self._evaluate_market(market, cycle_id)
            except Exception as e:
                log.error(f"Error on market {market.get('id')}: {e}")

    async def _evaluate_market(self, market: dict, cycle_id: str):
        market_id = market["id"]
        question = market["question"]
        log.info(f"  Analysing: {question[:70]}")

        analysis = await self.analyst.analyse(market)
        self.audit.log_analysis(cycle_id, market_id, question, analysis)

        if not analysis["should_trade"]:
            log.info(f"    → Skip ({analysis.get('confidence','?')} conf): {analysis['reasoning'][:70]}")
            return

        trade = analysis["trade"]
        trade["market_id"] = market_id

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

        result = await self.trader.place_order(market, trade)
        self.audit.log_trade(cycle_id, market_id, question, trade, result, analysis)

        # Register for resolution tracking so we can learn from this trade
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
            )

    def _stop(self):
        log.info("Shutdown signal — stopping after this cycle")
        self._running = False


async def main():
    bot = PolymarketBot()
    await bot.run()


if __name__ == "__main__":
    asyncio.run(main())
