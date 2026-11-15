"""Cars API."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Response
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .. import odometer
from ..auth import Principal, check_car_access, check_group_manager, current_user, target_group
from ..database import get_db
from ..i18n import msg
from ..models import Car, Charger, ChargeSession
from ..schemas import CarIn, CarOut, OdometerSource
from ..secretbox import SecretError, decrypt_json, encrypt_json
from ..services import car_out

router = APIRouter(prefix="/api/cars", tags=["cars"])


def _out(db: Session, car: Car, user: Principal) -> CarOut:
    out = car_out(db, car, language=user.language)
    if not user.can("car:edit", car.id):  # where the odometer is read from: for who may change it
        out.odometer_source = {} if out.odometer_source else None
        out.odometer_error = None
    return out


def _get(db: Session, car_id: int) -> Car:
    car = db.get(Car, car_id)
    if car is None:
        raise HTTPException(404, msg("Car not found"))
    return car


def _save(db: Session, car: Car) -> None:
    """Names are unique within a group (two groups may each have their "Tesla")."""
    same_group = Car.group_id == car.group_id if car.group_id is not None else Car.group_id.is_(None)
    if db.scalar(select(Car.id).where(same_group, func.lower(Car.name) == car.name.lower(), Car.id != (car.id or -1))):
        db.rollback()
        raise HTTPException(409, msg("A car named '{name}' already exists", name=car.name))
    db.commit()


@router.get("", response_model=list[CarOut])
def list_cars(active_only: bool = False, user: Principal = Depends(current_user), db: Session = Depends(get_db)) -> list[CarOut]:
    query = select(Car).order_by(Car.active.desc(), Car.name)
    scope = user.cars_with("car:view")
    if scope is not None:
        query = query.where(Car.id.in_(scope))
    if active_only:
        query = query.where(Car.active.is_(True))
    return [_out(db, c, user) for c in db.scalars(query)]


@router.get("/{car_id}", response_model=CarOut)
def get_car(car_id: int, user: Principal = Depends(current_user), db: Session = Depends(get_db)) -> CarOut:
    car = _get(db, car_id)
    check_car_access(user, car_id)
    return _out(db, car, user)


@router.post("", response_model=CarOut, status_code=201)
def create_car(body: CarIn, user: Principal = Depends(current_user), db: Session = Depends(get_db)) -> CarOut:
    """Administrators add cars to any group; a group manager adds cars to their group."""
    car = Car(**body.model_dump(exclude={"group_id"}), group_id=target_group(db, user, body.group_id, "cars:manage"))
    db.add(car)
    _save(db, car)
    db.refresh(car)
    return _out(db, car, user)


@router.put("/{car_id}", response_model=CarOut)
def update_car(car_id: int, body: CarIn, user: Principal = Depends(current_user), db: Session = Depends(get_db)) -> CarOut:
    car = _get(db, car_id)
    check_car_access(user, car_id, "car:edit")
    if body.group_id is not None and body.group_id != car.group_id:  # moving a car to another group: administrators
        if not user.has("cars:manage"):
            raise HTTPException(403, msg("Only an administrator can move a car to another group"))
        car.group_id = target_group(db, user, body.group_id, "cars:manage")
    for key, value in body.model_dump(exclude={"group_id"}).items():
        setattr(car, key, value)
    _save(db, car)
    db.refresh(car)
    return _out(db, car, user)


@router.delete("/{car_id}", status_code=204)
def delete_car(car_id: int, user: Principal = Depends(current_user), db: Session = Depends(get_db)) -> Response:
    car = _get(db, car_id)
    if not user.can("car:view", car_id):
        raise HTTPException(404, msg("Car not found"))
    check_group_manager(user, car.group_id, "cars:manage")
    count = db.scalar(select(func.count()).select_from(ChargeSession).where(ChargeSession.car_id == car_id)) or 0
    if count:
        raise HTTPException(409, msg("'{name}' has {count} charging session(s): delete them first, or mark the car inactive", name=car.name, count=count))
    for charger in db.scalars(select(Charger).where(Charger.default_car_id == car_id)):
        charger.default_car_id = None
    db.delete(car)
    db.commit()
    return Response(status_code=204)


# ── Odometer read from Home Assistant ────────────────────────────────────────


def _home_assistant_of_group(db: Session, group_id: int | None) -> dict[str, str] | None:
    """The address and token of Home Assistant, from a charger of the group that is connected to it."""
    for c in db.scalars(select(Charger).where(Charger.group_id == group_id).order_by(Charger.id)):
        for kind, secret in ((c.connector, c.connector_secret), (c.connector2, c.connector2_secret)):
            if kind == "homeassistant":
                try:
                    config = decrypt_json(secret)
                except SecretError:
                    continue
                if config.get("url") and config.get("token"):
                    return {"url": config["url"], "token": config["token"]}
    return None


@router.put("/{car_id}/odometer-source")
async def set_odometer_source(car_id: int, body: OdometerSource, user: Principal = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    """Read the car's odometer and/or its battery level from Home Assistant sensors: tested now, the token is stored encrypted.

    Without address and token, those of a charger of the group connected to Home Assistant are used."""
    car = _get(db, car_id)
    check_car_access(user, car_id, "car:edit")
    stored = odometer.settings(car) or {}
    url, token = body.url.strip(), body.token.strip()
    if not url and not token and not stored:
        shared = _home_assistant_of_group(db, car.group_id)
        if shared is None:
            raise HTTPException(422, msg("Enter the address of Home Assistant and an access token (no charger of the group is connected to Home Assistant)"))
        url, token = shared["url"], shared["token"]
    try:
        config = odometer.clean(url or stored.get("url", ""), token or stored.get("token", ""), body.entity, body.battery_entity)
        km = await odometer.read(config) if config["entity"] else None
        battery = await odometer.read_battery(config) if config["battery_entity"] else None
    except odometer.OdometerError as e:
        raise HTTPException(422, str(e)) from None
    car.odometer_secret, car.odometer_error = encrypt_json(config), None
    db.commit()
    return {"odometer": km, "battery_pct": battery, "car": _out(db, car, user)}


@router.delete("/{car_id}/odometer-source", response_model=CarOut)
def remove_odometer_source(car_id: int, user: Principal = Depends(current_user), db: Session = Depends(get_db)) -> CarOut:
    car = _get(db, car_id)
    check_car_access(user, car_id, "car:edit")
    car.odometer_secret = car.odometer_error = None
    db.commit()
    return _out(db, car, user)


@router.get("/{car_id}/odometer")
async def read_odometer(car_id: int, user: Principal = Depends(current_user), db: Session = Depends(get_db)) -> dict[str, int]:
    """The car's odometer now, from Home Assistant (for the session form)."""
    car = _get(db, car_id)
    check_car_access(user, car_id)
    config = odometer.settings(car)
    if config is None or not config.get("entity"):
        raise HTTPException(422, msg("The odometer of this car isn't read from Home Assistant"))
    try:
        return {"odometer": await odometer.read(config)}
    except odometer.OdometerError as e:
        raise HTTPException(502, str(e)) from None
