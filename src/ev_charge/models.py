"""ORM models: cars and their charging sessions."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, time

from sqlalchemy import Boolean, Date, DateTime, Float, ForeignKey, Integer, String, Text, Time, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .database import Base


def _now() -> datetime:
    return datetime.now(UTC)


class Role(Base):
    """A named set of permissions (see permissions.py). As a user's global role it gives its app-wide
    permissions, and its car permissions on every car when all_cars is set; given on one car
    (CarGrant), only its car permissions apply, on that car."""

    __tablename__ = "roles"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(50), unique=True)
    description: Mapped[str | None] = mapped_column(String(300))
    permissions: Mapped[str] = mapped_column(Text, default="[]")  # JSON list, wildcards "*" and "sessions:*"
    all_cars: Mapped[bool] = mapped_column(Boolean, default=False)
    builtin: Mapped[bool] = mapped_column(Boolean, default=False)  # created by the app (can't be deleted)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    @property
    def permission_set(self) -> set[str]:
        try:
            return set(json.loads(self.permissions or "[]"))
        except ValueError:
            return set()


class CarGrant(Base):
    """A user's role on one car (table kept from the first version: user_cars)."""

    __tablename__ = "user_cars"

    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    car_id: Mapped[int] = mapped_column(ForeignKey("cars.id", ondelete="CASCADE"), primary_key=True)
    role_id: Mapped[int | None] = mapped_column(ForeignKey("roles.id", ondelete="CASCADE"))

    user: Mapped[User] = relationship(back_populates="grants")
    car: Mapped[Car] = relationship(back_populates="grants")
    role: Mapped[Role | None] = relationship()


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
    grants: Mapped[list[CarGrant]] = relationship(back_populates="car", cascade="all, delete-orphan")


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


class ReimbursementDocument(Base):
    """A reimbursement document (credit note / statement) received from a reimbursing company: its sessions
    are matched with ours (reconcile.py), which become "invoiced", then "paid" once the payment is received."""

    __tablename__ = "reimbursement_documents"
    __table_args__ = (UniqueConstraint("source", "number", name="uq_reimbursement_documents_source_number"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source: Mapped[str] = mapped_column(String(30))  # file format: "eneco" (CSV) | "lms" (Last Mile Solutions PDF)
    number: Mapped[str] = mapped_column(String(100))
    issuer: Mapped[str | None] = mapped_column(String(200))
    document_date: Mapped[date | None] = mapped_column(Date)
    due_date: Mapped[date | None] = mapped_column(Date)
    period: Mapped[str | None] = mapped_column(String(20))  # "2026-10" when the document covers one month
    amount: Mapped[float] = mapped_column(Float)  # total to receive
    kwh: Mapped[float] = mapped_column(Float)
    filename: Mapped[str | None] = mapped_column(String(255))
    charger_id: Mapped[int | None] = mapped_column(ForeignKey("chargers.id", ondelete="SET NULL"))
    # Payment: received when paid_date is set
    paid_date: Mapped[date | None] = mapped_column(Date)
    paid_amount: Mapped[float | None] = mapped_column(Float)
    notes: Mapped[str | None] = mapped_column(Text)
    created_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    lines: Mapped[list[ReimbursementLine]] = relationship(back_populates="document", cascade="all, delete-orphan", order_by="ReimbursementLine.position")
    charger: Mapped[Charger | None] = relationship()


class ReimbursementLine(Base):
    """One session of a reimbursement document, linked to our session when it was matched."""

    __tablename__ = "reimbursement_lines"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    document_id: Mapped[int] = mapped_column(ForeignKey("reimbursement_documents.id", ondelete="CASCADE"), index=True)
    position: Mapped[int] = mapped_column(Integer)
    external_id: Mapped[str | None] = mapped_column(String(100))
    started_at: Mapped[datetime] = mapped_column(DateTime)  # as written in the document (no time zone)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime)
    kwh: Mapped[float] = mapped_column(Float)
    amount: Mapped[float] = mapped_column(Float)
    session_id: Mapped[int | None] = mapped_column(ForeignKey("charge_sessions.id", ondelete="SET NULL"), unique=True)  # a session is on one document

    document: Mapped[ReimbursementDocument] = relationship(back_populates="lines")
    session: Mapped[ChargeSession | None] = relationship()


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
    default_provider: Mapped[str | None] = mapped_column(String(200))  # e.g. the home electricity supplier
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
    # The old "role" column (admin | driver) is kept in the table but no longer used: see role_id
    role_id: Mapped[int | None] = mapped_column(ForeignKey("roles.id", ondelete="SET NULL"))
    password_hash: Mapped[str] = mapped_column(String(300))  # "!" = no local password (single sign-on only)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    last_login: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Single sign-on (OpenID Connect): the account at the identity provider
    oidc_issuer: Mapped[str | None] = mapped_column(String(300))
    oidc_subject: Mapped[str | None] = mapped_column(String(300), index=True)

    role: Mapped[Role | None] = relationship()
    grants: Mapped[list[CarGrant]] = relationship(back_populates="user", cascade="all, delete-orphan")

    @property
    def has_password(self) -> bool:
        return bool(self.password_hash) and self.password_hash != "!"


class AuthSession(Base):
    """A signed-in browser. The cookie holds a random token; only its SHA-256 is stored."""

    __tablename__ = "auth_sessions"

    token_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ip: Mapped[str | None] = mapped_column(String(64))
    user_agent: Mapped[str | None] = mapped_column(String(300))
    method: Mapped[str | None] = mapped_column(String(20))  # password | sso

    user: Mapped[User] = relationship()


class ApiToken(Base):
    """An API token (Authorization: Bearer evc_…), managed in Settings → API tokens. It has its own access:
    a role, or chosen permissions, on all cars or on chosen cars. user_id is who created it (it stops working
    when that user is disabled; sessions it creates are entered in their name)."""

    __tablename__ = "api_tokens"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(100))
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    prefix: Mapped[str] = mapped_column(String(16))  # shown to recognise the token
    description: Mapped[str | None] = mapped_column(String(300))
    role_id: Mapped[int | None] = mapped_column(ForeignKey("roles.id", ondelete="SET NULL"))  # access of a role…
    permissions: Mapped[str] = mapped_column(Text, default="[]")  # …or these permissions (JSON list), when no role
    car_ids: Mapped[str | None] = mapped_column(Text)  # JSON list of the cars it may access; NULL: all cars
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked: Mapped[bool] = mapped_column(Boolean, default=False)

    user: Mapped[User] = relationship()
    role: Mapped[Role | None] = relationship()

    @property
    def permission_set(self) -> set[str]:
        if self.role is not None:
            return self.role.permission_set
        try:
            return set(json.loads(self.permissions or "[]"))
        except ValueError:
            return set()

    @property
    def car_list(self) -> list[int] | None:
        try:
            return None if self.car_ids is None else [int(c) for c in json.loads(self.car_ids)]
        except ValueError:
            return []


class AuditEvent(Base):
    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, index=True)
    user_id: Mapped[int | None] = mapped_column(Integer, index=True)  # no foreign key: the log outlives users
    username: Mapped[str | None] = mapped_column(String(100))
    action: Mapped[str] = mapped_column(String(50), index=True)
    target: Mapped[str | None] = mapped_column(String(200))  # e.g. "user:3", "car:1"
    details: Mapped[str | None] = mapped_column(Text)  # JSON
    ip: Mapped[str | None] = mapped_column(String(64))


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
