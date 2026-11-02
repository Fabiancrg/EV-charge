"""Entry point: ``python -m ev_charge``, the ``ev-charge`` script or the shiv zipapp.

Usage:
    ev-charge [--config path/to/config.json] [--host 0.0.0.0] [--port 8000]
    ev-charge [--config ...] --reset-password USERNAME   (asks for the new password; locked-out admin)
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
    parser.add_argument("--reset-password", metavar="USERNAME", help="Set a new password for a user (and reactivate them), then exit")
    args = parser.parse_args()
    if args.config:
        os.environ["CONFIG_FILE"] = args.config  # before ev_charge.config is imported

    if args.reset_password:
        raise SystemExit(_reset_password(args.reset_password))

    import uvicorn

    from .config import get_config

    cfg = get_config()
    logging.basicConfig(level=getattr(logging, cfg.log_level.upper(), logging.INFO), format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    ssl = {"ssl_certfile": cfg.ssl_certfile, "ssl_keyfile": cfg.ssl_keyfile} if cfg.ssl_certfile and cfg.ssl_keyfile else {}
    uvicorn.run("ev_charge.main:app", host=args.host or cfg.host, port=args.port or cfg.port, log_level=cfg.log_level.lower(), **ssl)


def _reset_password(username: str) -> int:
    from getpass import getpass

    from sqlalchemy import select

    from .auth import MIN_PASSWORD_LENGTH, end_all_sessions, hash_password
    from .database import SessionLocal, init_db
    from .models import User

    init_db()
    with SessionLocal() as db:
        user = db.scalar(select(User).where(User.username == username.strip().lower()))
        if user is None:
            print(f"No user named '{username}'")
            return 1
        password = getpass(f"New password for {user.username}: ")
        if len(password) < MIN_PASSWORD_LENGTH:
            print(f"The password must have at least {MIN_PASSWORD_LENGTH} characters")
            return 1
        if getpass("Repeat it: ") != password:
            print("The passwords differ")
            return 1
        user.password_hash = hash_password(password)
        user.active = True
        end_all_sessions(db, user.id)
        db.commit()
    print(f"Password of {username} changed")
    return 0


if __name__ == "__main__":
    main()
