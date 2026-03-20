"""
AI Analyst — uses Claude to reason about markets and size trades
relative to current wallet balance.
"""

import json
import logging
import re
import anthropic

from .config import Config
from .audit_log import AuditLog

log = logging.getLogger("ai_analyst")

BASE_SYSTEM_PROMPT = """You are a prediction market trader. Maximise long-run profit on Polymarket binary markets.

Prices are implied probabilities (0-1). Edge = |your_prob - market_price|.

Process:
1. What exactly resolves this market?
2. What do the news snippets tell you?
3. What base rate applies?
4. Your probability estimate for YES.
5. Edge and EV. Size proportional to confidence × edge.

Strategy tags (pick 1-2):
news_momentum, mean_reversion, base_rate, expert_consensus, late_mover, thin_market, sentiment_gap, arbitrage

Output ONLY valid JSON:
{
  "should_trade": true|false,
  "reasoning": "2-3 sentences max",
  "your_probability": 0.0-1.0,
  "market_price": 0.0-1.0,
  "edge": 0.0-1.0,
  "ev_score": -1.0-1.0,
  "trade": {"outcome": "YES"|"NO", "price": 0.0-1.0, "size_fraction": 0.0-1.0},
  "confidence": "low"|"medium"|"high",
  "strategy_tags": ["tag"]
}
size_fraction is 0-1 relative to your max allowed trade size.
trade only required when should_trade is true."""


STRATEGY_CRITIQUE_PROMPT = """Review this Polymarket bot's performance and write 3-5 bullet points of actionable strategy notes. Be specific about what's working and what isn't. Plain text only, no JSON."""


class AIAnalyst:
    def __init__(self, config: Config, audit: AuditLog):
        self.config = config
        self.audit = audit
        self.client = anthropic.Anthropic(api_key=config.anthropic_api_key)
        self._cycle_count = 0

    async def analyse(self, market: dict, balance: float | None = None) -> dict:
        question = market["question"]
        yes_price = self._get_yes_price(market)

        search_snippets = await self._search_context(question)
        strategy_context = self._build_strategy_context()
        system = BASE_SYSTEM_PROMPT + strategy_context
        user_prompt = self._build_prompt(market, yes_price, search_snippets, balance)

        response = self.client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=1024,
            system=system,
            messages=[{"role": "user", "content": user_prompt}],
        )

        raw = response.content[0].text.strip()
        result = self._parse_response(raw, yes_price)

        # Convert size_fraction → actual USDC using balance-relative sizing
        if result.get("should_trade") and result.get("trade"):
            fraction = float(result["trade"].get("size_fraction", 0.5))
            usdc_size = self.config.compute_trade_size(balance, fraction)
            result["trade"]["usdc_size"] = usdc_size
            result["trade"]["size_fraction"] = fraction

        return result

    async def maybe_update_strategy(self):
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

    def _build_strategy_context(self) -> str:
        parts = []
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

        notes = self.audit.load_strategy_notes()
        if notes:
            parts.append(f"\n## Your own strategy notes (written after reviewing past trades)\n{notes}")

        return "\n".join(parts) if parts else ""

    def _build_prompt(self, market: dict, yes_price: float,
                      snippets: list[str], balance: float | None) -> str:
        question = market["question"]
        liquidity = float(market.get("liquidity") or 0)
        end_date = market.get("endDate") or market.get("end_date_iso") or "Unknown"
        description = (market.get("description") or "")[:200]
        search_text = "\n".join(f"- {s[:120]}" for s in snippets[:4]) if snippets else "No results."

        # Show Claude the actual max it can deploy so sizing is meaningful
        if balance and balance > 0:
            max_size = self.config.compute_trade_size(balance, 1.0)
            balance_line = f"Balance: ${balance:.2f} | Max trade: ${max_size:.2f} (size_fraction=1.0)"
        else:
            balance_line = f"Max trade: ${self.config.max_trade_usdc:.2f} (size_fraction=1.0)"

        return f"""Market: {question}
Desc: {description}
YES: {yes_price:.3f} | NO: {1-yes_price:.3f} | Liq: ${liquidity:,.0f} | Resolves: {end_date}
{balance_line}
News:
{search_text}
Return JSON."""

    async def _search_context(self, question: str) -> list[str]:
        try:
            search_response = self.client.messages.create(
                model="claude-sonnet-4-20250514",
                max_tokens=400,
                tools=[{"type": "web_search_20250305", "name": "web_search"}],
                messages=[{
                    "role": "user",
                    "content": f'Find 3 key recent facts relevant to: "{question}". Be brief.'
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
