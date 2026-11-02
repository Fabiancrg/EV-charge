import os
import tempfile

import pytest

# Isolated database per test session, set before ev_charge is imported
_tmp = tempfile.mkdtemp(prefix="ev-charge-test-")
os.environ["DATABASE_URL"] = f"sqlite:///{_tmp}/test.db"
os.environ["CONFIG_FILE"] = os.path.join(_tmp, "missing-config.json")

from fastapi.testclient import TestClient  # noqa: E402

from ev_charge.database import Base, engine  # noqa: E402
from ev_charge.main import app  # noqa: E402


@pytest.fixture()
def client():
    with TestClient(app) as c:
        yield c
    with engine.begin() as conn:  # empty the tables between tests (schema stays)
        for table in reversed(Base.metadata.sorted_tables):
            conn.execute(table.delete())


@pytest.fixture()
def car(client):
    r = client.post(
        "/api/cars", json={"name": "Ioniq 5", "make": "Hyundai", "lease_start": "2025-01-01", "lease_end": "2028-01-01", "lease_km_per_year": 20000, "initial_odometer": 100}
    )
    assert r.status_code == 201, r.text
    return r.json()
