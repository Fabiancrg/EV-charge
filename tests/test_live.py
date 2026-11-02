from datetime import datetime, timedelta

import pytest

from ev_charge import live
from ev_charge.connectors import CONNECTORS, ConnectorError, HourStat, LiveReading
from ev_charge.database import SessionLocal
from ev_charge.history import rebuild_sessions
from ev_charge.models import Charger
from ev_charge.secretbox import decrypt_json


class FakeCharger:
    """Stands in for a charger's cloud API: tests set the next reading / history."""

    label = "Fake"
    fields = (("email", "E-mail", False), ("password", "Password", True))
    reading = LiveReading(status="unplugged")
    stats: list[HourStat] = []
    fail: str | None = None
    logins = 0

    def __init__(self, config):
        if config.get("password") != "secret":
            raise ConnectorError("Wrong password")

    async def describe(self):
        FakeCharger.logins += 1
        return {"serial": "OHME-123", "model": "Home/Go"}

    async def live(self):
        if FakeCharger.fail:
            raise ConnectorError(FakeCharger.fail)
        return FakeCharger.reading

    async def history(self, start, end):
        return [h for h in FakeCharger.stats if start <= h.start < end]

    async def close(self):
        pass


@pytest.fixture()
def fake(monkeypatch):
    monkeypatch.setitem(CONNECTORS, "fake", FakeCharger)
    FakeCharger.reading, FakeCharger.stats, FakeCharger.fail = LiveReading(status="unplugged"), [], None
    live._connectors.clear()
    return FakeCharger


@pytest.fixture()
def charger(client, car, fake):
    c = client.post(
        "/api/chargers",
        json={"name": "Ohme Home/Go", "reimbursement_company": "Enco e-Mobility", "default_car_id": car["id"], "default_location": "Home", "default_provider": "Engie"},
    ).json()
    r = client.put(f"/api/chargers/{c['id']}/connection", json={"connector": "fake", "credentials": {"email": "me@example.com", "password": "secret"}})
    assert r.status_code == 200, r.text
    return r.json()


def _apply(charger_id, reading, now):
    db = SessionLocal()
    try:
        c = db.get(Charger, charger_id)
        action = live.apply_reading(db, c, reading, now)
        db.commit()
        return action
    finally:
        db.close()


def test_connection_is_tested_and_encrypted(client, car, fake):
    c = client.post("/api/chargers", json={"name": "Ohme"}).json()
    bad = client.put(f"/api/chargers/{c['id']}/connection", json={"connector": "fake", "credentials": {"email": "me@example.com", "password": "nope"}})
    assert bad.status_code == 422 and "Wrong password" in bad.json()["detail"]
    assert client.put(f"/api/chargers/{c['id']}/connection", json={"connector": "nope", "credentials": {}}).status_code == 422
    ok = client.put(f"/api/chargers/{c['id']}/connection", json={"connector": "fake", "credentials": {"email": "me@example.com", "password": "secret"}}).json()
    assert ok["connector"] == "fake" and ok["poll_enabled"] and ok["external_id"] == "OHME-123" and ok["serial_number"] == "OHME-123" and ok["model"] == "Home/Go"
    assert ok["connector_account"] == "me@example.com" and "secret" not in str(ok)
    db = SessionLocal()
    stored = db.get(Charger, c["id"]).connector_secret
    db.close()
    assert "secret" not in stored and decrypt_json(stored) == {"email": "me@example.com", "password": "secret"}
    # An empty password keeps the stored one
    again = client.put(f"/api/chargers/{c['id']}/connection", json={"connector": "fake", "credentials": {"email": "me@example.com", "password": ""}, "poll_enabled": False})
    assert again.status_code == 200 and again.json()["poll_enabled"] is False
    assert client.get("/api/chargers/connectors").json()[0]["id"] == "ohme"
    off = client.put(f"/api/chargers/{c['id']}/connection", json={"connector": None}).json()
    assert off["connector"] is None and off["connector_account"] is None


