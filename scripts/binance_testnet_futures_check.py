"""Compatibility entry point; demo orders require --execute-demo-orders."""
from binance_demo_verify import main
import argparse
import asyncio

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute-demo-orders", action="store_true")
    asyncio.run(main(parser.parse_args().execute_demo_orders))
