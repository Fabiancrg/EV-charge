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
    group_id: int | None = None  # empty: your own group (administrators may choose another)

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
    group_name: str | None = None
    color_index: int = 0  # position among all cars (by id): a car keeps its colour for every user
    drivers: list[str] = []
    session_count: int = 0
    total_kwh: float = 0
    last_odometer: int | None = None
    last_charge_date: date | None = None
    lease: LeaseStatus | None = None


ReimbursementStatus = Literal["in_progress", "not_recorded", "recorded", "invoiced", "paid"]


def default_status(to_reimburse: bool, status: str | None, charger_id: int | None) -> str | None:
    """A session to be reimbursed always has a status (recorded when made with a charger, else not recorded); others have none."""
    if not to_reimburse:
        return None
    return status or ("recorded" if charger_id else "not_recorded")


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
    charger_id: int | None = None  # a charger that uses the home tariff prices the session at the CREG tariff
    reimbursement_status: ReimbursementStatus | None = None

    _strip = field_validator("odometer", "location", "provider", "start_time", "end_time", "cost", "notes", "charger_id", "reimbursement_status", mode="before")(_blank_to_none)

    @model_validator(mode="after")
    def _status(self) -> SessionIn:
        self.reimbursement_status = default_status(self.to_reimburse, self.reimbursement_status, self.charger_id)  # type: ignore[assignment]
        return self


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
    charger_name: str | None = None
    reimbursement_company: str | None = None


class SessionBulk(BaseModel):
    """Bulk update of selected sessions; only the fields given are changed."""

    ids: list[int] = Field(min_length=1, max_length=10_000)
    to_reimburse: bool | None = None
    reimbursement_status: ReimbursementStatus | None = None
    charger_id: int | None = None
    clear_charger: bool = False  # remove the charger (charger_id null means "unchanged")
    provider: str | None = Field(default=None, max_length=200)  # set this provider (null: unchanged)
    clear_provider: bool = False  # empty the provider

    _strip = field_validator("provider", mode="before")(_blank_to_none)


class CarGrantIO(BaseModel):
    car_id: int
    role_id: int


class UserIn(BaseModel):
    username: str = Field(min_length=2, max_length=50, pattern=r"^[A-Za-z0-9._@-]+$")
    full_name: str | None = Field(default=None, max_length=100)
    email: str | None = Field(default=None, max_length=200)
    role_id: int | None = None  # global role (None: no global permission, only the cars below)
    active: bool = True
    grants: list[CarGrantIO] = []  # a role on each car
    password: str | None = Field(default=None, max_length=200)  # empty on update: unchanged; may stay empty for single sign-on users
    group_id: int | None = None  # empty on creation: the group of who creates the user
    group_manager: bool = False  # manages the cars, chargers and members of the group (set by administrators)

    _strip = field_validator("full_name", "email", "password", "role_id", "group_id", mode="before")(_blank_to_none)

    @field_validator("username", mode="after")
    @classmethod
    def _lower(cls, value: str) -> str:
        return value.strip().lower()


class UserOut(BaseModel):
    id: int
    username: str
    full_name: str | None
    email: str | None
    role_id: int | None
    role_name: str | None
    active: bool
    grants: list[CarGrantIO] = []
    has_password: bool
    sso: bool  # linked to a single sign-on account
    created_at: datetime
    last_login: datetime | None
    group_id: int | None = None
    group_name: str | None = None
    group_manager: bool = False


class Me(UserOut):
    display_name: str
    permissions: list[str]  # global permissions
    all_cars: list[str]  # car permissions on every car
    car_permissions: dict[int, list[str]]  # car permissions per car
    via_token: bool = False
    manages_group: bool = False  # a group manager: adds cars, chargers and members to their group


class RoleIn(BaseModel):
    name: str = Field(min_length=1, max_length=50)
    description: str | None = Field(default=None, max_length=300)
    permissions: list[str] = Field(default=[], max_length=50)
    all_cars: bool = False

    _strip = field_validator("name", "description", mode="before")(_blank_to_none)


class RoleOut(RoleIn):
    id: int
    builtin: bool
    users: int = 0  # users with this global role
    car_grants: int = 0  # car roles given with it


