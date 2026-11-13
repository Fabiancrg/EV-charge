import os
import tempfile

import pytest

# Isolated database per test session, set before ev_charge is imported
_tmp = tempfile.mkdtemp(prefix="ev-charge-test-")
os.environ["DATABASE_URL"] = f"sqlite:///{_tmp}/test.db"
os.environ["CONFIG_FILE"] = os.path.join(_tmp, "missing-config.json")
os.environ["CHARGER_POLLING"] = "0"  # tests poll explicitly
os.environ["EV_CHARGE_SECRET_KEY"] = "kNh0yW8wq1t6zJxH3o0m6l9Yv8G2cQZ3B6s4m1E9dXo="  # a fixed Fernet key

from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import select  # noqa: E402

from ev_charge import auth  # noqa: E402
from ev_charge.database import Base, SessionLocal, engine, init_db  # noqa: E402
from ev_charge.main import app  # noqa: E402
from ev_charge.models import Role  # noqa: E402
from ev_charge.permissions import ensure_builtin_roles, ensure_default_group  # noqa: E402

init_db()

ADMIN_PASSWORD = "admin-password"


def _empty_tables():
    with engine.begin() as conn:
        for table in reversed(Base.metadata.sorted_tables):
            conn.execute(table.delete())
    with SessionLocal() as db:
        ensure_builtin_roles(db)  # back to the installed roles
        ensure_default_group(db)
    auth._FAILS.clear()


def role_id(name):
    with SessionLocal() as db:
        return db.scalar(select(Role.id).where(Role.name == name))


@pytest.fixture()
def anon():
    """A client that is not signed in, on an empty database (setup not done)."""
    _empty_tables()
    with TestClient(app) as c:
        yield c
    _empty_tables()


@pytest.fixture()
def client(anon):
    """Signed in as the first administrator."""
    r = anon.post("/api/auth/setup", json={"username": "admin", "full_name": "Ada Admin", "password": ADMIN_PASSWORD})
    assert r.status_code == 201, r.text
    return anon


@pytest.fixture()
def make_driver(client):
    """Create a driver assigned to the given cars and return a client signed in as that driver."""
    opened = []

    def make(car_ids, username="dave", role="Driver", global_role="Driver"):
        grants = [{"car_id": c, "role_id": role_id(role)} for c in car_ids]
        body = {"username": username, "full_name": "Dave Driver", "role_id": role_id(global_role), "password": "driver-password", "grants": grants}
        r = client.post("/api/users", json=body)
        assert r.status_code == 201, r.text
        c = TestClient(app)
        opened.append(c)
        assert c.post("/api/auth/login", json={"username": username, "password": "driver-password"}).status_code == 200
        return c

    yield make
    for c in opened:
        c.close()


@pytest.fixture()
def car(client):
    r = client.post(
        "/api/cars", json={"name": "Ioniq 5", "make": "Hyundai", "lease_start": "2025-01-01", "lease_end": "2028-01-01", "lease_km_per_year": 20000, "initial_odometer": 100}
    )
    assert r.status_code == 201, r.text
    return r.json()
