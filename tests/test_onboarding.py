"""Sign-up requests (e-mail confirmed, approved by an administrator) and invitations to a group."""

from datetime import UTC, datetime, timedelta

import pytest
from conftest import role_id
from fastapi.testclient import TestClient

from ev_charge import auth
from ev_charge.database import SessionLocal
from ev_charge.main import app
from ev_charge.models import Invitation, SignupRequest

PASSWORD = "member-password"
FORM = {"full_name": "Greg Colleague", "email": "Greg@Example.org", "group_name": "Colleagues", "message": "We share two cars"}


@pytest.fixture()
def anyone():
    """A browser that isn't signed in."""
    with TestClient(app, base_url="https://ev.example.com") as c:
        yield c


@pytest.fixture()
def open_signup(client, mailbox):
    assert client.put("/api/signup/settings", json={"enabled": True, "default_max_members": 4}).json()["open"] is True
    return mailbox


def test_signup_is_closed_until_an_administrator_opens_it(client, anyone, make_driver, car):
    assert anyone.get("/api/signup/state").json() == {"enabled": False}
    assert anyone.post("/api/signup", json=FORM).status_code == 403
    # It needs e-mail, and an administrator
    r = client.put("/api/signup/settings", json={"enabled": True})
    assert r.status_code == 422 and "SMTP" in r.json()["detail"]
    assert make_driver([car["id"]]).put("/api/signup/settings", json={"enabled": False}).status_code == 403
    for url in ("/api/signup/requests", "/api/signup/settings"):
        assert anyone.get(url).status_code == 401
    assert "Request access" not in anyone.get("/login").text and anyone.get("/signup", follow_redirects=False).status_code == 303


def test_request_confirm_approve_and_first_sign_in(client, anyone, open_signup):
    box = open_signup
    assert anyone.get("/api/signup/state").json() == {"enabled": True} and "Request access" in anyone.get("/login").text
    assert anyone.get("/signup").status_code == 200
    for bad in ({"email": "not-an-address"}, {"full_name": " "}, {"group_name": ""}):
        assert anyone.post("/api/signup", json=FORM | bad).status_code == 422

    # The request: an e-mail with a link on the public address of the site, and nothing for the administrator yet
    assert anyone.post("/api/signup", json=FORM).status_code == 202
    to, subject, text = box.messages[-1]
    assert (to, subject) == ("greg@example.org", "Confirm your e-mail address") and "Colleagues" in text
    assert box.link().startswith("https://ev.example.com/signup/verify?token=")
    (waiting,) = client.get("/api/signup/requests").json()
    assert (waiting["status"], waiting["email"], waiting["message"]) == ("unverified", "greg@example.org", "We share two cars")
    assert client.post(f"/api/signup/requests/{waiting['id']}/approve", json={"username": "greg", "group_name": "Colleagues"}).status_code == 409  # not confirmed

    # Asking again replaces the link; the address is confirmed with the newest one only
    first = box.token()
    anyone.post("/api/signup", json=FORM | {"group_name": "The colleagues"})
    assert len(client.get("/api/signup/requests").json()) == 1
    assert anyone.post("/api/signup/verify", json={"token": first}).status_code == 404
    assert anyone.post("/api/signup/verify", json={"token": "x" * 30}).status_code == 404
    assert anyone.get(f"/signup/verify?token={box.token()}").status_code == 200  # the page of the link
    done = anyone.post("/api/signup/verify", json={"token": box.token()})
    assert done.status_code == 200 and done.json()["status"] == "pending"
    assert anyone.post("/api/signup/verify", json={"token": box.token()}).json()["status"] == "pending"  # clicking twice is fine
    assert anyone.post("/api/signup", json=FORM).status_code == 202 and len(box.messages) == 2  # already waiting: no new e-mail

    # The administrator approves, choosing the username, the group's name and its size
    (pending,) = client.get("/api/signup/requests").json()
    assert (pending["status"], pending["group_name"]) == ("pending", "The colleagues")
    url = f"/api/signup/requests/{pending['id']}"
    assert client.post(f"{url}/approve", json={"username": "admin", "group_name": "Colleagues"}).status_code == 409  # username taken
    assert client.post(f"{url}/approve", json={"username": "greg", "group_name": "default"}).status_code == 409  # group name taken
    approved = client.post(f"{url}/approve", json={"username": "Greg", "group_name": "Colleagues", "max_members": 3}, headers={"Origin": "http://testserver"})
    assert approved.status_code == 200 and (approved.json()["status"], approved.json()["decided_by"]) == ("approved", "Ada Admin")
    assert client.post(f"{url}/approve", json={"username": "greg2", "group_name": "Other"}).status_code == 409  # decided already
    group = next(g for g in client.get("/api/groups").json() if g["name"] == "Colleagues")
    assert (group["max_members"], group["members"], group["managers"]) == (3, 1, ["Greg Colleague"])
    to, subject, text = box.messages[-1]
    assert (to, subject) == ("greg@example.org", "Your EV Charge access is ready") and "username is greg" in text and "/invite?token=" in text

    # The link: only a password to choose, then signed in as the manager of the new group
    token = box.token()
    seen = anyone.get(f"/api/invitations/open?token={token}").json()
    assert (seen["group_name"], seen["username"], seen["password_login"]) == ("Colleagues", "greg", True)
    assert anyone.get(f"/invite?token={token}").status_code == 200
    assert anyone.post("/api/invitations/accept", json={"token": token, "password": "short"}).status_code == 422
    me = anyone.post("/api/invitations/accept", json={"token": token, "password": PASSWORD})
    assert me.status_code == 200 and (me.json()["username"], me.json()["group_name"], me.json()["manages_group"]) == ("greg", "Colleagues", True)
    assert anyone.get("/api/auth/me").json()["username"] == "greg"  # signed in
    assert anyone.post("/api/cars", json={"name": "Model Y"}).status_code == 201
    assert anyone.post("/api/invitations/accept", json={"token": token, "password": PASSWORD}).status_code == 404  # once only
    # Someone who already has an account gets the same answer, and no e-mail
    count = len(box.messages)
    assert TestClient(app).post("/api/signup", json=FORM).status_code == 202 and len(box.messages) == count


