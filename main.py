#!/usr/bin/env python3
import asyncio
import sys
import os

# Add project root to path
sys.path.insert(0, os.path.dirname(__file__))

from src.bot import main

if __name__ == "__main__":
    asyncio.run(main())
