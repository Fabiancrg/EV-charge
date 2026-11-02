"""Roles, per-car permissions, API tokens, sign-in sessions and the audit log."""

from conftest import ADMIN_PASSWORD, role_id
from fastapi.testclient import TestClient

from ev_charge.main import app


def _session(c, car_id, **kw):
    return c.post("/api/sessions", json={"car_id": car_id, "charge_date": "2026-03-01", "kwh": 20} | kw)


def _user(client, username, global_role=None, grants=()):
    body = {"username": username, "password": "user-password", "role_id": role_id(global_role) if global_role else None,
            "grants": [{"car_id": c, "role_id": role_id(r)} for c, r in grants]}  # fmt: skip
    r = client.post("/api/users", json=body)
    assert r.status_code == 201, r.text
    c = TestClient(app)
    assert c.post("/api/auth/login", json={"username": username, "password": "user-password"}).status_code == 200
    return c


def test_a_different_role_on_each_car(client, car):
    other = client.post("/api/cars", json={"name": "Model 3"}).json()
    s_other = _session(client, other["id"]).json()
    mia = _user(client, "mia", None, [(car["id"], "Driver"), (other["id"], "Viewer")])
    me = mia.get("/api/auth/me").json()
    assert me["permissions"] == [] and set(me["car_permissions"][str(car["id"])]) >= {"sessions:create", "sessions:edit"}
    assert me["car_permissions"][str(other["id"])] == ["car:view"]

    assert {c["name"] for c in mia.get("/api/cars").json()} == {"Ioniq 5", "Model 3"}
    assert _session(mia, car["id"]).status_code == 201
    assert _session(mia, other["id"]).status_code == 403  # viewer there
    assert mia.get(f"/api/sessions/{s_other['id']}").status_code == 200  # but may see it
    assert mia.delete(f"/api/sessions/{s_other['id']}").status_code == 403
    assert mia.put(f"/api/cars/{car['id']}", json={"name": "Renamed"}).status_code == 403  # Driver has no car:edit
    assert len(mia.get("/api/reports").json()["cars"]) == 2
    mia.close()


def test_custom_role_and_all_cars(client, car):
    other = client.post("/api/cars", json={"name": "Model 3"}).json()
    # A fleet manager: edits every car and adds cars, nothing else
    r = client.post("/api/roles", json={"name": "Fleet manager", "permissions": ["car:*", "cars:manage"], "all_cars": True})
    assert r.status_code == 201 and r.json()["permissions"] == ["car:*", "cars:manage"]
    assert client.post("/api/roles", json={"name": "fleet MANAGER", "permissions": []}).status_code == 409
    assert client.post("/api/roles", json={"name": "Bad", "permissions": ["nope:x"]}).status_code == 422
    fm = _user(client, "fred", "Fleet manager")
    assert fm.put(f"/api/cars/{other['id']}", json={"name": "Model 3 LR"}).status_code == 200
    assert fm.post("/api/cars", json={"name": "New car"}).status_code == 201
    assert _session(fm, car["id"]).status_code == 403  # no session permission
    assert fm.get("/api/users").status_code == 403
    fm.close()


def test_accountant_changes_reimbursement_only(client, car):
    s = _session(client, car["id"], to_reimburse=True).json()
    zap = client.post("/api/chargers", json={"name": "Zaptec"}).json()
    acc = _user(client, "anna", "Accountant")
    r = acc.patch("/api/sessions/bulk", json={"ids": [s["id"]], "reimbursement_status": "paid"}).json()
    assert r == {"updated": 1, "skipped": 0}
    r = acc.patch("/api/sessions/bulk", json={"ids": [s["id"]], "charger_id": zap["id"]}).json()
    assert r == {"updated": 0, "skipped": 1}  # needs sessions:edit
    assert _session(acc, car["id"]).status_code == 403
    assert acc.get("/api/reports/reimbursement").json()["totals"]["sessions"] == 1
    acc.close()


def test_without_reimbursement_permission_the_flags_follow_the_defaults(client, car):
    client.post("/api/roles", json={"name": "Logger", "permissions": ["car:view", "sessions:create", "sessions:edit"]})
    log = _user(client, "leo", None, [(car["id"], "Logger")])
    s = _session(log, car["id"], to_reimburse=True, reimbursement_status="paid").json()
    assert s["to_reimburse"] is False and s["reimbursement_status"] is None
    client.patch("/api/sessions/bulk", json={"ids": [s["id"]], "to_reimburse": True, "reimbursement_status": "invoiced"})
    body = {"car_id": car["id"], "charge_date": "2026-03-01", "kwh": 25, "to_reimburse": False}
    upd = log.put(f"/api/sessions/{s['id']}", json=body).json()
    assert upd["kwh"] == 25 and upd["to_reimburse"] is True and upd["reimbursement_status"] == "invoiced"  # kept
    log.close()