def test_reject_expiry_and_request_limit(client, anyone, open_signup):
    box = open_signup
    anyone.post("/api/signup", json=FORM)
    anyone.post("/api/signup/verify", json={"token": box.token()})
    (pending,) = client.get("/api/signup/requests").json()
    rejected = client.post(f"/api/signup/requests/{pending['id']}/reject").json()
    assert rejected["status"] == "rejected" and box.messages[-1][1] == "Your EV Charge request"
    assert client.get("/api/groups").json()[0]["name"] == "Default" and len(client.get("/api/groups").json()) == 1
    assert client.delete(f"/api/signup/requests/{pending['id']}").status_code == 204

    # A link not clicked in time: refused, and the request disappears from the administrator's list
    anyone.post("/api/signup", json=FORM | {"email": "late@example.org"})
    with SessionLocal() as db:
        req = db.query(SignupRequest).one()
        req.created_at = datetime.now(UTC) - timedelta(hours=49)
        db.commit()
    assert anyone.post("/api/signup/verify", json={"token": box.token("late@example.org")}).status_code == 410
    assert client.get("/api/signup/requests").json() == []

    # No more than a few requests from one address in a quarter of an hour
    auth._FAILS.clear()
    codes = [anyone.post("/api/signup", json=FORM | {"email": f"spam{n}@example.org"}).status_code for n in range(7)]
    assert codes == [202] * 5 + [429] * 2


@pytest.fixture()
def team(client, mailbox, car):
    """A second group of at most 3 members, with its manager Greg and a car."""
    group = client.post("/api/groups", json={"name": "Colleagues", "max_members": 3}).json()
    client.post("/api/users", json={"username": "greg", "password": PASSWORD, "group_id": group["id"], "group_manager": True, "full_name": "Greg Colleague"})
    greg = TestClient(app, base_url="https://ev.example.com")
    assert greg.post("/api/auth/login", json={"username": "greg", "password": PASSWORD}).status_code == 200
    mine = greg.post("/api/cars", json={"name": "Model Y"}).json()
    yield {"admin": client, "greg": greg, "group": group, "car": mine, "box": mailbox, "other_car": car}
    greg.close()


def test_a_manager_invites_a_member(team, anyone):
    greg, box, driver = team["greg"], team["box"], role_id("Driver")
    body = {"email": "Mia@Example.org", "full_name": "Mia Member", "grants": [{"car_id": team["car"]["id"], "role_id": driver}]}
    assert greg.post("/api/invitations", json=body | {"grants": [{"car_id": team["other_car"]["id"], "role_id": driver}]}).status_code == 422  # a car of another group
    assert greg.post("/api/invitations", json=body | {"email": "nope"}).status_code == 422
    sent = greg.post("/api/invitations", json=body | {"group_id": 1}, headers={"Origin": "https://ev.example.com"})  # the group can't be chosen: their own
    assert sent.status_code == 201 and (sent.json()["group_name"], sent.json()["email"], sent.json()["invited_by"]) == ("Colleagues", "mia@example.org", "Greg Colleague")
    to, subject, text = box.messages[-1]
    assert (to, subject) == ("mia@example.org", "Join “Colleagues” on EV Charge") and "Greg Colleague invites you" in text
    assert box.link().startswith("https://ev.example.com/invite?token=")
    assert [i["email"] for i in greg.get("/api/invitations").json()] == ["mia@example.org"]

    # The invited person chooses a username and a password, and lands in the group with the role given
    token = box.token()
    seen = anyone.get(f"/api/invitations/open?token={token}").json()
    assert (seen["group_name"], seen["email"], seen["username"], seen["invited_by"]) == ("Colleagues", "mia@example.org", None, "Greg Colleague")
    assert anyone.post("/api/invitations/accept", json={"token": token, "password": PASSWORD}).status_code == 422  # no username
    assert anyone.post("/api/invitations/accept", json={"token": token, "username": "greg", "password": PASSWORD}).status_code == 409
    me = anyone.post("/api/invitations/accept", json={"token": token, "username": "Mia", "password": PASSWORD}).json()
    assert (me["username"], me["full_name"], me["email"], me["group_name"], me["manages_group"]) == ("mia", "Mia Member", "mia@example.org", "Colleagues", False)
    assert me["car_permissions"] == {str(team["car"]["id"]): sorted(["car:view", "sessions:create", "sessions:edit", "sessions:import", "reimbursement:manage"])}
    assert [c["name"] for c in anyone.get("/api/cars").json()] == ["Model Y"] and anyone.get("/api/invitations").status_code == 403
    assert greg.get("/api/invitations").json() == [] and [u["username"] for u in greg.get("/api/users").json()] == ["greg", "mia"]
    assert greg.post("/api/invitations", json=body).status_code == 409  # this address has an account now


