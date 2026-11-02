"""Connector interface. A connector talks to one charger's cloud account."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol

# Normalised charger states
UNPLUGGED = "unplugged"
PENDING_APPROVAL = "pending_approval"
PLUGGED_IN = "plugged_in"  # plugged, not charging (waiting for a slot…)
CHARGING = "charging"
PAUSED = "paused"
FINISHED = "finished"  # car full / charge complete, still plugged
OFFLINE = "offline"
STATES = (UNPLUGGED, PENDING_APPROVAL, PLUGGED_IN, CHARGING, PAUSED, FINISHED, OFFLINE)


class ConnectorError(Exception):
    """The charger's API can't be reached or refused the request (message shown to the user)."""


@dataclass
class LiveReading:
    status: str  # one of STATES
    online: bool = True
    power_w: float = 0.0
    energy_wh: float = 0.0  # energy delivered in the current session
    battery_pct: int | None = None  # the car's state of charge, when the charger knows it
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class HourStat:
    """Energy charged in one bucket of the charger's history (an hour)."""

    start: datetime  # local time
    end: datetime
    energy_wh: float
    active_ms: int = 0


class Connector(Protocol):
    async def describe(self) -> dict[str, Any]:
        """Log in and return the charger's identity: {"serial": ..., "model": ..., "firmware": ...}."""

    async def live(self) -> LiveReading: ...

    async def history(self, start: datetime, end: datetime) -> list[HourStat]: ...

    async def close(self) -> None: ...
