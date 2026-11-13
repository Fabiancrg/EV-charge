"""Tariffs per group: a group uses the default tariffs, or switches to its own."""

import pytest
from conftest import role_id
from fastapi.testclient import TestClient

from ev_charge.main import app

PASSWORD = "member-password"


def login(username):
    c = TestClient(app)
    assert c.post("/api/auth/login", json={"username": username, "password": PASSWORD}).status_code == 200
    return c


def cost(c, session):
    return c.get(f"/api/sessions/{session['id']}").json()["cost"]


@pytest.fixture()
def world(client, car):
    """A default tariff of 0.30; the administrator's group and Greg's group each have a car, a charger at the home
    tariff and a session of 10 kWh."""
    default = client.post("/api/tariffs", json={"valid_from": "2026-01-01", "price_per_kwh": 0.30, "label": "Default 2026"}).json()
    wallbox = client.post("/api/chargers", json={"name": "Zaptec", "default_car_id": car["id"]}).json()
    home = client.post("/api/sessions", json={"car_id": car["id"], "charge_date": "2026-11-01", "kwh": 10, "charger_id": wallbox["id"], "to_reimburse": True}).json()
    team = client.post("/api/groups", json={"name": "Colleagues", "max_members": 3}).json()
    client.post("/api/users", json={"username": "greg", "password": PASSWORD, "group_id": team["id"], "group_manager": True})
    greg = login("greg")
    mine = greg.post("/api/cars", json={"name": "Model Y"}).json()
    box = greg.post("/api/chargers", json={"name": "Zaptec", "default_car_id": mine["id"]}).json()
    theirs = greg.post("/api/sessions", json={"car_id": mine["id"], "charge_date": "2026-11-01", "kwh": 10, "charger_id": box["id"], "to_reimburse": True}).json()
    opened = [greg]
    yield {"admin": client, "default": default, "home": home, "team": team, "greg": greg, "car": mine, "theirs": theirs, "opened": opened}
    for c in opened:
        c.close()


def test_a_group_uses_the_default_tariffs_until_it_chooses_its_own(world):
    greg, admin, team, default = world["greg"], world["admin"], world["team"], world["default"]
    mode = greg.get("/api/tariffs/mode").json()
    assert (mode["group_name"], mode["own_tariffs"], mode["can_switch"], mode["can_edit"], mode["own_count"]) == ("Colleagues", False, True, False, 0)
    assert [(t["label"], t["group_id"], t["sessions"]) for t in greg.get("/api/tariffs").json()] == [("Default 2026", None, 1)]  # the default ones, with their own session only
    assert cost(greg, world["theirs"]) == 3.0 and cost(admin, world["home"]) == 3.0
    # Read-only for the group while it uses them
    assert greg.post("/api/tariffs", json={"valid_from": "2027-01-01", "price_per_kwh": 0.5}).status_code == 409
    assert greg.put(f"/api/tariffs/{default['id']}", json={"valid_from": "2026-01-01", "price_per_kwh": 0.01}).status_code == 403
    assert greg.delete(f"/api/tariffs/{default['id']}").status_code == 403
    assert greg.get("/api/tariffs/creg").status_code == 409

    # Its own tariffs, starting from a copy of the default ones
    mode = greg.put("/api/tariffs/mode", json={"own_tariffs": True, "copy_default": True}).json()
    assert (mode["own_tariffs"], mode["can_edit"], mode["own_count"]) == (True, True, 1)
    (own,) = greg.get("/api/tariffs").json()
    assert (own["label"], own["group_id"], own["price_per_kwh"], own["sessions"]) == ("Default 2026", team["id"], 0.30, 1) and own["id"] != default["id"]
    assert greg.put(f"/api/tariffs/{own['id']}", json={"valid_from": "2026-01-01", "valid_to": "2026-12-31", "price_per_kwh": 0.40, "label": "Ours"}).status_code == 200
    assert greg.post("/api/tariffs", json={"valid_from": "2026-06-01", "price_per_kwh": 0.5}).status_code == 409  # overlaps, within its own set
    assert greg.post("/api/tariffs", json={"valid_from": "2027-01-01", "price_per_kwh": 0.45}).status_code == 201
    assert cost(greg, world["theirs"]) == 4.0 and cost(admin, world["home"]) == 3.0  # each group at its own price
    assert greg.get("/api/reports/reimbursement?date_from=2026-11-01&date_to=2026-11-30").json()["totals"]["amount"] == 4.0
    assert admin.get("/api/reports/reimbursement?date_from=2026-11-01&date_to=2026-11-30").json()["totals"]["amount"] == 7.0  # 3 + 4

    # The administrator: the default tariffs by default, a group's on request, and may change them
    assert [(t["label"], t["sessions"]) for t in admin.get("/api/tariffs").json()] == [("Default 2026", 1)]
    assert [t["label"] for t in admin.get(f"/api/tariffs?group_id={team['id']}").json()] == [None, "Ours"]
    assert admin.get(f"/api/tariffs/mode?group_id={team['id']}").json()["can_edit"] is True
    assert admin.put(f"/api/tariffs/{own['id']}", json={"valid_from": "2026-01-01", "valid_to": "2026-12-31", "price_per_kwh": 0.42}).status_code == 200
    assert cost(greg, world["theirs"]) == 4.2
    # The default tariffs can overlap a group's: they are separate sets
    assert admin.post("/api/tariffs", json={"valid_from": "2025-01-01", "valid_to": "2025-12-31", "price_per_kwh": 0.28}).status_code == 201

    # Back to the default tariffs: its own are kept for later, and no longer editable
    mode = greg.put("/api/tariffs/mode", json={"own_tariffs": False}).json()
    assert (mode["own_tariffs"], mode["can_edit"], mode["own_count"]) == (False, False, 2)
    assert cost(greg, world["theirs"]) == 3.0
    assert len(greg.get("/api/tariffs?own=true").json()) == 2 and len(greg.get("/api/tariffs").json()) == 2  # its own, kept / the two default ones
    assert greg.put(f"/api/tariffs/{own['id']}", json={"valid_from": "2026-01-01", "valid_to": "2026-12-31", "price_per_kwh": 0.1}).status_code == 200  # still theirs to prepare
    # Switching again doesn't copy over what it already has
    assert greg.put("/api/tariffs/mode", json={"own_tariffs": True, "copy_default": True}).json()["own_count"] == 2


