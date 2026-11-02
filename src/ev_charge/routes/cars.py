"""Cars API."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Response
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..auth import check_car_access, current_user, require_admin
from ..database import get_db
from ..models import Car, Charger, ChargeSession, User
from ..schemas import CarIn, CarOut
from ..services import car_out

router = APIRouter(prefix="/api/cars", tags=["cars"])


def _get(db: Session, car_id: int) -> Car:
    car = db.get(Car, car_id)
    if car is None:
        raise HTTPException(404, "Car not found")
    return car


def _save(db: Session, car: Car) -> None:
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, f"A car named '{car.name}' already exists") from None


@router.get("", response_model=list[CarOut])
def list_cars(active_only: bool = False, user: User = Depends(current_user), db: Session = Depends(get_db)) -> list[CarOut]:
    query = select(Car).order_by(Car.active.desc(), Car.name)
    if not user.is_admin:
        query = query.where(Car.id.in_([c.id for c in user.cars]))
    if active_only:
        query = query.where(Car.active.is_(True))
    return [car_out(db, c) for c in db.scalars(query)]


@router.get("/{car_id}", response_model=CarOut)
def get_car(car_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)) -> CarOut:
    car = _get(db, car_id)
    check_car_access(user, car_id)
    return car_out(db, car)


@router.post("", response_model=CarOut, status_code=201)
def create_car(body: CarIn, _: User = Depends(require_admin), db: Session = Depends(get_db)) -> CarOut:
    car = Car(**body.model_dump())
    db.add(car)
    _save(db, car)
    db.refresh(car)
    return car_out(db, car)


@router.put("/{car_id}", response_model=CarOut)
def update_car(car_id: int, body: CarIn, _: User = Depends(require_admin), db: Session = Depends(get_db)) -> CarOut:
    car = _get(db, car_id)
    for key, value in body.model_dump().items():
        setattr(car, key, value)
    _save(db, car)
    db.refresh(car)
    return car_out(db, car)


@router.delete("/{car_id}", status_code=204)
def delete_car(car_id: int, _: User = Depends(require_admin), db: Session = Depends(get_db)) -> Response:
    car = _get(db, car_id)
    count = db.scalar(select(func.count()).select_from(ChargeSession).where(ChargeSession.car_id == car_id)) or 0
    if count:
        raise HTTPException(409, f"'{car.name}' has {count} charging session(s): delete them first, or mark the car inactive")
    for charger in db.scalars(select(Charger).where(Charger.default_car_id == car_id)):
        charger.default_car_id = None
    db.delete(car)
    db.commit()
    return Response(status_code=204)
