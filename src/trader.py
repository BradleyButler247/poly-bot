"""
Trader — executes orders on Polymarket using the py-clob-client SDK.

Polymarket uses a Central Limit Order Book (CLOB) on Polygon.
Orders are signed with your wallet's private key.
"""

import logging
from typing import Any

from py_clob_client.client import ClobClient
from py_clob_client.clob_types import OrderArgs, OrderType, MarketOrderArgs
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
        """
        Initialise the Polymarket CLOB client.

        Polymarket embedded wallets use signature_type=0 (EOA).
        We pass the API key/secret/passphrase directly from env vars
        rather than trying to derive them (which caused the 400 error).
        """
        from py_clob_client.clob_types import ApiCreds

        client = ClobClient(
            host=self.config.clob_host,
            chain_id=POLYGON,
            key=self.config.wallet_private_key,
            signature_type=0,  # EOA — correct for Polymarket embedded wallets
            funder=self.config.wallet_private_key,
        )

        # Use the API credentials you generated on polymarket.com directly
        creds = ApiCreds(
            api_key=self.config.poly_api_key,
            api_secret=self.config.poly_api_secret,
            api_passphrase=self.config.poly_api_passphrase,
        )
        client.set_api_creds(creds)
        log.info("CLOB client initialised")
        return client

    async def place_order(self, market: dict, trade: dict) -> dict[str, Any]:
        """
        Place a limit order on Polymarket.
        Returns the order result dict.
        """
        token_id = self._get_token_id(market, trade["outcome"])
        if not token_id:
            return {"success": False, "error": "Could not find token_id for outcome"}

        price = float(trade["price"])
        size = float(trade["usdc_size"])

        # Convert USDC size to shares (shares = USDC / price for YES)
        shares = round(size / price, 2)

        order_args = OrderArgs(
            token_id=token_id,
            price=price,
            size=shares,
            side="BUY",  # We always buy the outcome we believe in
        )

        try:
            signed_order = self._client.create_and_sign_order(order_args)
            resp = self._client.post_order(signed_order, OrderType.GTC)  # Good-Till-Cancelled

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

    async def get_open_orders(self) -> list[dict]:
        """Fetch all open orders for this account."""
        try:
            return self._client.get_orders() or []
        except Exception as e:
            log.error(f"Failed to fetch open orders: {e}")
            return []

    async def cancel_order(self, order_id: str) -> bool:
        """Cancel a specific order."""
        try:
            resp = self._client.cancel(order_id)
            cancelled = resp.get("canceled", [])
            return order_id in cancelled
        except Exception as e:
            log.error(f"Failed to cancel order {order_id}: {e}")
            return False

    async def cancel_all_orders(self):
        """Emergency: cancel all open orders."""
        log.warning("Cancelling ALL open orders")
        try:
            self._client.cancel_all()
            log.info("All orders cancelled")
        except Exception as e:
            log.error(f"Cancel-all failed: {e}")

    def _get_token_id(self, market: dict, outcome: str) -> str | None:
        """Extract the CLOB token ID for a given outcome (YES or NO)."""
        tokens = market.get("tokens") or []
        for token in tokens:
            if str(token.get("outcome", "")).upper() == outcome.upper():
                return token.get("token_id") or token.get("tokenId")
        return None
