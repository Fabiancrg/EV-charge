"""The Zaptec connector against a local fake of the Zaptec API (same paths, payloads and auth)."""

import asyncio
import threading
from datetime import datetime

import pytest
from aiohttp import web

from ev_charge import live
from ev_charge.connectors import ConnectorError
from ev_charge.connectors.zaptec import ZaptecConnector


class FakeZaptec:
    def __init__(self):
        self.chargers = [{"Id": "c-1", "DeviceId": "ZAP049123", "Name": "Garage", "DeviceName": "Zaptec Go", "IsOnline": True, "OperatingMode": 3}]
        self.state = {710: "3", 513: "7360.5", 553: "12.345"}
        self.online = True
        self.logins = 0
        self.expire_next = False
        self.history_calls = []
        self.sessions = [
            {"Id": "s-1", "StartDateTime": "2026-10-05T19:58:12.37", "EndDateTime": "2026-10-05T23:41:03.1", "Energy": 24.512},
            {"Id": "s-2", "StartDateTime": "2026-10-09T05:00:00Z", "EndDateTime": "2026-10-09T06:30:00Z", "Energy": 9.75},
            {"Id": "s-3", "StartDateTime": "2026-10-12T18:00:00Z", "EndDateTime": None, "Energy": 3.1},  # still running
        ]

    def app(self):
        app = web.Application()
        app.router.add_post("/oauth/token", self.token)
        app.router.add_get("/api/chargers", self.list_chargers)
        app.router.add_get("/api/constants", self.constants)
        app.router.add_get("/api/chargers/{id}", self.charger)
        app.router.add_get("/api/chargers/{id}/state", self.charger_state)
        app.router.add_get("/api/chargehistory", self.history)
        return app

    async def token(self, request):
        form = await request.post()
        assert form["grant_type"] == "password"
        if form["password"] != "pw":
            return web.json_response({"error": "invalid_grant", "error_description": "The user name or password is incorrect."}, status=400)
        self.logins += 1
        return web.json_response({"access_token": f"T{self.logins}", "token_type": "bearer", "expires_in": 86400})

    def _auth(self, request):
        if request.headers.get("Authorization") != f"Bearer T{self.logins}":
            raise web.HTTPUnauthorized()
        if self.expire_next:
            self.expire_next = False
            raise web.HTTPUnauthorized()

    async def list_chargers(self, request):
        self._auth(request)
        return web.json_response({"Pages": 1, "Data": self.chargers})

    async def constants(self, request):
        self._auth(request)
        return web.json_response({
            "ObservationIds": {"ChargerOperationMode": 710, "TotalChargePower": 513, "TotalChargePowerSession": 553, "Humidity": 270},
            "ChargerOperationModes": {"Unknown": 0, "Disconnected": 1, "Connected_Requesting": 2, "Connected_Charging": 3, "Connected_Finished": 5},
        })  # fmt: skip

    async def charger(self, request):
        self._auth(request)
        return web.json_response({"Id": request.match_info["id"], "IsOnline": self.online})

    async def charger_state(self, request):
        self._auth(request)
        return web.json_response([{"StateId": k, "ValueAsString": v} for k, v in self.state.items()] + [{"StateId": 270, "ValueAsString": "41"}])

    async def history(self, request):
        self._auth(request)
        q = request.query
        self.history_calls.append(dict(q))
        assert q["ChargerId"] == "c-1" and q["From"].endswith("Z") and q["PageSize"] == "100"
        page = int(q["PageIndex"])
        data = self.sessions[:1] if page == 0 else self.sessions[1:]
        return web.json_response({"Pages": 2, "Data": data})


@pytest.fixture()
def zaptec(monkeypatch):
    """Run the fake API on a local port, in its own thread and event loop."""
    fake = FakeZaptec()
    loop = asyncio.new_event_loop()
    runner = web.AppRunner(fake.app())
    ready = threading.Event()
    port = {}

    def serve():
        asyncio.set_event_loop(loop)
        loop.run_until_complete(runner.setup())
        site = web.TCPSite(runner, "127.0.0.1", 0)
        loop.run_until_complete(site.start())
        port["n"] = site._server.sockets[0].getsockname()[1]
        ready.set()
        loop.run_forever()

    threading.Thread(target=serve, daemon=True).start()
    ready.wait(5)
    base = f"http://127.0.0.1:{port['n']}"
    monkeypatch.setattr(ZaptecConnector, "token_url", f"{base}/oauth/token")
    monkeypatch.setattr(ZaptecConnector, "api_url", f"{base}/api/")
    live._connectors.clear()
    yield fake
    asyncio.run_coroutine_threadsafe(runner.cleanup(), loop).result(5)
    loop.call_soon_threadsafe(loop.stop)


