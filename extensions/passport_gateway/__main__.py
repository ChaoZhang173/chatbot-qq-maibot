"""python -m passport_gateway  — run the gateway (config from the environment)."""
from __future__ import annotations

import asyncio
import logging
import signal
import sys

from .config import Config, ConfigError
from .server import GatewayServer


async def amain() -> int:
    try:
        cfg = Config.from_env()
        server = GatewayServer(cfg)
    except (ConfigError, ValueError, OSError) as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return 2
    await server.start()
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)
    await stop.wait()
    await server.stop()
    return 0


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    raise SystemExit(asyncio.run(amain()))


if __name__ == "__main__":
    main()
