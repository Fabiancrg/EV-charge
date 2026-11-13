"""Groups: each group manages its own cars, chargers and members, and sees nothing of the others."""

import pytest
from conftest import role_id
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, inspect, text

from ev_charge.database import run_migrations
from ev_charge.main import app

PASSWORD = "member-password"


def login(username):
    c = TestClient(app)
    assert c.post("/api/auth/login", json={"username": username, "password": PASSWORD}).status_code == 200
    return c


@pytest.fixture()
def world(client, car):
    """The administrator's group (a car, a charger), and a second group with its manager, Greg."""
    home = client.get("/api/groups").json()[0]
    wallbox = client.post("/api/chargers", json={"name": "Zaptec", "default_car_id": car["id"]}).json()
    team = client.post("/api/groups", json={"name": "Colleagues", "max_members": 2}).json()
    r = client.post("/api/users", json={"username": "greg", "password": PASSWORD, "group_id": team["id"], "group_manager": True})
    assert r.status_code == 201, r.text
    opened = [login("greg")]
    yield {"admin": client, "home": home, "car": car, "wallbox": wallbox, "team": team, "greg": opened[0], "opened": opened}
    for c in opened:
        c.close()


def test_existing_installation_is_one_group(client, car):
    (home,) = client.get("/api/groups").json()
    assert (home["name"], home["members"], home["managers"], home["cars"]) == ("Default", 1, ["Ada Admin"], 1)
    me = client.get("/api/auth/me").json()
    assert (me["group_id"], me["group_name"], me["manages_group"]) == (home["id"], "Default", True)
    assert (car["group_id"], car["group_name"]) == (home["id"], "Default")  # a new car joins the group of who adds it


def test_group_admin_api(client, car):
    assert client.post("/api/groups", json={"name": "default"}).status_code == 409  # names are unique, whatever the case
    g = client.post("/api/groups", json={"name": " Family ", "max_members": 3, "notes": "cousins"}).json()
    assert (g["name"], g["max_members"], g["members"], g["cars"]) == ("Family", 3, 0, 0)
    assert client.put(f"/api/groups/{g['id']}", json={"name": "Family", "max_members": 0}).status_code == 422
    assert client.put(f"/api/groups/{g['id']}", json={"name": "Family", "max_members": 6}).json()["max_members"] == 6
    # A car can be moved to another group by an administrator; a group with something in it can't be deleted
    moved = client.put(f"/api/cars/{car['id']}", json={"name": car["name"], "group_id": g["id"]}).json()
    assert moved["group_name"] == "Family"
    assert client.delete(f"/api/groups/{g['id']}").status_code == 409
    client.put(f"/api/cars/{car['id']}", json={"name": car["name"], "group_id": car["group_id"]})
    assert client.delete(f"/api/groups/{g['id']}").status_code == 204
    assert client.post("/api/cars", json={"name": "Ghost", "group_id": 9999}).status_code == 422


