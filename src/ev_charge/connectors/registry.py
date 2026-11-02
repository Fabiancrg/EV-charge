"""Known connector types (the Zaptec API or others can be added here)."""

from __future__ import annotations

from typing import Any

from .base import Connector, ConnectorError
from .ohme import OhmeConnector

CONNECTORS: dict[str, type] = {"ohme": OhmeConnector}


def make_connector(kind: str, config: dict[str, Any]) -> Connector:
    cls = CONNECTORS.get(kind)
    if cls is None:
        raise ConnectorError(f"Unknown connector '{kind}'")
    return cls(config)  # type: ignore[no-any-return]
