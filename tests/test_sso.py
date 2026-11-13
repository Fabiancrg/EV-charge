"""Single sign-on (OpenID Connect) against a local fake identity provider that behaves like Authelia."""

import asyncio
import base64
import hashlib
import json
import threading
import time
from urllib.parse import parse_qs, urlsplit

import pytest
from aiohttp import web
from conftest import role_id
from fastapi.testclient import TestClient

from ev_charge import sso
from ev_charge.main import app


def _jwt(claims):
    enc = lambda d: base64.urlsafe_b64encode(json.dumps(d).encode()).rstrip(b"=").decode()  # noqa: E731
    return f"{enc({'alg': 'RS256'})}.{enc(claims)}.sig"


class FakeProvider:
    def __init__(self):
        self.base = None
        self.codes = {}  # code → (nonce, challenge, claims)
        self.user = {"sub": "u-123", "preferred_username": "Carla", "email": "carla@example.com", "name": "Carla Racer", "groups": ["users", "ev-admins"]}
        self.token_auth = []

    def app(self):
        a = web.Application()
        a.router.add_get("/.well-known/openid-configuration", self.discovery)
        a.router.add_post("/api/oidc/token", self.token)
        a.router.add_get("/api/oidc/userinfo", self.userinfo)
        return a

    async def discovery(self, request):
        return web.json_response({
            "issuer": self.base, "authorization_endpoint": f"{self.base}/api/oidc/authorization", "token_endpoint": f"{self.base}/api/oidc/token",
            "userinfo_endpoint": f"{self.base}/api/oidc/userinfo", "scopes_supported": ["openid", "profile", "email", "groups"],
        })  # fmt: skip

    def authorize(self, url):
        """What the browser does at the provider: returns the code for the callback."""
        q = {k: v[0] for k, v in parse_qs(urlsplit(url).query).items()}
        assert q["response_type"] == "code" and q["client_id"] == "ev-charge" and q["code_challenge_method"] == "S256"
        code = f"code-{len(self.codes)}"
        self.codes[code] = (q["nonce"], q["code_challenge"], q["redirect_uri"])
        return code, q["state"]

    async def token(self, request):
        form = await request.post()
        self.token_auth.append(request.headers.get("Authorization", ""))
        if request.headers.get("Authorization") != "Basic " + base64.b64encode(b"ev-charge:s3cret").decode():
            return web.json_response({"error": "invalid_client"}, status=401)
        entry = self.codes.pop(form["code"], None)
        if entry is None:
            return web.json_response({"error": "invalid_grant", "error_description": "unknown code"}, status=400)
        nonce, challenge, redirect_uri = entry
        verifier_hash = base64.urlsafe_b64encode(hashlib.sha256(form["code_verifier"].encode()).digest()).rstrip(b"=").decode()
        if verifier_hash != challenge or form["redirect_uri"] != redirect_uri:
            return web.json_response({"error": "invalid_grant", "error_description": "PKCE"}, status=400)
        claims = {"iss": self.base, "aud": ["ev-charge"], "sub": self.user["sub"], "exp": time.time() + 300, "nonce": nonce}
        return web.json_response({"access_token": "at-1", "id_token": _jwt(claims), "token_type": "Bearer"})

    async def userinfo(self, request):
        assert request.headers["Authorization"] == "Bearer at-1"
        return web.json_response(self.user)


@pytest.fixture()
def provider():
    fake = FakeProvider()
    loop = asyncio.new_event_loop()
    runner = web.AppRunner(fake.app())
    ready = threading.Event()

    def serve():
        asyncio.set_event_loop(loop)
        loop.run_until_complete(runner.setup())
        site = web.TCPSite(runner, "127.0.0.1", 0)
        loop.run_until_complete(site.start())
        fake.base = f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}"
        ready.set()
        loop.run_forever()

    threading.Thread(target=serve, daemon=True).start()
    ready.wait(5)
    sso._discovery.clear()
    yield fake
    asyncio.run_coroutine_threadsafe(runner.cleanup(), loop).result(5)
    loop.call_soon_threadsafe(loop.stop)


def _configure(client, provider, **extra):
    body = {"enabled": True, "name": "Authelia", "issuer": provider.base, "client_id": "ev-charge", "client_secret": "s3cret",
            "default_role_id": role_id("Viewer"), "group_roles": [{"group": "ev-admins", "role_id": role_id("Accountant")}]} | extra  # fmt: skip
    r = client.put("/api/settings/sso", json=body)
    assert r.status_code == 200, r.text
    return r.json()


def _sso_login(provider, next_url="/sessions"):
    browser = TestClient(app, follow_redirects=False)
    r = browser.get(f"/auth/sso/login?next={next_url}")
    assert r.status_code == 303 and r.headers["location"].startswith(provider.base + "/api/oidc/authorization?")
    code, state = provider.authorize(r.headers["location"])
    return browser, browser.get(f"/auth/sso/callback?code={code}&state={state}")


