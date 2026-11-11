"""The Easee, go-e and NRGkick connectors against local fakes of their APIs (same paths, payloads and auth)."""

import asyncio
import threading
from datetime import datetime

import pytest
from aiohttp import BasicAuth, web

from ev_charge.connectors import CONNECTORS, ConnectorError, make_connector
from ev_charge.connectors.easee import EaseeConnector
from ev_charge.connectors.goe import GoeCloudConnector, GoeLocalConnector
from ev_charge.connectors.nrgkick import NrgkickConnector


@pytest.fixture()
def serve():
    """Run fake APIs on local ports, each in its own thread and event loop; returns their base URL."""
    started = []

    def start(app: web.Application) -> str:
        loop = asyncio.new_event_loop()
        runner = web.AppRunner(app)
        ready = threading.Event()
        port = {}

        def run_server():
            asyncio.set_event_loop(loop)
            loop.run_until_complete(runner.setup())
            site = web.TCPSite(runner, "127.0.0.1", 0)
            loop.run_until_complete(site.start())
            port["n"] = site._server.sockets[0].getsockname()[1]
            ready.set()
            loop.run_forever()

        threading.Thread(target=run_server, daemon=True).start()
        ready.wait(5)
        started.append((runner, loop))
        return f"http://127.0.0.1:{port['n']}"

    yield start
    for runner, loop in started:
        asyncio.run_coroutine_threadsafe(runner.cleanup(), loop).result(5)
        loop.call_soon_threadsafe(loop.stop)


def run(coro):
    return asyncio.run(coro)


async def use(conn, *calls):
    """Call the connector's methods in turn (name, or (name, args…)) and close it."""
    try:
        return [await (getattr(conn, c)() if isinstance(c, str) else getattr(conn, c[0])(*c[1:])) for c in calls]
    finally:
        await conn.close()


def test_registry_lists_the_new_connectors(client):
    assert {"easee", "goe", "goe_cloud", "nrgkick"} <= set(CONNECTORS)
    listed = {k["id"]: k for k in client.get("/api/chargers/connectors").json()}
    assert [listed[k]["history"] for k in ("zaptec", "easee", "goe", "goe_cloud", "nrgkick")] == [True, True, False, False, False]
    assert [f["name"] for f in listed["goe_cloud"]["fields"] if f["secret"]] == ["token"]
    for kind, config in (("easee", {}), ("goe", {}), ("goe_cloud", {"serial": "12", "token": ""}), ("goe_cloud", {"serial": "abc", "token": "t"}), ("nrgkick", {})):
        with pytest.raises(ConnectorError):
            make_connector(kind, config)


# ── go-e ─────────────────────────────────────────────────────────────────────


class FakeGoe:
    """/api/status of the HTTP API v2; with a token it plays the cloud (403 without it, 404 when the charger is silent)."""

    def __init__(self, token=None):
        self.token = token
        self.silent = False
        self.queries = []
        # nrg: U (L1, L2, L3, N), I (L1, L2, L3), P (L1, L2, L3, N, Total), pf (L1, L2, L3, N)
        self.status = {"car": 2, "nrg": [229, 230, 231, 1, 16, 16, 16, 3660, 3680, 3700, 0, 11040, 99, 99, 99, 0], "wh": 5321.5, "err": 0}
        self.status |= {"sse": "012345", "fna": "Garage", "typ": "go-eCharger_V4", "fwv": "057.1"}

    def app(self):
        app = web.Application()
        app.router.add_get("/api/status", self.get_status)
        return app

    async def get_status(self, request):
        self.queries.append(dict(request.query))
        if self.token is not None:
            if request.headers.get("Authorization") != f"Bearer {self.token}":
                raise web.HTTPForbidden()
            if self.silent:
                raise web.HTTPNotFound()
        wanted = request.query["filter"].split(",")
        return web.json_response({k: v for k, v in self.status.items() if k in wanted})


