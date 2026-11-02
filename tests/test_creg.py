from datetime import date
from pathlib import Path

import pytest

from ev_charge import creg

PAGE = (Path(__file__).parent / "fixtures" / "creg_fr.html").read_text(encoding="utf-8")


def test_parse_the_real_page():
    quarters = creg.parse_creg(PAGE)
    assert [q.key for q in quarters] == ["2026-Q4", "2026-Q3", "2026-Q2", "2026-Q1", "2025-Q4", "2025-Q3", "2025-Q2", "2025-Q1"]
    q4 = quarters[0]
    assert q4.rates == {"wallonia": 0.3779, "brussels": 0.3688, "flanders": 0.3225}
    assert (q4.valid_from, q4.valid_to) == (date(2026, 10, 1), date(2026, 12, 31))
    assert quarters[-1].rates["flanders"] == 0.2822


def test_parse_follows_column_names_and_dutch():
    # Dutch headers, columns in another order, the "Q3 2026 (01/07-30/09/2026)" row format
    html = """<table><thead><tr><th>Kwartaal</th><th>Vlaanderen</th><th>Wallonië</th><th>Brussel</th></tr></thead>
        <tbody><tr><td><strong>Q3 2026</strong> <em>(01/07-30/09/2026)</em></td><td>32,22</td><td>37,83</td><td>37,19</td></tr></tbody></table>"""
    (q,) = creg.parse_creg(html)
    assert q.key == "2026-Q3" and q.rates == {"flanders": 0.3222, "wallonia": 0.3783, "brussels": 0.3719}


def test_parse_rejects_a_changed_layout():
    with pytest.raises(creg.CregError):
        creg.parse_creg("<html><body><p>Page moved</p><table><tr><td>a</td><td>b</td></tr></table></body></html>")


@pytest.fixture()
def creg_page(monkeypatch):
    monkeypatch.setattr(creg, "fetch_page", lambda url, timeout=20: PAGE)


def test_preview_and_apply(client, creg_page):
    # Existing tariffs: Q4 2026 at another price, and a custom period overlapping Q1 2025
    client.post("/api/tariffs", json={"valid_from": "2026-10-01", "valid_to": "2026-12-31", "price_per_kwh": 0.30})
    client.post("/api/tariffs", json={"valid_from": "2025-02-01", "valid_to": "2025-02-28", "price_per_kwh": 0.30})
    client.post("/api/tariffs", json={"valid_from": "2026-07-01", "valid_to": "2026-09-30", "price_per_kwh": 0.3783})

    prev = client.get("/api/tariffs/creg?region=wallonia").json()
    status = {i["key"]: i["status"] for i in prev["items"]}
    assert status["2026-Q4"] == "changed" and status["2026-Q3"] == "same" and status["2025-Q1"] == "conflict" and status["2026-Q2"] == "new"
    assert prev["items"][0]["price_per_kwh"] == 0.3779 and prev["items"][0]["label"] == "CREG Q4 2026 (Wallonia)"

    apply = [i for i in prev["items"] if i["status"] in ("new", "changed")] + [i for i in prev["items"] if i["status"] == "conflict"]
    r = client.post("/api/tariffs/creg/apply", json={"region": "wallonia", "items": apply, "source": prev["source"]}).json()
    assert r == {"created": 5, "updated": 1, "skipped": ["2025-Q1"]}

    tariffs = {t["valid_from"]: t for t in client.get("/api/tariffs").json()}
    assert tariffs["2026-10-01"]["price_per_kwh"] == 0.3779 and tariffs["2026-10-01"]["notes"].startswith("Imported from the CREG")
    assert tariffs["2025-04-01"]["label"] == "CREG Q2 2025 (Wallonia)"
    # The region is remembered; everything is now up to date except the conflict
    again = client.get("/api/tariffs/creg").json()
    assert again["region"] == "wallonia"
    assert {i["status"] for i in again["items"]} == {"same", "conflict"}
    flanders = client.get("/api/tariffs/creg?region=flanders").json()
    assert next(i for i in flanders["items"] if i["key"] == "2026-Q4")["status"] == "changed"


def test_preview_from_uploaded_page(client):
    r = client.post("/api/tariffs/creg/upload", data={"region": "brussels"}, files={"file": ("creg.html", PAGE.encode())})
    assert r.status_code == 200 and r.json()["items"][0]["price_per_kwh"] == 0.3688
    bad = client.post("/api/tariffs/creg/upload", data={"region": "brussels"}, files={"file": ("x.html", b"<p>nothing</p>")})
    assert bad.status_code == 422


def test_fetch_failure_and_permissions(client, car, make_driver, monkeypatch):
    def offline(url, timeout=20):
        raise creg.CregError("Cannot reach the CREG website")

    monkeypatch.setattr(creg, "fetch_page", offline)
    r = client.get("/api/tariffs/creg")
    assert r.status_code == 502 and "Cannot reach" in r.json()["detail"]
    assert client.get("/api/tariffs/creg?region=mars").status_code == 422
    dave = make_driver([car["id"]])
    assert dave.get("/api/tariffs/creg").status_code == 403
    item = {"key": "2026-Q4", "valid_from": "2026-10-01", "valid_to": "2026-12-31", "price_per_kwh": 0.3}
    assert dave.post("/api/tariffs/creg/apply", json={"region": "wallonia", "items": [item]}).status_code == 403


def test_fetch_page_downloads_and_decodes():
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            body = PAGE.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=UTF-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        html = creg.fetch_page(f"http://127.0.0.1:{server.server_port}/fr/page")
        assert "Wallonie" in html and creg.parse_creg(html)[0].rates["wallonia"] == 0.3779
    finally:
        server.shutdown()
    with pytest.raises(creg.CregError, match="Cannot reach"):
        creg.fetch_page(f"http://127.0.0.1:{server.server_port}/gone", timeout=2)
