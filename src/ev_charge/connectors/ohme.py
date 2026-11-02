"""Ohme chargers (Home Pro, ePod, Home/Go…) through Ohme's cloud.

Ohme has no public API: this uses the open-source ``ohme`` library (MIT, the one behind the
Home Assistant integration), which logs in with the Ohme app account (e-mail and password).
One Ohme account = one charger. Unofficial: Ohme can change its API at any time.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

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
    fields = (("email", "Ohme account e-mail", False, True), ("password", "Ohme account password", True, True))

    def __init__(self, config: dict[str, Any]) -> None:
        if not config.get("email") or not config.get("password"):
            raise ConnectorError("Enter the e-mail and password of the Ohme app account")
        self._config = config
        self._client: Any = None

    async def _api(self) -> Any:
        try:
            from ohme import OhmeApiClient
        except ImportError as exc:  # pragma: no cover - dependency missing
            raise ConnectorError("The 'ohme' Python package is not installed") from exc
        if self._client is None:
            client = OhmeApiClient(self._config["email"], self._config["password"])
            try:
                if not await client.async_login():
                    raise ConnectorError("Ohme refused the login: check the e-mail and password")
                await client.async_update_device_info()
            except ConnectorError:
                await client.close()
                raise
            except Exception as exc:
                await client.close()
                raise ConnectorError(f"Ohme login failed: {exc}") from exc
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
            raise ConnectorError(f"Ohme: cannot read the charge session ({exc})") from exc
        if not api.available:
            return LiveReading(status=OFFLINE, online=False)
        status = _STATUS.get(getattr(api.status, "value", str(api.status)), PLUGGED_IN)
        power = api.power
        return LiveReading(
            status=status,
            power_w=float(power.watts or 0),
            energy_wh=self._session_wh(api, status),
            battery_pct=int(api.battery) if api.battery else None,
            extra={"amps": power.amps, "volts": power.volts, "mode": getattr(api.mode, "value", None)},
        )

    @staticmethod
    def _session_wh(api: Any, status: str) -> float:
        """Energy of the current session as Ohme reports it now.

        Not the library's ``energy``: it keeps the highest value seen by this client (it only resets when a
        read happens while unplugged), so a new session started between two reads (quick unplug / replug, or
        the charger offline meanwhile) would begin with the previous session's energy."""
        if status in (UNPLUGGED, PENDING_APPROVAL):
            return 0.0
        session = getattr(api, "_charge_session", None) or {}
        soc = session.get("batterySoc") if isinstance(session, dict) else None
        if isinstance(soc, dict) and soc.get("wh") is not None:
            return max(0.0, float(soc["wh"]))
        return float(api.energy or 0)  # library structure changed: fall back

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
                raise ConnectorError(f"Ohme: cannot read the charging history ({exc})") from exc
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