def test_a_group_manager_runs_their_own_group(world):
    greg, team = world["greg"], world["team"]
    me = greg.get("/api/auth/me").json()
    assert (me["group_name"], me["manages_group"], me["permissions"]) == ("Colleagues", True, [])
    assert greg.get("/api/cars").json() == [] and greg.get("/api/chargers").json() == [] and greg.get("/api/sessions").json() == []
    assert [g["name"] for g in greg.get("/api/groups").json()] == ["Colleagues"]
    assert [u["username"] for u in greg.get("/api/users").json()] == ["greg"]

    # Their own car and charger; the same names as in the other group are fine
    mine = greg.post("/api/cars", json={"name": "Ioniq 5"})
    assert mine.status_code == 201 and mine.json()["group_id"] == team["id"]
    mine = mine.json()
    assert greg.post("/api/cars", json={"name": "ioniq 5"}).status_code == 409  # unique in the group
    assert greg.post("/api/cars", json={"name": "Elsewhere", "group_id": world["home"]["id"]}).status_code == 403
    assert greg.put(f"/api/cars/{mine['id']}", json={"name": "Ioniq 5", "make": "Hyundai"}).json()["make"] == "Hyundai"
    assert greg.put(f"/api/cars/{mine['id']}", json={"name": "Ioniq 5", "group_id": world["home"]["id"]}).status_code == 403
    box = greg.post("/api/chargers", json={"name": "Zaptec", "default_car_id": mine["id"]})
    assert box.status_code == 201 and (box.json()["group_name"], box.json()["manageable"]) == ("Colleagues", True)
    box = box.json()
    assert greg.post("/api/chargers", json={"name": "Stolen link", "default_car_id": world["car"]["id"]}).status_code == 422
    assert greg.put(f"/api/chargers/{box['id']}", json={"name": "Zaptec", "brand": "Zaptec"}).json()["brand"] == "Zaptec"
    assert greg.put(f"/api/chargers/{box['id']}/connection", json={"connector": "goe", "credentials": {}}).status_code == 422  # reached: fields missing

    # Sessions on their car, with their charger only
    s = greg.post("/api/sessions", json={"car_id": mine["id"], "charge_date": "2026-11-01", "kwh": 12, "charger_id": box["id"]})
    assert s.status_code == 201 and s.json()["charger_name"] == "Zaptec"
    assert greg.post("/api/sessions", json={"car_id": mine["id"], "charge_date": "2026-11-01", "kwh": 12, "charger_id": world["wallbox"]["id"]}).status_code == 422
    assert [x["car_name"] for x in greg.get("/api/sessions").json()] == ["Ioniq 5"]
    assert greg.get("/api/reports").json()["totals"]["sessions"] == 1

    # The administrator sees both groups
    admin = world["admin"]
    assert sorted((c["name"], c["group_name"]) for c in admin.get("/api/cars").json()) == [("Ioniq 5", "Colleagues"), ("Ioniq 5", "Default")]
    assert sorted(c["group_name"] for c in admin.get("/api/chargers").json()) == ["Colleagues", "Default"]
    assert len(admin.get("/api/sessions").json()) == 1

    # A car without sessions can be deleted by its manager
    spare = greg.post("/api/cars", json={"name": "Spare"}).json()
    assert greg.delete(f"/api/cars/{spare['id']}").status_code == 204
    assert greg.delete(f"/api/chargers/{box['id']}").status_code == 409  # used by a session


def test_nothing_of_another_group_is_reachable(world):
    greg, car, wallbox = world["greg"], world["car"], world["wallbox"]
    admin = world["admin"]
    theirs = admin.post("/api/sessions", json={"car_id": car["id"], "charge_date": "2026-11-02", "kwh": 20, "charger_id": wallbox["id"], "to_reimburse": True}).json()
    mine = greg.post("/api/cars", json={"name": "Model Y"}).json()

    for method, url, body in (
        ("get", f"/api/cars/{car['id']}", None),
        ("put", f"/api/cars/{car['id']}", {"name": "Hijacked"}),
        ("delete", f"/api/cars/{car['id']}", None),
        ("put", f"/api/chargers/{wallbox['id']}", {"name": "Hijacked"}),
        ("delete", f"/api/chargers/{wallbox['id']}", None),
        ("put", f"/api/chargers/{wallbox['id']}/connection", {"connector": None}),
        ("post", f"/api/chargers/{wallbox['id']}/poll", None),
        ("get", f"/api/chargers/{wallbox['id']}/history?date_from=2026-10-01&date_to=2026-10-31", None),
        ("get", f"/api/sessions/{theirs['id']}", None),
        ("delete", f"/api/sessions/{theirs['id']}", None),
        ("post", "/api/sessions", {"car_id": car["id"], "charge_date": "2026-11-03", "kwh": 5}),
        ("put", f"/api/users/{admin.get('/api/auth/me').json()['id']}", {"username": "admin", "active": False}),
        ("delete", f"/api/users/{admin.get('/api/auth/me').json()['id']}", None),
        ("post", "/api/groups", {"name": "Mine too"}),
        ("put", f"/api/groups/{world['team']['id']}", {"name": "Colleagues", "max_members": 99}),
        ("get", "/api/tariffs", None) if False else ("put", "/api/settings/sso", {}),
    ):
        r = getattr(greg, method)(url, **({"json": body} if body is not None else {}))
        assert r.status_code in (403, 404), (method, url, r.status_code, r.text)

    assert admin.get(f"/api/cars/{car['id']}").json()["name"] == "Ioniq 5" and admin.get(f"/api/sessions/{theirs['id']}").status_code == 200
    # Bulk changes skip the sessions of others; a charger of another group can't be set on one's sessions
    own = greg.post("/api/sessions", json={"car_id": mine["id"], "charge_date": "2026-11-04", "kwh": 7}).json()
    assert greg.patch("/api/sessions/bulk", json={"ids": [theirs["id"], own["id"]], "reimbursement_status": "paid", "to_reimburse": True}).json() == {"updated": 1, "skipped": 1}
    assert greg.patch("/api/sessions/bulk", json={"ids": [own["id"]], "charger_id": wallbox["id"]}).json() == {"updated": 0, "skipped": 1}
    assert admin.get(f"/api/sessions/{theirs['id']}").json()["reimbursement_status"] == "recorded"
    # Reimbursement documents: only with a charger of one's group
    assert greg.post(f"/api/reimbursement-documents/preview?charger_id={wallbox['id']}", files={"file": ("d.csv", b"x;y\n1;2\n")}).status_code == 422
    assert greg.get("/api/sessions/export.csv").text.count("\n") == 2  # the header and their one session