def test_goe_local(serve):
    fake = FakeGoe()
    base = serve(fake.app())

    async def go():
        conn = GoeLocalConnector({"host": base + "/"})
        try:
            info, charging = await conn.describe(), await conn.live()
            fake.status |= {"car": 4, "nrg": fake.status["nrg"][:11] + [0], "wh": 9000}
            finished = await conn.live()
            fake.status |= {"car": 1}  # idle: the energy of the last session isn't a new session
            idle = await conn.live()
            fake.status |= {"car": 3, "wh": 0}
            waiting = await conn.live()
            fake.status |= {"car": 5}
            error = await conn.live()
            return info, charging, finished, idle, waiting, error
        finally:
            await conn.close()

    info, charging, finished, idle, waiting, error = run(go())
    assert info == {"serial": "012345", "model": "go-eCharger_V4", "firmware": "057.1", "name": "Garage"}
    assert (charging.status, charging.power_w, charging.energy_wh) == ("charging", 11040.0, 5321.5)
    assert (finished.status, finished.power_w, finished.energy_wh) == ("finished", 0.0, 9000.0)
    assert (idle.status, idle.energy_wh) == ("unplugged", 0.0)
    assert (waiting.status, error.status, error.online) == ("plugged_in", "offline", False)
    assert fake.queries[0]["filter"].startswith("car,nrg,wh")

    with pytest.raises(ConnectorError, match="no charging history"):
        run(use(GoeLocalConnector({"host": base}), ("history", datetime(2026, 1, 1), datetime(2026, 2, 1))))
    with pytest.raises(ConnectorError, match="Cannot read the go-e charger at http://127.0.0.1:9"):
        run(use(GoeLocalConnector({"host": "127.0.0.1:9"}), "describe"))  # nothing listens there: an address without http://


def test_goe_local_not_a_goe_charger(serve):
    async def other(request):
        return web.json_response({"hello": "world"})

    app = web.Application()
    app.router.add_get("/api/status", other)
    with pytest.raises(ConnectorError, match="isn't the API v2 status"):
        run(use(GoeLocalConnector({"host": serve(app)}), "describe"))


def test_goe_cloud(serve, monkeypatch):
    fake = FakeGoe(token="secret-token")
    base = serve(fake.app())
    assert GoeCloudConnector({"serial": "12345", "token": "t"})._url() == "https://012345.api.v3.go-e.io"  # padded to 6 digits
    monkeypatch.setattr(GoeCloudConnector, "cloud_url", base)

    async def reads():
        conn = GoeCloudConnector({"serial": "12345", "token": "secret-token"})
        try:
            info, charging = await conn.describe(), await conn.live()
            fake.silent = True  # the charger sent nothing in the last 30 seconds
            return info, charging, await conn.live()
        finally:
            await conn.close()

    info, charging, silent = run(reads())
    assert info["serial"] == "012345" and (charging.status, charging.energy_wh) == ("charging", 5321.5)
    assert (silent.status, silent.online) == ("offline", False)
    with pytest.raises(ConnectorError, match="offline"):
        run(use(GoeCloudConnector({"serial": "12345", "token": "secret-token"}), "describe"))
    with pytest.raises(ConnectorError, match="check the serial number and the token"):
        run(use(GoeCloudConnector({"serial": "12345", "token": "wrong"}), "live"))


# ── NRGkick ──────────────────────────────────────────────────────────────────


class FakeNrgkick:
    def __init__(self):
        self.enabled = True
        self.auth = None  # (user, password) when the JSON API is protected
        self.queries = []
        self.values = {"general": {"status": 3, "error_code": 0}, "energy": {"charged_energy": 4321, "total_charged_energy": 987654}, "powerflow": {"total_active_power": 7360}}

    def app(self):
        app = web.Application()
        app.router.add_get("/info", self.info)
        app.router.add_get("/values", self.get_values)
        return app

    def _check(self, request):
        if not self.enabled:
            raise web.HTTPOk(text='{"Response":"API must be enabled within the NRGkick App"}', content_type="application/json")
        if self.auth and request.headers.get("Authorization") != BasicAuth(*self.auth).encode():
            raise web.HTTPUnauthorized()
        self.queries.append((request.path, dict(request.query)))

    async def info(self, request):
        self._check(request)
        return web.json_response({"general": {"serial_number": "NRG-123456", "device_name": "NRGkick van", "model_type": "NRGkick Gen2 16A"}, "versions": {"sw_sm": "4.0.0.0"}})

    async def get_values(self, request):
        self._check(request)
        return web.json_response(self.values)