class TokenIn(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    description: str | None = Field(default=None, max_length=300)
    role_id: int | None = None  # the access of this role…
    permissions: list[str] = Field(default=[], max_length=50)  # …or these permissions
    car_ids: list[int] | None = None  # None: all cars
    expires_days: int | None = Field(default=None, ge=1, le=3650)  # on creation; None: never

    _strip = field_validator("description", mode="before")(_blank_to_none)


class TokenUpdate(BaseModel):
    """Change a token's access without changing its secret."""

    name: str = Field(min_length=1, max_length=100)
    description: str | None = Field(default=None, max_length=300)
    role_id: int | None = None
    permissions: list[str] = Field(default=[], max_length=50)
    car_ids: list[int] | None = None

    _strip = field_validator("description", mode="before")(_blank_to_none)


class TokenOut(BaseModel):
    id: int
    name: str
    description: str | None
    prefix: str
    role_id: int | None
    role_name: str | None
    permissions: list[str]  # the effective permissions (the role's, or the chosen ones)
    car_ids: list[int] | None
    created_by: str
    created_at: datetime
    expires_at: datetime | None
    last_used_at: datetime | None
    revoked: bool


class TokenCreated(TokenOut):
    token: str  # shown once


class SessionInfo(BaseModel):
    id: str
    created_at: datetime
    last_seen_at: datetime | None
    expires_at: datetime
    ip: str | None
    user_agent: str | None
    method: str | None
    current: bool


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
    group_id: int | None = None  # empty: a default tariff
    sessions: int = 0  # sessions priced with this tariff (made with a charger using the home tariff)
    kwh: float = 0


class ChargerIn(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    brand: str | None = Field(default=None, max_length=100)
    model: str | None = Field(default=None, max_length=100)
    serial_number: str | None = Field(default=None, max_length=100)
    reimbursement_company: str | None = Field(default=None, max_length=100)
    uses_home_tariff: bool = True
    active: bool = True
    notes: str | None = None
    default_car_id: int | None = None  # car of the sessions created from the charger (live, history)
    default_location: str | None = Field(default=None, max_length=200)
    default_provider: str | None = Field(default=None, max_length=200)  # e.g. the home electricity supplier
    group_id: int | None = None  # empty: your own group (administrators may choose another)

    _strip = field_validator(
        "name", "brand", "model", "serial_number", "reimbursement_company", "notes", "default_car_id", "default_location", "default_provider", "group_id", mode="before"
    )(_blank_to_none)


class ChargerOut(ChargerIn):
    model_config = ConfigDict(from_attributes=True)

    id: int
    group_name: str | None = None
    manageable: bool = False  # the caller may edit and connect this charger
    sessions: int = 0
    kwh: float = 0
    last_used: date | None = None
    connector: str | None = None
    connector_account: str | None = None  # e.g. the account e-mail (administrators only); never the password
    connector_settings: dict[str, str] | None = None  # the non-secret connection fields (administrators only)
    poll_enabled: bool = False
    external_id: str | None = None
    live: dict[str, Any] | None = None
    last_poll_at: datetime | None = None
    last_poll_error: str | None = None


class ChargerConnection(BaseModel):
    connector: str | None = Field(default=None, max_length=30)  # None: disconnect
    credentials: dict[str, str] = {}  # e.g. {"email": ..., "password": ...}; an empty value keeps the stored one
    poll_enabled: bool = True


class HistoryApply(BaseModel):
    date_from: date
    date_to: date
    keys: list[str] = Field(min_length=1, max_length=5000)
    car_id: int
    link_matches: bool = True


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


class TariffMode(BaseModel):
    own_tariffs: bool  # the group's own tariffs, or the default ones
    copy_default: bool = False  # when switching to its own tariffs with none yet: start from a copy of the default ones


class GroupIn(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    max_members: int = Field(default=5, ge=1, le=1000)
    notes: str | None = None

    _strip = field_validator("name", "notes", mode="before")(_blank_to_none)


class GroupOut(GroupIn):
    id: int
    members: int = 0  # active members
    managers: list[str] = []
    cars: int = 0
    chargers: int = 0
