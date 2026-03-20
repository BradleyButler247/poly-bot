#!/usr/bin/env python3
"""
Entry point — starts HTTP server first, then launches the trading bot.
Railway health checks the /health endpoint before marking deployment successful.
"""
import asyncio
import sys
import os

sys.path.insert(0, os.path.dirname(__file__))

import uvicorn
from src.api_server import app


async def run_bot():
    """Import and run bot after a delay so HTTP server starts first."""
    # Wait for uvicorn to bind and pass the health check
    await asyncio.sleep(10)
    from src.bot import PolymarketBot
    bot = PolymarketBot()
    await bot.run()


async def main():
    port = int(os.getenv("PORT", "8000"))
    config = uvicorn.Config(
        app,
        host="0.0.0.0",
        port=port,
        log_level="warning",
    )
    server = uvicorn.Server(config)

    await asyncio.gather(
        server.serve(),
        run_bot(),
    )


if __name__ == "__main__":
    asyncio.run(main())
