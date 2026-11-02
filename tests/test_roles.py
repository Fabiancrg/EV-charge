def _session(c, car_id, **kw):
    return c.post("/api/sessions", json={"car_id": car_id, "charge_date": "2026-03-01", "kwh": 20} | kw)


def test_driver_sees_and_edits_only_assigned_cars(client, car, make_driver):
    other = client.post("/api/cars", json={"name": "Model 3"}).json()
    other_session = _session(client, other["id"]).json()
    dave = make_driver([car["id"]])

    assert [c["name"] for c in dave.get("/api/cars").json()] == ["Ioniq 5"]
    assert dave.get(f"/api/cars/{other['id']}").status_code == 403
    assert dave.post("/api/cars", json={"name": "Mine"}).status_code == 403
    assert dave.put(f"/api/cars/{car['id']}", json={"name": "Renamed"}).status_code == 403

    mine = _session(dave, car["id"], location="Office")
    assert mine.status_code == 201 and mine.json()["created_by"] == "Dave Driver"
    assert _session(dave, other["id"]).status_code == 403
    assert [s["car_name"] for s in dave.get("/api/sessions").json()] == ["Ioniq 5"]
    assert len(dave.get(f"/api/sessions?car_id={other['id']}").json()) == 0
    assert dave.get(f"/api/sessions/{other_session['id']}").status_code == 404
    assert dave.delete(f"/api/sessions/{other_session['id']}").status_code == 404
    body = {"car_id": other["id"], "charge_date": "2026-03-01", "kwh": 5}
    assert dave.put(f"/api/sessions/{mine.json()['id']}", json=body).status_code == 403  # can't move a session to another car
    r = dave.patch("/api/sessions/reimburse", json={"ids": [mine.json()["id"], other_session["id"]], "to_reimburse": True})
    assert r.json()["updated"] == 1
    assert "Model 3" not in dave.get("/api/sessions/export.csv").text

    rep = dave.get("/api/reports").json()
    assert [c["name"] for c in rep["cars"]] == ["Ioniq 5"] and rep["totals"]["sessions"] == 1
    assert dave.get(f"/api/reports?car_id={other['id']}").json()["totals"]["sessions"] == 0


def test_driver_admin_pages_and_apis(client, car, make_driver):
    dave = make_driver([car["id"]])
    assert dave.get("/users", follow_redirects=False).headers["location"] == "/"
    assert dave.get("/tariffs", follow_redirects=False).headers["location"] == "/"
    assert dave.get("/api/users").status_code == 403
    assert dave.post("/api/tariffs", json={"valid_from": "2026-01-01", "price_per_kwh": 0.3}).status_code == 403
    assert dave.put("/api/tariffs/home-locations", json={"locations": []}).status_code == 403
    assert dave.get("/api/tariffs").status_code == 200  # read-only
    assert dave.get("/").status_code == 200


def test_driver_without_cars(client, car, make_driver):
    dave = make_driver([])
    _session(client, car["id"])
    assert dave.get("/api/cars").json() == []
    assert dave.get("/api/sessions").json() == []
    assert dave.get("/api/reports").json()["totals"]["sessions"] == 0


def test_driver_import_scope(client, car, make_driver):
    other = client.post("/api/cars", json={"name": "Model 3"}).json()
    dave = make_driver([car["id"]])
    body = {"rows": [["2026-01-05", "Ioniq 5", "10"], ["2026-01-06", "Model 3", "11"]], "mapping": {"charge_date": 0, "car": 1, "kwh": 2}}
    rows = dave.post("/api/import/sessions/preview", json=body).json()["rows"]
    assert [r["status"] for r in rows] == ["ok", "error"] and "not assigned to you" in rows[1]["errors"][0]
    assert dave.post("/api/import/sessions/preview", json=body | {"create_missing_cars": True}).status_code == 403
    fixed = {"rows": [["2026-01-05", "10"]], "mapping": {"charge_date": 0, "kwh": 1}, "car_id": other["id"]}
    assert dave.post("/api/import/sessions", json=fixed).status_code == 403
    assert dave.post("/api/import/sessions", json=body).json()["imported"] == 1
    assert dave.get("/api/sessions").json()[0]["created_by"] == "Dave Driver"


def test_car_colour_follows_the_car(client, car, make_driver):
    other = client.post("/api/cars", json={"name": "Model 3"}).json()
    assert other["color_index"] == 1
    dave = make_driver([other["id"]])
    assert dave.get("/api/cars").json()[0]["color_index"] == 1  # same colour as for the admin
