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
    poly_api_key: str = field(default_factory=lambda: _require("POLY_API_KEY"))
    poly_api_secret: str = field(default_factory=lambda: _require("POLY_API_SECRET"))
    poly_api_passphrase: str = field(default_factory=lambda: _require("POLY_API_PASSPHRASE"))
    # Polymarket embedded wallet private key
    # Export from: polymarket.com → Profile → Settings → Export private key
    wallet_private_key: str = field(default_factory=lambda: _require("WALLET_PRIVATE_KEY"))

    # ── Risk limits (circuit breakers only — no edge floor) ───────────────
    max_trade_usdc: float = field(default_factory=lambda: float(os.getenv("MAX_TRADE_USDC", "20")))
    max_daily_loss_usdc: float = field(default_factory=lambda: float(os.getenv("MAX_DAILY_LOSS_USDC", "100")))
    max_position_usdc: float = field(default_factory=lambda: float(os.getenv("MAX_POSITION_USDC", "50")))
    min_liquidity_usdc: float = field(default_factory=lambda: float(os.getenv("MIN_LIQUIDITY_USDC", "1000")))

    # ── Bot behaviour ──────────────────────────────────────────────────────
    cycle_interval_seconds: int = field(default_factory=lambda: int(os.getenv("CYCLE_INTERVAL_SECONDS", "300")))
    max_markets_per_cycle: int = field(default_factory=lambda: int(os.getenv("MAX_MARKETS_PER_CYCLE", "15")))
    market_categories: list = field(
        default_factory=lambda: [
            c.strip() for c in os.getenv("MARKET_CATEGORIES", "").split(",") if c.strip()
        ]
    )

    # ── Position sizing ────────────────────────────────────────────────────
    sizing_strategy: str = field(default_factory=lambda: os.getenv("SIZING_STRATEGY", "kelly"))
    kelly_fraction: float = field(default_factory=lambda: float(os.getenv("KELLY_FRACTION", "0.3")))

    # ── Learning loop ──────────────────────────────────────────────────────
    # Resolved trades fed back into Claude's context for strategy refinement
    learning_lookback_trades: int = field(default_factory=lambda: int(os.getenv("LEARNING_LOOKBACK_TRADES", "50")))
    min_trades_for_learning: int = field(default_factory=lambda: int(os.getenv("MIN_TRADES_FOR_LEARNING", "5")))

    # ── Polymarket hosts ───────────────────────────────────────────────────
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
