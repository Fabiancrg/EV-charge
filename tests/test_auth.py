from conftest import role_id
from fastapi.testclient import TestClient

from ev_charge.auth import hash_password, verify_password
from ev_charge.main import app

ADMIN_PASSWORD = "admin-password"  # see conftest.py


def test_password_hashing():
    h = hash_password("correct horse")
    assert h.startswith("scrypt$") and verify_password("correct horse", h)
    assert not verify_password("wrong", h) and not verify_password("x", "garbage")


def test_first_run_setup(anon):
    assert anon.get("/api/auth/setup").json() == {"needed": True}
    r = anon.get("/", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/setup"
    assert anon.get("/api/cars").status_code == 401
    assert anon.post("/api/auth/setup", json={"username": "admin", "password": "short"}).status_code == 422
    assert anon.post("/api/auth/setup", json={"username": "Admin", "password": ADMIN_PASSWORD}).status_code == 201
    me = anon.get("/api/auth/me").json()
    assert me["role_name"] == "Administrator" and "users:manage" in me["permissions"] and "sessions:edit" in me["all_cars"]
    assert anon.post("/api/auth/setup", json={"username": "other", "password": ADMIN_PASSWORD}).status_code == 409  # only once


def test_login_logout_and_redirects(client):
    assert client.post("/api/auth/logout").status_code == 204
    assert client.get("/api/auth/me").status_code == 401
    r = client.get("/sessions?car_id=1", follow_redirects=False)
    assert r.headers["location"] == "/login?next=/sessions%3Fcar_id%3D1"
    assert client.post("/api/auth/login", json={"username": "admin", "password": "nope"}).status_code == 401
    assert client.post("/api/auth/login", json={"username": " ADMIN ", "password": ADMIN_PASSWORD}).status_code == 200
    assert client.get("/login", follow_redirects=False).headers["location"] == "/"


def test_login_throttling(client):
    for _ in range(8):
        client.post("/api/auth/login", json={"username": "admin", "password": "nope"})
    assert client.post("/api/auth/login", json={"username": "admin", "password": ADMIN_PASSWORD}).status_code == 429


def test_change_password_signs_out_other_browsers(client):
    other = TestClient(app)
    assert other.post("/api/auth/login", json={"username": "admin", "password": ADMIN_PASSWORD}).status_code == 200
    assert client.post("/api/auth/password", json={"current_password": "bad", "new_password": "new-password"}).status_code == 422
    assert client.post("/api/auth/password", json={"current_password": ADMIN_PASSWORD, "new_password": "new-password"}).status_code == 204
    assert client.get("/api/auth/me").status_code == 200  # this browser stays signed in
    assert other.get("/api/auth/me").status_code == 401
    other.close()


def test_cross_site_writes_refused(client):
    r = client.post("/api/cars", json={"name": "X"}, headers={"Origin": "https://evil.example"})
    assert r.status_code == 403
    assert client.post("/api/cars", json={"name": "X"}, headers={"Origin": "http://testserver"}).status_code == 201


def test_user_management(client, car):
    driver, admin_role = role_id("Driver"), role_id("Administrator")
    body = {"username": "Dave", "role_id": driver, "password": "driver-password", "grants": [{"car_id": car["id"], "role_id": driver}]}
    r = client.post("/api/users", json=body)
    assert r.status_code == 201 and r.json()["username"] == "dave" and r.json()["grants"] == [{"car_id": car["id"], "role_id": driver}]
    assert r.json()["role_name"] == "Driver" and r.json()["has_password"]
    assert client.post("/api/users", json={"username": "dave", "password": "driver-password"}).status_code == 409
    sso_only = client.post("/api/users", json={"username": "eve"})  # no password: single sign-on only
    assert sso_only.status_code == 201 and not sso_only.json()["has_password"]
    assert client.post("/api/auth/login", json={"username": "eve", "password": ""}).status_code in (401, 422)
    assert client.post("/api/users", json={"username": "x1", "role_id": 999}).status_code == 422
    assert client.get(f"/api/cars/{car['id']}").json()["drivers"] == ["dave (Driver)"]
    users = client.get("/api/users").json()
    admin = next(u for u in users if u["username"] == "admin")
    # The last user who can manage users can't be demoted, disabled or deleted
    assert client.put(f"/api/users/{admin['id']}", json={"username": "admin", "role_id": driver}).status_code == 409
    assert client.put(f"/api/users/{admin['id']}", json={"username": "admin", "role_id": admin_role, "active": False}).status_code == 409
    assert client.delete(f"/api/users/{admin['id']}").status_code == 409
    # Disabling a user signs them out
    dave_client = TestClient(app)
    assert dave_client.post("/api/auth/login", json={"username": "dave", "password": "driver-password"}).status_code == 200
    dave = r.json()
    assert client.put(f"/api/users/{dave['id']}", json={"username": "dave", "role_id": driver, "active": False}).status_code == 200
    assert dave_client.get("/api/auth/me").status_code == 401
    assert dave_client.post("/api/auth/login", json={"username": "dave", "password": "driver-password"}).status_code == 401
    dave_client.close()
    assert client.delete(f"/api/users/{dave['id']}").status_code == 204
