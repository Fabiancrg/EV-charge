"""A charger seen through Home Assistant: a smart plug (or any power and energy sensors) on the charger's supply.

Useful when the charger's own cloud is unreliable (e.g. an Ohme Go on a metered plug). Uses Home Assistant's
official REST and WebSocket APIs with a long-lived access token (HA profile → Security).

- Power sensor (W or kW): the charger is charging while the power is above a threshold (100 W by default).
- Energy sensor (kWh or Wh): any increasing counter, cumulative or one that resets (a daily utility meter).
  A value going down is taken as a reset.
- Live session: the energy added since charging started. A smart plug can't tell whether a car is plugged,
  so a session ends after the energy has stopped increasing for a while (60 minutes by default, so that
  short pauses of the charger don't split a session). Computed from HA's recorded history of the energy
  sensor, so it survives restarts of EV Charge.
- History: the energy per hour from HA's long-term statistics (kept forever for sensors with a state class),
  or from the recorded history (about 10 days) otherwise.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import quote

import aiohttp

from .base import CHARGING, OFFLINE, PAUSED, UNPLUGGED, ConnectorError, HourStat, LiveReading

TIMEOUT = aiohttp.ClientTimeout(total=30)
LOOKBACK = timedelta(hours=48)  # history read on the first poll: longer than a session can last
OVERLAP = timedelta(minutes=5)  # re-read a little of the history already known, on each poll
DEFAULT_THRESHOLD_W = 100.0
DEFAULT_IDLE_MINUTES = 60
MIN_SESSION_WH = 100.0  # below this, energy counted while not charging is the plug's standby use, not a session
STANDBY_GAP = timedelta(minutes=10)  # increments further apart than this are standby, not charging (start of a session)
MIN_HOUR_WH = 20.0  # hours of history with less energy than this are standby, not charging
BAD_STATES = {"unavailable", "unknown", "none", ""}


def _float(value: Any) -> float | None:
    if value is None or str(value).strip().lower() in BAD_STATES:
        return None
    try:
        return float(str(value).replace(",", "."))
    except ValueError:
        return None


def _when(stamp: Any) -> datetime | None:
    """HA times: ISO strings with an offset, or Unix seconds / milliseconds → aware UTC datetime."""
    if stamp is None:
        return None
    if isinstance(stamp, int | float):
        return datetime.fromtimestamp(stamp / 1000 if stamp > 1e11 else stamp, UTC)
    try:
        dt = datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def _local(dt: datetime) -> datetime:
    return dt.astimezone().replace(tzinfo=None)


def _scale(unit: str | None, kind: str) -> float:
    """Factor to W (power) or Wh (energy)."""
    u = (unit or "").strip()
    if kind == "power":
        return {"W": 1.0, "kW": 1000.0, "MW": 1e6}.get(u, 1.0)
    return {"Wh": 1.0, "kWh": 1000.0, "MWh": 1e6}.get(u, 1000.0)  # no unit: assume kWh


def increments(points: list[tuple[datetime, float]]) -> list[tuple[datetime, float]]:
    """(time, energy added) from a counter's readings; a drop is a reset (the counter restarted from 0)."""
    out: list[tuple[datetime, float]] = []
    prev: float | None = None
    for t, v in points:
        if prev is not None and v != prev:
            delta = v - prev if v > prev else v
            if delta > 0:
                out.append((t, delta))
        prev = v
    return out


def current_session(incs: list[tuple[datetime, float]], now: datetime, idle: timedelta) -> tuple[float, datetime | None, datetime | None]:
    """The session still open at `now`: the last run of increments without a gap longer than `idle`.
    Returns (energy, first increment, last increment); (0, None, None) when the last increment is too old."""
    if not incs or now - incs[-1][0] > idle:
        return 0.0, None, None
    run: list[datetime] = []
    total = 0.0
    for t, wh in reversed(incs):
        if run and run[-1] - t > idle:
            break
        total += wh
        run.append(t)
    run.reverse()
    # The start: the first increment closely followed by another (an isolated one before is the plug's standby use)
    first = next((t for t, after in zip(run, run[1:], strict=False) if after - t <= STANDBY_GAP), run[0])
    return total, first, run[-1]


def hourly(incs: list[tuple[datetime, float]]) -> list[HourStat]:
    buckets: dict[datetime, float] = {}
    for t, wh in incs:
        hour = t.replace(minute=0, second=0, microsecond=0)
        buckets[hour] = buckets.get(hour, 0.0) + wh
    return [HourStat(start=_local(h), end=_local(h + timedelta(hours=1)), energy_wh=wh) for h, wh in sorted(buckets.items()) if wh >= MIN_HOUR_WH]


