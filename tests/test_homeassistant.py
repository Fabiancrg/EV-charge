"""The Home Assistant connector against a local fake of HA's REST and WebSocket APIs (same paths, payloads and auth)."""

import asyncio
import threading
from datetime import UTC, datetime, timedelta

import pytest
from aiohttp import WSMsgType, web

from ev_charge import live
from ev_charge.connectors import ConnectorError
from ev_charge.connectors.homeassistant import HomeAssistantConnector, current_session, increments

POWER, ENERGY = "sensor.carplug2_relay_power", "sensor.ve_plug_2_energy_daily"
TOKEN = "ha-token"


def iso(dt):
    return dt.isoformat()


class FakeHA:
    def __init__(self):
        self.now = datetime.now(UTC).replace(microsecond=0)
        self.power = "2190"
        # The plug's daily meter: standby 3 h ago, then charging for 40 min at ~2.4 kW, through a midnight reset
        self.energy_rows = [(self.now - timedelta(hours=5), 9.70), (self.now - timedelta(hours=3), 9.71)]
        value = 9.71
        for minute in range(40, 0, -1):
            value = round(value + 0.04, 2) if minute != 20 else 0.04  # the meter reset (midnight)
            self.energy_rows.append((self.now - timedelta(minutes=minute), value))
        self.energy_rows.insert(-5, (self.now - timedelta(minutes=6), "unavailable"))
        self.stats = [
            {"start": 1759687200000, "end": 1759690800000, "change": 2150.0},  # Unix ms, in Wh (the units asked for)
            {"start": 1759690800000, "end": 1759694400000, "change": 2200.0},
            {"start": 1759694400000, "end": 1759698000000, "change": 10.0},  # standby
        ]
        self.history_calls = []
        self.ws_requests = []

    def app(self):
        app = web.Application()
        app.router.add_get("/api/config", self.config)
        app.router.add_get("/api/states/{entity}", self.state)
        app.router.add_get("/api/history/period/{start}", self.history)
        app.router.add_get("/api/websocket", self.websocket)
        return app

    def _auth(self, request):
        if request.headers.get("Authorization") != f"Bearer {TOKEN}":
            raise web.HTTPUnauthorized()

    async def config(self, request):
        self._auth(request)
        return web.json_response({"version": "2026.11.0", "time_zone": "Europe/Brussels"})

    async def state(self, request):
        self._auth(request)
        entity = request.match_info["entity"]
        if entity == POWER:
            return web.json_response({"entity_id": POWER, "state": self.power, "attributes": {"unit_of_measurement": "W", "friendly_name": "CarPlug2_Relay Power"}})
        if entity == ENERGY:
            t, v = self.energy_rows[-1]
            return web.json_response({"entity_id": ENERGY, "state": str(v), "last_changed": iso(t), "attributes": {"unit_of_measurement": "kWh"}})
        return web.json_response({"message": "Entity not found."}, status=404)

    async def history(self, request):
        self._auth(request)
        start = datetime.fromisoformat(request.match_info["start"])
        end = datetime.fromisoformat(request.query["end_time"])
        assert request.query["filter_entity_id"] == ENERGY and "minimal_response" in request.query
        self.history_calls.append((start, end))
        rows = [r for r in self.energy_rows if start <= r[0] <= end]
        before = [r for r in self.energy_rows if r[0] < start]
        if before:  # HA starts with the state at the start of the period
            rows.insert(0, (start, before[-1][1]))
        return web.json_response([[{"state": str(v), "last_changed": iso(t)} for t, v in rows]])

    async def websocket(self, request):
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        await ws.send_json({"type": "auth_required", "ha_version": "2026.11.0"})
        auth = await ws.receive_json()
        if auth.get("access_token") != TOKEN:
            await ws.send_json({"type": "auth_invalid", "message": "Invalid access token"})
            await ws.close()
            return ws
        await ws.send_json({"type": "auth_ok"})
        async for msg in ws:
            if msg.type != WSMsgType.TEXT:
                break
            req = msg.json()
            self.ws_requests.append(req)
            assert req["type"] == "recorder/statistics_during_period" and req["types"] == ["change"] and req["period"] == "hour"
            await ws.send_json({"id": req["id"], "type": "result", "success": True, "result": {ENERGY: self.stats} if self.stats else {}})
        return ws


@pytest.fixture()
def ha():
    fake = FakeHA()
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
    fake.url = f"http://127.0.0.1:{port['n']}"
    live._connectors.clear()
    yield fake
    asyncio.run_coroutine_threadsafe(runner.cleanup(), loop).result(5)
    loop.call_soon_threadsafe(loop.stop)


def config(ha, **extra):
    return {"url": ha.url + "/", "token": TOKEN, "power_entity": POWER, "energy_entity": ENERGY, **extra}


def run(coro):
    return asyncio.run(coro)


def test_increments_and_session():
    t0 = datetime(2026, 11, 5, 18, 0, tzinfo=UTC)
    points = [(t0, 9.0), (t0 + timedelta(minutes=1), 9.5), (t0 + timedelta(minutes=2), 0.0), (t0 + timedelta(minutes=3), 0.25)]
    assert [wh for _, wh in increments(points)] == [0.5, 0.25]  # the drop to 0 is a reset, not negative energy
    incs = [(t0, 10.0), (t0 + timedelta(hours=2), 500.0), (t0 + timedelta(hours=2, minutes=1), 500.0)]
    assert current_session(incs, t0 + timedelta(hours=2, minutes=5), timedelta(minutes=60)) == (1000.0, t0 + timedelta(hours=2), t0 + timedelta(hours=2, minutes=1))
    assert current_session(incs, t0 + timedelta(hours=3, minutes=2), timedelta(minutes=60))[0] == 0.0  # idle for too long: no session