def test_invitations_count_in_the_group_limit(team, anyone):
    greg, admin, box = team["greg"], team["admin"], team["box"]
    assert greg.post("/api/invitations", json={"email": "a@example.org"}).status_code == 201
    assert greg.post("/api/invitations", json={"email": "b@example.org"}).status_code == 201
    full = greg.post("/api/invitations", json={"email": "c@example.org"})
    assert full.status_code == 409 and "limited to 3 member(s), invitations included" in full.json()["detail"]
    assert greg.post("/api/users", json={"username": "direct", "password": PASSWORD}).status_code == 201  # a place is only taken once accepted…
    # …so the last one to accept finds the group full
    first, second = box.token("a@example.org"), box.token("b@example.org")
    assert anyone.post("/api/invitations/accept", json={"token": first, "username": "anna", "password": PASSWORD}).status_code == 200
    late = TestClient(app).post("/api/invitations/accept", json={"token": second, "username": "bob", "password": PASSWORD})
    assert late.status_code == 409 and "limited to 3" in late.json()["detail"]
    assert "bob" not in [u["username"] for u in admin.get("/api/users").json()]

    # Inviting the same address again replaces its link without taking another place; a new link, a withdrawal
    admin.put(f"/api/groups/{team['group']['id']}", json={"name": "Colleagues", "max_members": 5})
    again = greg.post("/api/invitations", json={"email": "b@example.org", "full_name": "Bob"}).json()
    assert len(greg.get("/api/invitations").json()) == 1 and TestClient(app).get(f"/api/invitations/open?token={second}").status_code == 404
    old = box.token("b@example.org")
    assert greg.post(f"/api/invitations/{again['id']}/resend").status_code == 200 and box.token("b@example.org") != old
    assert TestClient(app).get(f"/api/invitations/open?token={old}").status_code == 404
    with SessionLocal() as db:  # past its validity
        inv = db.get(Invitation, again["id"])
        inv.expires_at = datetime.now(UTC) - timedelta(minutes=1)
        db.commit()
    assert TestClient(app).get(f"/api/invitations/open?token={box.token('b@example.org')}").status_code == 410
    assert greg.get("/api/invitations").json()[0]["expired"] is True
    assert greg.delete(f"/api/invitations/{again['id']}").status_code == 204 and greg.get("/api/invitations").json() == []


def test_invitations_stay_in_their_group(team, make_driver):
    greg, admin = team["greg"], team["admin"]
    mine = greg.post("/api/invitations", json={"email": "a@example.org"}).json()
    home = admin.post("/api/invitations", json={"email": "cousin@example.org"}).json()  # the administrator's own group by default
    assert home["group_name"] == "Default"
    elsewhere = admin.post("/api/invitations", json={"email": "z@example.org", "group_id": team["group"]["id"]}).json()
    assert elsewhere["group_name"] == "Colleagues"
    assert {i["email"] for i in greg.get("/api/invitations").json()} == {"a@example.org", "z@example.org"}
    assert len(admin.get("/api/invitations").json()) == 3
    assert greg.delete(f"/api/invitations/{home['id']}").status_code == 404 and greg.post(f"/api/invitations/{home['id']}/resend").status_code == 404
    member = make_driver([team["other_car"]["id"]])
    assert member.post("/api/invitations", json={"email": "x@example.org"}).status_code == 403 and member.get("/api/invitations").status_code == 403
    assert admin.delete(f"/api/invitations/{mine['id']}").status_code == 204


def test_invitations_need_email_settings(client, car):
    r = client.post("/api/invitations", json={"email": "a@example.org"})
    assert r.status_code == 422 and "SMTP" in r.json()["detail"]
