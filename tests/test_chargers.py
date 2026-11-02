def _session(c, car_id, **kw):
    r = c.post("/api/sessions", json={"car_id": car_id, "charge_date": "2026-02-10", "kwh": 20} | kw)
    assert r.status_code == 201, r.text
    return r.json()


def test_charger_crud(client):
    r = client.post("/api/chargers", json={"name": " Zaptec Go ", "brand": "Zaptec", "serial_number": "ZAP-123", "reimbursement_company": "50five"})
    assert r.status_code == 201 and r.json()["name"] == "Zaptec Go" and r.json()["uses_home_tariff"] is True
    assert client.post("/api/chargers", json={"name": "Zaptec Go"}).status_code == 409
    assert client.post("/api/chargers", json={"name": ""}).status_code == 422
    c = r.json()
    assert client.put(f"/api/chargers/{c['id']}", json={"name": "Zaptec Go garage", "reimbursement_company": "50five"}).json()["name"] == "Zaptec Go garage"
    assert client.delete(f"/api/chargers/{c['id']}").status_code == 204


def test_status_defaults_and_charger_rules(client, car):
    ohme = client.post("/api/chargers", json={"name": "Ohme", "reimbursement_company": "Enco e-Mobility"}).json()
    s = _session(client, car["id"], to_reimburse=True, charger_id=ohme["id"])
    assert s["reimbursement_status"] == "recorded"
    assert _session(client, car["id"], to_reimburse=True)["reimbursement_status"] == "not_recorded"
    assert _session(client, car["id"], to_reimburse=False, reimbursement_status="paid")["reimbursement_status"] is None
    assert _session(client, car["id"], to_reimburse=True, reimbursement_status="in_progress")["reimbursement_status"] == "in_progress"
    bad = client.post("/api/sessions", json={"car_id": car["id"], "charge_date": "2026-02-10", "kwh": 5, "reimbursement_status": "lost"})
    assert bad.status_code == 422
    assert client.post("/api/sessions", json={"car_id": car["id"], "charge_date": "2026-02-10", "kwh": 5, "charger_id": 999}).status_code == 422

    # A used charger can't be deleted; once inactive it can't be picked for new sessions, but old ones keep it
    assert client.delete(f"/api/chargers/{ohme['id']}").status_code == 409
    client.put(f"/api/chargers/{ohme['id']}", json={"name": "Ohme", "active": False})
    assert client.post("/api/sessions", json={"car_id": car["id"], "charge_date": "2026-02-11", "kwh": 5, "charger_id": ohme["id"]}).status_code == 422
    body = {k: s[k] for k in ("car_id", "charge_date", "kwh", "to_reimburse", "charger_id")} | {"reimbursement_status": "invoiced"}
    assert client.put(f"/api/sessions/{s['id']}", json=body).json()["reimbursement_status"] == "invoiced"


def test_bulk_update_and_filters(client, car):
    zap = client.post("/api/chargers", json={"name": "Zaptec Go", "reimbursement_company": "50five"}).json()
    a = _session(client, car["id"])
    b = _session(client, car["id"], to_reimburse=True)
    c = _session(client, car["id"], to_reimburse=True, charger_id=zap["id"])

    # Setting a charger makes the sessions reimbursable (status: recorded)
    r = client.patch("/api/sessions/bulk", json={"ids": [a["id"], b["id"]], "charger_id": zap["id"]})
    assert r.json()["updated"] == 2
    got = {s["id"]: s for s in client.get("/api/sessions").json()}
    assert got[a["id"]]["to_reimburse"] and got[a["id"]]["reimbursement_status"] == "recorded"
    assert got[b["id"]]["reimbursement_status"] == "not_recorded"  # kept

    client.patch("/api/sessions/bulk", json={"ids": [a["id"], b["id"], c["id"]], "reimbursement_status": "paid"})
    assert {s["reimbursement_status"] for s in client.get("/api/sessions").json()} == {"paid"}
    assert len(client.get("/api/sessions?status=paid&status=invoiced").json()) == 3
    assert len(client.get(f"/api/sessions?charger_id={zap['id']}").json()) == 3

    client.patch("/api/sessions/bulk", json={"ids": [a["id"]], "clear_charger": True, "to_reimburse": False})
    a2 = client.get(f"/api/sessions/{a['id']}").json()
    assert a2["charger_id"] is None and a2["reimbursement_status"] is None
    assert [s["id"] for s in client.get("/api/sessions?charger_id=0").json()] == [a["id"]]
    lines = client.get("/api/sessions/export.csv").text.splitlines()
    assert "charger;reimbursement_company" in lines[0]
    assert all(line.count(";") == lines[0].count(";") for line in lines)  # every row matches the header


def test_import_with_charger_and_status(client, car):
    zap = client.post("/api/chargers", json={"name": "Zaptec Go", "serial_number": "ZAP-123", "reimbursement_company": "50five"}).json()
    body = {
        "rows": [
            ["2026-01-05", "12", "Zaptec Go", "Payé"],
            ["2026-01-06", "13", "zap 123", "facturé"],
            ["2026-01-07", "14", "", "niet geregistreerd"],
            ["2026-01-08", "15", "", "In progress"],
            ["2026-01-09", "16", "Wallbox X", ""],
            ["2026-01-10", "17", "", "lost"],
        ],
        "mapping": {"charge_date": 0, "kwh": 1, "charger": 2, "reimbursement_status": 3},
        "car_id": car["id"],
    }
    rows = client.post("/api/import/sessions/preview", json=body).json()["rows"]
    assert [(r["status"], r["charger_name"], r["reimbursement_status"]) for r in rows] == [
        ("ok", "Zaptec Go", "paid"),
        ("ok", "Zaptec Go", "invoiced"),
        ("ok", None, "not_recorded"),
        ("ok", None, "in_progress"),
        ("error", None, None),
        ("error", None, None),  # unreadable status: not imported
    ]
    assert all(r["to_reimburse"] for r in rows[:4])
    assert "unknown charger" in rows[4]["errors"][0] and "status" in rows[5]["errors"][0]

    # A fixed charger for every row, no status column: recorded
    fixed = {"rows": [["2026-02-01", "20"]], "mapping": {"charge_date": 0, "kwh": 1}, "car_id": car["id"], "charger_id": zap["id"]}
    assert client.post("/api/import/sessions", json=fixed).json()["imported"] == 1
    s = client.get("/api/sessions").json()[0]
    assert s["charger_name"] == "Zaptec Go" and s["to_reimburse"] and s["reimbursement_status"] == "recorded"