def test_members_and_the_group_limit(world):
    greg, admin, team = world["greg"], world["admin"], world["team"]
    mine = greg.post("/api/cars", json={"name": "Model Y"}).json()
    driver, administrator, accountant = role_id("Driver"), role_id("Administrator"), role_id("Accountant")

    # A member, with a role on the group's car
    body = {
        "username": "mia",
        "password": PASSWORD,
        "role_id": driver,
        "grants": [{"car_id": mine["id"], "role_id": driver}],
        "group_id": world["home"]["id"],
        "group_manager": True,
    }
    mia = greg.post("/api/users", json=body)
    assert mia.status_code == 201, mia.text
    mia = mia.json()
    assert (mia["group_name"], mia["group_manager"]) == ("Colleagues", False)  # their own group, and not a manager: that's the administrator's call
    assert [u["username"] for u in greg.get("/api/users").json()] == ["greg", "mia"]

    # What a manager can't do: privileged roles, cars of other groups, more members than allowed
    assert greg.post("/api/users", json={"username": "eve", "password": PASSWORD, "role_id": administrator}).status_code == 403
    assert greg.put(f"/api/users/{mia['id']}", json={"username": "mia", "role_id": accountant}).status_code == 403
    assert greg.put(f"/api/users/{mia['id']}", json={"username": "mia", "grants": [{"car_id": world["car"]["id"], "role_id": driver}]}).status_code == 422
    full = greg.post("/api/users", json={"username": "tom", "password": PASSWORD})
    assert full.status_code == 409 and "limited to 2 member(s)" in full.json()["detail"]
    assert admin.post("/api/users", json={"username": "tom", "password": PASSWORD, "group_id": team["id"]}).status_code == 409  # the limit holds for everyone

    # The member sees the group's car and charger, and manages nothing
    box = greg.post("/api/chargers", json={"name": "Wall box", "default_car_id": mine["id"]}).json()
    greg.put(f"/api/chargers/{box['id']}/connection", json={"connector": None})
    world["opened"].append(login("mia"))
    m = world["opened"][-1]
    assert [c["name"] for c in m.get("/api/cars").json()] == ["Model Y"]
    seen = m.get("/api/chargers").json()
    assert [(c["name"], c["manageable"], c["connector_settings"]) for c in seen] == [("Wall box", False, None)]
    assert m.get("/api/auth/me").json()["manages_group"] is False
    for method, url, body in (
        ("post", "/api/cars", {"name": "Mine"}),
        ("post", "/api/chargers", {"name": "Mine"}),
        ("get", "/api/users", None),
        ("delete", f"/api/cars/{mine['id']}", None),
    ):
        assert getattr(m, method)(url, **({"json": body} if body else {})).status_code == 403, url
    assert m.put(f"/api/chargers/{box['id']}", json={"name": "Mine now"}).status_code == 403
    assert m.post("/api/sessions", json={"car_id": mine["id"], "charge_date": "2026-11-05", "kwh": 9, "charger_id": box["id"]}).status_code == 201

    # A disabled member frees a place; the administrator raises the limit and names a second manager
    greg.put(f"/api/users/{mia['id']}", json={"username": "mia", "active": False, "grants": [{"car_id": mine["id"], "role_id": driver}]})
    assert greg.post("/api/users", json={"username": "tom", "password": PASSWORD}).status_code == 201
    assert greg.put(f"/api/users/{mia['id']}", json={"username": "mia", "active": True}).status_code == 409  # no place left to come back
    admin.put(f"/api/groups/{team['id']}", json={"name": "Colleagues", "max_members": 3})
    assert greg.put(f"/api/users/{mia['id']}", json={"username": "mia", "active": True}).status_code == 200
    promoted = admin.put(f"/api/users/{mia['id']}", json={"username": "mia", "group_manager": True}).json()
    assert promoted["group_manager"] and admin.get("/api/groups").json()[0]["managers"] == ["greg", "mia"]
    assert greg.put(f"/api/users/{mia['id']}", json={"username": "mia"}).json()["group_manager"] is True  # a manager's edit keeps it
    assert greg.delete(f"/api/users/{mia['id']}").status_code == 204 and greg.delete(f"/api/users/{greg.get('/api/auth/me').json()['id']}").status_code == 409