def test_nrgkick(serve):
    fake = FakeNrgkick()
    base = serve(fake.app())

    async def go():
        conn = NrgkickConnector({"host": base})
        try:
            info, charging = await conn.describe(), await conn.live()
            fake.values["general"]["status"], fake.values["powerflow"]["total_active_power"] = 2, 0
            connected = await conn.live()
            fake.values["general"]["status"] = 1  # standby: the counter still shows the last session
            standby = await conn.live()
            fake.values["general"]["status"] = 6
            error = await conn.live()
            return info, charging, connected, standby, error
        finally:
            await conn.close()

    info, charging, connected, standby, error = run(go())
    assert info == {"serial": "NRG-123456", "model": "NRGkick Gen2 16A", "name": "NRGkick van"}
    assert (charging.status, charging.power_w, charging.energy_wh) == ("charging", 7360.0, 4321.0)
    assert (connected.status, connected.energy_wh) == ("plugged_in", 4321.0)
    assert (standby.status, standby.energy_wh, error.status) == ("unplugged", 0.0, "offline")
    assert ("/values", {"raw": "1", "general": "1", "energy": "1", "powerflow": "1"}) in fake.queries  # numeric status codes

    fake.auth = ("admin", "pw")
    with pytest.raises(ConnectorError, match="refused the user name or password"):
        run(use(NrgkickConnector({"host": base}), "describe"))
    assert run(use(NrgkickConnector({"host": base, "username": "admin", "password": "pw"}), "describe"))[0]["serial"] == "NRG-123456"
    fake.auth, fake.enabled = None, False
    with pytest.raises(ConnectorError, match="JSON API of the NRGkick is off"):
        run(use(NrgkickConnector({"host": base}), "describe"))
    with pytest.raises(ConnectorError, match="no charging history"):
        run(use(NrgkickConnector({"host": base}), ("history", datetime(2026, 1, 1), datetime(2026, 2, 1))))

    # Taken away from home: connecting it reports the problem, following it just shows it offline
    away = {"host": "127.0.0.1:9"}
    with pytest.raises(ConnectorError, match="Cannot reach the NRGkick"):
        run(use(NrgkickConnector(away), "describe"))
    (reading,) = run(use(NrgkickConnector(away), "live"))
    assert (reading.status, reading.online) == ("offline", False)


# ── Easee ────────────────────────────────────────────────────────────────────