def test_role_rules(client):
    roles = {r["name"]: r for r in client.get("/api/roles").json()}
    assert {"Administrator", "Driver", "Viewer", "Accountant"} <= set(roles)
    assert client.delete(f"/api/roles/{roles['Driver']['id']}").status_code == 409  # built-in
    # Removing user management from the only administrator's role is refused
    admin = roles["Administrator"]
    body = {"name": "Administrator", "permissions": ["car:view"], "all_cars": True}
    assert client.put(f"/api/roles/{admin['id']}", json=body).status_code == 409
    custom = client.post("/api/roles", json={"name": "Temp", "permissions": ["car:view"]}).json()
    client.post("/api/users", json={"username": "tom", "role_id": custom["id"]})
    assert client.delete(f"/api/roles/{custom['id']}").status_code == 409  # in use
    cat = client.get("/api/roles/permissions").json()
    assert "users:manage" in {p["name"] for p in cat["global"]} and "sessions:edit" in {p["name"] for p in cat["car"]}


def test_api_tokens(client, car):
    _session(client, car["id"])
    r = client.post("/api/auth/tokens", json={"name": "Home Assistant", "permissions": ["car:view"], "expires_days": 30})
    assert r.status_code == 201
    raw = r.json()["token"]
    assert raw.startswith("evc_") and r.json()["prefix"] == raw[:12]
    api = TestClient(app)
    h = {"Authorization": f"Bearer {raw}"}
    assert len(api.get("/api/sessions", headers=h).json()) == 1
    assert api.get("/api/auth/me", headers=h).json()["via_token"] is True
    # Narrowed: the admin's token can't write nor manage users
    assert api.post("/api/sessions", headers=h, json={"car_id": car["id"], "charge_date": "2026-03-01", "kwh": 5}).status_code == 403
    assert api.get("/api/users", headers=h).status_code == 403
    assert api.post("/api/auth/tokens", headers=h, json={"name": "x", "permissions": ["car:view"]}).status_code == 403
    assert api.get("/api/auth/sessions", headers=h).status_code == 403  # account pages need a browser sign-in
    assert api.get("/api/sessions", headers={"Authorization": "Bearer evc_wrong"}).status_code == 401
    assert client.post("/api/auth/tokens", json={"name": "bad", "permissions": ["root"]}).status_code == 422
    tid = r.json()["id"]
    assert client.get("/api/auth/tokens").json()[0]["last_used_at"] is not None
    assert client.delete(f"/api/auth/tokens/{tid}").status_code == 204
    assert api.get("/api/sessions", headers=h).status_code == 401
    # A Viewer has no tokens:create
    viewer = _user(client, "vic", "Viewer")
    assert viewer.post("/api/auth/tokens", json={"name": "x", "permissions": ["car:view"]}).status_code == 403
    viewer.close()
    api.close()


def test_sign_in_sessions(client):
    other = TestClient(app, headers={"User-Agent": "Mozilla/5.0 (iPhone) Safari/605.1"})
    assert other.post("/api/auth/login", json={"username": "admin", "password": ADMIN_PASSWORD}).status_code == 200
    rows = client.get("/api/auth/sessions").json()
    assert len(rows) == 2 and sum(r["current"] for r in rows) == 1
    phone = next(r for r in rows if not r["current"])
    assert "iPhone" in phone["user_agent"] and phone["method"] == "password"
    assert client.delete(f"/api/auth/sessions/{phone['id']}").status_code == 204
    assert other.get("/api/auth/me").status_code == 401
    assert client.delete("/api/auth/sessions/short").status_code == 404
    assert other.post("/api/auth/login", json={"username": "admin", "password": ADMIN_PASSWORD}).status_code == 200
    assert client.post("/api/auth/sessions/revoke-others").status_code == 204
    assert other.get("/api/auth/me").status_code == 401 and client.get("/api/auth/me").status_code == 200
    other.close()


def test_audit_log(client, car, make_driver):
    client.post("/api/auth/login", json={"username": "admin", "password": "nope"})
    client.put(f"/api/cars/{car['id']}", json={"name": "Ioniq 5 N"})
    dave = make_driver([car["id"]])
    events = client.get("/api/audit").json()
    actions = [e["action"] for e in events["items"]]
    assert {"setup", "login.failed", "cars.update", "user.created", "login"} <= set(actions)
    upd = next(e for e in events["items"] if e["action"] == "cars.update")
    assert upd["username"] == "admin" and upd["target"] == f"cars:{car['id']}"
    assert client.get("/api/audit?action=login.").json()["total"] >= 2
    assert client.get("/api/audit?username=dave").json()["items"][0]["action"] == "login"
    assert dave.get("/api/audit").status_code == 403