def run(coro):
    return asyncio.run(coro)


def test_login_live_and_token_renewal(zaptec):
    async def go():
        conn = ZaptecConnector({"email": "me@example.com", "password": "pw"})
        try:
            info = await conn.describe()
            reading = await conn.live()
            zaptec.expire_next = True  # the token is refused once: log in again
            zaptec.state = {710: "1", 513: "0", 553: "12.345"}
            unplugged = await conn.live()
            zaptec.online = False
            offline = await conn.live()
            return info, reading, unplugged, offline
        finally:
            await conn.close()

    info, reading, unplugged, offline = run(go())
    assert info == {"serial": "ZAP049123", "model": "Zaptec Go", "name": "Garage"}
    assert (reading.status, reading.power_w, reading.energy_wh) == ("charging", 7360.5, 12345.0)
    assert (unplugged.status, unplugged.energy_wh) == ("unplugged", 0.0)  # the last session's energy is not a new session
    assert zaptec.logins == 2
    assert offline.status == "offline" and not offline.online


def test_wrong_password_and_several_chargers(zaptec):
    async def describe(config):
        conn = ZaptecConnector(config)
        try:
            return await conn.describe()
        finally:
            await conn.close()

    with pytest.raises(ConnectorError, match="incorrect"):
        run(describe({"email": "me@example.com", "password": "bad"}))
    zaptec.chargers.append({"Id": "c-2", "DeviceId": "ZAP777", "Name": "Office", "DeviceName": "Zaptec Pro"})
    with pytest.raises(ConnectorError, match="2 chargers: enter the serial number"):
        run(describe({"email": "me@example.com", "password": "pw"}))
    assert run(describe({"email": "me@example.com", "password": "pw", "serial": "zap777"}))["serial"] == "ZAP777"
    with pytest.raises(ConnectorError, match="No charger ZAP000"):
        run(describe({"email": "me@example.com", "password": "pw", "serial": "ZAP000"}))


def test_history_lists_the_sessions(zaptec):
    async def go():
        conn = ZaptecConnector({"email": "me@example.com", "password": "pw"})
        try:
            return await conn.history(datetime(2026, 10, 1), datetime(2026, 10, 31))
        finally:
            await conn.close()

    stats = run(go())
    assert len(zaptec.history_calls) == 2  # both pages read
    assert [(s.energy_wh, s.is_session, s.ref) for s in stats] == [(24512.0, True, "s-1"), (9750.0, True, "s-2")]  # the running one is skipped
    expected_start = datetime.fromisoformat("2026-10-05T19:58:12.370000+00:00").astimezone().replace(tzinfo=None)
    assert stats[0].start == expected_start


def test_zaptec_end_to_end(client, car, zaptec):
    """Through EV Charge's own API: connect, poll, import the history."""
    c = client.post("/api/chargers", json={"name": "Zaptec Go (garage)", "reimbursement_company": "50five", "default_car_id": car["id"]}).json()
    types = {t["id"]: t for t in client.get("/api/chargers/connectors").json()}
    assert [f["required"] for f in types["zaptec"]["fields"]] == [True, True, False]
    r = client.put(f"/api/chargers/{c['id']}/connection", json={"connector": "zaptec", "credentials": {"email": "me@example.com", "password": "pw"}})
    assert r.status_code == 200, r.text
    assert r.json()["external_id"] == "ZAP049123" and r.json()["connector_settings"] == {"email": "me@example.com"}
    polled = client.post(f"/api/chargers/{c['id']}/poll").json()
    assert polled["live"]["status"] == "charging" and polled["last_poll_error"] is None
    s = client.get("/api/sessions").json()[0]
    assert s["kwh"] == 12.345 and s["reimbursement_status"] == "in_progress" and s["charger_name"] == "Zaptec Go (garage)"

    items = client.get(f"/api/chargers/{c['id']}/history?date_from=2026-10-01&date_to=2026-10-31").json()["items"]
    assert [(i["kwh"], i["exact"], i["status"]) for i in items] == [(24.512, True, "new"), (9.75, True, "new")]
    body = {"date_from": "2026-10-01", "date_to": "2026-10-31", "keys": [i["key"] for i in items], "car_id": car["id"]}
    assert client.post(f"/api/chargers/{c['id']}/history/apply", json=body).json() == {"created": 2, "linked": 0}
    imported = [x for x in client.get("/api/sessions").json() if x["kwh"] == 24.512][0]
    assert imported["reimbursement_status"] == "recorded" and "rounded" not in imported["notes"]