class FakeEasee:
    def __init__(self):
        self.chargers = [{"id": "EH123456", "name": "Garage", "productCode": 1}]
        self.observations = {109: 3, 120: 7.36, 121: 12.345}
        self.logins = self.refreshes = 0
        self.expires_in = 3600
        self.expire_next = False
        self.session_paths = []
        self.sessions = [
            {"carConnected": "2026-10-05T19:58:12Z", "carDisconnected": "2026-10-05T23:41:03Z", "kiloWattHours": 24.512, "sessionId": 77},
            {"carConnected": "2026-10-09T05:00:00", "carDisconnected": "2026-10-09T06:30:00", "kiloWattHours": 9.75},
            {"carConnected": "2026-10-12T18:00:00Z", "carDisconnected": None, "kiloWattHours": 3.1},  # still running
            {"carConnected": "2026-10-11T18:00:00Z", "carDisconnected": "2026-10-11T18:01:00Z", "kiloWattHours": 0},  # plugged, nothing charged
        ]

    def app(self):
        app = web.Application()
        app.router.add_post("/api/accounts/login", self.login)
        app.router.add_post("/api/accounts/refresh_token", self.refresh)
        app.router.add_get("/api/chargers", self.list_chargers)
        app.router.add_get("/state/{id}/observations", self.get_observations)
        app.router.add_get("/api/sessions/charger/{id}/sessions/{start}/{end}", self.get_sessions)
        return app

    def _tokens(self):
        return web.json_response(
            {"accessToken": f"A{self.logins}.{self.refreshes}", "refreshToken": f"R{self.logins}.{self.refreshes}", "expiresIn": self.expires_in, "tokenType": "Bearer"}
        )

    async def login(self, request):
        body = await request.json()
        if body != {"userName": "me@example.com", "password": "pw"}:
            return web.json_response({"errorCode": 100, "title": "Invalid credentials"}, status=400)
        self.logins += 1
        return self._tokens()

    async def refresh(self, request):
        body = await request.json()
        if body != {"accessToken": f"A{self.logins}.{self.refreshes}", "refreshToken": f"R{self.logins}.{self.refreshes}"}:
            raise web.HTTPUnauthorized()
        self.refreshes += 1
        return self._tokens()

    def _auth(self, request):
        if request.headers.get("Authorization") != f"Bearer A{self.logins}.{self.refreshes}":
            raise web.HTTPUnauthorized()
        if self.expire_next:
            self.expire_next = False
            raise web.HTTPUnauthorized()

    async def list_chargers(self, request):
        self._auth(request)
        return web.json_response(self.chargers)

    async def get_observations(self, request):
        self._auth(request)
        assert request.match_info["id"] == "EH123456" and request.query["ids"] == "109,120,121"
        return web.json_response({"observations": [{"id": k, "timestamp": "2026-10-05T20:00:00Z", "dataType": 3, "value": v} for k, v in self.observations.items()]})

    async def get_sessions(self, request):
        self._auth(request)
        self.session_paths.append((request.match_info["start"], request.match_info["end"]))
        return web.json_response(self.sessions)


@pytest.fixture()
def easee(serve, monkeypatch):
    fake = FakeEasee()
    monkeypatch.setattr(EaseeConnector, "base_url", serve(fake.app()))
    return fake


def test_easee_login_live_and_token_renewal(easee):
    async def go():
        conn = EaseeConnector({"username": "me@example.com", "password": "pw"})
        try:
            info, charging = await conn.describe(), await conn.live()
            easee.expire_next = True  # the token is refused once: log in again
            easee.observations = {109: "1", 120: "0", 121: "12.345"}  # values may come as text
            unplugged = await conn.live()
            easee.observations = {109: 7, 120: 0, 121: 0}
            waiting = await conn.live()
            easee.observations = {109: 4, 120: 0, 121: 30.5}
            finished = await conn.live()
            easee.observations = {109: 0}
            offline = await conn.live()
            easee.observations = {}  # a charger that never reported
            silent = await conn.live()
            return info, charging, unplugged, waiting, finished, offline, silent
        finally:
            await conn.close()

    info, charging, unplugged, waiting, finished, offline, silent = run(go())
    assert info == {"serial": "EH123456", "model": "Easee (1)", "name": "Garage"}
    assert (charging.status, charging.power_w, charging.energy_wh) == ("charging", 7360.0, 12345.0)
    assert (unplugged.status, unplugged.energy_wh) == ("unplugged", 0.0)  # the last session's energy is not a new session
    assert (waiting.status, finished.status, finished.energy_wh) == ("pending_approval", "finished", 30500.0)
    assert (offline.status, offline.online, silent.status) == ("offline", False, "offline")
    assert easee.logins == 2


def test_easee_refreshes_an_expired_token(easee):
    easee.expires_in = 30  # shorter than the safety margin: renewed before each request

    async def go():
        conn = EaseeConnector({"username": "me@example.com", "password": "pw"})
        try:
            await conn.describe()
            conn._token_until = datetime.min.replace(tzinfo=conn._token_until.tzinfo)  # expired
            return await conn.live()
        finally:
            await conn.close()

    assert run(go()).status == "charging"
    assert (easee.logins, easee.refreshes) == (1, 1)  # renewed with the refresh token, not the password