def test_tariffs_of_another_group_and_members(world):
    greg, admin, team = world["greg"], world["admin"], world["team"]
    home_group = admin.get("/api/auth/me").json()["group_id"]
    admin.put(f"/api/tariffs/mode?group_id={home_group}", json={"own_tariffs": True, "copy_default": True})
    (theirs,) = admin.get(f"/api/tariffs?group_id={home_group}").json()
    for method, url, body in (
        ("get", f"/api/tariffs?group_id={home_group}", None),
        ("get", f"/api/tariffs/mode?group_id={home_group}", None),
        ("put", f"/api/tariffs/mode?group_id={home_group}", {"own_tariffs": False}),
        ("post", f"/api/tariffs?group_id={home_group}", {"valid_from": "2030-01-01", "price_per_kwh": 0.1}),
        ("put", f"/api/tariffs/{theirs['id']}", {"valid_from": "2026-01-01", "price_per_kwh": 0.01}),
        ("delete", f"/api/tariffs/{theirs['id']}", None),
    ):
        r = getattr(greg, method)(url, **({"json": body} if body is not None else {}))
        assert r.status_code == 404, (method, url, r.status_code)
    assert admin.put("/api/tariffs/mode", json={"own_tariffs": True}).status_code == 422  # the default tariffs have no mode

    # A member reads the tariffs of the group and changes nothing
    driver = role_id("Driver")
    greg.post("/api/users", json={"username": "mia", "password": PASSWORD, "role_id": driver, "grants": [{"car_id": world["car"]["id"], "role_id": driver}]})
    world["opened"].append(login("mia"))
    mia = world["opened"][-1]
    greg.put("/api/tariffs/mode", json={"own_tariffs": True, "copy_default": True})
    mode = mia.get("/api/tariffs/mode").json()
    assert (mode["own_tariffs"], mode["can_switch"], mode["can_edit"]) == (True, False, False)
    (own,) = mia.get("/api/tariffs").json()
    assert own["group_id"] == team["id"]
    assert mia.put("/api/tariffs/mode", json={"own_tariffs": False}).status_code == 403
    assert mia.post("/api/tariffs", json={"valid_from": "2030-01-01", "price_per_kwh": 0.1}).status_code == 403
    assert mia.put(f"/api/tariffs/{own['id']}", json={"valid_from": "2026-01-01", "price_per_kwh": 0.1}).status_code == 403


def test_creg_import_into_a_groups_own_tariffs(world):
    greg, admin = world["greg"], world["admin"]
    greg.put("/api/tariffs/mode", json={"own_tariffs": True})
    body = {"region": "flanders", "source": "test", "items": [{"key": "2026-Q4", "valid_from": "2026-10-01", "valid_to": "2026-12-31", "price_per_kwh": 0.33}]}
    assert greg.post("/api/tariffs/creg/apply", json=body).json() == {"created": 1, "updated": 0, "skipped": []}
    (own,) = greg.get("/api/tariffs").json()
    assert (own["price_per_kwh"], own["label"]) == (0.33, "CREG Q4 2026 (Flanders)") and cost(greg, world["theirs"]) == 3.3
    assert greg.get("/api/tariffs/mode").json()["creg_region"] == "flanders"
    assert admin.get("/api/tariffs/mode").json()["creg_region"] == "wallonia"  # the installation's own region is untouched
    assert len(admin.get("/api/tariffs").json()) == 1 and cost(admin, world["home"]) == 3.0
    assert greg.post("/api/tariffs/creg/apply", json=body | {"items": [body["items"][0] | {"price_per_kwh": 0.35}]}).json()["updated"] == 1
