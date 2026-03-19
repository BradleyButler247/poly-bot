"""
Audit Log — append-only JSONL log of every bot decision.
Also tracks resolved trade outcomes for the learning loop.
"""

import json
import logging
import os
from datetime import datetime, timezone
from typing import Optional

log = logging.getLogger("audit")

AUDIT_LOG_PATH = os.getenv("AUDIT_LOG_PATH", "logs/audit.jsonl")
RESOLVED_LOG_PATH = os.getenv("RESOLVED_LOG_PATH", "logs/resolved.jsonl")
STRATEGY_PATH = os.getenv("STRATEGY_PATH", "logs/strategy.json")


class AuditLog:
    def __init__(self):
        os.makedirs("logs", exist_ok=True)

    # ── Write events ────────────────────────────────────────────────────────

    def log_analysis(self, cycle_id: str, market_id: str, question: str, analysis: dict):
        self._write(AUDIT_LOG_PATH, {
            "event": "analysis",
            "cycle_id": cycle_id,
            "market_id": market_id,
            "question": question,
            "should_trade": analysis.get("should_trade"),
            "your_probability": analysis.get("your_probability"),
            "market_price": analysis.get("market_price"),
            "edge": analysis.get("edge"),
            "confidence": analysis.get("confidence"),
            "reasoning": analysis.get("reasoning"),
            "strategy_tags": analysis.get("strategy_tags", []),
        })

    def log_trade(self, cycle_id: str, market_id: str, question: str,
                  trade: dict, result: dict, analysis: dict):
        self._write(AUDIT_LOG_PATH, {
            "event": "trade",
            "cycle_id": cycle_id,
            "market_id": market_id,
            "question": question,
            "outcome": trade.get("outcome"),
            "price": trade.get("price"),
            "usdc_size": trade.get("usdc_size"),
            "your_probability": analysis.get("your_probability"),
            "edge": analysis.get("edge"),
            "confidence": analysis.get("confidence"),
            "reasoning": analysis.get("reasoning"),
            "strategy_tags": analysis.get("strategy_tags", []),
            "success": result.get("success"),
            "order_id": result.get("order_id"),
            "error": result.get("error"),
        })

    def log_resolved_trade(self, order_id: str, market_id: str, question: str,
                           outcome_traded: str, price_paid: float, usdc_size: float,
                           market_resolved_yes: bool, pnl: float,
                           your_probability: float, strategy_tags: list):
        """
        Call this when a market resolves and we know the P&L.
        These records are what Claude learns from.
        """
        won = (outcome_traded == "YES" and market_resolved_yes) or \
              (outcome_traded == "NO" and not market_resolved_yes)
        self._write(RESOLVED_LOG_PATH, {
            "event": "resolved",
            "order_id": order_id,
            "market_id": market_id,
            "question": question,
            "outcome_traded": outcome_traded,
            "price_paid": price_paid,
            "usdc_size": usdc_size,
            "market_resolved_yes": market_resolved_yes,
            "won": won,
            "pnl": pnl,
            "your_probability": your_probability,
            "strategy_tags": strategy_tags,
        })

    def log_risk_block(self, cycle_id: str, market_id: str, trade: dict, reason: str):
        self._write(AUDIT_LOG_PATH, {
            "event": "risk_block",
            "cycle_id": cycle_id,
            "market_id": market_id,
            "trade": trade,
            "reason": reason,
        })

    def log_emergency_stop(self, reason: str):
        self._write(AUDIT_LOG_PATH, {"event": "emergency_stop", "reason": reason})

    def log_error(self, error: str):
        self._write(AUDIT_LOG_PATH, {"event": "error", "error": error})

    def save_strategy_notes(self, notes: str):
        """Persist Claude's latest strategy critique to disk."""
        with open(STRATEGY_PATH, "w") as f:
            json.dump({"notes": notes, "updated": _now()}, f, indent=2)
        log.info("Strategy notes updated")

    def load_strategy_notes(self) -> Optional[str]:
        """Load the last strategy critique Claude wrote."""
        try:
            with open(STRATEGY_PATH) as f:
                data = json.load(f)
                return data.get("notes")
        except FileNotFoundError:
            return None

    # ── Read resolved trades ────────────────────────────────────────────────

    def get_resolved_trades(self, limit: int = 50) -> list[dict]:
        """Return the most recent resolved trades for the learning loop."""
        trades = []
        try:
            with open(RESOLVED_LOG_PATH) as f:
                for line in f:
                    line = line.strip()
                    if line:
                        try:
                            trades.append(json.loads(line))
                        except json.JSONDecodeError:
                            pass
        except FileNotFoundError:
            return []
        return trades[-limit:]

    def get_performance_summary(self) -> dict:
        """Aggregate stats over all resolved trades."""
        trades = self.get_resolved_trades(limit=10000)
        if not trades:
            return {"total": 0}

        total = len(trades)
        wins = sum(1 for t in trades if t.get("won"))
        total_pnl = sum(t.get("pnl", 0) for t in trades)
        total_wagered = sum(t.get("usdc_size", 0) for t in trades)
        roi = (total_pnl / total_wagered * 100) if total_wagered > 0 else 0

        # Break down by strategy tag
        tag_stats: dict[str, dict] = {}
        for t in trades:
            for tag in t.get("strategy_tags", []):
                s = tag_stats.setdefault(tag, {"wins": 0, "total": 0, "pnl": 0})
                s["total"] += 1
                s["pnl"] += t.get("pnl", 0)
                if t.get("won"):
                    s["wins"] += 1

        return {
            "total": total,
            "wins": wins,
            "win_rate": round(wins / total, 3),
            "total_pnl": round(total_pnl, 2),
            "total_wagered": round(total_wagered, 2),
            "roi_pct": round(roi, 2),
            "recent_10": trades[-10:],
            "tag_stats": tag_stats,
        }

    # ── Private ─────────────────────────────────────────────────────────────

    def _write(self, path: str, record: dict):
        record["ts"] = _now()
        with open(path, "a") as f:
            f.write(json.dumps(record) + "\n")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