def test_live_session_lifecycle(client, charger):
    t0 = datetime(2026, 11, 2, 22, 5)
    cid = charger["id"]
    assert _apply(cid, LiveReading(status="plugged_in"), t0) == "idle"
    assert _apply(cid, LiveReading(status="charging", power_w=7200), t0 + timedelta(minutes=1)) == "idle"  # no energy yet
    assert _apply(cid, LiveReading(status="charging", power_w=7200, energy_wh=1200), t0 + timedelta(minutes=11)) == "created"
    s = client.get("/api/sessions").json()[0]
    assert s["start_time"] == "22:06:00" and s["kwh"] == 1.2 and s["reimbursement_status"] == "in_progress" and s["charger_name"] == "Ohme Home/Go"
    assert s["location"] == "Home" and s["provider"] == "Engie" and s["to_reimburse"] and s["car_id"] == charger["default_car_id"]
    # Charging through midnight
    assert _apply(cid, LiveReading(status="charging", energy_wh=30500), t0 + timedelta(hours=4)) == "updated"
    assert _apply(cid, LiveReading(status="finished", energy_wh=30500), t0 + timedelta(hours=5)) == "updated"
    s = client.get("/api/sessions").json()[0]
    assert s["kwh"] == 30.5 and s["end_time"] == "02:05:00" and s["duration_minutes"] == 239 and s["reimbursement_status"] == "in_progress"
    assert _apply(cid, LiveReading(status="unplugged"), t0 + timedelta(hours=9)) == "closed"
    rows = client.get("/api/sessions").json()
    assert len(rows) == 1 and rows[0]["reimbursement_status"] == "recorded"
    # The live state is kept for the dashboard
    live_state = client.get("/api/chargers").json()[0]["live"]
    assert live_state["status"] == "unplugged" and live_state["updated_at"].startswith("2026-11-03T07:05")


def test_new_session_without_unplugging_and_missing_car(client, charger):
    cid, t0 = charger["id"], datetime(2026, 11, 2, 8, 0)
    _apply(cid, LiveReading(status="charging", energy_wh=5000), t0)
    assert _apply(cid, LiveReading(status="charging", energy_wh=300), t0 + timedelta(hours=3)) == "restarted"  # counter restarted
    rows = client.get("/api/sessions").json()
    assert sorted((r["kwh"], r["reimbursement_status"]) for r in rows) == [(0.3, "in_progress"), (5.0, "recorded")]
    # Without a default car, nothing is created and the live state says why
    client.put(f"/api/chargers/{cid}", json={"name": "Ohme Home/Go", "default_car_id": None})
    _apply(cid, LiveReading(status="unplugged"), t0 + timedelta(hours=4))
    assert _apply(cid, LiveReading(status="charging", energy_wh=900), t0 + timedelta(hours=5)) == "no_car"
    assert "default car" in client.get("/api/chargers").json()[0]["live"]["warning"]


def test_poll_endpoint_and_errors(client, car, charger, fake, make_driver):
    fake.reading = LiveReading(status="charging", power_w=3600, energy_wh=2500, battery_pct=64)
    r = client.post(f"/api/chargers/{charger['id']}/poll").json()
    assert r["live"]["status"] == "charging" and r["live"]["battery_pct"] == 64 and r["last_poll_error"] is None
    assert client.get("/api/sessions").json()[0]["kwh"] == 2.5
    fake.fail = "Ohme is down"
    r = client.post(f"/api/chargers/{charger['id']}/poll").json()
    assert r["last_poll_error"] == "Ohme is down" and r["live"]["status"] == "charging"  # last known state kept
    # A driver sees (and refreshes) the live data of the chargers of their car only
    dave = make_driver([car["id"]])
    assert dave.get("/api/chargers").json()[0]["live"]["status"] == "charging"
    assert dave.get("/api/chargers").json()[0]["connector_account"] is None
    other = client.post("/api/cars", json={"name": "Other"}).json()
    eve = make_driver([other["id"]], username="eve")
    assert eve.get("/api/chargers").json()[0]["live"] is None
    assert eve.post(f"/api/chargers/{charger['id']}/poll").status_code == 403
    assert dave.put(f"/api/chargers/{charger['id']}/connection", json={"connector": None}).status_code == 403
    assert dave.get(f"/api/chargers/{charger['id']}/history?date_from=2026-01-01&date_to=2026-01-31").status_code == 403


