#!/usr/bin/env python3
"""
Entry point — runs the trading bot loop and the API server concurrently.
Railway exposes the PORT env var; the frontend is served from /dashboard.
"""
import asyncio
import sys
import os

sys.path.insert(0, os.path.dirname(__file__))

import uvicorn
from src.bot import PolymarketBot
from src.api_server import app


async def main():
    bot = PolymarketBot()

    port = int(os.getenv("PORT", "8000"))
    config = uvicorn.Config(app, host="0.0.0.0", port=port, log_level="warning")
    server = uvicorn.Server(config)

    # Bot trading loop + HTTP server run side by side
    await asyncio.gather(
        bot.run(),
        server.serve(),
    )


if __name__ == "__main__":
    asyncio.run(main())
