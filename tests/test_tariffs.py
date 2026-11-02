import io

import openpyxl


def _session(c, car_id, **kw):
    r = c.post("/api/sessions", json={"car_id": car_id, "charge_date": "2026-02-10", "kwh": 20} | kw)
    assert r.status_code == 201, r.text
    return r.json()


def test_tariff_periods_and_overlaps(client):
    q1 = client.post("/api/tariffs", json={"valid_from": "2026-01-01", "valid_to": "2026-03-31", "price_per_kwh": 0.28, "label": "Q1 2026"})
    assert q1.status_code == 201
    assert client.post("/api/tariffs", json={"valid_from": "2026-03-31", "valid_to": "2026-06-30", "price_per_kwh": 0.3}).status_code == 409
    assert client.post("/api/tariffs", json={"valid_from": "2026-04-01", "valid_to": "2026-03-01", "price_per_kwh": 0.3}).status_code == 422
    q2 = client.post("/api/tariffs", json={"valid_from": "2026-04-01", "valid_to": "", "price_per_kwh": 0.31})  # open-ended
    assert q2.status_code == 201 and q2.json()["valid_to"] is None
    assert client.post("/api/tariffs", json={"valid_from": "2027-01-01", "price_per_kwh": 0.3}).status_code == 409  # inside the open-ended one
    assert client.put(f"/api/tariffs/{q2.json()['id']}", json={"valid_from": "2026-04-01", "valid_to": "2026-06-30", "price_per_kwh": 0.31}).status_code == 200
    assert [t["label"] for t in client.get("/api/tariffs").json()] == [None, "Q1 2026"]  # newest first
    assert client.delete(f"/api/tariffs/{q2.json()['id']}").status_code == 204


def test_home_sessions_priced_at_tariff(client, car):
    client.post("/api/tariffs", json={"valid_from": "2026-01-01", "valid_to": "2026-03-31", "price_per_kwh": 0.25})
    client.post("/api/tariffs", json={"valid_from": "2026-04-01", "valid_to": "2026-06-30", "price_per_kwh": 0.30})
    assert client.put("/api/tariffs/home-locations", json={"locations": ["Home", " home ", "Garage  Wavre"]}).json()["locations"] == ["Home", "Garage Wavre"]

    q1 = _session(client, car["id"], location="HOME", kwh=40, cost=99, to_reimburse=True)
    assert q1["home"] and q1["cost"] == 10.0 and q1["cost_source"] == "tariff" and q1["tariff_price"] == 0.25 and q1["entered_cost"] == 99
    q2 = _session(client, car["id"], location="garage wavre", charge_date="2026-04-01", kwh=10, to_reimburse=True)
    assert q2["cost"] == 3.0
    public = _session(client, car["id"], location="Ionity", kwh=30, cost=18, to_reimburse=True)
    assert not public["home"] and public["cost"] == 18
    late = _session(client, car["id"], location="Home", charge_date="2026-08-01", kwh=10, to_reimburse=False)
    assert late["missing_tariff"] and late["cost"] is None

    tariffs = client.get("/api/tariffs").json()
    assert [(t["sessions"], t["kwh"]) for t in tariffs] == [(1, 10), (1, 40)]

    rep = client.get("/api/reports").json()["totals"]
    assert rep["cost"] == 31.0 and rep["home_kwh"] == 60 and rep["home_without_tariff"] == 1 and rep["cost_to_reimburse"] == 31.0

    # A new tariff reprices the past home sessions
    client.post("/api/tariffs", json={"valid_from": "2026-07-01", "valid_to": "2026-09-30", "price_per_kwh": 0.4})
    assert client.get(f"/api/sessions/{late['id']}").json()["cost"] == 4.0

    sugg = client.get("/api/sessions/suggestions").json()
    assert sugg["home_locations"] == ["Home", "Garage Wavre"] and "Garage Wavre" in sugg["locations"]


def test_reimbursement_statement(client, car):
    client.post("/api/tariffs", json={"valid_from": "2026-01-01", "valid_to": "2026-03-31", "price_per_kwh": 0.25})
    _session(client, car["id"], location="Home", kwh=40, to_reimburse=True, charge_date="2026-01-15")
    _session(client, car["id"], location="Fastned", kwh=30, cost=21, to_reimburse=True, charge_date="2026-02-15")
    _session(client, car["id"], location="Fastned", kwh=30, cost=21, to_reimburse=False, charge_date="2026-02-16")
    _session(client, car["id"], location="Fastned", kwh=5, to_reimburse=True, charge_date="2026-02-17")  # no cost entered
    _session(client, car["id"], location="Home", kwh=10, to_reimburse=True, charge_date="2026-04-02")  # outside the period

    data = client.get("/api/reports/reimbursement?date_from=2026-01-01&date_to=2026-03-31").json()
    assert data["totals"] == {"sessions": 3, "kwh": 75, "home_kwh": 40, "amount": 31.0, "home_amount": 10.0, "missing_price": 1}
    assert data["rows"][0]["price_per_kwh"] == 0.25 and data["rows"][1]["price_per_kwh"] == 0.7
    assert data["prepared_by"] == "Ada Admin" and data["cars"][0]["name"] == "Ioniq 5"

    csv = client.get("/api/reports/reimbursement?date_from=2026-01-01&date_to=2026-03-31&fmt=csv")
    assert csv.status_code == 200 and "reimbursement-2026-01-01_2026-03-31.csv" in csv.headers["content-disposition"]
    assert csv.text.strip().splitlines()[-1].startswith("Total;")

    xlsx = client.get("/api/reports/reimbursement?date_from=2026-01-01&date_to=2026-03-31&fmt=xlsx")
    ws = openpyxl.load_workbook(io.BytesIO(xlsx.content)).active
    values = [row for row in ws.iter_rows(values_only=True)]
    assert values[-1][0] == "Total" and values[-1][11] == 31.0
    assert sum(1 for row in values if hasattr(row[0], "year")) == 3  # one dated row per session
