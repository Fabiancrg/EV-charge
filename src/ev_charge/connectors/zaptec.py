"""Zaptec chargers (Go, Go 2, Pro…) through Zaptec's official cloud API (https://docs.zaptec.com).

- Login: the Zaptec Portal / app account (OAuth 2.0 "password" grant, token valid 24 h).
  Zaptec plans to replace this login method with the "code" grant one day.
- Live: GET /api/chargers/{id}/state, read through the observation ids of GET /api/constants
  (ChargerOperationMode, TotalChargePower in W, TotalChargePowerSession in kWh).
- History: GET /api/chargehistory lists the completed sessions (exact start, end, energy),
  at most 365 days back.
An account can hold several chargers: the serial number (e.g. ZAP049123) picks one.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import aiohttp

from .base import CHARGING, FINISHED, OFFLINE, PLUGGED_IN, UNPLUGGED, ConnectorError, HourStat, LiveReading

TOKEN_URL = "https://api.zaptec.com/oauth/token"
API_URL = "https://api.zaptec.com/api/"
TIMEOUT = aiohttp.ClientTimeout(total=20)
PAGE_SIZE = 100  # the API's maximum
HISTORY_DAYS = 365  # the API refuses older history
# Used when /api/constants can't be read (values from the Zaptec documentation)
FALLBACK_OBSERVATIONS = {"ChargerOperationMode": 710, "TotalChargePower": 513, "TotalChargePowerSession": 553}
FALLBACK_MODES = {"Unknown": 0, "Disconnected": 1, "Connected_Requesting": 2, "Connected_Charging": 3, "Connected_Finished": 5}
_MODE_STATUS = {"Disconnected": UNPLUGGED, "Connected_Requesting": PLUGGED_IN, "Connected_Charging": CHARGING, "Connected_Finished": FINISHED}


def _number(value: Any) -> float:
    try:
        return float(str(value).replace(",", "."))
    except (TypeError, ValueError):
        return 0.0


def _local(stamp: str | None) -> datetime | None:
    """Zaptec times are UTC ("2026-10-05T19:58:12.37" or with an offset) → local naive datetime."""
    if not stamp:
        return None
    try:
        dt = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone().replace(tzinfo=None)


class ZaptecConnector:
    label = "Zaptec (official API)"
    fields = (
        ("email", "Zaptec account e-mail", False, True),
        ("password", "Zaptec account password", True, True),
        ("serial", "Charger serial number (if the account has several)", False, False),
    )
    token_url = TOKEN_URL
    api_url = API_URL

    def __init__(self, config: dict[str, Any]) -> None:
        if not config.get("email") or not config.get("password"):
            raise ConnectorError("Enter the e-mail and password of the Zaptec account")
        self._config = config
        self._session: aiohttp.ClientSession | None = None
        self._token: str | None = None
        self._token_until = datetime.min.replace(tzinfo=UTC)
        self._charger: dict[str, Any] | None = None
        self._observations: dict[str, int] | None = None
        self._modes: dict[int, str] | None = None

    # ── HTTP ──────────────────────────────────────────────────────────────
    def _http(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=TIMEOUT, trust_env=True)  # trust_env: honour HTTPS_PROXY
        return self._session

    async def _login(self) -> None:
        data = {"grant_type": "password", "username": self._config["email"], "password": self._config["password"]}
        try:
            async with self._http().post(self.token_url, data=data) as resp:
                body = await resp.json(content_type=None)
                if resp.status == 400:
                    raise ConnectorError(f"Zaptec refused the login: {body.get('error_description') or 'check the e-mail and password'}")
                if resp.status != 200 or not body.get("access_token"):
                    raise ConnectorError(f"Zaptec login failed (HTTP {resp.status})")
        except aiohttp.ClientError as exc:
            raise ConnectorError(f"Cannot reach Zaptec ({exc})") from exc
        self._token = body["access_token"]
        self._token_until = datetime.now(UTC) + timedelta(seconds=int(body.get("expires_in") or 3600) - 300)

    async def _get(self, path: str, params: dict[str, Any] | None = None, retry: bool = True) -> Any:
        if not self._token or datetime.now(UTC) >= self._token_until:
            await self._login()
        try:
            async with self._http().get(self.api_url + path, params=params, headers={"Authorization": f"Bearer {self._token}", "Accept": "application/json"}) as resp:
                if resp.status == 401 and retry:  # token revoked or expired early
                    self._token = None
                    return await self._get(path, params, retry=False)
                if resp.status == 403:
                    raise ConnectorError("Zaptec: this account may not read this charger")
                if resp.status != 200:
                    raise ConnectorError(f"Zaptec: {path} answered HTTP {resp.status}")
                return await resp.json(content_type=None)
        except aiohttp.ClientError as exc:
            raise ConnectorError(f"Cannot reach Zaptec ({exc})") from exc

    # ── Charger and constants ─────────────────────────────────────────────
    async def _find_charger(self) -> dict[str, Any]:
        if self._charger is None:
            body = await self._get("chargers")
            chargers = body.get("Data", body) if isinstance(body, dict) else body
            if not chargers:
                raise ConnectorError("No charger on this Zaptec account")
            serial = (self._config.get("serial") or "").strip().upper()
            if serial:
                found = [c for c in chargers if serial in (str(c.get("DeviceId", "")).upper(), str(c.get("Name", "")).upper())]
                if not found:
                    raise ConnectorError(f"No charger {serial} on this Zaptec account (found: {', '.join(str(c.get('DeviceId')) for c in chargers)})")
                self._charger = found[0]
            elif len(chargers) > 1:
                raise ConnectorError(f"This Zaptec account has {len(chargers)} chargers: enter the serial number of one ({', '.join(str(c.get('DeviceId')) for c in chargers)})")
            else:
                self._charger = chargers[0]
        return self._charger

    async def _constants(self) -> tuple[dict[str, int], dict[int, str]]:
        if self._observations is None:
            observations, modes = dict(FALLBACK_OBSERVATIONS), dict(FALLBACK_MODES)
            try:
                const = await self._get("constants")
                ids = const.get("ObservationIds") or const.get("Observations") or {}
                observations.update({k: int(v) for k, v in ids.items() if k in FALLBACK_OBSERVATIONS and str(v).isdigit()})
                modes.update({k: int(v) for k, v in (const.get("ChargerOperationModes") or {}).items() if str(v).lstrip("-").isdigit()})
            except ConnectorError:
                pass  # the documented ids still work
            self._observations, self._modes = observations, {v: k for k, v in modes.items()}
        return self._observations, self._modes  # type: ignore[return-value]

    # ── Connector interface ───────────────────────────────────────────────
    async def describe(self) -> dict[str, Any]:
        c = await self._find_charger()
        return {"serial": c.get("DeviceId"), "model": c.get("DeviceName") or "Zaptec", "name": c.get("Name")}

    async def live(self) -> LiveReading:
        charger = await self._find_charger()
        observations, modes = await self._constants()
        info = await self._get(f"chargers/{charger['Id']}")
        if isinstance(info, dict) and info.get("IsOnline") is False:
            return LiveReading(status=OFFLINE, online=False)
        values = {int(o["StateId"]): o.get("ValueAsString", o.get("Value")) for o in await self._get(f"chargers/{charger['Id']}/state") if "StateId" in o}
        mode_raw = values.get(observations["ChargerOperationMode"], info.get("OperatingMode") if isinstance(info, dict) else None)
        mode = modes.get(int(_number(mode_raw)), "Unknown")
        status = _MODE_STATUS.get(mode, PLUGGED_IN if mode != "Unknown" else OFFLINE)
        plugged = status in (CHARGING, PLUGGED_IN, FINISHED)
        return LiveReading(
            status=status,
            online=status != OFFLINE,
            power_w=_number(values.get(observations["TotalChargePower"])),
            energy_wh=_number(values.get(observations["TotalChargePowerSession"])) * 1000 if plugged else 0.0,
            extra={"mode": mode},
        )

    async def history(self, start: datetime, end: datetime) -> list[HourStat]:
        charger = await self._find_charger()
        start = max(start, datetime.now() - timedelta(days=HISTORY_DAYS))
        out: list[HourStat] = []
        page = 0
        while True:
            params = {
                "ChargerId": charger["Id"],
                "From": start.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "To": end.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "PageSize": PAGE_SIZE,
                "PageIndex": page,
                "DetailLevel": 0,
            }
            body = await self._get("chargehistory", params)
            for s in body.get("Data") or []:
                begin, finish = _local(s.get("StartDateTime")), _local(s.get("EndDateTime"))
                kwh = _number(s.get("Energy"))
                if begin is None or finish is None or kwh <= 0:
                    continue  # still running, or empty
                out.append(HourStat(start=begin, end=finish, energy_wh=kwh * 1000, is_session=True, ref=str(s.get("Id") or "")))
            page += 1
            if page >= int(body.get("Pages") or 1):
                break
        return sorted(out, key=lambda h: h.start)

    async def close(self) -> None:
        if self._session is not None:
            await self._session.close()
            self._session = None
