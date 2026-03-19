"""
AI Analyst — uses Claude to reason about each market and decide whether to trade.

Claude gets:
  - Market question + current odds
  - Recent web search results about the topic
  - Statistical context (base rates, historical accuracy)
  - Your custom trading rules

Claude returns a structured trade decision.
"""

import json
import logging
import re
import anthropic
import aiohttp

from .config import Config

log = logging.getLogger("ai_analyst")

SYSTEM_PROMPT = """You are an expert prediction market trader and probabilistic reasoner.

Your job: analyse a Polymarket binary market and decide whether there is a profitable edge to trade.

A binary market has two outcomes: YES and NO. Each trades at a price between 0 and 1 (= implied probability).
If YES trades at 0.30, the market implies 30% chance of YES. If you believe the true probability is 45%, you have +15% edge — buy YES.

Your analysis process:
1. What is the market actually asking? Be precise about the resolution criteria.
2. What do the news search results tell you? Weigh recency and source quality.
3. What base rate or reference class applies? (e.g. "incumbents win re-election X% of the time")
4. Synthesise into YOUR probability estimate for YES.
5. Compare to market price. Edge = |your_prob - market_price|.
6. Apply the user's custom rules.
7. Decide: trade or no trade.

IMPORTANT RULES:
- Only trade if edge >= MIN_EDGE (provided in the prompt).
- Be conservative with uncertainty. If you're unsure, widen your confidence interval and trade smaller or not at all.
- Never chase thin liquidity or markets with unclear resolution criteria.
- Document your reasoning clearly — the audit log depends on it.

Output ONLY valid JSON matching this schema:
{
  "should_trade": true | false,
  "reasoning": "string — your full reasoning chain, 2-5 sentences",
  "your_probability": 0.0-1.0,   // your estimate of YES probability
  "market_price": 0.0-1.0,       // current YES price (from the prompt)
  "edge": 0.0-1.0,               // abs(your_probability - market_price)
  "trade": {                      // only required if should_trade == true
    "outcome": "YES" | "NO",
    "price": 0.0-1.0,            // limit price to use
    "usdc_size": 0.0             // BEFORE risk manager adjusts it — put your "ideal" size
  },
  "confidence": "low" | "medium" | "high"
}"""


class AIAnalyst:
    def __init__(self, config: Config):
        self.config = config
        self.client = anthropic.Anthropic(api_key=config.anthropic_api_key)

    async def analyse(self, market: dict) -> dict:
        """
        Run full analysis pipeline for one market:
          1. Gather web search context
          2. Ask Claude to reason and decide
        """
        question = market["question"]
        yes_price = self._get_yes_price(market)

        # Gather news context
        search_snippets = await self._search_context(question)

        # Build the user prompt
        user_prompt = self._build_prompt(market, yes_price, search_snippets)

        # Call Claude
        response = self.client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=1024,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_prompt}],
        )

        raw = response.content[0].text.strip()
        return self._parse_response(raw, yes_price)

    def _build_prompt(self, market: dict, yes_price: float, snippets: list[str]) -> str:
        question = market["question"]
        liquidity = float(market.get("liquidity") or 0)
        end_date = market.get("endDate") or market.get("end_date_iso") or "Unknown"
        description = market.get("description") or ""

        search_text = "\n".join(f"- {s}" for s in snippets) if snippets else "No results found."

        custom_rules = """
USER'S CUSTOM TRADING RULES:
- Prefer political, sports, and macro-economic markets
- Avoid crypto price markets (too noisy)
- Avoid markets where resolution is subjective or unclear
- Be more aggressive (larger size) when confidence is high and edge > 10%
- Be conservative near market close (< 24h to resolve)
""".strip()

        return f"""MARKET: {question}

DESCRIPTION: {description or 'N/A'}

CURRENT YES PRICE: {yes_price:.3f} (implied probability: {yes_price*100:.1f}%)
LIQUIDITY: ${liquidity:,.0f} USDC
RESOLVES: {end_date}
MIN EDGE REQUIRED: {self.config.min_edge:.2f} ({self.config.min_edge*100:.0f}%)

RECENT WEB SEARCH RESULTS:
{search_text}

{custom_rules}

Analyse this market and return your JSON decision."""

    async def _search_context(self, question: str) -> list[str]:
        """
        Use Anthropic's web search tool to get current context.
        Returns a list of short text snippets.
        """
        try:
            search_response = self.client.messages.create(
                model="claude-sonnet-4-20250514",
                max_tokens=1024,
                tools=[{"type": "web_search_20250305", "name": "web_search"}],
                messages=[{
                    "role": "user",
                    "content": (
                        f"Search for recent news relevant to this prediction market question: "
                        f'"{question}". Return a concise summary of 3-5 key facts that would '
                        f"help estimate the probability. Focus on most recent developments."
                    ),
                }],
            )
            snippets = []
            for block in search_response.content:
                if block.type == "text" and block.text:
                    # Split into sentences for cleaner snippets
                    sentences = [s.strip() for s in block.text.split(".") if len(s.strip()) > 20]
                    snippets.extend(sentences[:6])
            return snippets
        except Exception as e:
            log.warning(f"Web search failed: {e}")
            return []

    def _get_yes_price(self, market: dict) -> float:
        """Extract the current YES price from the market dict."""
        # Gamma API stores prices in tokens list
        tokens = market.get("tokens") or []
        for token in tokens:
            if str(token.get("outcome", "")).upper() == "YES":
                price = token.get("price")
                if price is not None:
                    return float(price)

        # Fallback: outcomePrices field
        prices = market.get("outcomePrices")
        if prices:
            if isinstance(prices, list) and len(prices) > 0:
                return float(prices[0])
            if isinstance(prices, str):
                try:
                    parsed = json.loads(prices)
                    if isinstance(parsed, list):
                        return float(parsed[0])
                except Exception:
                    pass

        return 0.5  # unknown — default to 50/50

    def _parse_response(self, raw: str, yes_price: float) -> dict:
        """Parse Claude's JSON response, with fallback on malformed output."""
        try:
            # Strip markdown code fences if present
            cleaned = re.sub(r"```json|```", "", raw).strip()
            result = json.loads(cleaned)

            # Enforce edge threshold
            edge = abs(result.get("your_probability", 0.5) - yes_price)
            result["edge"] = round(edge, 4)
            if edge < self.config.min_edge:
                result["should_trade"] = False
                result["reasoning"] = (
                    result.get("reasoning", "") +
                    f" (Edge {edge:.3f} below threshold {self.config.min_edge:.3f})"
                )

            return result

        except Exception as e:
            log.error(f"Failed to parse AI response: {e}\nRaw: {raw[:300]}")
            return {
                "should_trade": False,
                "reasoning": f"Parse error: {e}",
                "your_probability": 0.5,
                "market_price": yes_price,
                "edge": 0.0,
                "confidence": "low",
            }