class HomeAssistantConnector:
    label = "Home Assistant (smart plug sensors)"
    fields = (
        ("url", "Home Assistant URL (e.g. http://homeassistant.local:8123)", False, True),
        ("token", "Long-lived access token", True, True),
        ("power_entity", "Power sensor (e.g. sensor.carplug2_relay_power)", False, True),
        ("energy_entity", "Energy sensor (e.g. sensor.ve_plug_2_energy_daily)", False, True),
        ("threshold_w", f"Charging above (W, default {DEFAULT_THRESHOLD_W:.0f})", False, False),
        ("idle_minutes", f"Session ends after no energy for (minutes, default {DEFAULT_IDLE_MINUTES})", False, False),
    )

    def __init__(self, config: dict[str, Any]) -> None:
        missing = [label for name, label, _secret, required in self.fields if required and not str(config.get(name) or "").strip()]
        if missing:
            raise ConnectorError(f"Enter: {', '.join(missing)}")
        url = str(config["url"]).strip().rstrip("/")
        if url.endswith("/api"):
            url = url[:-4]
        if not url.startswith(("http://", "https://")):
            raise ConnectorError("The Home Assistant URL must start with http:// or https://")
        self._url = url
        self._token = str(config["token"]).strip()
        self._power = str(config["power_entity"]).strip()
        self._energy = str(config["energy_entity"]).strip()
        try:
            self._threshold = float(str(config.get("threshold_w") or DEFAULT_THRESHOLD_W).replace(",", "."))
            self._idle = timedelta(minutes=float(str(config.get("idle_minutes") or DEFAULT_IDLE_MINUTES).replace(",", ".")))
        except ValueError as exc:
            raise ConnectorError("The power threshold and the idle time must be numbers") from exc
        if self._idle <= timedelta(0):
            raise ConnectorError("The idle time must be more than 0 minutes")
        self._session: aiohttp.ClientSession | None = None
        self._points: list[tuple[datetime, float]] = []  # energy readings (UTC, Wh) known so far
        self._points_until: datetime | None = None

    # ── HTTP ──────────────────────────────────────────────────────────────
    def _http(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=TIMEOUT, trust_env=True)
        return self._session

    async def _get(self, path: str, params: dict[str, str] | None = None) -> Any:
        headers = {"Authorization": f"Bearer {self._token}", "Accept": "application/json"}
        try:
            async with self._http().get(f"{self._url}/api/{path}", params=params, headers=headers) as resp:
                if resp.status == 401:
                    raise ConnectorError("Home Assistant refused the access token")
                if resp.status == 404 and path.startswith("states/"):
                    raise ConnectorError(f"Home Assistant has no entity {path[7:]}")
                if resp.status != 200:
                    raise ConnectorError(f"Home Assistant: /api/{path} answered HTTP {resp.status}")
                return await resp.json(content_type=None)
        except aiohttp.ClientError as exc:
            raise ConnectorError(f"Cannot reach Home Assistant at {self._url} ({exc})") from exc
        except TimeoutError as exc:
            raise ConnectorError(f"Home Assistant at {self._url} did not answer in time") from exc

    async def _state(self, entity: str) -> dict[str, Any]:
        return await self._get(f"states/{quote(entity)}")  # type: ignore[no-any-return]

    async def _energy_history(self, start: datetime, end: datetime, scale: float) -> list[tuple[datetime, float]]:
        body = await self._get(
            f"history/period/{quote(start.isoformat())}",
            {"filter_entity_id": self._energy, "end_time": end.isoformat(), "minimal_response": "", "no_attributes": ""},
        )
        rows = body[0] if body and isinstance(body[0], list) else []
        points = []
        for row in rows:
            value, t = _float(row.get("state")), _when(row.get("last_changed") or row.get("last_updated"))
            if value is not None and t is not None:
                points.append((t, value * scale))
        return sorted(points)

    # ── Connector interface ───────────────────────────────────────────────
    async def describe(self) -> dict[str, Any]:
        config = await self._get("config")
        power, energy = await self._state(self._power), await self._state(self._energy)
        units = {"power": power.get("attributes", {}).get("unit_of_measurement"), "energy": energy.get("attributes", {}).get("unit_of_measurement")}
        if units["power"] not in (None, "W", "kW"):
            raise ConnectorError(f"{self._power} is in {units['power']}, not a power (W or kW)")
        if units["energy"] not in (None, "Wh", "kWh", "MWh"):
            raise ConnectorError(f"{self._energy} is in {units['energy']}, not an energy (Wh or kWh)")
        name = power.get("attributes", {}).get("friendly_name") or self._power
        return {"serial": self._power, "model": f"Home Assistant: {name}", "firmware": config.get("version") if isinstance(config, dict) else None}

    async def live(self) -> LiveReading:
        power_state = await self._state(self._power)
        energy_state = await self._state(self._energy)
        power = _float(power_state.get("state"))
        if power is None:  # the plug is offline (or briefly unavailable): keep the session as it is
            return LiveReading(status=OFFLINE, online=False)
        power_w = power * _scale(power_state.get("attributes", {}).get("unit_of_measurement"), "power")
        scale = _scale(energy_state.get("attributes", {}).get("unit_of_measurement"), "energy")

        now = datetime.now(UTC)
        start = (self._points_until - OVERLAP) if self._points_until else now - LOOKBACK
        fresh = await self._energy_history(start, now, scale)
        known = {t for t, _ in self._points}
        self._points = sorted(self._points + [p for p in fresh if p[0] not in known])
        self._points = [p for p in self._points if p[0] >= now - LOOKBACK]
        self._points_until = now
        value, changed = _float(energy_state.get("state")), _when(energy_state.get("last_changed"))
        points = list(self._points)
        if value is not None and changed is not None and (not points or changed > points[-1][0]):
            points.append((changed, value * scale))

        energy_wh, first, last = current_session(increments(points), now, self._idle)
        charging = power_w >= self._threshold
        if charging:
            status = CHARGING
        elif energy_wh >= MIN_SESSION_WH:
            status = PAUSED  # stopped recently: the session stays open until the idle time has passed
        else:
            status, energy_wh = UNPLUGGED, 0.0
        extra: dict[str, Any] = {"source": "home_assistant"}
        if first is not None and status != UNPLUGGED:
            extra["session_start"] = _local(first).isoformat(timespec="minutes")
            extra["last_energy_at"] = _local(last).isoformat(timespec="minutes") if last else None
        return LiveReading(status=status, power_w=round(power_w, 1), energy_wh=round(energy_wh, 1), extra=extra)

    async def history(self, start: datetime, end: datetime) -> list[HourStat]:
        """Energy per hour between two local times."""
        a, b = start.astimezone(UTC), end.astimezone(UTC)
        stats = await self._statistics(a, b)
        if stats is None:  # no long-term statistics: the recorded history (HA keeps about 10 days)
            energy_state = await self._state(self._energy)
            scale = _scale(energy_state.get("attributes", {}).get("unit_of_measurement"), "energy")
            stats = hourly(increments(await self._energy_history(a, b, scale)))
        return stats

    async def _statistics(self, start: datetime, end: datetime) -> list[HourStat] | None:
        """Hourly change of the energy sensor from HA's long-term statistics (WebSocket API); None if it has none."""
        ws_url = ("wss://" if self._url.startswith("https://") else "ws://") + self._url.split("://", 1)[1] + "/api/websocket"
        try:
            async with self._http().ws_connect(ws_url, max_msg_size=64 * 1024 * 1024, heartbeat=30) as ws:
                await ws.receive_json()  # auth_required
                await ws.send_json({"type": "auth", "access_token": self._token})
                auth = await ws.receive_json()
                if auth.get("type") != "auth_ok":
                    raise ConnectorError("Home Assistant refused the access token")
                await ws.send_json({
                    "id": 1, "type": "recorder/statistics_during_period", "statistic_ids": [self._energy], "period": "hour",
                    "start_time": start.isoformat(), "end_time": end.isoformat(), "types": ["change"], "units": {"energy": "Wh"},
                })  # fmt: skip
                while True:
                    msg = await ws.receive_json()
                    if msg.get("id") == 1 and msg.get("type") == "result":
                        break
        except (aiohttp.ClientError, TimeoutError, TypeError, ValueError) as exc:
            raise ConnectorError(f"Home Assistant: cannot read the statistics ({exc})") from exc
        if not msg.get("success"):
            raise ConnectorError(f"Home Assistant: cannot read the statistics ({(msg.get('error') or {}).get('message', 'refused')})")
        rows = (msg.get("result") or {}).get(self._energy) or []
        if not rows:
            return None
        out = []
        for row in rows:
            begin, finish, wh = _when(row.get("start")), _when(row.get("end")), _float(row.get("change"))
            if begin is None or wh is None or wh < MIN_HOUR_WH:
                continue
            out.append(HourStat(start=_local(begin), end=_local(finish or begin + timedelta(hours=1)), energy_wh=wh))
        return sorted(out, key=lambda h: h.start)

    async def close(self) -> None:
        if self._session is not None:
            await self._session.close()
            self._session = None
