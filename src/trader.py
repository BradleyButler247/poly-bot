"""
Trader — executes orders on Polymarket using the py-clob-client SDK.

Polymarket uses a Central Limit Order Book (CLOB) on Polygon.
Orders are signed with your wallet's private key.
"""

import logging
import os
from typing import Any

from py_clob_client.client import ClobClient
from py_clob_client.clob_types import OrderArgs
from py_clob_client.constants import POLYGON

from .config import Config
from .risk_manager import RiskManager
from .audit_log import AuditLog

log = logging.getLogger("trader")


class Trader:
    def __init__(self, config: Config, risk: RiskManager, audit: AuditLog):
        self.config = config
        self.risk = risk
        self.audit = audit
        self._client = self._build_client()

    def _build_client(self) -> ClobClient:
        wallet_address = os.getenv("WALLET_ADDRESS", "")
        client = ClobClient(
            host=self.config.clob_host,
            chain_id=POLYGON,
            key=self.config.wallet_private_key,
            signature_type=1,
            funder=wallet_address,
        )
        try:
            creds = client.create_or_derive_api_creds()
            client.set_api_creds(creds)
            log.info(f"CLOB client initialised (derived creds, key: {creds.api_key[:8]}...)")
        except Exception as e:
            log.warning(f"Could not derive creds ({e}), falling back to env var credentials")
            from py_clob_client.clob_types import ApiCreds
            creds = ApiCreds(
                api_key=self.config.poly_api_key,
                api_secret=self.config.poly_api_secret,
                api_passphrase=self.config.poly_api_passphrase,
            )
            client.set_api_creds(creds)
            log.info("CLOB client initialised (env var creds)")
        return client

    async def place_order(self, market: dict, trade: dict) -> dict[str, Any]:
        token_id = await self._get_token_id(market, trade["outcome"])
        if not token_id:
            log.error(f"Could not find token_id for {trade['outcome']} in market {market.get('id')}")
            return {"success": False, "error": "Could not find token_id for outcome"}

        if not await self._check_orderbook_exists(token_id):
            log.warning(f"No orderbook for token {token_id[:16]}... — skipping")
            return {"success": False, "error": "orderbook does not exist"}

        price = float(trade["price"])
        price = max(0.001, min(0.999, price))
        price = round(price, 3)

        size = float(trade["usdc_size"])
        shares = round(size / price, 2)

        log.info(f"Placing order: {trade['outcome']} {shares} shares @ {price} (${size} USDC) token={token_id[:16]}...")

        order_args = OrderArgs(
            token_id=token_id,
            price=price,
            size=shares,
            side="BUY",
        )

        try:
            log.info(f"Signing and submitting order...")
            resp = self._client.create_and_post_order(order_args)
            log.info(f"CLOB response: {resp}")

            success = resp.get("success", False) or resp.get("orderID") is not None
            order_id = resp.get("orderID") or resp.get("order_id", "")

            if success:
                self.risk.record_open_position(market["id"], size)
                log.info(f"Order placed: {order_id} | {trade['outcome']} {shares} shares @ {price}")
            else:
                log.error(f"Order failed: {resp}")

            return {
                "success": success,
                "order_id": order_id,
                "token_id": token_id,
                "outcome": trade["outcome"],
                "price": price,
                "shares": shares,
                "usdc_size": size,
                "raw": resp,
            }

        except Exception as e:
            log.error(f"Order exception: {e}", exc_info=True)
            return {"success": False, "error": str(e)}

    async def _get_token_id(self, market: dict, outcome: str) -> str | None:
        outcome_upper = outcome.upper()
        tokens = market.get("tokens") or []
        if isinstance(tokens, str):
            import json as _json
            try:
                tokens = _json.loads(tokens)
            except Exception:
                tokens = []
        for token in tokens:
            if str(token.get("outcome", "")).upper() == outcome_upper:
                tid = token.get("token_id") or token.get("tokenId") or token.get("id")
                if tid:
                    log.info(f"Found token_id from tokens list: {tid[:16]}...")
                    return tid

        clob_ids = market.get("clobTokenIds")
        if clob_ids:
            if isinstance(clob_ids, str):
                import json as _json
                try:
                    clob_ids = _json.loads(clob_ids)
                except Exception:
                    clob_ids = []
            if isinstance(clob_ids, list):
                idx = 0 if outcome_upper == "YES" else 1
                if len(clob_ids) > idx and clob_ids[idx]:
                    log.info(f"Found token_id from clobTokenIds: {str(clob_ids[idx])[:16]}...")
                    return str(clob_ids[idx])

        condition_id = market.get("conditionId") or market.get("condition_id")
        if condition_id:
            try:
                import aiohttp
                url = f"{self.config.clob_host}/markets/{condition_id}"
                async with aiohttp.ClientSession() as session:
                    async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                        if resp.status == 200:
                            data = await resp.json()
                            tokens = data.get("tokens") or []
                            for token in tokens:
                                if str(token.get("outcome", "")).upper() == outcome_upper:
                                    tid = token.get("token_id") or token.get("tokenId")
                                    if tid:
                                        log.info(f"Found token_id from CLOB API: {tid[:16]}...")
                                        return tid
            except Exception as e:
                log.warning(f"CLOB token lookup failed: {e}")

        log.error(f"Could not find token_id for {outcome} in market {market.get('id')}")
        return None

    async def _check_orderbook_exists(self, token_id: str) -> bool:
        try:
            import aiohttp
            url = f"{self.config.clob_host}/book"
            params = {"token_id": token_id}
            async with aiohttp.ClientSession() as session:
                async with session.get(url, params=params,
                                       timeout=aiohttp.ClientTimeout(total=5)) as resp:
                    return resp.status == 200
        except Exception:
            return False