def test_closed_live_session_takes_the_final_figures_of_the_charger(client, car, zaptec):
    """The last reading before unplugging is a few minutes old: once closed, the session gets the energy and the end
    the charger recorded (here 1.716 kWh seen live, 1.936 kWh in the end)."""
    from datetime import UTC, timedelta

    from ev_charge.database import SessionLocal
    from ev_charge.models import Charger

    c = client.post("/api/chargers", json={"name": "Zaptec Go", "default_car_id": car["id"]}).json()
    client.put(f"/api/chargers/{c['id']}/connection", json={"connector": "zaptec", "credentials": {"email": "me@example.com", "password": "pw"}})
    zaptec.state = {710: "3", 513: "4400", 553: "1.716"}
    zaptec.sessions = []
    client.post(f"/api/chargers/{c['id']}/poll")
    seen = client.get("/api/sessions").json()[0]
    assert (seen["kwh"], seen["reimbursement_status"]) == (1.716, "in_progress")

    def pending():
        with SessionLocal() as db:
            return live.live_state(db.get(Charger, c["id"])).get("finalize")

    # Unplugged; the charger doesn't list the session yet: closed as it was seen, to be looked up again
    zaptec.state = {710: "1", 513: "0", 553: "1.936"}
    client.post(f"/api/chargers/{c['id']}/poll")
    closed = client.get("/api/sessions").json()[0]
    assert (closed["kwh"], closed["reimbursement_status"], closed["end_time"]) == (1.716, "recorded", seen["end_time"])
    assert pending() == {"id": seen["id"], "kwh": 1.716, "polls": 1} and len(zaptec.history_calls) == 2  # asked once (two pages)

    # Now it does (with another, older session): looked up again at the third poll after closing, not at each one
    now = datetime.now(UTC)
    stamp = "%Y-%m-%dT%H:%M:%S"
    ended = now + timedelta(minutes=3)
    zaptec.sessions = [
        {"Id": "old", "StartDateTime": (now - timedelta(hours=1, minutes=30)).strftime(stamp), "EndDateTime": (now - timedelta(hours=1)).strftime(stamp), "Energy": 8.0},
        {"Id": "this", "StartDateTime": (now - timedelta(minutes=1)).strftime(stamp), "EndDateTime": ended.strftime(stamp), "Energy": 1.936},
    ]
    client.post(f"/api/chargers/{c['id']}/poll")
    assert client.get("/api/sessions").json()[0]["kwh"] == 1.716 and len(zaptec.history_calls) == 2
    client.post(f"/api/chargers/{c['id']}/poll")
    final = client.get("/api/sessions").json()[0]
    assert (final["kwh"], final["reimbursement_status"], final["start_time"]) == (1.936, "recorded", seen["start_time"])
    assert final["end_time"] == ended.astimezone().strftime("%H:%M:00") and pending() is None
    assert len(client.get("/api/sessions").json()) == 1

    # A session edited by hand after closing is left alone
    zaptec.state = {710: "3", 513: "4400", 553: "2.5"}
    client.post(f"/api/chargers/{c['id']}/poll")
    zaptec.state = {710: "1", 513: "0", 553: "0"}
    zaptec.sessions = []
    client.post(f"/api/chargers/{c['id']}/poll")
    second = client.get("/api/sessions").json()[0]
    body = {k: second[k] for k in ("car_id", "charge_date", "start_time", "end_time", "charger_id", "to_reimburse", "reimbursement_status")} | {"kwh": 3.0}
    client.put(f"/api/sessions/{second['id']}", json=body)
    zaptec.sessions = [{"Id": "x", "StartDateTime": (now - timedelta(minutes=5)).strftime(stamp), "EndDateTime": (now + timedelta(minutes=9)).strftime(stamp), "Energy": 2.9}]
    for _ in range(3):
        client.post(f"/api/chargers/{c['id']}/poll")
    assert client.get(f"/api/sessions/{second['id']}").json()["kwh"] == 3.0 and pending() is None
