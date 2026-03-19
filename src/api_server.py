"""
API Server — lightweight FastAPI server that runs alongside the bot
and exposes trading data as JSON endpoints for the frontend dashboard.

Runs in a separate thread so it doesn't block the async bot loop.
"""

import json
import logging
import os
from datetime import datetime, timezone
from typing import Optional

import aiohttp
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse

log = logging.getLogger("api")

AUDIT_LOG_PATH = os.getenv("AUDIT_LOG_PATH", "logs/audit.jsonl")
RESOLVED_LOG_PATH = os.getenv("RESOLVED_LOG_PATH", "logs/resolved.jsonl")
OPEN_TRADES_PATH = os.getenv("OPEN_TRADES_PATH", "logs/open_trades.jsonl")
GAMMA_HOST = os.getenv("GAMMA_HOST", "https://gamma-api.polymarket.com")
CLOB_HOST = os.getenv("CLOB_HOST", "https://clob.polymarket.com")

app = FastAPI(title="Polymarket Bot API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ── Data loaders ─────────────────────────────────────────────────────────────

def _load_jsonl(path: str) -> list[dict]:
    records = []
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        records.append(json.loads(line))
                    except json.JSONDecodeError:
                        pass
    except FileNotFoundError:
        pass
    return records


def _load_open_trades() -> list[dict]:
    return [r for r in _load_jsonl(OPEN_TRADES_PATH) if not r.get("resolved")]


def _load_resolved_trades() -> list[dict]:
    return _load_jsonl(RESOLVED_LOG_PATH)


async def _fetch_current_price(market_id: str) -> Optional[float]:
    """Fetch the current YES price for an open position."""
    try:
        url = f"{GAMMA_HOST}/markets/{market_id}"
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                if resp.status != 200:
                    return None
                data = await resp.json()
        tokens = data.get("tokens") or []
        for token in tokens:
            if str(token.get("outcome", "")).upper() == "YES":
                price = token.get("price")
                if price is not None:
                    return float(price)
    except Exception:
        pass
    return None


async def _fetch_wallet_balance(wallet_address: str) -> Optional[float]:
    """Fetch USDC balance on Polygon for this wallet via public RPC."""
    # USDC contract on Polygon
    USDC = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
    RPC = "https://polygon-rpc.com"
    # balanceOf(address) selector
    data = "0x70a08231" + wallet_address.replace("0x", "").lower().zfill(64)
    payload = {
        "jsonrpc": "2.0", "method": "eth_call",
        "params": [{"to": USDC, "data": data}, "latest"],
        "id": 1,
    }
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(RPC, json=payload, timeout=aiohttp.ClientTimeout(total=8)) as resp:
                result = await resp.json()
        hex_val = result.get("result", "0x0")
        raw = int(hex_val, 16)
        return raw / 1e6  # USDC has 6 decimals
    except Exception:
        return None


# ── Endpoints ─────────────────────────────────────────────────────────────────

@app.get("/api/summary")
async def summary():
    """Top-level account stats."""
    resolved = _load_resolved_trades()
    open_trades = _load_open_trades()

    total_pnl = sum(t.get("pnl", 0) for t in resolved)
    total_wagered = sum(t.get("usdc_size", 0) for t in resolved)
    wins = sum(1 for t in resolved if t.get("won"))
    total = len(resolved)

    # Unrealised P&L on open positions (uses last known price from open_trades)
    unrealised = 0.0
    open_volume = sum(t.get("usdc_size", 0) for t in open_trades)

    return {
        "total_pnl_usd": round(total_pnl, 2),
        "total_pnl_pct": round(total_pnl / total_wagered * 100, 2) if total_wagered else 0,
        "total_volume_usd": round(total_wagered + open_volume, 2),
        "total_trades": total,
        "win_rate_pct": round(wins / total * 100, 1) if total else 0,
        "open_positions_count": len(open_trades),
        "open_volume_usd": round(open_volume, 2),
        "realised_pnl_usd": round(total_pnl, 2),
    }


@app.get("/api/positions/open")
async def open_positions():
    """Current open positions with live unrealised P&L."""
    trades = _load_open_trades()
    result = []

    for t in trades:
        outcome = t.get("outcome_traded", "YES")
        price_paid = float(t.get("price_paid", 0.5))
        usdc_size = float(t.get("usdc_size", 0))
        shares = usdc_size / price_paid if price_paid > 0 else 0

        # Try to get current market price
        current_price = await _fetch_current_price(t.get("market_id", ""))
        if current_price is None:
            current_price = price_paid  # fallback: no change

        if outcome == "NO":
            current_price = 1.0 - current_price

        current_value = shares * current_price
        unrealised_pnl = current_value - usdc_size
        unrealised_pct = (unrealised_pnl / usdc_size * 100) if usdc_size else 0

        result.append({
            "market_id": t.get("market_id"),
            "question": t.get("question"),
            "outcome_traded": outcome,
            "price_paid": round(price_paid, 4),
            "current_price": round(current_price, 4),
            "usdc_size": round(usdc_size, 2),
            "shares": round(shares, 2),
            "current_value": round(current_value, 2),
            "unrealised_pnl_usd": round(unrealised_pnl, 2),
            "unrealised_pnl_pct": round(unrealised_pct, 2),
            "date_opened": t.get("ts"),
            "strategy_tags": t.get("strategy_tags", []),
            "your_probability": t.get("your_probability"),
        })

    result.sort(key=lambda x: x.get("date_opened") or "", reverse=True)
    return result


@app.get("/api/positions/history")
async def trade_history(limit: int = 100):
    """Resolved trade history."""
    trades = _load_resolved_trades()
    trades.sort(key=lambda t: t.get("ts") or "", reverse=True)
    trades = trades[:limit]

    result = []
    for t in trades:
        usdc_size = float(t.get("usdc_size", 0))
        pnl = float(t.get("pnl", 0))
        pnl_pct = (pnl / usdc_size * 100) if usdc_size else 0

        result.append({
            "market_id": t.get("market_id"),
            "question": t.get("question"),
            "outcome_traded": t.get("outcome_traded"),
            "price_paid": t.get("price_paid"),
            "usdc_size": round(usdc_size, 2),
            "pnl_usd": round(pnl, 2),
            "pnl_pct": round(pnl_pct, 2),
            "won": t.get("won"),
            "market_resolved_yes": t.get("market_resolved_yes"),
            "date_opened": t.get("ts"),
            "date_closed": t.get("ts"),  # resolved timestamp
            "strategy_tags": t.get("strategy_tags", []),
            "your_probability": t.get("your_probability"),
        })

    return result


@app.get("/api/pnl/curve")
async def pnl_curve():
    """Cumulative P&L over time for the chart."""
    trades = _load_resolved_trades()
    trades.sort(key=lambda t: t.get("ts") or "")

    cumulative = 0.0
    points = []
    for t in trades:
        cumulative += float(t.get("pnl", 0))
        points.append({
            "ts": t.get("ts"),
            "cumulative_pnl": round(cumulative, 2),
            "trade_pnl": round(float(t.get("pnl", 0)), 2),
            "question": (t.get("question") or "")[:50],
            "won": t.get("won"),
        })

    return points


@app.get("/api/health")
async def health():
    return {"status": "ok", "ts": datetime.now(timezone.utc).isoformat()}


# Serve the frontend dashboard SPA
if os.path.exists("dashboard"):
    app.mount("/", StaticFiles(directory="dashboard", html=True), name="static")