def test_easee_wrong_password_and_charger_choice(easee):
    with pytest.raises(ConnectorError, match="refused the login"):
        run(use(EaseeConnector({"username": "me@example.com", "password": "nope"}), "describe"))
    easee.chargers.append({"id": "EH654321", "name": "Carport"})
    with pytest.raises(ConnectorError, match="2 chargers.*EH123456, EH654321"):
        run(use(EaseeConnector({"username": "me@example.com", "password": "pw"}), "describe"))
    with pytest.raises(ConnectorError, match="No charger EH000000"):
        run(use(EaseeConnector({"username": "me@example.com", "password": "pw", "serial": "eh000000"}), "describe"))
    (info,) = run(use(EaseeConnector({"username": "me@example.com", "password": "pw", "serial": "eh654321"}), "describe"))
    assert info["serial"] == "EH654321" and info["model"] == "Easee"


def test_easee_history_lists_sessions(easee):
    (stats,) = run(use(EaseeConnector({"username": "me@example.com", "password": "pw"}), ("history", datetime(2026, 10, 1), datetime(2026, 11, 1))))
    assert [(h.energy_wh, h.is_session, h.ref) for h in stats] == [(24512.0, True, "77"), (9750.0, True, "2026-10-09T05:00:00")]
    local = datetime.fromisoformat("2026-10-05T19:58:12+00:00").astimezone().replace(tzinfo=None)  # Easee times are UTC
    assert (stats[0].start, stats[0].end - stats[0].start) == (local, datetime(2026, 10, 5, 23, 41, 3) - datetime(2026, 10, 5, 19, 58, 12))
    start, end = easee.session_paths[0]
    assert start.endswith("Z") and end.endswith("Z") and len(start) == 20


# ── Through EV Charge's own API ──────────────────────────────────────────────


def test_goe_and_easee_end_to_end(client, car, serve, easee):
    """Connect each charger, poll it (a live session starts), and import the history where the API has one."""
    from ev_charge import live

    live._connectors.clear()
    goe = FakeGoe()
    wallbox = client.post("/api/chargers", json={"name": "go-e garage", "default_car_id": car["id"]}).json()
    r = client.put(f"/api/chargers/{wallbox['id']}/connection", json={"connector": "goe", "credentials": {"host": serve(goe.app())}})
    assert r.status_code == 200, r.text
    assert r.json()["external_id"] == "012345" and r.json()["connector_settings"]["host"].startswith("http://127.0.0.1")
    polled = client.post(f"/api/chargers/{wallbox['id']}/poll").json()
    assert polled["live"]["status"] == "charging" and polled["last_poll_error"] is None
    s = client.get("/api/sessions").json()[0]
    assert (s["kwh"], s["reimbursement_status"], s["charger_name"]) == (5.322, "in_progress", "go-e garage")
    goe.status |= {"car": 1}  # unplugged: the session is closed
    client.post(f"/api/chargers/{wallbox['id']}/poll")
    assert client.get("/api/sessions").json()[0]["reimbursement_status"] == "recorded"
    r = client.get(f"/api/chargers/{wallbox['id']}/history?date_from=2026-10-01&date_to=2026-10-31")
    assert r.status_code == 502 and "no charging history" in r.json()["detail"]

    home = client.post("/api/chargers", json={"name": "Easee Home", "default_car_id": car["id"]}).json()
    assert client.put(f"/api/chargers/{home['id']}/connection", json={"connector": "easee", "credentials": {"username": "me@example.com", "password": "nope"}}).status_code == 422
    r = client.put(f"/api/chargers/{home['id']}/connection", json={"connector": "easee", "credentials": {"username": "me@example.com", "password": "pw"}})
    assert r.status_code == 200, r.text
    assert r.json()["external_id"] == "EH123456" and r.json()["connector_settings"] == {"username": "me@example.com"}  # the password stays on the server
    assert client.post(f"/api/chargers/{home['id']}/poll").json()["live"]["status"] == "charging"
    items = client.get(f"/api/chargers/{home['id']}/history?date_from=2026-10-01&date_to=2026-10-31").json()["items"]
    assert [(i["kwh"], i["exact"], i["status"]) for i in items] == [(24.512, True, "new"), (9.75, True, "new")]
    live._connectors.clear()
