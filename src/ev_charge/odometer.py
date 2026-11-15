"""The odometer and the battery level of a car, read from Home Assistant.

Home Assistant has an integration for most makes, and shows the car's mileage as a sensor. A car can be given
that sensor (Home Assistant's address, an access token and the entity): its odometer is then written on the
sessions recorded from a charger, and can be fetched in the session form.

It is read when the charge ends, not when it starts: many cars refresh that figure rarely, and do it at the
end of a charge. As the refresh may reach Home Assistant a little later, it is read again a few times in the
following minutes (live.py) and the highest figure is kept. A reading lower than the car's last one is refused.

The car can also be given the sensor of its battery level (%): it is shown on the dashboard while the car is
plugged in to a charger whose own API doesn't know it.
"""

from __future__ import annotations

import logging
from typing import Any
from urllib.parse import quote

import aiohttp
from sqlalchemy import select
from sqlalchemy.orm import Session

from . import i18n
from .i18n import msg
from .models import Car, ChargeSession
from .secretbox import SecretError, decrypt_json

logger = logging.getLogger(__name__)
TIMEOUT = aiohttp.ClientTimeout(total=15)
KM_PER_MILE = 1.609344


class OdometerError(Exception):
    pass


def settings(car: Car) -> dict[str, Any] | None:
    """The car's odometer source ({url, token, entity}), None when it has none."""
    if not car.odometer_secret:
        return None
    try:
        return decrypt_json(car.odometer_secret)
    except SecretError:
        return None


def clean(url: str, token: str, entity: str, battery_entity: str = "") -> dict[str, str]:
    url, token, entity, battery_entity = url.strip().rstrip("/"), token.strip(), entity.strip(), battery_entity.strip()
    if url.endswith("/api"):
        url = url[:-4]
    if not url.startswith(("http://", "https://")):
        raise OdometerError(msg("The Home Assistant URL must start with http:// or https://"))
    if not token or not (entity or battery_entity):
        raise OdometerError(msg("Enter the access token and at least one sensor"))
    return {"url": url, "token": token, "entity": entity, "battery_entity": battery_entity}


async def _number(config: dict[str, Any], entity: str) -> tuple[float, str | None, str]:
    """The value of a sensor of Home Assistant, with its unit and its raw state."""
    url = config["url"]
    headers = {"Authorization": f"Bearer {config['token']}", "Accept": "application/json"}
    try:
        async with aiohttp.ClientSession(timeout=TIMEOUT, trust_env=True) as http, http.get(f"{url}/api/states/{quote(entity)}", headers=headers) as resp:
            if resp.status == 401:
                raise OdometerError(msg("Home Assistant refused the access token"))
            if resp.status == 404:
                raise OdometerError(msg("Home Assistant has no entity {entity}", entity=entity))
            if resp.status != 200:
                raise OdometerError(msg("Home Assistant: /api/{path} answered HTTP {status}", path=f"states/{entity}", status=resp.status))
            body = await resp.json(content_type=None)
    except aiohttp.ClientError as exc:
        raise OdometerError(msg("Cannot reach Home Assistant at {url} ({error})", url=url, error=str(exc) or type(exc).__name__)) from exc
    except TimeoutError as exc:
        raise OdometerError(msg("Home Assistant at {url} did not answer in time", url=url)) from exc
    except ValueError as exc:
        raise OdometerError(msg("Home Assistant: /api/{path} answered HTTP {status}", path=f"states/{entity}", status="200, not JSON")) from exc
    state = body.get("state") if isinstance(body, dict) else None
    try:
        value = float(str(state).replace(",", "."))
    except ValueError:
        raise OdometerError(msg("{entity} has no value for now ({state})", entity=entity, state=str(state))) from None
    return value, (body.get("attributes") or {}).get("unit_of_measurement"), str(state)


async def read(config: dict[str, Any]) -> int:
    """The odometer in km, as Home Assistant has it now."""
    entity = config.get("entity") or ""
    if not entity:
        raise OdometerError(msg("The odometer of this car isn't read from Home Assistant"))
    value, unit, state = await _number(config, entity)
    if unit not in (None, "km", "mi"):
        raise OdometerError(msg("{entity} is in {unit}, not a distance (km or mi)", entity=entity, unit=str(unit)))
    km = round(value * KM_PER_MILE) if unit == "mi" else round(value)
    if km < 0:
        raise OdometerError(msg("{entity} has no value for now ({state})", entity=entity, state=state))
    return km


async def read_battery(config: dict[str, Any]) -> int:
    """The battery level of the car in %, as Home Assistant has it now."""
    entity = config.get("battery_entity") or ""
    value, unit, state = await _number(config, entity)
    if unit not in (None, "%"):
        raise OdometerError(msg("{entity} is in {unit}, not a battery level (%)", entity=entity, unit=str(unit)))
    if not 0 <= value <= 100:
        raise OdometerError(msg("{entity} has no value for now ({state})", entity=entity, state=state))
    return round(value)


async def battery_of(car: Car | None) -> int | None:
    """The battery level of a car that has a sensor for it; None when it has none, or when it can't be read now."""
    config = settings(car) if car else None
    if not config or not config.get("battery_entity"):
        return None
    try:
        return await read_battery(config)
    except OdometerError as exc:
        logger.info("Car %s: its battery level couldn't be read (%s)", car.name, exc)  # type: ignore[union-attr]
        return None


def last_reading(db: Session, car_id: int, but: int | None = None) -> int | None:
    """The highest odometer written on the car's sessions (but one)."""
    rows = db.scalars(select(ChargeSession.odometer).where(ChargeSession.car_id == car_id, ChargeSession.odometer.is_not(None), ChargeSession.id != (but or -1)))
    return max(rows, default=None)


async def fill(db: Session, s: ChargeSession, written: int | None = None) -> bool:
    """Write the car's odometer on a session that has none, or that has the figure we wrote before (`written`) when
    Home Assistant now has a higher one. The error, if any, is kept on the car. Returns True when written."""
    car = db.get(Car, s.car_id)
    config = settings(car) if car else None
    if car is None or config is None or not config.get("entity") or s.odometer != written:  # no source, or an odometer entered by hand
        return False
    try:
        km = await read(config)
        last = last_reading(db, car.id, s.id)
        if last is not None and km < last:
            raise OdometerError(msg("Home Assistant says {km} km, less than the last reading of the car ({last} km): not written", km=km, last=last))
    except OdometerError as exc:
        car.odometer_error = i18n.store(str(exc))
        logger.info("Car %s: its odometer couldn't be read (%s)", car.name, exc)
        return False
    car.odometer_error = None
    if written is not None and km <= written:
        return False
    s.odometer = km
    logger.info("Car %s: odometer %s km written on its session of %s", car.name, km, s.charge_date)
    return True
