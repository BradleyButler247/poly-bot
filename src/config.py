"""
Configuration — all values from environment variables.
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
    # Hard ceiling per trade regardless of balance
    max_trade_usdc: float = field(default_factory=lambda: float(os.getenv("MAX_TRADE_USDC", "50")))
    max_daily_loss_usdc: float = field(default_factory=lambda: float(os.getenv("MAX_DAILY_LOSS_USDC", "100")))
    max_position_usdc: float = field(default_factory=lambda: float(os.getenv("MAX_POSITION_USDC", "100")))
    min_liquidity_usdc: float = field(default_factory=lambda: float(os.getenv("MIN_LIQUIDITY_USDC", "1000")))

    # ── Balance-relative sizing ────────────────────────────────────────────
    # Max % of wallet to risk per trade (e.g. 0.05 = 5%)
    # Final size = balance * max_trade_pct * size_fraction, capped at max_trade_usdc
    max_trade_pct: float = field(default_factory=lambda: float(os.getenv("MAX_TRADE_PCT", "0.05")))
    # Minimum trade size — skip trades below this
    min_trade_usdc: float = field(default_factory=lambda: float(os.getenv("MIN_TRADE_USDC", "2.0")))

    # ── Exit thresholds ────────────────────────────────────────────────────
    # Full stop-loss exit when unrealised loss >= this %
    stop_loss_pct: float = field(default_factory=lambda: float(os.getenv("STOP_LOSS_PCT", "50")))
    # Partial take-profit tiers: "gain_pct:sell_pct,gain_pct:sell_pct,..."
    # Default: sell 33% at +100%, 33% at +200%, 50% at +400%
    partial_exit_tiers: str = field(
        default_factory=lambda: os.getenv("PARTIAL_EXIT_TIERS", "100:33,200:33,400:50")
    )

    # ── Bot behaviour ──────────────────────────────────────────────────────
    cycle_interval_seconds: int = field(default_factory=lambda: int(os.getenv("CYCLE_INTERVAL_SECONDS", "120")))
    max_markets_per_cycle: int = field(default_factory=lambda: int(os.getenv("MAX_MARKETS_PER_CYCLE", "20")))
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
        """Parse PARTIAL_EXIT_TIERS into sorted [(gain_pct, sell_pct)] list."""
        tiers = []
        try:
            for part in self.partial_exit_tiers.split(","):
                gain, sell = part.strip().split(":")
                tiers.append((float(gain), float(sell)))
        except Exception:
            tiers = [(100.0, 33.0), (200.0, 33.0), (400.0, 50.0)]
        return sorted(tiers, key=lambda t: t[0])

    def compute_trade_size(self, balance: float | None, size_fraction: float) -> float:
        """
        Compute USDC trade size relative to wallet balance.

        Formula: balance * max_trade_pct * size_fraction
        - Capped at max_trade_usdc (hard ceiling)
        - Floored at min_trade_usdc
        - Falls back to max_trade_usdc * size_fraction if balance unavailable
        """
        size_fraction = max(0.05, min(1.0, size_fraction))

        if balance and balance > 0:
            size = balance * self.max_trade_pct * size_fraction
        else:
            size = self.max_trade_usdc * size_fraction

        size = min(size, self.max_trade_usdc)
        size = max(size, self.min_trade_usdc)
        return round(size, 2)


def _require(key: str) -> str:
    val = os.getenv(key)
    if not val:
        raise EnvironmentError(
            f"Required environment variable '{key}' is not set. "
            f"Add it in your Railway project → Variables tab."
        )
    return val
