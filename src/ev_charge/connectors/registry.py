"""Known connector types. A connector class has `label`, `fields` ((name, label, secret[, required]), …)
and the methods of connectors.base.Connector."""

from __future__ import annotations

from typing import Any

from .base import Connector, ConnectorError
from .ohme import OhmeConnector
from .zaptec import ZaptecConnector

CONNECTORS: dict[str, type] = {"ohme": OhmeConnector, "zaptec": ZaptecConnector}


def make_connector(kind: str, config: dict[str, Any]) -> Connector:
    cls = CONNECTORS.get(kind)
    if cls is None:
        raise ConnectorError(f"Unknown connector '{kind}'")
    return cls(config)  # type: ignore[no-any-return]