def test_settings_and_test_button(client, provider):
    assert client.put("/api/settings/sso", json={"enabled": True, "issuer": provider.base}).status_code == 422  # client id/secret missing
    cfg = _configure(client, provider)
    assert cfg["has_secret"] and "client_secret" not in cfg and cfg["callback_url"].endswith("/auth/sso/callback")
    assert client.post("/api/settings/sso/test", json={"issuer": provider.base}).json()["token_endpoint"].endswith("/api/oidc/token")
    assert client.post("/api/settings/sso/test", json={"issuer": "http://127.0.0.1:9"}).status_code == 422
    assert client.get("/api/auth/sso").json() == {"enabled": True, "name": "Authelia", "local_login": True}
    # Turning password sign-in off is refused until the admin has used single sign-on once (no lock-out)
    assert client.put("/api/settings/sso", json={"enabled": True, "issuer": provider.base, "client_id": "ev-charge", "local_login": False}).status_code == 409


def test_sso_creates_the_user_with_the_group_role(client, provider):
    _configure(client, provider)
    browser, r = _sso_login(provider)
    assert r.status_code == 303 and r.headers["location"] == "/sessions", r.headers.get("location")
    me = browser.get("/api/auth/me").json()
    assert me["username"] == "carla" and me["full_name"] == "Carla Racer" and me["role_name"] == "Accountant" and me["sso"] and not me["has_password"]
    assert browser.get("/api/auth/sessions").json()[0]["method"] == "sso"
    # Next sign-in: same account; without the group, the role stays as it is
    provider.user["groups"] = ["users"]
    browser2, r = _sso_login(provider)
    assert browser2.get("/api/auth/me").json()["id"] == me["id"]
    actions = [e["action"] for e in client.get("/api/audit?action=user.").json()["items"]]
    assert "user.sso_created" in actions
    # The new user has no password: password sign-in is impossible
    assert client.post("/api/auth/login", json={"username": "carla", "password": "anything"}).status_code == 401
    browser.close()
    browser2.close()


def test_sso_links_an_existing_account_and_respects_auto_create(client, provider):
    _configure(client, provider, auto_create=False, group_roles=[])
    provider.user = {"sub": "u-9", "preferred_username": "someone", "email": "x@example.com", "groups": []}
    browser, r = _sso_login(provider)
    assert r.status_code == 303 and r.headers["location"].startswith("/login?error=") and "No%20EV%20Charge%20account" in r.headers["location"]
    # The admin account is linked by username
    provider.user = {"sub": "u-admin", "preferred_username": "Admin", "groups": []}
    browser, r = _sso_login(provider, "/")
    assert browser.get("/api/auth/me").json()["username"] == "admin"
    assert browser.get("/api/users").json()[0]["sso"] is True
    # Now password sign-in can be turned off: only single sign-on remains
    assert browser.put("/api/settings/sso", json={"enabled": True, "issuer": provider.base, "client_id": "ev-charge", "local_login": False}).status_code == 200
    anon = TestClient(app)
    assert anon.post("/api/auth/login", json={"username": "admin", "password": "admin-password"}).status_code == 403
    assert 'id="sso-btn"' in anon.get("/login").text
    anon.close()
    browser.close()


def test_sso_rejects_bad_states(client, provider):
    _configure(client, provider)
    browser = TestClient(app, follow_redirects=False)
    r = browser.get("/auth/sso/login")
    code, state = provider.authorize(r.headers["location"])
    bad = browser.get(f"/auth/sso/callback?code={code}&state=forged")
    assert bad.headers["location"].startswith("/login?error=") and "state" in bad.headers["location"]
    stranger = TestClient(app, follow_redirects=False)  # no flow cookie (e.g. a link sent by someone else)
    assert "cookies" in stranger.get(f"/auth/sso/callback?code={code}&state={state}").headers["location"]
    denied = browser.get("/auth/sso/callback?error=access_denied&error_description=User%20denied")
    assert "User%20denied" in denied.headers["location"]
    assert client.get("/api/audit?action=login.failed").json()["total"] == 3
    browser.close()
    stranger.close()


def test_redirect_uri_must_be_the_callback_address(client):
    """Any other path would send users to a page that doesn't exist after the sign-in ("Not Found")."""
    cfg = client.get("/api/settings/sso").json()
    assert cfg["default_callback_url"] == "http://testserver/auth/sso/callback" == cfg["callback_url"]
    for bad in ("https://ev.example.com/oauth/callback", "https://ev.example.com", "ev.example.com/auth/sso/callback", "https://ev.example.com/auth/sso/callback?x=1"):
        r = client.put("/api/settings/sso", json={"redirect_uri": bad})
        assert r.status_code == 422 and "/auth/sso/callback" in r.json()["detail"], bad
    cfg = client.put("/api/settings/sso", json={"redirect_uri": " https://ev.example.com/auth/sso/callback "}).json()
    assert cfg["redirect_uri"] == cfg["callback_url"] == "https://ev.example.com/auth/sso/callback"
    assert cfg["default_callback_url"] == "http://testserver/auth/sso/callback"  # what an empty field would use
    assert client.put("/api/settings/sso", json={"redirect_uri": ""}).json()["callback_url"] == "http://testserver/auth/sso/callback"
    assert client.put("/api/settings/sso", json={"redirect_uri": None, "issuer": None}).json()["redirect_uri"] == ""  # a field emptied in the form
