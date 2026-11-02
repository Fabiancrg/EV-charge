"""Pydantic request/response models."""

from __future__ import annotations

from datetime import date, datetime, time
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


def _blank_to_none(value: Any) -> Any:
    if isinstance(value, str):
        value = value.strip()
        return value or None
    return value


class CarIn(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    make: str | None = Field(default=None, max_length=100)
    model: str | None = Field(default=None, max_length=100)
    year: int | None = Field(default=None, ge=1990, le=2100)
    license_plate: str | None = Field(default=None, max_length=30)
    vin: str | None = Field(default=None, max_length=30)
    color: str | None = Field(default=None, max_length=50)
    battery_kwh: float | None = Field(default=None, gt=0, le=500)
    max_ac_kw: float | None = Field(default=None, gt=0, le=100)
    max_dc_kw: float | None = Field(default=None, gt=0, le=1000)
    wltp_range_km: int | None = Field(default=None, gt=0, le=3000)
    wltp_consumption: float | None = Field(default=None, gt=0, le=100)
    lease_company: str | None = Field(default=None, max_length=100)
    lease_contract: str | None = Field(default=None, max_length=100)
    lease_start: date | None = None
    lease_end: date | None = None
    lease_km_per_year: int | None = Field(default=None, gt=0, le=500_000)
    lease_monthly_cost: float | None = Field(default=None, ge=0)
    initial_odometer: int | None = Field(default=None, ge=0)
    notes: str | None = None
    active: bool = True

    _strip = field_validator("*", mode="before")(_blank_to_none)

    @field_validator("name", mode="after")
    @classmethod
    def _name_required(cls, value: str | None) -> str:
        if not value:
            raise ValueError("A name is required")
        return value

    @model_validator(mode="after")
    def _lease_dates(self) -> CarIn:
        if self.lease_start and self.lease_end and self.lease_end <= self.lease_start:
            raise ValueError("The lease must end after it starts")
        return self


class LeaseStatus(BaseModel):
    days_total: int
    days_elapsed: int
    days_left: int
    km_allowed_total: int | None = None
    km_allowed_to_date: int | None = None
    km_driven: int | None = None
    km_projected_end: int | None = None  # at the current pace


class CarOut(CarIn):
    model_config = ConfigDict(from_attributes=True)

    id: int
    color_index: int = 0  # position among all cars (by id): a car keeps its colour for every user
    drivers: list[str] = []
    session_count: int = 0
    total_kwh: float = 0
    last_odometer: int | None = None
    last_charge_date: date | None = None
    lease: LeaseStatus | None = None


class SessionIn(BaseModel):
    car_id: int
    charge_date: date
    odometer: int | None = Field(default=None, ge=0, le=5_000_000)
    location: str | None = Field(default=None, max_length=200)
    provider: str | None = Field(default=None, max_length=200)
    kwh: float = Field(gt=0, le=1000)
    start_time: time | None = None
    end_time: time | None = None
    to_reimburse: bool = False
    cost: float | None = Field(default=None, ge=0)
    notes: str | None = None

    _strip = field_validator("odometer", "location", "provider", "start_time", "end_time", "cost", "notes", mode="before")(_blank_to_none)


class SessionOut(SessionIn):
    model_config = ConfigDict(from_attributes=True)

    id: int
    car_name: str
    # cost (inherited) is the effective cost: the home tariff for home charging, else the cost entered
    entered_cost: float | None = None  # the cost typed in (ignored for home charging)
    cost_source: str | None = None  # "tariff" | "entered" | None
    home: bool = False  # charged at a home location → priced at the home tariff
    tariff_price: float | None = None
    missing_tariff: bool = False  # home charging on a date no tariff covers
    duration_minutes: int | None = None
    avg_power_kw: float | None = None
    price_per_kwh: float | None = None
    km_since_previous: int | None = None  # negative: odometer lower than the previous session (typo?)
    created_by: str | None = None


class SessionPatch(BaseModel):
    """Bulk update of the reimbursement flag."""

    ids: list[int] = Field(min_length=1)
    to_reimburse: bool


Role = Literal["admin", "driver"]


class UserIn(BaseModel):
    username: str = Field(min_length=2, max_length=50, pattern=r"^[A-Za-z0-9._@-]+$")
    full_name: str | None = Field(default=None, max_length=100)
    email: str | None = Field(default=None, max_length=200)
    role: Role = "driver"
    active: bool = True
    car_ids: list[int] = []
    password: str | None = Field(default=None, max_length=200)  # required on creation; empty on update: unchanged

    _strip = field_validator("full_name", "email", "password", mode="before")(_blank_to_none)

    @field_validator("username", mode="after")
    @classmethod
    def _lower(cls, value: str) -> str:
        return value.strip().lower()


class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    username: str
    full_name: str | None
    email: str | None
    role: Role
    active: bool
    car_ids: list[int] = []
    created_at: datetime
    last_login: datetime | None


class Me(UserOut):
    display_name: str


class LoginIn(BaseModel):
    username: str = Field(max_length=50)
    password: str = Field(max_length=200)


class SetupIn(BaseModel):
    username: str = Field(min_length=2, max_length=50, pattern=r"^[A-Za-z0-9._@-]+$")
    full_name: str | None = Field(default=None, max_length=100)
    password: str = Field(max_length=200)


class PasswordChange(BaseModel):
    current_password: str = Field(max_length=200)
    new_password: str = Field(max_length=200)


class TariffIn(BaseModel):
    valid_from: date
    valid_to: date | None = None
    price_per_kwh: float = Field(gt=0, le=10)
    label: str | None = Field(default=None, max_length=100)
    notes: str | None = None

    _strip = field_validator("valid_to", "label", "notes", mode="before")(_blank_to_none)

    @model_validator(mode="after")
    def _period(self) -> TariffIn:
        if self.valid_to and self.valid_to < self.valid_from:
            raise ValueError("The period must end on or after its start")
        return self


class TariffOut(TariffIn):
    model_config = ConfigDict(from_attributes=True)

    id: int
    sessions: int = 0  # home sessions priced with this tariff
    kwh: float = 0


class HomeLocations(BaseModel):
    locations: list[str] = Field(max_length=50)


CregRegion = Literal["wallonia", "brussels", "flanders"]


class CregItem(BaseModel):
    key: str = Field(pattern=r"^20\d{2}-Q[1-4]$")
    valid_from: date
    valid_to: date
    price_per_kwh: float = Field(gt=0, le=10)


class CregApply(BaseModel):
    region: CregRegion
    items: list[CregItem] = Field(min_length=1, max_length=200)
    source: str | None = Field(default=None, max_length=500)
