"""Permission catalogue and evaluation.

A role is a set of permission names ("sessions:edit"), with wildcards: "*" (everything) and
"sessions:*" (every sessions permission). Permissions are either global (app-wide) or per car:

- a user's **global role** gives its global permissions, and its car permissions on **every car**
  when the role has all_cars set (Administrator, Accountant…);
- a **role on a car** (CarGrant) gives its car permissions on that car only.

An API token's permissions are the intersection of its own list and its owner's.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# name: (label, group)
GLOBAL_PERMISSIONS: dict[str, tuple[str, str]] = {
    "users:manage": ("Manage users, their roles and car access", "Administration"),
    "roles:manage": ("Create and edit roles", "Administration"),
    "settings:manage": ("Single sign-on and security settings", "Administration"),
    "audit:read": ("Read the audit log", "Administration"),
    "cars:manage": ("Add and delete cars", "Fleet"),
    "chargers:manage": ("Manage chargers, their connection and history import", "Fleet"),
    "tariffs:manage": ("Manage home charging tariffs (CREG import)", "Fleet"),
    "tokens:manage": ("Manage API tokens (their access and cars)", "Administration"),
}
CAR_PERMISSIONS: dict[str, tuple[str, str]] = {
    "car:view": ("See the car, its sessions, reports, statements and live charger", "Car"),
    "car:edit": ("Edit the car (specs, lease)", "Car"),
    "sessions:create": ("Add sessions", "Sessions"),
    "sessions:edit": ("Edit and delete sessions", "Sessions"),
    "sessions:import": ("Import sessions from Excel / CSV", "Sessions"),
    "reimbursement:manage": ("Change the reimbursement flag and status; reimbursement documents and payments", "Sessions"),
}
ALL_PERMISSIONS = {**GLOBAL_PERMISSIONS, **CAR_PERMISSIONS}


def matches(granted: set[str], permission: str) -> bool:
    return "*" in granted or permission in granted or f"{permission.split(':')[0]}:*" in granted


def expand(granted: set[str], catalogue: dict[str, tuple[str, str]]) -> set[str]:
    """The catalogue permissions a set of grants (with wildcards) covers."""
    return {p for p in catalogue if matches(granted, p)}


def valid_permission(name: str) -> bool:
    if name == "*" or name in ALL_PERMISSIONS:
        return True
    return name.endswith(":*") and any(p.startswith(name[:-1]) for p in ALL_PERMISSIONS)


@dataclass
class Grants:
    """What a request may do: global permissions, and car permissions (on all cars and/or per car)."""

    global_perms: set[str] = field(default_factory=set)
    all_cars: set[str] = field(default_factory=set)  # car permissions on every car
    per_car: dict[int, set[str]] = field(default_factory=dict)

    def has(self, permission: str) -> bool:
        return permission in self.global_perms

    def on_car(self, permission: str, car_id: int) -> bool:
        return permission in self.all_cars or permission in self.per_car.get(car_id, set())

    def cars_with(self, permission: str) -> set[int] | None:
        """Car ids where the permission holds; None means every car."""
        if permission in self.all_cars:
            return None
        return {c for c, perms in self.per_car.items() if permission in perms}

    def restrict(self, allowed: set[str]) -> Grants:
        """Keep only the permissions an API token was given."""
        return Grants(
            global_perms={p for p in self.global_perms if matches(allowed, p)},
            all_cars={p for p in self.all_cars if matches(allowed, p)},
            per_car={c: {p for p in perms if matches(allowed, p)} for c, perms in self.per_car.items()},
        )

    def car_permissions(self, car_id: int) -> set[str]:
        return self.all_cars | self.per_car.get(car_id, set())


# Built-in roles: created at install (migration 006) and recreated at startup if missing
BUILTIN_ROLES: list[tuple[str, str, list[str], bool]] = [
    ("Administrator", "Everything, on every car", ["*"], True),
    (
        "Driver",
        "Records, edits and imports the sessions of the cars given to them",
        ["car:view", "sessions:create", "sessions:edit", "sessions:import", "reimbursement:manage"],
        False,
    ),
    ("Viewer", "Sees the cars given to them, their sessions and reports", ["car:view"], False),
    ("Accountant", "Sees every car and follows the reimbursements (status, statements)", ["car:view", "reimbursement:manage"], True),
]


def ensure_builtin_roles(db: Any) -> None:
    import json

    from sqlalchemy import select

    from .models import Role

    existing = {r.name for r in db.scalars(select(Role))}
    for name, description, perms, all_cars in BUILTIN_ROLES:
        if name not in existing:
            db.add(Role(name=name, description=description, permissions=json.dumps(perms), all_cars=all_cars, builtin=True))
    db.commit()
