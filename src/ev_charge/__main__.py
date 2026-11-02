"""Entry point: ``python -m ev_charge``, the ``ev-charge`` script or the shiv zipapp.

Usage:
    ev-charge [--config path/to/config.json] [--host 0.0.0.0] [--port 8000]
"""

from __future__ import annotations

import argparse
import logging
import os


def main() -> None:
    parser = argparse.ArgumentParser(description="EV Charge — charging session tracker")
    parser.add_argument("--config", help="Path to config.json (default: CONFIG_FILE env var, then ./config.json)")
    parser.add_argument("--host", help="Override the host from config.json")
    parser.add_argument("--port", type=int, help="Override the port from config.json")
    args = parser.parse_args()
    if args.config:
        os.environ["CONFIG_FILE"] = args.config  # before ev_charge.config is imported

    import uvicorn

    from .config import get_config

    cfg = get_config()
    logging.basicConfig(level=getattr(logging, cfg.log_level.upper(), logging.INFO), format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    ssl = {"ssl_certfile": cfg.ssl_certfile, "ssl_keyfile": cfg.ssl_keyfile} if cfg.ssl_certfile and cfg.ssl_keyfile else {}
    uvicorn.run("ev_charge.main:app", host=args.host or cfg.host, port=args.port or cfg.port, log_level=cfg.log_level.lower(), **ssl)


if __name__ == "__main__":
    main()
