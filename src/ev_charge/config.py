"""Application configuration: config.json (or CONFIG_FILE) with computed defaults."""

from __future__ import annotations

import json
import os
import platform
from pathlib import Path

from pydantic import BaseModel, model_validator


def default_data_dir() -> str:
    if platform.system() == "Windows":
        base = os.environ.get("LOCALAPPDATA") or str(Path.home())
        return str(Path(base) / "ev-charge")
    return str(Path.home() / ".ev-charge")


class AppConfig(BaseModel):
    host: str = "127.0.0.1"
    port: int = 8000
    data_dir: str | None = None  # Computed: ~/.ev-charge (Linux/macOS), %LOCALAPPDATA%\ev-charge (Windows)
    database_url: str | None = None  # Computed: sqlite:///{data_dir}/ev_charge.db
    currency: str = "EUR"  # Shown next to costs (ISO code, e.g. EUR, USD, CHF)
    log_level: str = "INFO"
    session_days: int = 30  # how long a sign-in lasts
    # Chargers connected to their cloud API (Chargers page) are read every charger_poll_seconds
    charger_polling: bool = True
    charger_poll_seconds: int = 60
    # Page of the CREG home charging reimbursement tariffs (Tariffs → Import from CREG); the Dutch page works too
    creg_url: str = "https://www.creg.be/fr/consommateurs/prix-et-tarifs/tarif-de-remboursement-pour-la-recharge-a-domicile-des-voitures-de-0"
    # Optional HTTPS: both must be set (no certificate is generated)
    ssl_certfile: str | None = None
    ssl_keyfile: str | None = None

    @model_validator(mode="after")
    def _computed_defaults(self) -> AppConfig:
        self.data_dir = str(Path(os.path.expanduser(self.data_dir or default_data_dir())))
        if not self.database_url:
            self.database_url = f"sqlite:///{(Path(self.data_dir) / 'ev_charge.db').as_posix()}"
        return self

    @classmethod
    def load(cls, path: str | None = None) -> AppConfig:
        """Explicit path, then the CONFIG_FILE env var, then ./config.json; defaults if none exists."""
        cfg_path = Path(path or os.getenv("CONFIG_FILE") or Path.cwd() / "config.json")
        data = json.loads(cfg_path.read_text(encoding="utf-8")) if cfg_path.exists() else {}
        if os.getenv("DATABASE_URL"):
            data["database_url"] = os.environ["DATABASE_URL"]
        if os.getenv("CHARGER_POLLING") is not None:
            data["charger_polling"] = os.environ["CHARGER_POLLING"].lower() in ("1", "true", "yes")
        return cls(**data)


CONFIG = AppConfig.load()


def get_config() -> AppConfig:
    return CONFIG