def test_describe_live_and_end_of_session(ha):
    async def go():
        conn = HomeAssistantConnector(config(ha, idle_minutes="30"))
        try:
            info = await conn.describe()
            charging = await conn.live()
            ha.power = "3"  # charging stopped: the session stays open until the idle time has passed
            paused = await conn.live()
            ha.power = "unavailable"
            offline = await conn.live()
            return info, charging, paused, offline
        finally:
            await conn.close()

    info, charging, paused, offline = run(go())
    assert info == {"serial": POWER, "model": "Home Assistant: CarPlug2_Relay Power", "firmware": "2026.11.0"}
    assert charging.status == "charging" and charging.power_w == 2190.0
    assert charging.energy_wh == pytest.approx(40 * 40, abs=1)  # 40 minutes of 40 Wh, across the reset; not the standby
    start = datetime.fromisoformat(charging.extra["session_start"])
    expected = (ha.now - timedelta(minutes=40)).astimezone().replace(tzinfo=None)
    assert abs(start - expected) <= timedelta(minutes=1)
    assert paused.status == "paused" and paused.energy_wh == charging.energy_wh
    assert offline.status == "offline"
    assert len(ha.history_calls) == 2 and ha.history_calls[1][0] > ha.history_calls[0][0]  # later polls only read what is new; offline: no history read


def test_session_over_and_standby(ha):
    ha.power = "2"
    ha.energy_rows = [(ha.now - timedelta(hours=4), 9.0), (ha.now - timedelta(hours=2), 12.0), (ha.now - timedelta(minutes=2), 12.01)]

    async def go():
        conn = HomeAssistantConnector(config(ha))
        try:
            return await conn.live()
        finally:
            await conn.close()

    reading = run(go())
    assert (reading.status, reading.energy_wh) == ("unplugged", 0.0)  # an old charge, then 10 Wh of standby: no session


def test_errors(ha):
    async def describe(cfg):
        conn = HomeAssistantConnector(cfg)
        try:
            return await conn.describe()
        finally:
            await conn.close()

    with pytest.raises(ConnectorError, match="refused the access token"):
        run(describe({**config(ha), "token": "bad"}))
    with pytest.raises(ConnectorError, match="no entity sensor.nope"):
        run(describe({**config(ha), "power_entity": "sensor.nope"}))
    with pytest.raises(ConnectorError, match="Enter: Energy sensor"):
        HomeAssistantConnector({**config(ha), "energy_entity": ""})
    with pytest.raises(ConnectorError, match="http"):
        HomeAssistantConnector({**config(ha), "url": "homeassistant.local"})


def test_history_from_statistics_then_recorder(ha):
    async def go():
        conn = HomeAssistantConnector(config(ha))
        try:
            from_stats = await conn.history(datetime(2026, 10, 5), datetime(2026, 10, 6))
            ha.stats = []  # no long-term statistics: the recorded history
            from_recorder = await conn.history(datetime.now() - timedelta(hours=6), datetime.now() + timedelta(minutes=1))
            return from_stats, from_recorder
        finally:
            await conn.close()

    from_stats, from_recorder = run(go())
    assert [h.energy_wh for h in from_stats] == [2150.0, 2200.0]  # the standby hour is left out
    assert ha.ws_requests[0]["statistic_ids"] == [ENERGY] and ha.ws_requests[0]["units"] == {"energy": "Wh"}
    assert sum(h.energy_wh for h in from_recorder) == pytest.approx(1600, abs=1)


def test_home_assistant_end_to_end(client, car, ha):
    """Through EV Charge's own API: connect, poll (a session in progress, with its real start), stop."""
    c = client.post("/api/chargers", json={"name": "Ohme Go (plug 2)", "reimbursement_company": "50five", "default_car_id": car["id"]}).json()
    types = {t["id"]: t for t in client.get("/api/chargers/connectors").json()}
    assert [f["secret"] for f in types["homeassistant"]["fields"]] == [False, True, False, False, False, False]
    creds = {"url": ha.url, "token": TOKEN, "power_entity": POWER, "energy_entity": ENERGY}
    r = client.put(f"/api/chargers/{c['id']}/connection", json={"connector": "homeassistant", "credentials": creds})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["external_id"] == POWER and body["connector_account"] == ha.url and "token" not in body["connector_settings"]
    polled = client.post(f"/api/chargers/{c['id']}/poll").json()
    assert polled["live"]["status"] == "charging" and polled["last_poll_error"] is None
    s = client.get("/api/sessions").json()[0]
    assert s["kwh"] == pytest.approx(1.6, abs=0.002) and s["reimbursement_status"] == "in_progress"
    started = (ha.now - timedelta(minutes=40)).astimezone().replace(tzinfo=None)
    assert s["start_time"][:5] in {(started + timedelta(minutes=d)).strftime("%H:%M") for d in (-1, 0, 1)}  # when charging started, not the first poll