def _hours(day, start_hour, kwhs, active_ms=None):
    out = []
    for i, kwh in enumerate(kwhs):
        s = datetime(2026, 10, day, start_hour) + timedelta(hours=i)
        out.append(HourStat(start=s, end=s + timedelta(hours=1), energy_wh=kwh * 1000, active_ms=(active_ms or 3_600_000) if kwh else 0))
    return out


def test_rebuild_sessions_from_hours():
    stats = _hours(5, 22, [7, 7, 0, 3.5]) + _hours(7, 18, [2], active_ms=20 * 60_000)  # one empty hour inside the first session
    sessions = rebuild_sessions(stats)
    assert [(s.start.strftime("%d %H:%M"), s.end.strftime("%d %H:%M"), s.kwh, s.hours) for s in sessions] == [
        ("05 22:00", "06 02:00", 17.5, 3),
        ("07 18:00", "07 18:20", 2.0, 1),
    ]


def test_history_preview_and_apply(client, car, charger, fake):
    fake.stats = _hours(5, 22, [7, 7, 3.5]) + _hours(9, 12, [11]) + _hours(12, 20, [6])
    # An existing session for the 9th (entered by hand, no charger): it matches
    client.post("/api/sessions", json={"car_id": car["id"], "charge_date": "2026-10-09", "kwh": 10.6, "location": "Home"})
    url = f"/api/chargers/{charger['id']}/history?date_from=2026-10-01&date_to=2026-10-31"
    items = client.get(url).json()["items"]
    assert [(i["charge_date"], i["start_time"], i["kwh"], i["status"]) for i in items] == [
        ("2026-10-05", "22:00", 17.5, "new"),
        ("2026-10-09", "12:00", 11.0, "match"),
        ("2026-10-12", "20:00", 6.0, "new"),
    ]
    keys = [items[0]["key"], items[1]["key"]]
    body = {"date_from": "2026-10-01", "date_to": "2026-10-31", "keys": keys, "car_id": car["id"]}
    assert client.post(f"/api/chargers/{charger['id']}/history/apply", json=body).json() == {"created": 1, "linked": 1}
    rows = {r["charge_date"]: r for r in client.get("/api/sessions").json()}
    assert rows["2026-10-05"]["kwh"] == 17.5 and rows["2026-10-05"]["reimbursement_status"] == "recorded" and rows["2026-10-05"]["end_time"] == "01:00:00"
    assert rows["2026-10-09"]["charger_name"] == "Ohme Home/Go" and rows["2026-10-09"]["kwh"] == 10.6  # linked, kWh kept
    assert rows["2026-10-05"]["provider"] == "Engie" and rows["2026-10-09"]["provider"] == "Engie"  # the default fills an empty provider
    assert "2026-10-12" not in rows
    # Already imported sessions are recognised
    assert [i["status"] for i in client.get(url).json()["items"]] == ["imported", "match", "new"]
    assert client.get(f"/api/chargers/{charger['id']}/history?date_from=2026-10-31&date_to=2026-10-01").status_code == 422


