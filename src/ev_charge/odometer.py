"""The odometer of a car, read from Home Assistant.

Home Assistant has an integration for most makes, and shows the car's mileage as a sensor. A car can be given
that sensor (Home Assistant's address, an access token and the entity): its odometer is then written on the
sessions recorded from a charger, when the charge starts and again when it ends if the first reading failed,
and can be fetched in the session form. A reading is refused when it is lower than the car's last one.
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


def clean(url: str, token: str, entity: str) -> dict[str, str]:
    url, token, entity = url.strip().rstrip("/"), token.strip(), entity.strip()
    if url.endswith("/api"):
        url = url[:-4]
    if not url.startswith(("http://", "https://")):
        raise OdometerError(msg("The Home Assistant URL must start with http:// or https://"))
    if not token or not entity:
        raise OdometerError(msg("Enter the access token and the odometer sensor"))
    return {"url": url, "token": token, "entity": entity}


async def read(config: dict[str, Any]) -> int:
    """The odometer in km, as Home Assistant has it now."""
    url, entity = config["url"], config["entity"]
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
    unit = (body.get("attributes") or {}).get("unit_of_measurement")
    if unit not in (None, "km", "mi"):
        raise OdometerError(msg("{entity} is in {unit}, not a distance (km or mi)", entity=entity, unit=str(unit)))
    km = round(value * KM_PER_MILE) if unit == "mi" else round(value)
    if km < 0:
        raise OdometerError(msg("{entity} has no value for now ({state})", entity=entity, state=str(state)))
    return km


def last_reading(db: Session, car_id: int, but: int | None = None) -> int | None:
    """The highest odometer written on the car's sessions (but one)."""
    rows = db.scalars(select(ChargeSession.odometer).where(ChargeSession.car_id == car_id, ChargeSession.odometer.is_not(None), ChargeSession.id != (but or -1)))
    return max(rows, default=None)


async def fill(db: Session, s: ChargeSession) -> bool:
    """Write the car's odometer on a session that has none. The error, if any, is kept on the car. Returns True when written."""
    car = db.get(Car, s.car_id)
    config = settings(car) if car else None
    if car is None or config is None or s.odometer is not None:
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
    s.odometer, car.odometer_error = km, None
    logger.info("Car %s: odometer %s km written on its session of %s", car.name, km, s.charge_date)
    return True
