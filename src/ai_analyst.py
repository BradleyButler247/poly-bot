"""
AI Analyst — uses Claude to reason about markets and maximise expected value.

Each analysis cycle Claude receives:
  - Market question + current odds + liquidity
  - Recent web search results
  - Performance stats from past trades
  - Strategy notes Claude wrote after reviewing past failures/successes
  - Categorised breakdown of what's working and what isn't

Claude outputs a trade decision with EV-based sizing (no minimum edge floor).
After enough trades accumulate, Claude also writes updated strategy notes.
"""

import json
import logging
import re
import anthropic

from .config import Config
from .audit_log import AuditLog

log = logging.getLogger("ai_analyst")

BASE_SYSTEM_PROMPT = """You are an expert prediction market trader. Your single goal is to maximise
long-run profit (expected value) on Polymarket binary markets.

A binary market resolves YES or NO. Prices are implied probabilities (0–1).
If YES trades at 0.30 but your best estimate is 0.45, you have +15pp edge — buy YES.
If YES trades at 0.72 but you think true probability is 0.55, you have +17pp edge — buy NO.

## Decision framework
1. Understand exactly what resolves the market (read description carefully).
2. Weigh the web search evidence. Prioritise recency and source quality.
3. Apply a base rate / reference class (e.g. "polls miss by avg X pts", "incumbents win Y% of time").
4. Form your probability estimate. Be honest about uncertainty — widen intervals, don't fake precision.
5. Compute edge = |your_prob - market_price|. Compute EV = edge * (1/market_price - 1) roughly.
6. Decide whether to trade and how much. Size proportional to edge and confidence.
   - High confidence + large edge → larger fraction of max allowed size
   - Low confidence or small edge → smaller size or skip
   - Never bet just because edge is marginally positive if confidence is low
7. Tag the trade with strategy categories (see below) so the learning loop can track them.

## Strategy tags (pick 1–3 that best describe why you're trading this market)
- "news_momentum"     — strong directional news flow clearly not priced in
- "mean_reversion"    — market has overreacted, fundamentals unchanged
- "base_rate"         — market ignoring well-established historical rate
- "expert_consensus"  — clear expert/institutional consensus vs market
- "late_mover"        — closing soon, resolution nearly certain
- "thin_market"       — low liquidity creates mispricing opportunity
- "sentiment_gap"     — social/media sentiment divorced from fundamentals
- "arbitrage"         — related markets imply inconsistency

## Output format
Return ONLY valid JSON, no markdown fences:
{
  "should_trade": true | false,
  "reasoning": "string — your full chain of reasoning, 3–6 sentences",
  "your_probability": 0.0–1.0,
  "market_price": 0.0–1.0,
  "edge": 0.0–1.0,
  "ev_score": -1.0–1.0,
  "trade": {
    "outcome": "YES" | "NO",
    "price": 0.0–1.0,
    "size_fraction": 0.0–1.0,
    "usdc_size": 0.0
  },
  "confidence": "low" | "medium" | "high",
  "strategy_tags": ["tag1", "tag2"]
}
trade field is required only when should_trade is true.
size_fraction is 0–1 representing fraction of max allowed trade size to use."""


STRATEGY_CRITIQUE_PROMPT = """You are reviewing the recent performance of a Polymarket trading bot
that you also operate. Your job is to update the trading strategy based on what's working and what isn't.

Review the resolved trades and performance stats provided. Then write concise, actionable strategy notes
(3–8 bullet points) that should guide future trading decisions. Focus on:
- Which market categories / strategy tags are profitable vs losing
- Systematic biases (e.g. overconfident on political markets, underestimating late-mover edge)
- Calibration issues (are your probability estimates consistently too high or too low?)
- Sizing mistakes (betting too much on low-confidence trades?)
- Any patterns in winning vs losing trades

Be specific and self-critical. These notes will be injected into your system prompt for every
future analysis, so they must be actionable.

Return ONLY the bullet points as a plain string, no JSON."""


