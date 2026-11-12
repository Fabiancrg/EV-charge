"""Easee chargers (Home, Charge, Charge Lite, Charge Max…) through Easee's official cloud API
(https://developer.easee.com). Easee chargers have no local API.

- Login: the Easee app / portal account (POST /api/accounts/login; the token is renewed with its refresh token).
- Live: GET /state/{id}/observations?ids=109,120,121: operation mode, total power (kW) and session energy (kWh).
  It replaces GET /api/chargers/{id}/state, which Easee retired on 1 September 2026. Limited to 100 requests
  per 5 minutes, far above one read a minute.
- History: GET /api/sessions/charger/{id}/sessions/{from}/{to} lists the sessions (car connected, car disconnected,
  kWh). Easee allows about 10 of these requests per hour.
An account can hold several chargers: the serial number (e.g. EH123456) picks one.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import aiohttp

from .base import CHARGING, FINISHED, OFFLINE, PENDING_APPROVAL, PLUGGED_IN, UNPLUGGED, ConnectorError, HourStat, LiveReading

BASE_URL = "https://api.easee.com"
TIMEOUT = aiohttp.ClientTimeout(total=20)
OP_MODE, TOTAL_POWER, SESSION_ENERGY = 109, 120, 121  # observation ids
# chargerOpMode: 0 offline, 1 disconnected, 2 awaiting start, 3 charging, 4 completed, 5 error, 6 ready to charge,
# 7 awaiting authorization, 8 de-authorizing
_MODE_STATUS = {1: UNPLUGGED, 2: PLUGGED_IN, 3: CHARGING, 4: FINISHED, 6: PLUGGED_IN, 7: PENDING_APPROVAL, 8: PLUGGED_IN}


def _number(value: Any) -> float:
    try:
        return float(str(value).replace(",", "."))
    except (TypeError, ValueError):
        return 0.0


def _local(stamp: Any) -> datetime | None:
    """Easee times are UTC ("2026-10-05T19:58:12Z", or without an offset) → local naive datetime."""
    if not stamp:
        return None
    try:
        dt = datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone().replace(tzinfo=None)


class EaseeConnector:
    label = "Easee (official cloud API)"
    lists_sessions = True  # its history is the list of sessions: a live session takes its final figures from it
    fields = (
        ("username", "Easee account (e-mail or phone number with country code)", False, True),
        ("password", "Easee account password", True, True),
        ("serial", "Charger serial number (if the account has several)", False, False),
    )
    base_url = BASE_URL

    def __init__(self, config: dict[str, Any]) -> None:
        if not config.get("username") or not config.get("password"):
            raise ConnectorError("Enter the user name and password of the Easee account")
        self._config = config
        self._session: aiohttp.ClientSession | None = None
        self._token: dict[str, Any] | None = None
        self._token_until = datetime.min.replace(tzinfo=UTC)
        self._charger: dict[str, Any] | None = None

    # ── HTTP ──────────────────────────────────────────────────────────────
    def _http(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=TIMEOUT, trust_env=True)
        return self._session

    async def _token_request(self, path: str, data: dict[str, Any]) -> bool:
        try:
            async with self._http().post(f"{self.base_url}/api/accounts/{path}", json=data, headers={"Accept": "application/json"}) as resp:
                body = await resp.json(content_type=None) if resp.status == 200 else None
                status = resp.status
        except (aiohttp.ClientError, TimeoutError) as exc:
            raise ConnectorError(f"Cannot reach Easee ({str(exc) or type(exc).__name__})") from exc
        except ValueError:
            body, status = None, 200
        if not isinstance(body, dict) or not body.get("accessToken"):
            if path == "login":
                raise ConnectorError("Easee refused the login: check the user name and password" if status in (400, 401) else f"Easee login failed (HTTP {status})")
            return False
        self._token = body
        self._token_until = datetime.now(UTC) + timedelta(seconds=max(60, int(_number(body.get("expiresIn")) or 3600) - 60))
        return True

    async def _authorize(self) -> None:
        if self._token and datetime.now(UTC) < self._token_until:
            return
        if self._token and self._token.get("refreshToken"):
            if await self._token_request("refresh_token", {"accessToken": self._token["accessToken"], "refreshToken": self._token["refreshToken"]}):
                return
        await self._token_request("login", {"userName": self._config["username"], "password": self._config["password"]})

    async def _get(self, path: str, retry: bool = True) -> Any:
        await self._authorize()
        assert self._token is not None
        try:
            async with self._http().get(self.base_url + path, headers={"Authorization": f"Bearer {self._token['accessToken']}", "Accept": "application/json"}) as resp:
                if resp.status == 401 and retry:  # token revoked or expired early
                    self._token = None
                    return await self._get(path, retry=False)
                if resp.status == 403:
                    raise ConnectorError("Easee: this account may not read this charger")
                if resp.status == 429:
                    raise ConnectorError("Easee: too many requests for now (its API is rate limited); try again in a few minutes")
                if resp.status != 200:
                    raise ConnectorError(f"Easee: {path.split('?')[0]} answered HTTP {resp.status}")
                return await resp.json(content_type=None)
        except (aiohttp.ClientError, TimeoutError) as exc:
            raise ConnectorError(f"Cannot reach Easee ({str(exc) or type(exc).__name__})") from exc

    # ── Charger ───────────────────────────────────────────────────────────
    async def _find_charger(self) -> dict[str, Any]:
        if self._charger is None:
            chargers = await self._get("/api/chargers")
            if not chargers:
                raise ConnectorError("No charger on this Easee account")
            ids = ", ".join(str(c.get("id")) for c in chargers)
            serial = (self._config.get("serial") or "").strip().upper()
            if serial:
                found = [c for c in chargers if serial in (str(c.get("id", "")).upper(), str(c.get("name", "")).upper())]
                if not found:
                    raise ConnectorError(f"No charger {serial} on this Easee account (found: {ids})")
                self._charger = found[0]
            elif len(chargers) > 1:
                raise ConnectorError(f"This Easee account has {len(chargers)} chargers: enter the serial number of one ({ids})")
            else:
                self._charger = chargers[0]
        return self._charger

    # ── Connector interface ───────────────────────────────────────────────
    async def describe(self) -> dict[str, Any]:
        c = await self._find_charger()
        return {"serial": c.get("id"), "model": c.get("productCode") and f"Easee ({c['productCode']})" or "Easee", "name": c.get("name")}

    async def live(self) -> LiveReading:
        charger = await self._find_charger()
        body = await self._get(f"/state/{charger['id']}/observations?ids={OP_MODE},{TOTAL_POWER},{SESSION_ENERGY}")
        observations = body.get("observations") if isinstance(body, dict) else None
        if not isinstance(observations, list):
            raise ConnectorError("Easee: unexpected answer to the charger's state")
        values = {int(_number(o.get("id"))): o.get("value") for o in observations if isinstance(o, dict)}
        if OP_MODE not in values:
            return LiveReading(status=OFFLINE, online=False)  # the charger never reported: not installed or offline
        mode = int(_number(values[OP_MODE]))
        status = _MODE_STATUS.get(mode, OFFLINE)  # 0 offline, 5 error
        plugged = status in (CHARGING, PLUGGED_IN, FINISHED, PENDING_APPROVAL)
        return LiveReading(
            status=status,
            online=status != OFFLINE,
            power_w=_number(values.get(TOTAL_POWER)) * 1000,
            energy_wh=_number(values.get(SESSION_ENERGY)) * 1000 if plugged else 0.0,
            extra={"mode": mode},
        )

    async def history(self, start: datetime, end: datetime) -> list[HourStat]:
        charger = await self._find_charger()
        stamp = "%Y-%m-%dT%H:%M:%SZ"
        sessions = await self._get(f"/api/sessions/charger/{charger['id']}/sessions/{start.astimezone(UTC).strftime(stamp)}/{end.astimezone(UTC).strftime(stamp)}")
        out: list[HourStat] = []
        for s in sessions or []:
            begin, finish = _local(s.get("carConnected")), _local(s.get("carDisconnected"))
            kwh = _number(s.get("kiloWattHours"))
            if begin is None or finish is None or kwh <= 0:
                continue  # still running, or empty
            out.append(HourStat(start=begin, end=finish, energy_wh=kwh * 1000, is_session=True, ref=str(s.get("sessionId") or s.get("id") or s.get("carConnected"))))
        return sorted(out, key=lambda h: h.start)

    async def close(self) -> None:
        if self._session is not None:
            await self._session.close()
            self._session = None
