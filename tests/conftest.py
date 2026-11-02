import os
import tempfile

import pytest

# Isolated database per test session, set before ev_charge is imported
_tmp = tempfile.mkdtemp(prefix="ev-charge-test-")
os.environ["DATABASE_URL"] = f"sqlite:///{_tmp}/test.db"
os.environ["CONFIG_FILE"] = os.path.join(_tmp, "missing-config.json")

from fastapi.testclient import TestClient  # noqa: E402

from ev_charge import auth  # noqa: E402
from ev_charge.database import Base, engine, init_db  # noqa: E402
from ev_charge.main import app  # noqa: E402

init_db()

ADMIN_PASSWORD = "admin-password"


def _empty_tables():
    with engine.begin() as conn:
        for table in reversed(Base.metadata.sorted_tables):
            conn.execute(table.delete())
    auth._FAILS.clear()


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

    def make(car_ids, username="dave"):
        r = client.post("/api/users", json={"username": username, "full_name": "Dave Driver", "role": "driver", "password": "driver-password", "car_ids": car_ids})
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
