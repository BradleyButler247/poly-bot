"""
Configuration — all values come from environment variables.
Set these in Railway's Variables tab.
"""

import os
from dataclasses import dataclass, field


@dataclass
class Config:
    # ── Anthropic ──────────────────────────────────────────────────────────
    anthropic_api_key: str = field(default_factory=lambda: _require("ANTHROPIC_API_KEY"))

    # ── Polymarket ─────────────────────────────────────────────────────────
    # Get these from: https://docs.polymarket.com/#authentication
    poly_api_key: str = field(default_factory=lambda: _require("POLY_API_KEY"))
    poly_api_secret: str = field(default_factory=lambda: _require("POLY_API_SECRET"))
    poly_api_passphrase: str = field(default_factory=lambda: _require("POLY_API_PASSPHRASE"))
    # Your Polygon wallet private key (KEEP SECRET — never commit to git)
    wallet_private_key: str = field(default_factory=lambda: _require("WALLET_PRIVATE_KEY"))

    # ── Risk limits ────────────────────────────────────────────────────────
    # Max USDC per single trade
    max_trade_usdc: float = field(default_factory=lambda: float(os.getenv("MAX_TRADE_USDC", "10")))
    # Max total USDC lost in one calendar day before bot pauses
    max_daily_loss_usdc: float = field(default_factory=lambda: float(os.getenv("MAX_DAILY_LOSS_USDC", "50")))
    # Max USDC exposed in any single market at once
    max_position_usdc: float = field(default_factory=lambda: float(os.getenv("MAX_POSITION_USDC", "25")))
    # Minimum edge required to trade (market_price vs our estimated probability)
    min_edge: float = field(default_factory=lambda: float(os.getenv("MIN_EDGE", "0.05")))
    # Minimum market liquidity (USDC) — skip thin markets
    min_liquidity_usdc: float = field(default_factory=lambda: float(os.getenv("MIN_LIQUIDITY_USDC", "5000")))

    # ── Bot behaviour ──────────────────────────────────────────────────────
    # Seconds between full scan cycles
    cycle_interval_seconds: int = field(default_factory=lambda: int(os.getenv("CYCLE_INTERVAL_SECONDS", "300")))
    # Max markets to evaluate per cycle (controls API costs)
    max_markets_per_cycle: int = field(default_factory=lambda: int(os.getenv("MAX_MARKETS_PER_CYCLE", "10")))
    # Categories to focus on (comma-separated); empty = all
    market_categories: list = field(
        default_factory=lambda: [
            c.strip() for c in os.getenv("MARKET_CATEGORIES", "").split(",") if c.strip()
        ]
    )

    # ── Position sizing ────────────────────────────────────────────────────
    # "kelly" (recommended) or "fixed"
    sizing_strategy: str = field(default_factory=lambda: os.getenv("SIZING_STRATEGY", "kelly"))
    # Kelly fraction (0.25 = quarter-Kelly, safer)
    kelly_fraction: float = field(default_factory=lambda: float(os.getenv("KELLY_FRACTION", "0.25")))

    # ── Polymarket CLOB host ───────────────────────────────────────────────
    clob_host: str = field(default_factory=lambda: os.getenv("CLOB_HOST", "https://clob.polymarket.com"))
    gamma_host: str = field(default_factory=lambda: os.getenv("GAMMA_HOST", "https://gamma-api.polymarket.com"))


def _require(key: str) -> str:
    val = os.getenv(key)
    if not val:
        raise EnvironmentError(
            f"Required environment variable '{key}' is not set. "
            f"Add it in your Railway project → Variables tab."
        )
    return val
