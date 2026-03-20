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
    wallet_private_key: str = field(default_factory=lambda: _require("WALLET_PRIVATE_KEY"))

    # ── Risk limits ────────────────────────────────────────────────────────
    max_trade_usdc: float = field(default_factory=lambda: float(os.getenv("MAX_TRADE_USDC", "20")))
    max_daily_loss_usdc: float = field(default_factory=lambda: float(os.getenv("MAX_DAILY_LOSS_USDC", "100")))
    max_position_usdc: float = field(default_factory=lambda: float(os.getenv("MAX_POSITION_USDC", "50")))
    min_liquidity_usdc: float = field(default_factory=lambda: float(os.getenv("MIN_LIQUIDITY_USDC", "1000")))

    # ── Exit thresholds ────────────────────────────────────────────────────
    # Stop-loss: full exit when unrealised loss exceeds this % of cost basis
    stop_loss_pct: float = field(default_factory=lambda: float(os.getenv("STOP_LOSS_PCT", "50")))

    # Partial take-profit tiers — comma-separated pairs of gain%:sell%
    # Default: sell 33% at +100%, sell 33% at +200%, sell 50% at +400%
    # Format: "gain_pct:sell_pct,gain_pct:sell_pct,..."
    # Each tier fires once and only once per position.
    partial_exit_tiers: str = field(
        default_factory=lambda: os.getenv("PARTIAL_EXIT_TIERS", "100:33,200:33,400:50")
    )

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
    learning_lookback_trades: int = field(default_factory=lambda: int(os.getenv("LEARNING_LOOKBACK_TRADES", "50")))
    min_trades_for_learning: int = field(default_factory=lambda: int(os.getenv("MIN_TRADES_FOR_LEARNING", "5")))

    # ── Polymarket hosts ───────────────────────────────────────────────────
    clob_host: str = field(default_factory=lambda: os.getenv("CLOB_HOST", "https://clob.polymarket.com"))
    gamma_host: str = field(default_factory=lambda: os.getenv("GAMMA_HOST", "https://gamma-api.polymarket.com"))

    def get_partial_exit_tiers(self) -> list[tuple[float, float]]:
        """
        Parse PARTIAL_EXIT_TIERS into a sorted list of (gain_pct, sell_pct) tuples.
        e.g. "100:33,200:33,400:50" → [(100.0, 33.0), (200.0, 33.0), (400.0, 50.0)]
        """
        tiers = []
        try:
            for part in self.partial_exit_tiers.split(","):
                gain, sell = part.strip().split(":")
                tiers.append((float(gain), float(sell)))
        except Exception:
            tiers = [(100.0, 33.0), (200.0, 33.0), (400.0, 50.0)]
        return sorted(tiers, key=lambda t: t[0])


def _require(key: str) -> str:
    val = os.getenv(key)
    if not val:
        raise EnvironmentError(
            f"Required environment variable '{key}' is not set. "
            f"Add it in your Railway project → Variables tab."
        )
    return val