class AIAnalyst:
    def __init__(self, config: Config, audit: AuditLog):
        self.config = config
        self.audit = audit
        self.client = anthropic.Anthropic(api_key=config.anthropic_api_key)
        self._cycle_count = 0

    async def analyse(self, market: dict) -> dict:
        question = market["question"]
        yes_price = self._get_yes_price(market)

        search_snippets = await self._search_context(question)
        strategy_context = self._build_strategy_context()
        system = BASE_SYSTEM_PROMPT + strategy_context
        user_prompt = self._build_prompt(market, yes_price, search_snippets)

        response = self.client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=1024,
            system=system,
            messages=[{"role": "user", "content": user_prompt}],
        )

        raw = response.content[0].text.strip()
        result = self._parse_response(raw, yes_price)

        # Apply size_fraction to get actual USDC size
        if result.get("should_trade") and result.get("trade"):
            fraction = float(result["trade"].get("size_fraction", 0.5))
            fraction = max(0.05, min(1.0, fraction))
            result["trade"]["usdc_size"] = round(self.config.max_trade_usdc * fraction, 2)

        return result

    async def maybe_update_strategy(self):
        """
        Periodically ask Claude to review past performance and update strategy notes.
        Runs every 10 cycles if we have enough resolved trades.
        """
        self._cycle_count += 1
        if self._cycle_count % 10 != 0:
            return

        stats = self.audit.get_performance_summary()
        if stats.get("total", 0) < self.config.min_trades_for_learning:
            return

        log.info("Running strategy self-critique...")
        try:
            recent = stats.get("recent_10", [])
            tag_stats = stats.get("tag_stats", {})

            critique_prompt = f"""PERFORMANCE SUMMARY:
Total trades: {stats['total']}
Win rate: {stats.get('win_rate', 0)*100:.1f}%
Total P&L: ${stats.get('total_pnl', 0):.2f}
ROI: {stats.get('roi_pct', 0):.1f}%

STRATEGY TAG BREAKDOWN:
{json.dumps(tag_stats, indent=2)}

MOST RECENT 10 RESOLVED TRADES:
{json.dumps(recent, indent=2)}

Write updated strategy notes based on this performance data."""

            response = self.client.messages.create(
                model="claude-sonnet-4-20250514",
                max_tokens=512,
                system=STRATEGY_CRITIQUE_PROMPT,
                messages=[{"role": "user", "content": critique_prompt}],
            )

            notes = response.content[0].text.strip()
            self.audit.save_strategy_notes(notes)
            log.info("Strategy notes updated")

        except Exception as e:
            log.error(f"Strategy critique failed: {e}")

    # ── Private ──────────────────────────────────────────────────────────────

    def _build_strategy_context(self) -> str:
        """Append learned strategy notes + recent stats to the system prompt."""
        parts = []

        # Performance stats
        stats = self.audit.get_performance_summary()
        if stats.get("total", 0) >= self.config.min_trades_for_learning:
            parts.append(f"""
## Your recent performance
Total resolved trades: {stats['total']} | Win rate: {stats.get('win_rate',0)*100:.1f}% | P&L: ${stats.get('total_pnl',0):.2f} | ROI: {stats.get('roi_pct',0):.1f}%""")

            tag_stats = stats.get("tag_stats", {})
            if tag_stats:
                lines = ["Strategy tag performance:"]
                for tag, s in sorted(tag_stats.items(), key=lambda x: x[1]["pnl"], reverse=True):
                    wr = s["wins"] / s["total"] * 100 if s["total"] else 0
                    lines.append(f"  {tag}: {s['total']} trades, {wr:.0f}% win rate, ${s['pnl']:.2f} P&L")
                parts.append("\n".join(lines))

        # Strategy notes from last self-critique
        notes = self.audit.load_strategy_notes()
        if notes:
            parts.append(f"\n## Your own strategy notes (written after reviewing past trades)\n{notes}")

        return "\n".join(parts) if parts else ""

    def _build_prompt(self, market: dict, yes_price: float, snippets: list[str]) -> str:
        question = market["question"]
        liquidity = float(market.get("liquidity") or 0)
        end_date = market.get("endDate") or market.get("end_date_iso") or "Unknown"
        description = market.get("description") or ""
        search_text = "\n".join(f"- {s}" for s in snippets) if snippets else "No results found."

        return f"""MARKET: {question}

DESCRIPTION: {description or "N/A"}

YES PRICE: {yes_price:.4f}  (implied prob: {yes_price*100:.1f}%)
NO PRICE:  {1-yes_price:.4f}  (implied prob: {(1-yes_price)*100:.1f}%)
LIQUIDITY: ${liquidity:,.0f} USDC
RESOLVES:  {end_date}
MAX TRADE: ${self.config.max_trade_usdc:.2f} USDC

RECENT WEB SEARCH RESULTS:
{search_text}

Analyse this market and return your JSON trade decision.
Use size_fraction (0–1) to express how much of the ${self.config.max_trade_usdc:.2f} max you want to use.
Maximise expected value — do not apply any arbitrary minimum edge threshold."""

    async def _search_context(self, question: str) -> list[str]:
        try:
            search_response = self.client.messages.create(
                model="claude-sonnet-4-20250514",
                max_tokens=1024,
                tools=[{"type": "web_search_20250305", "name": "web_search"}],
                messages=[{
                    "role": "user",
                    "content": (
                        f'Search for recent news and data relevant to this prediction market: '
                        f'"{question}". Summarise 4–6 key facts that help estimate the probability. '
                        f"Focus on the most recent developments and any data points (polls, odds, statistics)."
                    ),
                }],
            )
            snippets = []
            for block in search_response.content:
                if block.type == "text" and block.text:
                    sentences = [s.strip() for s in block.text.split(".") if len(s.strip()) > 20]
                    snippets.extend(sentences[:8])
            return snippets
        except Exception as e:
            log.warning(f"Web search failed: {e}")
            return []

    def _get_yes_price(self, market: dict) -> float:
        tokens = market.get("tokens") or []
        for token in tokens:
            if str(token.get("outcome", "")).upper() == "YES":
                price = token.get("price")
                if price is not None:
                    return float(price)
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
        return 0.5

    def _parse_response(self, raw: str, yes_price: float) -> dict:
        try:
            cleaned = re.sub(r"```json|```", "", raw).strip()
            result = json.loads(cleaned)
            edge = abs(result.get("your_probability", 0.5) - yes_price)
            result["edge"] = round(edge, 4)
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
                "strategy_tags": [],
            }
