from ev_charge.services import duration_minutes


def _session(client, car_id, **kw):
    body = {"car_id": car_id, "charge_date": "2026-03-01", "kwh": 30.5} | kw
    r = client.post("/api/sessions", json=body)
    assert r.status_code == 201, r.text
    return r.json()


def test_pages_render(client, car):
    for path in ("/", "/cars", "/sessions", "/reports"):
        r = client.get(path)
        assert r.status_code == 200, path
        assert "EV Charge" in r.text
    assert client.get("/static/js/vendor/chart.umd.min.js").status_code == 200


def test_car_crud_and_validation(client, car):
    assert client.post("/api/cars", json={"name": "Ioniq 5"}).status_code == 409
    assert client.post("/api/cars", json={"name": "  "}).status_code == 422
    assert client.post("/api/cars", json={"name": "X", "lease_start": "2026-01-01", "lease_end": "2025-01-01"}).status_code == 422
    body = {k: v for k, v in car.items() if k not in ("id", "session_count", "total_kwh", "last_odometer", "last_charge_date", "lease", "drivers")}
    body |= {"battery_kwh": 74, "license_plate": " 1-ABC-123 ", "vin": ""}
    r = client.put(f"/api/cars/{car['id']}", json=body)
    assert r.status_code == 200
    assert r.json()["license_plate"] == "1-ABC-123"
    assert r.json()["vin"] is None
    assert r.json()["lease"]["km_allowed_total"] == 60000  # 36 months


def test_car_with_sessions_cannot_be_deleted(client, car):
    _session(client, car["id"])
    assert client.delete(f"/api/cars/{car['id']}").status_code == 409


def test_session_crud_and_derived_values(client, car):
    s = _session(client, car["id"], odometer=1000, start_time="22:30", end_time="06:30", cost=12.2, location=" Home ", provider="")
    assert s["duration_minutes"] == 480
    assert s["avg_power_kw"] == round(30.5 / 8, 1)
    assert s["location"] == "Home" and s["provider"] is None
    # No charger: the location doesn't matter, the cost entered is kept
    assert s["home"] is False and s["cost"] == 12.2 and s["cost_source"] == "entered" and s["reimbursement_status"] is None
    s2 = _session(client, car["id"], charge_date="2026-03-05", odometer=1350, to_reimburse=True, location="Office", cost=12.2)
    assert s2["price_per_kwh"] == 0.4 and s2["cost_source"] == "entered" and s2["created_by"] == "Ada Admin"
    rows = client.get("/api/sessions").json()
    assert [r["id"] for r in rows] == [s2["id"], s["id"]]  # newest first
    assert rows[0]["km_since_previous"] == 350

    body = {k: s2[k] for k in ("car_id", "charge_date", "odometer", "location", "provider", "kwh", "start_time", "end_time", "to_reimburse", "cost", "notes")}
    body["kwh"] = 40
    assert client.put(f"/api/sessions/{s2['id']}", json=body).json()["kwh"] == 40

    assert len(client.get("/api/sessions?to_reimburse=true").json()) == 1
    assert len(client.get("/api/sessions?q=home").json()) == 1
    assert len(client.get("/api/sessions?date_from=2026-03-02").json()) == 1

    assert s2["reimbursement_status"] == "not_recorded"  # to be reimbursed, no charger
    r = client.patch("/api/sessions/bulk", json={"ids": [s["id"], s2["id"]], "to_reimburse": True})
    assert r.json()["updated"] == 2
    assert len(client.get("/api/sessions?to_reimburse=true").json()) == 2

    csv = client.get("/api/sessions/export.csv")
    assert csv.status_code == 200 and "Ioniq 5" in csv.text and csv.text.count("\n") == 3

    sugg = client.get(f"/api/sessions/suggestions?car_id={car['id']}").json()
    assert sugg["locations"] == ["Home", "Office"]
    assert sugg["last_odometer"] == 1350
    assert sugg["by_location"]["Office"]["to_reimburse"] is True

    assert client.delete(f"/api/sessions/{s['id']}").status_code == 204
    assert client.get(f"/api/sessions/{s['id']}").status_code == 404


def test_session_validation(client, car):
    assert client.post("/api/sessions", json={"car_id": car["id"], "charge_date": "2026-03-01", "kwh": 0}).status_code == 422
    assert client.post("/api/sessions", json={"car_id": 999, "charge_date": "2026-03-01", "kwh": 5}).status_code == 422


def test_report(client, car):
    other = client.post("/api/cars", json={"name": "Model 3"}).json()
    _session(client, car["id"], charge_date="2026-01-10", odometer=1000, kwh=20, provider="Engie", location="Home", cost=6)
    _session(client, car["id"], charge_date="2026-02-10", odometer=1500, kwh=80, provider="Ionity", location="A4", to_reimburse=True, cost=40)
    _session(client, other["id"], charge_date="2026-02-15", kwh=10, location="Home")
    rep = client.get("/api/reports").json()
    assert rep["months"] == ["2026-01", "2026-02"]
    assert rep["totals"]["kwh"] == 110
    assert rep["totals"]["km"] == 500
    assert rep["totals"]["consumption_kwh_100km"] == 16.0
    assert rep["totals"]["kwh_to_reimburse"] == 80
    assert rep["kwh_by_month"][str(car["id"])] == [20, 80]
    assert rep["km_by_month"][str(car["id"])] == [0, 500]
    assert rep["by_location"][0] == {"name": "A4", "kwh": 80, "sessions": 1, "cost": 40}
    assert rep["lease"] is None  # two cars selected

    one = client.get(f"/api/reports?car_id={car['id']}&date_from=2026-02-01").json()
    assert one["totals"]["km"] == 500  # distance since the January reading still counts
    assert one["lease"]["allowed_end_odometer"] == 100 + 60000
    assert [c["color_index"] for c in one["cars"]] == [0]


def test_years_between():
    from datetime import date

    from ev_charge.services import years_between

    assert years_between(date(2024, 2, 29), date(2028, 2, 29)) == 4.0
    assert years_between(date(2025, 1, 31), date(2025, 2, 28)) == 1 / 12  # month-end clamps
    assert years_between(date(2025, 1, 1), date(2025, 7, 1)) == 0.5
    assert years_between(date(2025, 1, 1), date(2025, 1, 1)) == 0.0


def test_duration_wraps_midnight():
    from datetime import time

    assert duration_minutes(time(23, 0), time(1, 30)) == 150
    assert duration_minutes(time(8, 0), None) is None


def test_kwh_keeps_three_decimals(client, car):
    s = _session(client, car["id"], kwh=30.125)
    assert s["kwh"] == 30.125
    _session(client, car["id"], kwh=0.005, charge_date="2026-03-02")
    assert client.get("/api/reports").json()["totals"]["kwh"] == 30.13
    assert client.get(f"/api/cars/{car['id']}").json()["total_kwh"] == 30.13
    # The import tells 30.125 and 30.126 apart (not duplicates)
    body = {"rows": [["2026-03-01", "30,126"], ["2026-03-01", "30.125"]], "mapping": {"charge_date": 0, "kwh": 1}, "car_id": car["id"]}
    rows = client.post("/api/import/sessions/preview", json=body).json()["rows"]
    assert [(r["kwh"], r["status"]) for r in rows] == [(30.126, "ok"), (30.125, "duplicate")]
