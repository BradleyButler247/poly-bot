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
        markets = await self._fetch_active_markets()
        log.info(f"Fetched {len(markets)} raw markets from Gamma API")

        candidates = []
        filter_counts = {"liquidity": 0, "outcomes": 0, "expired": 0, "category": 0}
        for m in markets:
            passed, reason = self._passes_prefilter(m)
            if passed:
                candidates.append(m)
            else:
                filter_counts[reason] = filter_counts.get(reason, 0) + 1

        log.info(f"Pre-filter results: {len(candidates)} passed, filtered out → {filter_counts}")
        candidates.sort(key=lambda m: float(m.get("liquidity") or 0), reverse=True)
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
            params["tag"] = self.config.market_categories[0]

        url = f"{self.config.gamma_host}/markets"
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                    resp.raise_for_status()
                    data = await resp.json()
            if isinstance(data, list):
                return data
            return data.get("markets", [])
        except Exception as e:
            log.error(f"Failed to fetch markets from Gamma API: {e}")
            return []

    def _passes_prefilter(self, market: dict) -> tuple[bool, str]:
        import datetime
        import json as _json

        # Liquidity check
        liquidity = float(market.get("liquidity") or 0)
        if liquidity < self.config.min_liquidity_usdc:
            return False, "liquidity"

        # Binary market check — outcomes field can be a JSON string or a list
        outcomes_raw = market.get("outcomes", [])
        if isinstance(outcomes_raw, str):
            try:
                outcomes_raw = _json.loads(outcomes_raw)
            except Exception:
                outcomes_raw = []
        if len(outcomes_raw) != 2:
            return False, "outcomes"

        # Skip markets resolving in < 1 hour
        end_date = market.get("endDate") or market.get("end_date_iso")
        if end_date:
            try:
                closes = datetime.datetime.fromisoformat(end_date.replace("Z", "+00:00"))
                now = datetime.datetime.now(datetime.timezone.utc)
                if (closes - now).total_seconds() / 3600 < 1:
                    return False, "expired"
            except Exception:
                pass

        # Category filter
        if self.config.market_categories:
            tags = [t.lower() for t in (market.get("tags") or [])]
            if not any(cat.lower() in tags for cat in self.config.market_categories):
                return False, "category"

        return True, "ok"

    async def get_market_orderbook(self, token_id: str) -> dict[str, Any]:
        """Fetch the live order book for a specific outcome token."""
        url = f"{self.config.clob_host}/book"
        params = {"token_id": token_id}
        async with aiohttp.ClientSession() as session:
            async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                resp.raise_for_status()
                return await resp.json()