def test_ohme_connector_maps_the_library(monkeypatch):
    """The Ohme adapter against a stand-in for the 'ohme' library (no network)."""
    import asyncio

    import ohme

    from ev_charge.connectors.ohme import OhmeConnector

    class Client:
        def __init__(self, email, password):
            self.serial, self.device_info, self.available, self.energy, self.battery = "", {}, True, 4321.0, 72
            self.status, self.mode = ohme.ChargerStatus.CHARGING, ohme.ChargerMode.SMART_CHARGE
            self.power = ohme.ChargerPower(watts=7100, amps=31, volts=229)
            self.summary_calls = []

        async def async_login(self):
            return True

        async def async_update_device_info(self):
            self.serial, self.device_info = "OH123", {"name": "Ohme Home/Go", "sw_version": "1.2"}

        async def async_get_charge_session(self):
            pass

        async def async_get_charge_summary(self, start_ts, end_ts, granularity):
            self.summary_calls.append((start_ts, end_ts, granularity))
            hour = 3_600_000
            return {"stats": [{"startTime": start_ts, "endTime": start_ts + hour, "energyChargedTotalWh": 6500, "activeChargeMs": hour},
                              {"startTime": start_ts + hour, "endTime": start_ts + 2 * hour, "energyChargedTotalWh": 0}]}  # fmt: skip

        async def close(self):
            pass

    monkeypatch.setattr(ohme, "OhmeApiClient", Client)
    conn = OhmeConnector({"email": "a@b.c", "password": "x"})

    async def run():
        info = await conn.describe()
        reading = await conn.live()
        stats = await conn.history(datetime(2026, 10, 1), datetime(2026, 10, 15))
        return info, reading, stats, conn._client.summary_calls

    info, reading, stats, calls = asyncio.run(run())
    assert info == {"serial": "OH123", "model": "Ohme Home/Go", "firmware": "1.2"}
    assert (reading.status, reading.power_w, reading.energy_wh, reading.battery_pct) == ("charging", 7100, 4321.0, 72)
    assert len(calls) == 2 and calls[0][2] == ohme.SummaryGranularity.HOUR  # 14 days = 2 weekly requests
    assert len(stats) == 2 and stats[0].energy_wh == 6500  # empty hours dropped
    with pytest.raises(ConnectorError):
        OhmeConnector({"email": "a@b.c"})


def test_ohme_reads_the_session_energy_not_the_library_maximum(monkeypatch):
    """The ohme library keeps max(energy seen); a new session must not start with the previous one's energy."""
    import asyncio

    import ohme

    from ev_charge.connectors.ohme import OhmeConnector

    class Client:
        def __init__(self, email, password):
            self.serial, self.device_info, self.available, self.battery = "OH1", {}, True, 50
            self.energy = 18_400.0  # the library's carried maximum, from the previous session
            self.status, self.mode = ohme.ChargerStatus.CHARGING, None
            self.power = ohme.ChargerPower(watts=7000, amps=30, volts=230)
            self._charge_session = {"mode": "SMART_CHARGE", "batterySoc": {"wh": 1_250, "percent": 50}}

        async def async_login(self):
            return True

        async def async_update_device_info(self):
            pass

        async def async_get_charge_session(self):
            pass

        async def close(self):
            pass

    monkeypatch.setattr(ohme, "OhmeApiClient", Client)
    conn = OhmeConnector({"email": "a@b.c", "password": "x"})

    async def run():
        first = await conn.live()
        conn._client.status = ohme.ChargerStatus.UNPLUGGED
        unplugged = await conn.live()
        conn._client.status = ohme.ChargerStatus.CHARGING
        conn._client._charge_session = {"mode": "SMART_CHARGE"}  # no batterySoc: falls back to the library value
        fallback = await conn.live()
        return first, unplugged, fallback

    first, unplugged, fallback = asyncio.run(run())
    assert first.energy_wh == 1250.0  # not 18 400
    assert unplugged.energy_wh == 0.0
    assert fallback.energy_wh == 18_400.0


def test_a_new_charge_after_an_unseen_unplug_is_a_new_session(client, charger):
    cid, t0 = charger["id"], datetime(2026, 11, 2, 18, 0)
    _apply(cid, LiveReading(status="charging", energy_wh=18_400), t0)
    # Ohme sends 0 while it recalculates: the session is kept as it is
    assert _apply(cid, LiveReading(status="charging", energy_wh=0), t0 + timedelta(minutes=1)) == "updated"
    rows = client.get("/api/sessions").json()
    assert len(rows) == 1 and rows[0]["kwh"] == 18.4 and rows[0]["reimbursement_status"] == "in_progress"
    # Unplugged and replugged between two reads: the counter starts again from a small value
    assert _apply(cid, LiveReading(status="charging", energy_wh=1_250), t0 + timedelta(hours=14)) == "restarted"
    rows = sorted(client.get("/api/sessions").json(), key=lambda r: r["id"])
    assert [(r["kwh"], r["reimbursement_status"]) for r in rows] == [(18.4, "recorded"), (1.25, "in_progress")]
    assert rows[1]["start_time"] == "08:00:00" and rows[1]["charge_date"] == "2026-11-03"
