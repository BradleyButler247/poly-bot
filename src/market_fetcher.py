"""
Market Fetcher — pulls open markets from Polymarket's Gamma API
and filters them down to tradeable candidates.
"""

import logging
import aiohttp
from typing import Any

from .config import Config

log = logging.getLogger("market_fetcher")


class MarketFetcher:
    def __init__(self, config: Config):
        self.config = config

    async def get_candidate_markets(self) -> list[dict]:
        """
        Fetch active markets, apply pre-filters, return the best candidates.
        """
        markets = await self._fetch_active_markets()
        candidates = [m for m in markets if self._passes_prefilter(m)]

        # Sort by liquidity descending, take top N
        candidates.sort(key=lambda m: float(m.get("liquidity", 0)), reverse=True)
        return candidates[: self.config.max_markets_per_cycle]

    async def _fetch_active_markets(self) -> list[dict]:
        params = {
            "active": "true",
            "closed": "false",
            "limit": 100,
            "order": "liquidity",
            "ascending": "false",
        }
        if self.config.market_categories:
            params["tag"] = self.config.market_categories[0]  # Gamma supports one tag filter

        url = f"{self.config.gamma_host}/markets"
        async with aiohttp.ClientSession() as session:
            async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                resp.raise_for_status()
                data = await resp.json()

        # Gamma returns a list directly or wrapped in {"markets": [...]}
        if isinstance(data, list):
            return data
        return data.get("markets", [])

    def _passes_prefilter(self, market: dict) -> bool:
        """Quick rule-based filters before spending API calls on AI analysis."""

        # Must have enough liquidity
        liquidity = float(market.get("liquidity") or 0)
        if liquidity < self.config.min_liquidity_usdc:
            return False

        # Must be binary (YES/NO) — easier to price
        outcomes = market.get("outcomes", [])
        if len(outcomes) != 2:
            return False

        # Skip markets resolving in < 1 hour (too close to resolution)
        import datetime
        end_date = market.get("endDate") or market.get("end_date_iso")
        if end_date:
            try:
                closes = datetime.datetime.fromisoformat(end_date.replace("Z", "+00:00"))
                now = datetime.datetime.now(datetime.timezone.utc)
                hours_left = (closes - now).total_seconds() / 3600
                if hours_left < 1:
                    return False
            except Exception:
                pass

        # Category filter (if set)
        if self.config.market_categories:
            tags = [t.lower() for t in (market.get("tags") or [])]
            if not any(cat.lower() in tags for cat in self.config.market_categories):
                return False

        return True

    async def get_market_orderbook(self, token_id: str) -> dict[str, Any]:
        """Fetch the live order book for a specific outcome token."""
        url = f"{self.config.clob_host}/book"
        params = {"token_id": token_id}
        async with aiohttp.ClientSession() as session:
            async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                resp.raise_for_status()
                return await resp.json()
