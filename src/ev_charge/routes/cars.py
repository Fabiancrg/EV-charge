"""Cars API."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Response
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..auth import Principal, check_car_access, check_group_manager, current_user, target_group
from ..database import get_db
from ..models import Car, Charger, ChargeSession
from ..schemas import CarIn, CarOut
from ..services import car_out

router = APIRouter(prefix="/api/cars", tags=["cars"])


def _get(db: Session, car_id: int) -> Car:
    car = db.get(Car, car_id)
    if car is None:
        raise HTTPException(404, "Car not found")
    return car


def _save(db: Session, car: Car) -> None:
    """Names are unique within a group (two groups may each have their "Tesla")."""
    same_group = Car.group_id == car.group_id if car.group_id is not None else Car.group_id.is_(None)
    if db.scalar(select(Car.id).where(same_group, func.lower(Car.name) == car.name.lower(), Car.id != (car.id or -1))):
        db.rollback()
        raise HTTPException(409, f"A car named '{car.name}' already exists")
    db.commit()


@router.get("", response_model=list[CarOut])
def list_cars(active_only: bool = False, user: Principal = Depends(current_user), db: Session = Depends(get_db)) -> list[CarOut]:
    query = select(Car).order_by(Car.active.desc(), Car.name)
    scope = user.cars_with("car:view")
    if scope is not None:
        query = query.where(Car.id.in_(scope))
    if active_only:
        query = query.where(Car.active.is_(True))
    return [car_out(db, c) for c in db.scalars(query)]


@router.get("/{car_id}", response_model=CarOut)
def get_car(car_id: int, user: Principal = Depends(current_user), db: Session = Depends(get_db)) -> CarOut:
    car = _get(db, car_id)
    check_car_access(user, car_id)
    return car_out(db, car)


@router.post("", response_model=CarOut, status_code=201)
def create_car(body: CarIn, user: Principal = Depends(current_user), db: Session = Depends(get_db)) -> CarOut:
    """Administrators add cars to any group; a group manager adds cars to their group."""
    car = Car(**body.model_dump(exclude={"group_id"}), group_id=target_group(db, user, body.group_id, "cars:manage"))
    db.add(car)
    _save(db, car)
    db.refresh(car)
    return car_out(db, car)


@router.put("/{car_id}", response_model=CarOut)
def update_car(car_id: int, body: CarIn, user: Principal = Depends(current_user), db: Session = Depends(get_db)) -> CarOut:
    car = _get(db, car_id)
    check_car_access(user, car_id, "car:edit")
    if body.group_id is not None and body.group_id != car.group_id:  # moving a car to another group: administrators
        if not user.has("cars:manage"):
            raise HTTPException(403, "Only an administrator can move a car to another group")
        car.group_id = target_group(db, user, body.group_id, "cars:manage")
    for key, value in body.model_dump(exclude={"group_id"}).items():
        setattr(car, key, value)
    _save(db, car)
    db.refresh(car)
    return car_out(db, car)


@router.delete("/{car_id}", status_code=204)
def delete_car(car_id: int, user: Principal = Depends(current_user), db: Session = Depends(get_db)) -> Response:
    car = _get(db, car_id)
    if not user.can("car:view", car_id):
        raise HTTPException(404, "Car not found")
    check_group_manager(user, car.group_id, "cars:manage")
    count = db.scalar(select(func.count()).select_from(ChargeSession).where(ChargeSession.car_id == car_id)) or 0
    if count:
        raise HTTPException(409, f"'{car.name}' has {count} charging session(s): delete them first, or mark the car inactive")
    for charger in db.scalars(select(Charger).where(Charger.default_car_id == car_id)):
        charger.default_car_id = None
    db.delete(car)
    db.commit()
    return Response(status_code=204)
