"""
Audit Log — append-only JSONL log of every bot decision.

Every analysis, trade, block, and error is written here.
You can tail this on Railway or download it for review.
"""

import json
import logging
import os
from datetime import datetime, timezone

log = logging.getLogger("audit")

LOG_PATH = os.getenv("AUDIT_LOG_PATH", "logs/audit.jsonl")


class AuditLog:
    def __init__(self):
        os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)

    def log_analysis(self, cycle_id: str, market_id: str, question: str, analysis: dict):
        self._write({
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
        })

    def log_trade(self, cycle_id: str, market_id: str, trade: dict, result: dict):
        self._write({
            "event": "trade",
            "cycle_id": cycle_id,
            "market_id": market_id,
            "outcome": trade.get("outcome"),
            "price": trade.get("price"),
            "usdc_size": trade.get("usdc_size"),
            "success": result.get("success"),
            "order_id": result.get("order_id"),
            "error": result.get("error"),
        })

    def log_risk_block(self, cycle_id: str, market_id: str, trade: dict, reason: str):
        self._write({
            "event": "risk_block",
            "cycle_id": cycle_id,
            "market_id": market_id,
            "trade": trade,
            "reason": reason,
        })

    def log_emergency_stop(self, reason: str):
        self._write({
            "event": "emergency_stop",
            "reason": reason,
        })

    def log_error(self, error: str):
        self._write({
            "event": "error",
            "error": error,
        })

    def _write(self, record: dict):
        record["ts"] = datetime.now(timezone.utc).isoformat()
        line = json.dumps(record)
        with open(LOG_PATH, "a") as f:
            f.write(line + "\n")
        log.debug(f"Audit: {record['event']}")
