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


def test_api_tokens_have_their_own_access(client, car):
    other = client.post("/api/cars", json={"name": "Model 3"}).json()
    _session(client, car["id"])
    s_other = _session(client, other["id"]).json()
    # A token with the Driver role, on one car only
    r = client.post("/api/tokens", json={"name": "Home Assistant", "description": "Logs the wall box sessions", "role_id": role_id("Driver"),
                                         "car_ids": [car["id"]], "expires_days": 30})  # fmt: skip
    assert r.status_code == 201, r.text
    tok = r.json()
    raw = tok["token"]
    assert raw.startswith("evc_") and tok["prefix"] == raw[:12] and tok["role_name"] == "Driver" and tok["car_ids"] == [car["id"]]
    assert "sessions:create" in tok["permissions"] and tok["created_by"] == "Ada Admin"
    api = TestClient(app)
    h = {"Authorization": f"Bearer {raw}"}
    assert len(api.get("/api/sessions", headers=h).json()) == 1  # only its car
    assert api.get(f"/api/sessions/{s_other['id']}", headers=h).status_code == 404
    assert _session(api, car["id"]).status_code == 401  # no header: not signed in
    assert api.post("/api/sessions", headers=h, json={"car_id": car["id"], "charge_date": "2026-03-01", "kwh": 5}).status_code == 201
    assert api.post("/api/sessions", headers=h, json={"car_id": other["id"], "charge_date": "2026-03-01", "kwh": 5}).status_code == 403
    assert api.get("/api/users", headers=h).status_code == 403
    assert api.get("/api/tokens", headers=h).status_code == 403  # tokens are managed from a browser
    assert api.get("/api/auth/sessions", headers=h).status_code == 403
    me = api.get("/api/auth/me", headers=h).json()
    assert me["via_token"] and me["car_permissions"] == {str(car["id"]): sorted(me["car_permissions"][str(car["id"])])}
    # Change its access without changing the secret: chosen permissions, all cars, read only
    upd = client.put(f"/api/tokens/{tok['id']}", json={"name": "Home Assistant", "permissions": ["car:view"], "car_ids": None})
    assert upd.status_code == 200 and upd.json()["role_id"] is None and upd.json()["car_ids"] is None
    assert len(api.get("/api/sessions", headers=h).json()) == 3
    assert api.post("/api/sessions", headers=h, json={"car_id": car["id"], "charge_date": "2026-03-01", "kwh": 5}).status_code == 403
    assert client.put(f"/api/tokens/{tok['id']}", json={"name": "x", "permissions": []}).status_code == 422  # no access at all
    assert client.post("/api/tokens", json={"name": "bad", "permissions": ["root"]}).status_code == 422
    assert client.post("/api/tokens", json={"name": "bad", "permissions": ["car:view"], "car_ids": [999]}).status_code == 422
    listed = client.get("/api/tokens").json()
    assert listed[0]["last_used_at"] is not None and "token" not in listed[0]
    assert client.post(f"/api/tokens/{tok['id']}/revoke").json()["revoked"] is True
    assert api.get("/api/sessions", headers=h).status_code == 401
    assert client.delete(f"/api/tokens/{tok['id']}").status_code == 204
    assert {e["action"] for e in client.get("/api/audit?action=token.").json()["items"]} >= {"token.created", "token.updated", "token.revoked", "token.deleted"}
    api.close()


def test_token_rules(client, car, make_driver):
    dave = make_driver([car["id"]])
    assert dave.get("/api/tokens").status_code == 403  # Driver has no tokens:manage
    r = client.post("/api/tokens", json={"name": "ci", "role_id": role_id("Viewer")}).json()
    viewer_role = role_id("Viewer")
    # A role used by a token can't be deleted; custom roles only
    custom = client.post("/api/roles", json={"name": "Robot", "permissions": ["car:view"]}).json()
    client.put(f"/api/tokens/{r['id']}", json={"name": "ci", "role_id": custom["id"]})
    assert client.delete(f"/api/roles/{custom['id']}").status_code == 409
    assert viewer_role
    # The token stops working when its creator is disabled
    other_admin = client.post("/api/users", json={"username": "boss2", "role_id": role_id("Administrator"), "password": "boss-password"}).json()
    boss = TestClient(app)
    boss.post("/api/auth/login", json={"username": "boss2", "password": "boss-password"})
    t2 = boss.post("/api/tokens", json={"name": "boss token", "permissions": ["car:view"]}).json()
    h = {"Authorization": f"Bearer {t2['token']}"}
    assert boss.get("/api/cars", headers=h).status_code == 200
    client.put(f"/api/users/{other_admin['id']}", json={"username": "boss2", "role_id": role_id("Administrator"), "active": False})
    assert TestClient(app).get("/api/cars", headers=h).status_code == 401
    boss.close()


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
