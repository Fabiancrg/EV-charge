"""NRGkick Gen2 mobile chargers through their local JSON API (the charger itself answers on your network;
there is no public cloud API).

To enable in the NRGkick app: Settings → JSON API, optionally with a user name and password.
The server must be on the charger's network (Wi-Fi), so a charger taken on the road isn't followed there.

- GET /info: serial number, model, name.
- GET /values?raw=1: status (1 standby, 2 connected, 3 charging, 6 error, 7 wake-up), total active power (W)
  and the energy charged in the session (Wh).
History: the API has none, so sessions are recorded live.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

import aiohttp

from .base import CHARGING, OFFLINE, PLUGGED_IN, UNPLUGGED, ConnectorError, HourStat, LiveReading

TIMEOUT = aiohttp.ClientTimeout(total=15)
_STATUS = {1: UNPLUGGED, 2: PLUGGED_IN, 3: CHARGING, 7: PLUGGED_IN}  # 0 unknown, 6 error: offline


def _number(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


class NrgkickConnector:
    label = "NRGkick Gen2 (local network)"
    has_history = False
    fields = (
        ("host", "Charger address on your network (IP or name, e.g. 192.168.1.60)", False, True),
        ("username", "User name (if set for the JSON API in the NRGkick app)", False, False),
        ("password", "Password (if set)", True, False),
    )

    def __init__(self, config: dict[str, Any]) -> None:
        host = (config.get("host") or "").strip().rstrip("/")
        if not host:
            raise ConnectorError("Enter the address of the NRGkick on your network")
        self._base = host if host.startswith(("http://", "https://")) else f"http://{host}"
        user, password = (config.get("username") or "").strip(), config.get("password") or ""
        self._auth = aiohttp.BasicAuth(user, password) if user else None
        self._session: aiohttp.ClientSession | None = None

    def _http(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=TIMEOUT, trust_env=True)
        return self._session

    async def _get(self, path: str, params: dict[str, str] | None = None) -> dict[str, Any]:
        try:
            async with self._http().get(self._base + path, params=params, auth=self._auth) as resp:
                if resp.status in (401, 403):
                    raise ConnectorError("NRGkick refused the user name or password of its JSON API")
                body = await resp.json(content_type=None)
                if isinstance(body, dict) and "API must be enabled" in str(body.get("Response", "")):
                    raise ConnectorError("The JSON API of the NRGkick is off: enable it in the NRGkick app (Settings → JSON API)")
                if resp.status != 200:
                    raise ConnectorError(f"NRGkick: {path} answered HTTP {resp.status}")
        except (aiohttp.ClientError, TimeoutError) as exc:
            raise ConnectorError(
                f"Cannot reach the NRGkick at {self._base} ({str(exc) or type(exc).__name__}): check its address and that this server is on the same network"
            ) from exc
        except ValueError as exc:
            raise ConnectorError(f"The device at {self._base} didn't answer like an NRGkick (JSON API)") from exc
        if not isinstance(body, dict):
            raise ConnectorError(f"The device at {self._base} didn't answer like an NRGkick (JSON API)")
        return body

    async def describe(self) -> dict[str, Any]:
        general = (await self._get("/info", {"general": "1", "versions": "1"})).get("general")
        if not isinstance(general, dict):
            raise ConnectorError(f"The device at {self._base} didn't answer like an NRGkick (no device information)")
        return {"serial": general.get("serial_number"), "model": general.get("model_type") or "NRGkick", "name": general.get("device_name")}

    async def live(self) -> LiveReading:
        try:
            values = await self._get("/values", {"raw": "1", "general": "1", "energy": "1", "powerflow": "1"})
        except ConnectorError as exc:
            if "Cannot reach" in str(exc):  # unplugged from the socket or taken away: not an error to report every minute
                return LiveReading(status=OFFLINE, online=False)
            raise
        general = values.get("general") or {}
        code = int(_number(general.get("status")))
        status = _STATUS.get(code, OFFLINE)
        return LiveReading(
            status=status,
            online=status != OFFLINE,
            power_w=_number((values.get("powerflow") or {}).get("total_active_power")),
            energy_wh=_number((values.get("energy") or {}).get("charged_energy")) if status in (CHARGING, PLUGGED_IN) else 0.0,
            extra={"status": code, "error": general.get("error_code")},
        )

    async def history(self, start: datetime, end: datetime) -> list[HourStat]:
        raise ConnectorError("The NRGkick has no charging history in its API: its sessions are recorded live, from now on")

    async def close(self) -> None:
        if self._session is not None:
            await self._session.close()
            self._session = None
