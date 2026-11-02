"""Pydantic request/response models."""

from __future__ import annotations

from datetime import date, time
from typing import Any

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
    duration_minutes: int | None = None
    avg_power_kw: float | None = None
    price_per_kwh: float | None = None
    km_since_previous: int | None = None  # negative: odometer lower than the previous session (typo?)


class SessionPatch(BaseModel):
    """Bulk update of the reimbursement flag."""

    ids: list[int] = Field(min_length=1)
    to_reimburse: bool
