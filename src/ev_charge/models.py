"""ORM models: cars and their charging sessions."""

from __future__ import annotations

from datetime import UTC, date, datetime, time

from sqlalchemy import Boolean, Column, Date, DateTime, Float, ForeignKey, Integer, String, Table, Text, Time
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .database import Base


def _now() -> datetime:
    return datetime.now(UTC)


# Cars a driver may work with (admins see every car)
user_cars = Table(
    "user_cars",
    Base.metadata,
    Column("user_id", ForeignKey("users.id", ondelete="CASCADE"), primary_key=True),
    Column("car_id", ForeignKey("cars.id", ondelete="CASCADE"), primary_key=True),
)


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
    drivers: Mapped[list[User]] = relationship(secondary=user_cars, back_populates="cars")


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

    created_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    charger_id: Mapped[int | None] = mapped_column(ForeignKey("chargers.id", ondelete="RESTRICT"), index=True)
    # Set when the session comes from the charger's API: "live:<charger id>:<start>" or "history:<charger id>:<start ms>"
    external_ref: Mapped[str | None] = mapped_column(String(100), index=True)
    # Where the reimbursement stands (sessions to be reimbursed): in_progress | not_recorded | recorded | invoiced | paid
    # (in_progress: still being recorded, e.g. by a future automatic link with the charger)
    reimbursement_status: Mapped[str | None] = mapped_column(String(20))

    car: Mapped[Car] = relationship(back_populates="sessions")
    created_by: Mapped[User | None] = relationship()
    charger: Mapped[Charger | None] = relationship()


REIMBURSEMENT_STATUSES = ("in_progress", "not_recorded", "recorded", "invoiced", "paid")


class Charger(Base):
    """A charger whose sessions are reimbursed (home wall box, mobile charger…), and the company that pays them back."""

    __tablename__ = "chargers"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(100), unique=True)  # e.g. "Zaptec Go (garage)"
    brand: Mapped[str | None] = mapped_column(String(100))
    model: Mapped[str | None] = mapped_column(String(100))
    serial_number: Mapped[str | None] = mapped_column(String(100))
    reimbursement_company: Mapped[str | None] = mapped_column(String(100))  # e.g. "50five", "Enco e-Mobility"
    uses_home_tariff: Mapped[bool] = mapped_column(Boolean, default=True)  # sessions priced at the CREG tariff of their date
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    notes: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now)
    # Sessions created from the charger (live or history) go to this car, at this location
    default_car_id: Mapped[int | None] = mapped_column(ForeignKey("cars.id", ondelete="SET NULL"))
    default_location: Mapped[str | None] = mapped_column(String(200))
    # Connection to the charger's cloud API (connectors/): kind ("ohme"), encrypted credentials, polling
    connector: Mapped[str | None] = mapped_column(String(30))
    connector_secret: Mapped[str | None] = mapped_column(Text)  # encrypted JSON (secretbox)
    poll_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    external_id: Mapped[str | None] = mapped_column(String(100))  # serial number reported by the API
    live_state: Mapped[str | None] = mapped_column(Text)  # JSON of the last reading
    last_poll_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_poll_error: Mapped[str | None] = mapped_column(Text)


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    username: Mapped[str] = mapped_column(String(50), unique=True)  # stored lower case
    full_name: Mapped[str | None] = mapped_column(String(100))
    email: Mapped[str | None] = mapped_column(String(200))
    role: Mapped[str] = mapped_column(String(20), default="driver")  # admin | driver
    password_hash: Mapped[str] = mapped_column(String(300))
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    last_login: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    cars: Mapped[list[Car]] = relationship(secondary=user_cars, back_populates="drivers", order_by="Car.name")

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"


class AuthSession(Base):
    """A signed-in browser. The cookie holds a random token; only its SHA-256 is stored."""

    __tablename__ = "auth_sessions"

    token_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    user: Mapped[User] = relationship()


class Tariff(Base):
    """Price per kWh of home charging over a period (usually a quarter)."""

    __tablename__ = "tariffs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    valid_from: Mapped[date] = mapped_column(Date, index=True)
    valid_to: Mapped[date | None] = mapped_column(Date)  # inclusive; empty: until further notice
    price_per_kwh: Mapped[float] = mapped_column(Float)
    label: Mapped[str | None] = mapped_column(String(100))  # e.g. "CREG Q1 2026"
    notes: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now)


class Setting(Base):
    """Application settings as JSON values (e.g. creg_region)."""

    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(String(50), primary_key=True)
    value: Mapped[str] = mapped_column(Text)  # JSON
