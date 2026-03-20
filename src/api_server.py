"""
API Server — lightweight FastAPI server that runs alongside the bot
and exposes trading data as JSON endpoints for the frontend dashboard.
"""

import json
import logging
import os
from datetime import datetime, timezone
from typing import Optional

import aiohttp
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import RedirectResponse

log = logging.getLogger("api")

AUDIT_LOG_PATH = os.getenv("AUDIT_LOG_PATH", "logs/audit.jsonl")
RESOLVED_LOG_PATH = os.getenv("RESOLVED_LOG_PATH", "logs/resolved.jsonl")
OPEN_TRADES_PATH = os.getenv("OPEN_TRADES_PATH", "logs/open_trades.jsonl")
GAMMA_HOST = os.getenv("GAMMA_HOST", "https://gamma-api.polymarket.com")
CLOB_HOST = os.getenv("CLOB_HOST", "https://clob.polymarket.com")

ALLOWED_ORIGINS = os.getenv("ALLOWED_ORIGINS", "*").split(",")

app = FastAPI(title="Polymarket Bot API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_methods=["GET"],
    allow_headers=["*"],
)


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


async def _fetch_wallet_balance() -> Optional[float]:
    """
    Fetch USDC.e balance directly from Polygon blockchain.
    Uses the proxy wallet address (WALLET_ADDRESS env var).
    USDC.e contract per official Polymarket docs: 0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174
    """
    wallet_address = os.getenv("WALLET_ADDRESS", "")
    if not wallet_address:
        return None

    # Direct Polygon RPC call — no auth needed, just reads the blockchain
    USDC_E = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
    RPC_ENDPOINTS = [
        "https://polygon-rpc.com",
        "https://rpc-mainnet.matic.network",
        "https://rpc.ankr.com/polygon",
    ]

    addr_padded = wallet_address.replace("0x", "").lower().zfill(64)
    data = "0x70a08231" + addr_padded  # balanceOf(address)
    payload = {
        "jsonrpc": "2.0",
        "method": "eth_call",
        "params": [{"to": USDC_E, "data": data}, "latest"],
        "id": 1,
    }

    for rpc in RPC_ENDPOINTS:
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(rpc, json=payload,
                                        timeout=aiohttp.ClientTimeout(total=8)) as resp:
                    result = await resp.json()
            hex_val = result.get("result", "0x0")
            if hex_val and hex_val != "0x":
                raw = int(hex_val, 16)
                balance = raw / 1e6  # USDC.e has 6 decimals
                if balance > 0:
                    log.info(f"Balance fetched from {rpc}: ${balance:.2f}")
                    return balance
        except Exception as e:
            log.debug(f"RPC {rpc} failed: {e}")
            continue

    # If all RPCs return 0 or fail, try fetching from Polymarket's CLOB
    # The CLOB balance endpoint reflects trading balance not on-chain balance
    try:
        url = f"{CLOB_HOST}/balance-allowance"
        params = {"asset_type": "USDC", "signature_type": 1}
        headers = {"poly-address": wallet_address}
        async with aiohttp.ClientSession() as session:
            async with session.get(url, params=params, headers=headers,
                                   timeout=aiohttp.ClientTimeout(total=8)) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    log.debug(f"CLOB balance response: {data}")
                    balance = float(data.get("balance", 0) or 0)
                    return balance / 1e6
    except Exception as e:
        log.warning(f"CLOB balance fetch failed: {e}")

    return None


@app.get("/api/summary")
async def summary():
    resolved = _load_resolved_trades()
    open_trades = _load_open_trades()

    total_pnl = sum(t.get("pnl", 0) for t in resolved)
    total_wagered = sum(t.get("usdc_size", 0) for t in resolved)
    wins = sum(1 for t in resolved if t.get("won"))
    total = len(resolved)
    open_volume = sum(t.get("usdc_size", 0) for t in open_trades)

    balance = await _fetch_wallet_balance()

    return {
        "total_pnl_usd": round(total_pnl, 2),
        "total_pnl_pct": round(total_pnl / total_wagered * 100, 2) if total_wagered else 0,
        "total_volume_usd": round(total_wagered + open_volume, 2),
        "total_trades": total,
        "win_rate_pct": round(wins / total * 100, 1) if total else 0,
        "open_positions_count": len(open_trades),
        "open_volume_usd": round(open_volume, 2),
        "realised_pnl_usd": round(total_pnl, 2),
        "wallet_balance_usd": round(balance, 2) if balance is not None else None,
    }


@app.get("/api/positions/open")
async def open_positions():
    trades = _load_open_trades()
    result = []

    for t in trades:
        outcome = t.get("outcome_traded", "YES")
        price_paid = float(t.get("price_paid", 0.5))
        usdc_size = float(t.get("usdc_size", 0))
        shares = usdc_size / price_paid if price_paid > 0 else 0

        current_price = await _fetch_current_price(t.get("market_id", ""))
        if current_price is None:
            current_price = price_paid

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
            "date_closed": t.get("ts"),
            "strategy_tags": t.get("strategy_tags", []),
            "your_probability": t.get("your_probability"),
        })

    return result


@app.get("/api/pnl/curve")
async def pnl_curve():
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


@app.get("/api/audit")
async def audit_log(limit: int = 20):
    records = _load_jsonl(AUDIT_LOG_PATH)
    return records[-limit:]


@app.get("/api/debug")
async def debug():
    balance = await _fetch_wallet_balance()
    return {
        "audit_exists": os.path.exists(AUDIT_LOG_PATH),
        "open_trades_exists": os.path.exists(OPEN_TRADES_PATH),
        "resolved_exists": os.path.exists(RESOLVED_LOG_PATH),
        "audit_lines": sum(1 for _ in open(AUDIT_LOG_PATH)) if os.path.exists(AUDIT_LOG_PATH) else 0,
        "open_trades_lines": sum(1 for _ in open(OPEN_TRADES_PATH)) if os.path.exists(OPEN_TRADES_PATH) else 0,
        "wallet_address": os.getenv("WALLET_ADDRESS", "not set"),
        "wallet_balance_usd": round(balance, 2) if balance is not None else None,
        "logs_dir": os.listdir("logs") if os.path.exists("logs") else "missing",
    }


@app.get("/api/health")
async def health():
    return {"status": "ok", "ts": datetime.now(timezone.utc).isoformat()}


@app.get("/health")
async def health_simple():
    return {"status": "ok"}


if os.path.exists("dashboard"):
    app.mount("/dashboard", StaticFiles(directory="dashboard", html=True), name="static")


@app.get("/")
async def root():
    return RedirectResponse(url="/dashboard/index.html")
