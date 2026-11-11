"""go-e chargers (go-eCharger Gemini, HOMEfix/HOME+, V3/V4, PRO…) through go-e's official HTTP API v2
(https://github.com/goecharger/go-eCharger-API-v2).

Two ways to reach the same API:

- **Local network**: http://<charger>/api/status. It must be allowed in the go-e app
  ("local HTTP API v2", in the connection settings). No password. The server must be on the charger's network.
- **go-e Cloud**: https://<serial>.api.v3.go-e.io/api/status, with the charger's cloud API token. To enable in the
  app: Internet → Advanced settings → "Cloud API", which also shows the token (firmware 052 or later).

Live: car state (`car`), power (`nrg`, total in W) and the energy since the car was connected (`wh`, in Wh).
History: the API has none (only the app's export), so sessions are recorded live.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

import aiohttp

from .base import CHARGING, FINISHED, OFFLINE, PLUGGED_IN, UNPLUGGED, ConnectorError, HourStat, LiveReading

TIMEOUT = aiohttp.ClientTimeout(total=15)
CLOUD_URL = "https://{serial}.api.v3.go-e.io"
KEYS = "car,nrg,wh,err,sse,fna,typ,fwv"
# carState: Unknown/Error=0, Idle=1, Charging=2, WaitCar=3, Complete=4, Error=5
_CAR_STATUS = {1: UNPLUGGED, 2: CHARGING, 3: PLUGGED_IN, 4: FINISHED}
NRG_TOTAL_POWER = 11  # nrg: U (L1, L2, L3, N), I (L1, L2, L3), P (L1, L2, L3, N, Total), pf (…)


def _number(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


class _GoeBase:
    """The API v2 status object, read from the charger itself or from go-e's cloud."""

    name = "go-e"
    has_history = False

    def __init__(self) -> None:
        self._session: aiohttp.ClientSession | None = None

    def _url(self) -> str:
        raise NotImplementedError

    def _headers(self) -> dict[str, str]:
        return {}

    def _unreachable(self, detail: str) -> str:
        raise NotImplementedError

    def _http(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=TIMEOUT, trust_env=True)
        return self._session

    async def _status(self) -> dict[str, Any] | None:
        """The status object; None when the charger is offline (cloud: no recent data)."""
        try:
            async with self._http().get(f"{self._url()}/api/status", params={"filter": KEYS}, headers=self._headers()) as resp:
                if resp.status == 404 and self.name == "go-e Cloud":
                    return None  # the token is right but the charger isn't sending data right now
                if resp.status in (401, 403):
                    raise ConnectorError(self._unreachable(f"HTTP {resp.status}"))
                if resp.status != 200:
                    raise ConnectorError(f"{self.name}: the charger answered HTTP {resp.status}")
                body = await resp.json(content_type=None)
        except (aiohttp.ClientError, TimeoutError) as exc:
            raise ConnectorError(self._unreachable(str(exc) or type(exc).__name__)) from exc
        except ValueError as exc:
            raise ConnectorError(f"{self.name}: the answer isn't the API v2 status (is this a go-e charger?)") from exc
        if not isinstance(body, dict) or "car" not in body:
            raise ConnectorError(f"{self.name}: the answer isn't the API v2 status (is this a go-e charger, with the HTTP API v2 enabled?)")
        return body

    async def describe(self) -> dict[str, Any]:
        s = await self._status()
        if s is None:
            raise ConnectorError(f"{self.name}: the charger is offline (no data in the last 30 seconds)")
        return {"serial": s.get("sse"), "model": s.get("typ") or "go-eCharger", "firmware": s.get("fwv"), "name": s.get("fna")}

    async def live(self) -> LiveReading:
        s = await self._status()
        if s is None:
            return LiveReading(status=OFFLINE, online=False)
        car = int(_number(s.get("car")))
        status = _CAR_STATUS.get(car, OFFLINE)  # unknown, error
        nrg = s.get("nrg") or []
        plugged = status in (CHARGING, PLUGGED_IN, FINISHED)
        return LiveReading(
            status=status,
            online=status != OFFLINE,
            power_w=_number(nrg[NRG_TOTAL_POWER]) if len(nrg) > NRG_TOTAL_POWER else 0.0,
            energy_wh=_number(s.get("wh")) if plugged else 0.0,  # "energy since car connected"
            extra={"car": car, "error": s.get("err")},
        )

    async def history(self, start: datetime, end: datetime) -> list[HourStat]:
        raise ConnectorError("go-e chargers have no charging history in their API: their sessions are recorded live, from now on")

    async def close(self) -> None:
        if self._session is not None:
            await self._session.close()
            self._session = None


class GoeLocalConnector(_GoeBase):
    label = "go-e (local network)"
    fields = (("host", "Charger address on your network (IP or name, e.g. 192.168.1.50)", False, True),)
    name = "go-e"

    def __init__(self, config: dict[str, Any]) -> None:
        super().__init__()
        host = (config.get("host") or "").strip().rstrip("/")
        if not host:
            raise ConnectorError("Enter the address of the go-e charger on your network")
        self._base = host if host.startswith(("http://", "https://")) else f"http://{host}"

    def _url(self) -> str:
        return self._base

    def _unreachable(self, detail: str) -> str:
        hint = "check its address, that this server is on the same network, and that 'local HTTP API v2' is allowed in the go-e app"
        return f"Cannot read the go-e charger at {self._base} ({detail}): {hint}"


class GoeCloudConnector(_GoeBase):
    label = "go-e (go-e Cloud)"
    fields = (("serial", "Charger serial number (6 digits)", False, True), ("token", "Cloud API token (go-e app: Internet → Advanced settings)", True, True))
    name = "go-e Cloud"
    cloud_url = CLOUD_URL

    def __init__(self, config: dict[str, Any]) -> None:
        super().__init__()
        serial, token = (config.get("serial") or "").strip(), (config.get("token") or "").strip()
        if not serial or not token:
            raise ConnectorError("Enter the serial number of the go-e charger and its cloud API token")
        if not serial.isdigit():
            raise ConnectorError("The serial number of a go-e charger is made of digits (see the go-e app or the label)")
        self._serial, self._token = serial.zfill(6), token

    def _url(self) -> str:
        return self.cloud_url.format(serial=self._serial)

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._token}"}

    def _unreachable(self, detail: str) -> str:
        hint = "check the serial number and the token, that the Cloud API is enabled in the go-e app, and that the charger is online"
        return f"go-e Cloud refused or can't be reached ({detail}): {hint}"
