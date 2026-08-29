import asyncio
import os
import sys

from dotenv import load_dotenv
load_dotenv()

sys.path.append(os.path.abspath('.'))

from backend.llm.provider import get_provider, ModelTier
from backend.core.config import settings

async def main():
    try:
        provider = get_provider()
        print(f"Provider: {provider.name}")
        print(f"Available: {provider.available}")
        if not provider.available:
            print("PROVIDER NOT AVAILABLE. Check your .env setup.")
            return

        print("Making a test completion...")
        result = await provider.complete(
            system="You are a trading bot.",
            user="Should I buy BTC?",
            tier=ModelTier.REASONING,
            max_tokens=50
        )
        print(f"Result OK: {result.ok}")
        print(f"Text: {result.text}")
        print(f"Error: {result.error}")
    except Exception as e:
        import traceback
        traceback.print_exc()

asyncio.run(main())
