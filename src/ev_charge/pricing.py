"""Session cost: sessions made with a charger that uses the home tariff (home wall box, mobile charger…) are priced
at the CREG tariff valid on the charge date; other sessions keep the cost entered."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import Charger, ChargeSession, Setting, Tariff


def get_setting(db: Session, key: str, default: Any) -> Any:
    row = db.get(Setting, key)
    return json.loads(row.value) if row else default


def set_setting(db: Session, key: str, value: Any) -> None:
    row = db.get(Setting, key)
    if row is None:
        db.add(Setting(key=key, value=json.dumps(value)))
    else:
        row.value = json.dumps(value)


@dataclass
class Price:
    cost: float | None
    source: str | None  # "tariff" | "entered" | None
    tariff_price: float | None = None
    home: bool = False  # priced at the home tariff (made with such a charger)
    missing_tariff: bool = False  # priced at the home tariff, but no tariff covers its date


class Pricing:
    """Tariffs and chargers loaded once per request."""

    def __init__(self, db: Session) -> None:
        self.tariffs = sorted(db.scalars(select(Tariff)).all(), key=lambda t: t.valid_from)
        self.tariff_chargers = {c.id for c in db.scalars(select(Charger).where(Charger.uses_home_tariff.is_(True)))}

    def is_home(self, s: ChargeSession) -> bool:
        return s.charger_id is not None and s.charger_id in self.tariff_chargers

    def tariff_on(self, day: date) -> Tariff | None:
        for t in reversed(self.tariffs):
            if t.valid_from <= day and (t.valid_to is None or day <= t.valid_to):
                return t
        return None

    def price(self, s: ChargeSession) -> Price:
        if self.is_home(s):
            t = self.tariff_on(s.charge_date)
            if t is None:
                return Price(cost=None, source=None, home=True, missing_tariff=True)
            return Price(cost=round(s.kwh * t.price_per_kwh, 2), source="tariff", tariff_price=t.price_per_kwh, home=True)
        return Price(cost=s.cost, source="entered" if s.cost is not None else None)
