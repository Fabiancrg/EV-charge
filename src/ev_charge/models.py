"""ORM models: cars and their charging sessions."""

from __future__ import annotations

from datetime import UTC, date, datetime, time

from sqlalchemy import Boolean, Date, DateTime, Float, ForeignKey, Integer, String, Text, Time
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .database import Base


def _now() -> datetime:
    return datetime.now(UTC)


class Car(Base):
    __tablename__ = "cars"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(100), unique=True)  # nickname shown everywhere
    make: Mapped[str | None] = mapped_column(String(100))
    model: Mapped[str | None] = mapped_column(String(100))
    year: Mapped[int | None] = mapped_column(Integer)
    license_plate: Mapped[str | None] = mapped_column(String(30))
    vin: Mapped[str | None] = mapped_column(String(30))
    color: Mapped[str | None] = mapped_column(String(50))
    # Specs
    battery_kwh: Mapped[float | None] = mapped_column(Float)  # usable capacity
    max_ac_kw: Mapped[float | None] = mapped_column(Float)
    max_dc_kw: Mapped[float | None] = mapped_column(Float)
    wltp_range_km: Mapped[int | None] = mapped_column(Integer)
    wltp_consumption: Mapped[float | None] = mapped_column(Float)  # kWh/100 km
    # Leasing
    lease_company: Mapped[str | None] = mapped_column(String(100))
    lease_contract: Mapped[str | None] = mapped_column(String(100))
    lease_start: Mapped[date | None] = mapped_column(Date)
    lease_end: Mapped[date | None] = mapped_column(Date)
    lease_km_per_year: Mapped[int | None] = mapped_column(Integer)
    lease_monthly_cost: Mapped[float | None] = mapped_column(Float)
    initial_odometer: Mapped[int | None] = mapped_column(Integer)  # km at delivery / lease start
    notes: Mapped[str | None] = mapped_column(Text)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now)

    sessions: Mapped[list[ChargeSession]] = relationship(back_populates="car")


class ChargeSession(Base):
    __tablename__ = "charge_sessions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    car_id: Mapped[int] = mapped_column(ForeignKey("cars.id", ondelete="RESTRICT"), index=True)
    charge_date: Mapped[date] = mapped_column(Date, index=True)
    odometer: Mapped[int | None] = mapped_column(Integer)  # km
    location: Mapped[str | None] = mapped_column(String(200))
    provider: Mapped[str | None] = mapped_column(String(200))
    kwh: Mapped[float] = mapped_column(Float)
    start_time: Mapped[time | None] = mapped_column(Time)
    end_time: Mapped[time | None] = mapped_column(Time)  # earlier than start_time: ended the next day
    to_reimburse: Mapped[bool] = mapped_column(Boolean, default=False)
    cost: Mapped[float | None] = mapped_column(Float)
    notes: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now)

    car: Mapped[Car] = relationship(back_populates="sessions")
