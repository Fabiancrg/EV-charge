"""Session cost: home charging is priced at the tariff valid on the charge date; other sessions keep the cost entered."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import ChargeSession, Setting, Tariff


def norm_location(value: str | None) -> str:
    return " ".join((value or "").split()).casefold()


def get_setting(db: Session, key: str, default: Any) -> Any:
    row = db.get(Setting, key)
    return json.loads(row.value) if row else default


def set_setting(db: Session, key: str, value: Any) -> None:
    row = db.get(Setting, key)
    if row is None:
        db.add(Setting(key=key, value=json.dumps(value)))
    else:
        row.value = json.dumps(value)


def home_locations(db: Session) -> list[str]:
    return get_setting(db, "home_locations", ["Home"])


@dataclass
class Price:
    cost: float | None
    source: str | None  # "tariff" | "entered" | None
    tariff_price: float | None = None
    home: bool = False
    missing_tariff: bool = False  # home session with no tariff for its date


class Pricing:
    """Tariffs and home locations loaded once per request."""

    def __init__(self, db: Session) -> None:
        self.tariffs = sorted(db.scalars(select(Tariff)).all(), key=lambda t: t.valid_from)
        self.home_names = home_locations(db)
        self.home = {norm_location(x) for x in self.home_names}

    def is_home(self, location: str | None) -> bool:
        return bool(location) and norm_location(location) in self.home

    def tariff_on(self, day: date) -> Tariff | None:
        for t in reversed(self.tariffs):
            if t.valid_from <= day and (t.valid_to is None or day <= t.valid_to):
                return t
        return None

    def price(self, s: ChargeSession) -> Price:
        if self.is_home(s.location):
            t = self.tariff_on(s.charge_date)
            if t is None:
                return Price(cost=None, source=None, home=True, missing_tariff=True)
            return Price(cost=round(s.kwh * t.price_per_kwh, 2), source="tariff", tariff_price=t.price_per_kwh, home=True)
        return Price(cost=s.cost, source="entered" if s.cost is not None else None)
