"""
AI Analyst — uses Claude to reason about markets and size trades.

Learning loop improvements:
  1. Slow critique every 5 cycles (not 10) — faster feedback from resolved trades
  2. Exit-type awareness — distinguishes stop-losses vs partial exits vs resolution
  3. Fast-learning every cycle — open position drift adjusts per-tag sizing multiplier
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
1. What exactly resolves this market? What are the exact resolution criteria?
2. What do the news snippets tell you?
3. What base rate applies?
4. Your probability estimate for YES.
5. Edge and EV. Size proportional to confidence × edge.

NEAR-RESOLVED MARKETS (YES > 0.90 or YES < 0.10):
These are high-confidence opportunities — trade WITH or AGAINST the crowd based purely on evidence.

WITH the crowd (most common, largest positions):
- YES > 0.90 and outcome is near-certain → buy YES, collect $1.00 per share at resolution
- YES < 0.10 and outcome is near-certainly NO → buy NO, collect $1.00 per share at resolution
- Example: "Will Jesus return in 2026?" YES=0.01 with 2 hours left → buy NO aggressively, 99x near-certain
- Example: "Will [team] win?" YES=0.97, game already finished and they won → buy YES, guaranteed payout
- Size up to size_fraction=1.0 when near-certain. This is where biggest returns come from.

AGAINST the crowd (requires strong contradicting evidence):
- YES > 0.90 but you have clear evidence outcome will NOT resolve YES → buy NO
- YES < 0.10 but you have clear evidence outcome WILL resolve YES → buy YES

Only skip if genuinely uncertain. Never skip solely because the price is extreme.

Strategy tags (pick 1-2):
news_momentum, mean_reversion, base_rate, expert_consensus, late_mover, thin_market, sentiment_gap, arbitrage, tail_risk, contrarian, near_certain

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
size_fraction is 0-1 where 1.0 = your maximum allowed trade size shown in the prompt.
trade only required when should_trade is true."""


STRATEGY_CRITIQUE_PROMPT = """You are reviewing a Polymarket trading bot's performance. Write 3-5 bullet points of actionable strategy notes.

Be specific about:
- Which strategy tags are working and why
- Which are losing and what pattern explains it
- How stop-losses and partial exits are performing vs natural resolution
- What to do differently for sizing, market selection, or confidence thresholds

Plain text only, no JSON."""