def test_an_administrator_in_a_group_is_not_managed_by_its_manager(world):
    """The administrator is a member of the first group: another manager of that group can't touch that account."""
    admin, home = world["admin"], world["home"]
    admin.post("/api/users", json={"username": "partner", "password": PASSWORD, "group_id": home["id"], "group_manager": True})
    world["opened"].append(login("partner"))
    partner = world["opened"][-1]
    boss = admin.get("/api/auth/me").json()
    assert {u["username"] for u in partner.get("/api/users").json()} == {"admin", "partner"}
    assert partner.put(f"/api/users/{boss['id']}", json={"username": "admin", "active": False}).status_code == 403
    assert partner.delete(f"/api/users/{boss['id']}").status_code == 403
    assert [c["name"] for c in partner.get("/api/cars").json()] == ["Ioniq 5"]  # the cars of the group, as its manager
    assert partner.put(f"/api/cars/{world['car']['id']}", json={"name": "Ioniq 5", "color": "blue"}).json()["color"] == "blue"


def test_import_uses_the_chargers_of_the_cars_group(world):
    greg = world["greg"]
    mine = greg.post("/api/cars", json={"name": "Model Y"}).json()
    box = greg.post("/api/chargers", json={"name": "Zaptec", "default_car_id": mine["id"]}).json()  # the other group has a "Zaptec" too
    req = {"rows": [["2026-11-01", "12", "Zaptec"]], "mapping": {"charge_date": 0, "kwh": 1, "charger": 2}, "car_id": mine["id"]}
    assert greg.post("/api/import/sessions", json=req).json()["imported"] == 1
    assert greg.get("/api/sessions").json()[0]["charger_id"] == box["id"]
    theirs = {"rows": [["2026-11-02", "8"]], "mapping": {"charge_date": 0, "kwh": 1}, "car_id": mine["id"], "charger_id": world["wallbox"]["id"]}
    preview = greg.post("/api/import/sessions/preview", json=theirs).json()
    assert preview["summary"]["to_import"] == 0 and "isn't in the group" in preview["rows"][0]["errors"][0]


def test_migration_puts_everything_in_a_first_group(tmp_path):
    eng = create_engine(f"sqlite:///{tmp_path / 'v9.db'}")
    run_migrations(eng, "009")
    with eng.begin() as conn:
        conn.execute(text("INSERT INTO cars (id, name, active, created_at, updated_at) VALUES (1, 'EV6', 1, '2026-01-01', '2026-01-01')"))
        conn.execute(
            text(
                "INSERT INTO chargers (id, name, uses_home_tariff, active, poll_enabled, created_at, updated_at, default_car_id) "
                "VALUES (1, 'Zaptec', 1, 1, 0, '2026-01-01', '2026-01-01', 1)"
            )
        )
        conn.execute(
            text(
                "INSERT INTO charge_sessions (id, car_id, charge_date, kwh, to_reimburse, charger_id, created_at, updated_at) "
                "VALUES (7, 1, '2026-02-01', 12.5, 1, 1, '2026-02-01', '2026-02-01')"
            )
        )
        conn.execute(text("INSERT INTO users (id, username, password_hash, active, created_at) VALUES (1, 'boss', 'x', 1, '2026-01-01')"))
    run_migrations(eng)
    with eng.begin() as conn:
        assert conn.execute(text("SELECT name, max_members FROM groups")).all() == [("Default", 20)]
        assert [conn.execute(text(f"SELECT group_id FROM {t}")).scalar() for t in ("cars", "chargers", "users")] == [1, 1, 1]
        assert conn.execute(text("SELECT car_id, charger_id, kwh FROM charge_sessions")).all() == [(1, 1, 12.5)]
        # Names are unique per group now: another group may have its own "EV6"
        conn.execute(text("INSERT INTO cars (name, active, created_at, updated_at, group_id) VALUES ('EV6', 1, '2026-01-01', '2026-01-01', 2)"))
    assert inspect(eng).get_unique_constraints("cars") == [] and inspect(eng).get_unique_constraints("chargers") == []
    eng.dispose()
