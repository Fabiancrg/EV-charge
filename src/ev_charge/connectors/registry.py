"""Known connector types. A connector class has `label`, `fields` ((name, label, secret[, required]), …),
optionally `has_history = False` (its API has no charging history), and the methods of connectors.base.Connector."""

from __future__ import annotations

from typing import Any

from .base import Connector, ConnectorError
from .easee import EaseeConnector
from .goe import GoeCloudConnector, GoeLocalConnector
from .homeassistant import HomeAssistantConnector
from .nrgkick import NrgkickConnector
from .ohme import OhmeConnector
from .zaptec import ZaptecConnector

CONNECTORS: dict[str, type] = {
    "ohme": OhmeConnector,
    "zaptec": ZaptecConnector,
    "easee": EaseeConnector,
    "goe": GoeLocalConnector,
    "goe_cloud": GoeCloudConnector,
    "nrgkick": NrgkickConnector,
    "homeassistant": HomeAssistantConnector,
}


def make_connector(kind: str, config: dict[str, Any]) -> Connector:
    cls = CONNECTORS.get(kind)
    if cls is None:
        raise ConnectorError(f"Unknown connector '{kind}'")
    return cls(config)  # type: ignore[no-any-return]
