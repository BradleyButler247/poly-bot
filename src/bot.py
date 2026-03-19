"""
Polymarket AI Trading Bot
Main orchestration loop
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
        self.analyst = AIAnalyst(self.config)
        self.trader = Trader(self.config, self.risk, self.audit)
        self._running = False

    async def run(self):
        log.info("=== Polymarket AI Bot starting ===")
        self._running = True

        # Graceful shutdown on SIGTERM (Railway sends this)
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
                log.info(f"Sleeping {self.config.cycle_interval_seconds}s until next cycle")
                await asyncio.sleep(self.config.cycle_interval_seconds)

        log.info("Bot stopped cleanly.")

    async def _cycle(self):
        cycle_id = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
        log.info(f"--- Cycle {cycle_id} ---")

        # 1. Check emergency stop
        if self.risk.emergency_stopped:
            log.warning("Emergency stop active — skipping cycle")
            return

        # 2. Check daily loss limit
        if not self.risk.check_daily_loss_ok():
            log.warning("Daily loss limit hit — skipping cycle")
            return

        # 3. Fetch candidate markets
        markets = await self.fetcher.get_candidate_markets()
        log.info(f"Found {len(markets)} candidate markets")
        if not markets:
            return

        # 4. Analyse each market with Claude
        for market in markets:
            if not self._running:
                break
            try:
                await self._evaluate_market(market, cycle_id)
            except Exception as e:
                log.error(f"Error evaluating market {market.get('id')}: {e}")

    async def _evaluate_market(self, market: dict, cycle_id: str):
        market_id = market["id"]
        question = market["question"]
        log.info(f"Evaluating: {question}")

        # AI analysis
        analysis = await self.analyst.analyse(market)

        self.audit.log_analysis(cycle_id, market_id, question, analysis)

        if not analysis["should_trade"]:
            log.info(f"  → No trade: {analysis['reasoning'][:80]}")
            return

        # Risk check
        trade = analysis["trade"]
        allowed, reason = self.risk.approve_trade(trade)
        if not allowed:
            log.info(f"  → Risk blocked: {reason}")
            self.audit.log_risk_block(cycle_id, market_id, trade, reason)
            return

        # Execute
        log.info(f"  → TRADING: {trade['outcome']} @ {trade['price']:.3f} for ${trade['usdc_size']:.2f}")
        result = await self.trader.place_order(market, trade)
        self.audit.log_trade(cycle_id, market_id, trade, result)

    def _stop(self):
        log.info("Shutdown signal received")
        self._running = False


async def main():
    bot = PolymarketBot()
    await bot.run()


if __name__ == "__main__":
    asyncio.run(main())
