"""Ohme chargers (Home Pro, ePod, Home/Go…) through Ohme's cloud.

Ohme has no public API: this uses the open-source ``ohme`` library (MIT, the one behind the
Home Assistant integration), which logs in with the Ohme app account (e-mail and password).
One Ohme account = one charger. Unofficial: Ohme can change its API at any time.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from ..i18n import msg
from .base import CHARGING, FINISHED, OFFLINE, PAUSED, PENDING_APPROVAL, PLUGGED_IN, UNPLUGGED, ConnectorError, HourStat, LiveReading

_STATUS = {
    "unplugged": UNPLUGGED,
    "pending_approval": PENDING_APPROVAL,
    "plugged_in": PLUGGED_IN,
    "charging": CHARGING,
    "paused": PAUSED,
    "finished": FINISHED,
}
HISTORY_CHUNK = timedelta(days=7)  # one summary request per week of history


class OhmeConnector:
    label = "Ohme (cloud account)"
    estimates_energy = True  # no session energy in its API: estimated live, then taken from its hourly history
    fields = (("email", "Ohme account e-mail", False, True), ("password", "Ohme account password", True, True))

    def __init__(self, config: dict[str, Any]) -> None:
        if not config.get("email") or not config.get("password"):
            raise ConnectorError(msg("Enter the e-mail and password of the Ohme app account"))
        self._config = config
        self._client: Any = None

    async def _api(self) -> Any:
        try:
            from ohme import OhmeApiClient
        except ImportError as exc:  # pragma: no cover - dependency missing
            raise ConnectorError(msg("The 'ohme' Python package is not installed")) from exc
        if self._client is None:
            client = OhmeApiClient(self._config["email"], self._config["password"])
            try:
                if not await client.async_login():
                    raise ConnectorError(msg("Ohme refused the login: check the e-mail and password"))
                await client.async_update_device_info()
            except ConnectorError:
                await client.close()
                raise
            except Exception as exc:
                await client.close()
                raise ConnectorError(msg("Ohme login failed: {error}", error=exc)) from exc
            self._client = client
        return self._client

    async def describe(self) -> dict[str, Any]:
        api = await self._api()
        info = api.device_info or {}
        return {"serial": api.serial, "model": info.get("name"), "firmware": info.get("sw_version")}

    async def live(self) -> LiveReading:
        api = await self._api()
        try:
            await api.async_get_charge_session()
        except Exception as exc:
            raise ConnectorError(msg("Ohme: cannot read the charge session ({error})", error=exc)) from exc
        if not api.available:
            return LiveReading(status=OFFLINE, online=False)
        status = _STATUS.get(getattr(api.status, "value", str(api.status)), PLUGGED_IN)
        power = api.power
        # Ohme doesn't give the energy of the session: the library's ``energy`` (``batterySoc.wh``) is the energy
        # in the car's battery, not what this charge delivered. It is estimated from the power instead.
        return LiveReading(
            status=status,
            power_w=float(power.watts or 0),
            energy_from_power=True,
            battery_pct=int(api.battery) if api.battery else None,
            extra={"amps": power.amps, "volts": power.volts, "mode": getattr(api.mode, "value", None)},
        )

    async def history(self, start: datetime, end: datetime) -> list[HourStat]:
        from ohme import SummaryGranularity

        api = await self._api()
        stats: list[HourStat] = []
        cursor = start
        while cursor < end:
            chunk_end = min(cursor + HISTORY_CHUNK, end)
            try:
                summary = await api.async_get_charge_summary(int(cursor.timestamp() * 1000), int(chunk_end.timestamp() * 1000), SummaryGranularity.HOUR)
            except Exception as exc:
                raise ConnectorError(msg("Ohme: cannot read the charging history ({error})", error=exc)) from exc
            for s in summary.get("stats") or []:
                wh = float(s.get("energyChargedTotalWh") or 0)
                if wh <= 0:
                    continue
                stats.append(
                    HourStat(
                        start=datetime.fromtimestamp(s["startTime"] / 1000),
                        end=datetime.fromtimestamp(s["endTime"] / 1000),
                        energy_wh=wh,
                        active_ms=int(s.get("activeChargeMs") or 0),
                    )
                )
            cursor = chunk_end
        return sorted(stats, key=lambda h: h.start)

    async def close(self) -> None:
        if self._client is not None:
            await self._client.close()
            self._client = None
