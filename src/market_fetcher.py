"""
Market Fetcher — pulls markets from Polymarket's Gamma API.

Per official docs, the events endpoint is most efficient for market discovery.
Events contain their associated markets, reducing API calls.
Markets are scored by a combination of liquidity and proximity to resolution.
"""

import json as _json
import logging
import datetime
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
        filter_counts = {}
        for m in markets:
            passed, reason = self._passes_prefilter(m)
            if passed:
                candidates.append(m)
            else:
                filter_counts[reason] = filter_counts.get(reason, 0) + 1

        log.info(f"Pre-filter: {len(candidates)} passed, filtered → {filter_counts}")

        now = datetime.datetime.now(datetime.timezone.utc)

        def score(m):
            liquidity = float(m.get("liquidity") or 0)
            end_date = m.get("endDate") or m.get("end_date_iso")
            days_left = 365
            if end_date:
                try:
                    closes = datetime.datetime.fromisoformat(end_date.replace("Z", "+00:00"))
                    days_left = max(1, (closes - now).total_seconds() / 86400)
                except Exception:
                    pass
            recency_bonus = max(0, (30 - days_left) / 30) * 5000
            return liquidity + recency_bonus

        candidates.sort(key=score, reverse=True)
        return candidates[: self.config.max_markets_per_cycle]

    async def _fetch_active_markets(self) -> list[dict]:
        """
        Use events endpoint per official docs — events contain their markets.
        Fall back to markets endpoint if needed.
        """
        url = f"{self.config.gamma_host}/events"
        params = {
            "active": "true",
            "closed": "false",
            "limit": 100,
            "order": "liquidity",
            "ascending": "false",
        }

        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                    resp.raise_for_status()
                    events = await resp.json()

            markets = []
            for event in (events if isinstance(events, list) else []):
                for market in (event.get("markets") or []):
                    if not market.get("liquidity") and event.get("liquidity"):
                        market["liquidity"] = event["liquidity"]
                    markets.append(market)

            if markets:
                return markets

            log.warning("Events endpoint returned no markets, falling back to /markets")
        except Exception as e:
            log.error(f"Events endpoint failed: {e}, falling back to /markets")

        return await self._fetch_markets_fallback()

    async def _fetch_markets_fallback(self) -> list[dict]:
        url = f"{self.config.gamma_host}/markets"
        params = {
            "active": "true",
            "closed": "false",
            "limit": 200,
            "order": "liquidity",
            "ascending": "false",
        }
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                    resp.raise_for_status()
                    data = await resp.json()
            return data if isinstance(data, list) else data.get("markets", [])
        except Exception as e:
            log.error(f"Fallback markets fetch failed: {e}")
            return []

    def _passes_prefilter(self, market: dict) -> tuple[bool, str]:
        # Liquidity check
        liquidity = float(market.get("liquidity") or 0)
        if liquidity < self.config.min_liquidity_usdc:
            return False, "liquidity"

        # Binary market — outcomes must be exactly 2
        outcomes_raw = market.get("outcomes", [])
        if isinstance(outcomes_raw, str):
            try:
                outcomes_raw = _json.loads(outcomes_raw)
            except Exception:
                outcomes_raw = []
        if len(outcomes_raw) != 2:
            return False, "outcomes"

        # Must have clobTokenIds for order placement
        clob_ids = market.get("clobTokenIds")
        if isinstance(clob_ids, str):
            try:
                clob_ids = _json.loads(clob_ids)
            except Exception:
                clob_ids = []
        tokens = market.get("tokens") or []
        if isinstance(tokens, str):
            try:
                tokens = _json.loads(tokens)
            except Exception:
                tokens = []
        has_token_ids = (isinstance(clob_ids, list) and len(clob_ids) >= 2) or bool(tokens)
        if not has_token_ids:
            return False, "no_token_ids"

        # Skip near-resolved markets — YES price outside 0.10-0.90
        yes_price = None
        for token in tokens:
            if str(token.get("outcome", "")).upper() == "YES":
                yes_price = float(token.get("price") or 0.5)
                break

        # Also check outcomePrices field (used by events endpoint)
        if yes_price is None:
            outcome_prices = market.get("outcomePrices")
            if outcome_prices:
                if isinstance(outcome_prices, str):
                    try:
                        outcome_prices = _json.loads(outcome_prices)
                    except Exception:
                        outcome_prices = None
                if isinstance(outcome_prices, list) and len(outcome_prices) > 0:
                    yes_price = float(outcome_prices[0])

        if yes_price is not None and (yes_price > 0.90 or yes_price < 0.10):
            return False, "near_resolved"

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

        # Skip markets with very short or generic questions (usually bad data)
        question = market.get("question") or ""
        if len(question) < 15:
            return False, "liquidity"

        return True, "ok"

    async def get_market_orderbook(self, token_id: str) -> dict[str, Any]:
        url = f"{self.config.clob_host}/book"
        params = {"token_id": token_id}
        async with aiohttp.ClientSession() as session:
            async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                resp.raise_for_status()
                return await resp.json()