class AIAnalyst:
    def __init__(self, config: Config, audit: AuditLog):
        self.config = config
        self.audit = audit
        self.client = anthropic.Anthropic(api_key=config.anthropic_api_key)
        self._cycle_count = 0
        # Fast-learning: per-tag sizing multiplier, updated every cycle from open position drift
        # Range 0.5-1.5 — tags with firing partial exits get boosted, tags with stop-losses get penalised
        self._tag_multipliers: dict[str, float] = {}

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
        # Apply fast-learning tag multiplier to size_fraction
        if result.get("should_trade") and result.get("trade"):
            fraction = float(result["trade"].get("size_fraction", 0.5))
            fraction = self._apply_tag_multiplier(fraction, result.get("strategy_tags", []))
            usdc_size = self.config.compute_trade_size(balance, fraction)
            result["trade"]["usdc_size"] = usdc_size
            result["trade"]["size_fraction"] = fraction

        return result

    async def maybe_update_strategy(self):
        """
        Two-speed learning:
        - Fast (every cycle): update tag multipliers from open position drift
        - Slow (every 5 cycles): full Claude critique of resolved trade history
        """
        self._cycle_count += 1

        # Fast-learning: always update tag multipliers from open position behaviour
        self._update_tag_multipliers()

        # Slow-learning: full critique every 5 cycles if enough resolved trades
        if self._cycle_count % 5 != 0:
            return

        stats = self.audit.get_performance_summary()
        if stats.get("total", 0) < self.config.min_trades_for_learning:
            log.debug(f"Not enough resolved trades for critique ({stats.get('total',0)} < {self.config.min_trades_for_learning})")
            return

        log.info("Running strategy self-critique...")
        try:
            tag_stats = stats.get("tag_stats", {})
            exit_stats = stats.get("exit_stats", {})

            # Format tag stats with exit type breakdown
            tag_lines = []
            for tag, s in sorted(tag_stats.items(), key=lambda x: x[1]["pnl"], reverse=True):
                wr = s["wins"] / s["total"] * 100 if s["total"] else 0
                sl = s.get("stop_losses", 0)
                pe = s.get("partial_exits", 0)
                mult = self._tag_multipliers.get(tag, 1.0)
                tag_lines.append(
                    f"  {tag}: {s['total']} trades | {wr:.0f}% win | ${s['pnl']:.2f} P&L | "
                    f"{sl} stop-losses | {pe} partial exits | current_multiplier={mult:.2f}x"
                )

            # Format exit type stats
            exit_lines = []
            for et, s in exit_stats.items():
                wr = s["wins"] / s["total"] * 100 if s["total"] else 0
                exit_lines.append(f"  {et}: {s['total']} trades | {wr:.0f}% win | ${s['pnl']:.2f} P&L")

            critique_prompt = f"""PERFORMANCE SUMMARY:
Total resolved trades: {stats['total']}
Win rate: {stats.get('win_rate', 0)*100:.1f}%
Total P&L: ${stats.get('total_pnl', 0):.2f}
ROI: {stats.get('roi_pct', 0):.1f}%

STRATEGY TAG BREAKDOWN (includes stop-loss/partial-exit counts and current size multiplier):
{chr(10).join(tag_lines)}

EXIT TYPE BREAKDOWN:
{chr(10).join(exit_lines)}

MOST RECENT 10 RESOLVED TRADES:
{json.dumps(stats.get('recent_10', []), indent=2)}

Write updated strategy notes. Pay attention to which tags are triggering stop-losses vs winning at resolution."""

            response = self.client.messages.create(
                model="claude-sonnet-4-20250514",
                max_tokens=600,
                system=STRATEGY_CRITIQUE_PROMPT,
                messages=[{"role": "user", "content": critique_prompt}],
            )

            notes = response.content[0].text.strip()
            self.audit.save_strategy_notes(notes)
            log.info("Strategy notes updated")

        except Exception as e:
            log.error(f"Strategy critique failed: {e}")

    # ── Fast-learning ────────────────────────────────────────────────────────

    def _update_tag_multipliers(self):
        """
        Every cycle: update per-tag size multipliers based on open position behaviour.

        Logic:
        - Tags whose open positions have fired partial exits → slightly boost (good signal)
        - Tags whose resolved trades had stop-losses → penalise sizing
        - Tags with consistent resolved wins → restore toward 1.0
        - Multiplier range clamped to [0.5, 1.5]
        """
        drift = self.audit.get_open_position_drift()
        resolved = self.audit.get_performance_summary()
        tag_stats = resolved.get("tag_stats", {})

        for tag, d in drift.items():
            current = self._tag_multipliers.get(tag, 1.0)
            # Partial exits firing = position moving our way = slight boost
            if d.get("partial_exits_fired", 0) > 0:
                current = min(1.5, current + 0.05)

        for tag, s in tag_stats.items():
            current = self._tag_multipliers.get(tag, 1.0)
            total = s.get("total", 0)
            if total < 3:
                continue
            stop_loss_rate = s.get("stop_losses", 0) / total
            win_rate = s.get("wins", 0) / total

            # High stop-loss rate = reduce sizing for this tag
            if stop_loss_rate > 0.3:
                current = max(0.5, current - 0.1)
            # High win rate with low stop-losses = restore toward 1.0
            elif win_rate > 0.6 and stop_loss_rate < 0.1:
                current = min(1.0, current + 0.05)

            self._tag_multipliers[tag] = round(current, 3)

        if self._tag_multipliers:
            log.debug(f"Tag multipliers: {self._tag_multipliers}")

    def _apply_tag_multiplier(self, fraction: float, tags: list[str]) -> float:
        """Scale size_fraction by the average multiplier of the trade's strategy tags."""
        if not tags or not self._tag_multipliers:
            return fraction
        multipliers = [self._tag_multipliers.get(tag, 1.0) for tag in tags]
        avg_multiplier = sum(multipliers) / len(multipliers)
        adjusted = fraction * avg_multiplier
        return round(max(0.05, min(1.0, adjusted)), 3)

    # ── Context builders ─────────────────────────────────────────────────────

    def _build_strategy_context(self) -> str:
        parts = []
        stats = self.audit.get_performance_summary()

        if stats.get("total", 0) >= self.config.min_trades_for_learning:
            parts.append(f"""
## Your recent performance
Resolved trades: {stats['total']} | Win rate: {stats.get('win_rate',0)*100:.1f}% | P&L: ${stats.get('total_pnl',0):.2f} | ROI: {stats.get('roi_pct',0):.1f}%""")

            tag_stats = stats.get("tag_stats", {})
            if tag_stats:
                lines = ["Strategy tag performance (win rate | P&L | stop-losses | size multiplier):"]
                for tag, s in sorted(tag_stats.items(), key=lambda x: x[1]["pnl"], reverse=True):
                    wr = s["wins"] / s["total"] * 100 if s["total"] else 0
                    sl = s.get("stop_losses", 0)
                    mult = self._tag_multipliers.get(tag, 1.0)
                    lines.append(f"  {tag}: {wr:.0f}% win | ${s['pnl']:.2f} | {sl} SL | {mult:.2f}x sizing")
                parts.append("\n".join(lines))

            exit_stats = stats.get("exit_stats", {})
            if exit_stats:
                lines = ["Exit type performance:"]
                for et, s in exit_stats.items():
                    wr = s["wins"] / s["total"] * 100 if s["total"] else 0
                    lines.append(f"  {et}: {s['total']} trades | {wr:.0f}% win | ${s['pnl']:.2f} P&L")
                parts.append("\n".join(lines))

        notes = self.audit.load_strategy_notes()
        if notes:
            parts.append(f"\n## Your strategy notes (self-written after reviewing past trades)\n{notes}")

        return "\n".join(parts) if parts else ""

    def _build_prompt(self, market: dict, yes_price: float,
                      snippets: list[str], balance: float | None) -> str:
        question = market["question"]
        liquidity = float(market.get("liquidity") or 0)
        end_date = market.get("endDate") or market.get("end_date_iso") or "Unknown"
        description = (market.get("description") or "")[:200]
        search_text = "\n".join(f"- {s[:120]}" for s in snippets[:4]) if snippets else "No results."

        max_size = self.config.compute_trade_size(balance, 1.0)
        if balance and balance > 0:
            balance_line = f"Wallet: ${balance:.2f} USDC | Max trade (size_fraction=1.0): ${max_size:.2f}"
        else:
            balance_line = f"Max trade (size_fraction=1.0): ${max_size:.2f}"

        near_resolved = yes_price > 0.90 or yes_price < 0.10
        near_flag = "\n⚠️  NEAR-RESOLVED MARKET — high confidence opportunity, size up if certain." if near_resolved else ""

        return f"""Market: {question}
Desc: {description}
YES: {yes_price:.3f} | NO: {1-yes_price:.3f} | Liq: ${liquidity:,.0f} | Resolves: {end_date}
{balance_line}{near_flag}
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
