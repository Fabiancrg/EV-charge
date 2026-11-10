"""Reimbursement documents: reading the two formats, matching their lines with sessions, payments to receive."""

from datetime import datetime

import pytest
from sqlalchemy import create_engine, inspect, text

from ev_charge.database import run_migrations
from ev_charge.reconcile import DocumentError, parse_document, parse_lms_text

ENECO_HEADER = (
    '"Version";"Invoice Number";"Invoice Type";"Invoice Amount Excl";"Invoice Amount Incl";"Invoice VAT Amount";"Invoice Description";"Invoice Date";"Invoice Due Date";'
    '"Currency Code";"Invoice From Name";"Invoice To Name";"Item ID";"Item Description";"Session Start Date";"Session Start Time";"Session End Date";"Session End Time";'
    '"Session Duration";"Session Period";"Volume";"Volume Unit";"Item Amount Excl";"Item Amount Incl"'
)
# (item id, start date, start time, end date, end time, period, kWh, amount)
ENECO_ROWS = [
    ("ID_1", "01-10-2026", "09:28:40", "01-10-2026", "10:42:32", "202610", "2,713", "-1,03"),
    ("ID_2", "02-10-2026", "10:48:14", "02-10-2026", "17:10:29", "202610", "11,336", "-4,29"),
    ("ID_3", "03-10-2026", "22:11:12", "04-10-2026", "03:10:57", "202610", "6,704", "-2,54"),
    ("ID_4", "05-10-2026", "08:00:00", "05-10-2026", "08:01:00", "202610", "0,007", "0,00"),
]


def eneco_csv(rows=ENECO_ROWS, number="BEX26-000001", total="-7,86"):
    lines = [ENECO_HEADER]
    for item, d1, t1, d2, t2, period, kwh, amount in rows:
        cells = [
            "1.5",
            number,
            "Credit Reimbursement Document",
            total,
            total,
            "0,00",
            "Service Transport Électrique",
            "10-11-2026",
            "24-11-2026",
            "EUR",
            "Eneco eMobility B.V.",
            "Dana Driver",
        ]
        cells += [item, "SmartCable", d1, t1, d2, t2, "60", period, kwh, "kWh", amount, amount]
        lines.append(";".join(f'"{c}"' for c in cells))
    return ("\n".join(lines) + "\n").encode()


LMS_TEXT = """
Dana Driver
Rue de l'Exemple 1
DOCUMENT DE REMBOURSEMENT
De la part de votre employeur pour des sessions de charge au domicile
Numéro du
document
032600000000001 Numéro client 100001
Date de facture 10-janv.-2027
Période du
document
déc.-2026
Abonnement Numéro Prix unitaire Prix
Remboursement des sessions de charge au domicile 19,35 kWh EUR 7,31
Sous-total EUR 7,31
Total à recevoir en EUR EUR 7,31
réglées financièrement via la plateforme et le réseau de Last Mile Solutions. Le contrat avec 50five
CoC Rotterdam n°: 24360819 Banque (IBAN): NL00XXXX0000000000
Annexe : Remboursement des sessions de charge au domicile
ID Site Période Coûts
408884271
BE-LMS-000001-2
NLLMSE0000000001*1 01-déc. 17:34
02-déc. 06:19
12:44 heures
Energie 9,79 kWh EUR 0,3779 / kWh EUR 3,70
Rue de l'Exemple 1, Ville,
BE TOTAL EUR 3,70

409246051
BE-LMS-000001-2
NLLMSE0000000001*1 31-déc. 18:31
01-janv. 09:59
15:28 heures
Energie 9,56 kWh EUR 0,3779 / kWh EUR 3,61
Rue de l'Exemple 1, Ville,
BE TOTAL EUR 3,61
"""


def minimal_pdf(text: str) -> bytes:
    """A one-page PDF showing the text line by line (enough for a text extractor)."""
    esc = lambda s: s.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")  # noqa: E731
    stream = ("BT /F1 9 Tf 40 800 Td 11 TL\n" + "\n".join(f"({esc(line)}) Tj T*" for line in text.splitlines()) + "\nET").encode("cp1252")
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>",
    ]
    out, offsets = b"%PDF-1.4\n", []
    for n, obj in enumerate(objects, start=1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % n + obj + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1) + b"".join(b"%010d 00000 n \n" % o for o in offsets)
    return out + b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objects) + 1, xref)


# ── Parsing ──────────────────────────────────────────────────────────────────


def test_parse_eneco_csv():
    doc = parse_document("BEX26-000001.csv", eneco_csv())
    assert (doc.source, doc.number, doc.issuer) == ("eneco", "BEX26-000001", "Eneco eMobility B.V.")
    assert (doc.document_date.isoformat(), doc.due_date.isoformat(), doc.period) == ("2026-11-10", "2026-11-24", "2026-10")
    assert (doc.amount, doc.kwh, len(doc.lines)) == (7.86, 20.76, 4)  # the credit is an amount to receive
    line = doc.lines[2]
    assert (line.external_id, line.started_at, line.ended_at, line.kwh, line.amount) == ("ID_3", datetime(2026, 10, 3, 22, 11, 12), datetime(2026, 10, 4, 3, 10, 57), 6.704, 2.54)
    assert doc.lines[3].amount == 0  # not "-0.0"

    with pytest.raises(DocumentError, match="Eneco"):
        parse_document("sessions.csv", b"date;kwh\n2026-01-01;12\n")
    with pytest.raises(DocumentError, match="several documents"):
        parse_document("two.csv", eneco_csv() + eneco_csv(number="BEX26-000002").split(b"\n", 1)[1])
    with pytest.raises(DocumentError, match="Unknown file"):
        parse_document("photo.jpg", b"\xff\xd8\xff\xe0 not a document")
    with pytest.raises(DocumentError, match="empty"):
        parse_document("empty.csv", b"")


def test_parse_lms_document():
    doc = parse_lms_text(LMS_TEXT)
    assert (doc.source, doc.number, doc.issuer) == ("lms", "032600000000001", "Last Mile Solutions (50five)")
    assert (doc.document_date.isoformat(), doc.due_date, doc.period, doc.amount, doc.kwh) == ("2027-01-10", None, "2026-12", 7.31, 19.35)
    first, second = doc.lines
    assert (first.external_id, first.started_at, first.ended_at, first.kwh, first.amount) == ("408884271", datetime(2026, 12, 1, 17, 34), datetime(2026, 12, 2, 6, 19), 9.79, 3.70)
    # No year in the annex: 31 December is in the document's year, the end on 1 January in the next one
    assert (second.started_at, second.ended_at) == (datetime(2026, 12, 31, 18, 31), datetime(2027, 1, 1, 9, 59))

    assert [ln.external_id for ln in parse_document("doc.pdf", minimal_pdf(LMS_TEXT)).lines] == ["408884271", "409246051"]  # through the PDF reader
    with pytest.raises(DocumentError, match="No charging session"):
        parse_document("other.pdf", minimal_pdf("Invoice 12\nNothing to see"))
    with pytest.raises(DocumentError, match="can't be read"):
        parse_document("broken.pdf", b"%PDF-1.4 broken")


# ── Reconciliation ───────────────────────────────────────────────────────────

BASE = "/api/reimbursement-documents"


def _session(c, car_id, day, start=None, kwh=10.0, **kw):
    body = {"car_id": car_id, "charge_date": day, "kwh": kwh, "start_time": start, "to_reimburse": "charger_id" in kw} | kw
    r = c.post("/api/sessions", json=body)
    assert r.status_code == 201, r.text
    return r.json()


def _upload(c, content, name="doc.csv", path="", **params):
    return c.post(f"{BASE}{path}", files={"file": (name, content)}, params=params)


def _statuses(c):
    return {s["id"]: s["reimbursement_status"] for s in c.get("/api/sessions").json()}


@pytest.fixture()
def home(client, car):
    """A charger at the home tariff, and three of the four sessions of the Eneco document (one differs in energy)."""
    client.post("/api/tariffs", json={"valid_from": "2026-10-01", "valid_to": "2026-12-31", "price_per_kwh": 0.3779})
    ohme = client.post("/api/chargers", json={"name": "Ohme", "reimbursement_company": "Eneco eMobility"}).json()
    a = _session(client, car["id"], "2026-10-01", "09:29", 2.713, charger_id=ohme["id"])
    b = _session(client, car["id"], "2026-10-02", "10:50", 11.34, charger_id=ohme["id"])
    c = _session(client, car["id"], "2026-10-03", None, 6.7, charger_id=ohme["id"])  # no start time: day and energy
    other = _session(client, car["id"], "2026-10-04", "12:00", 8, charger_id=ohme["id"])  # not on the document
    private = _session(client, car["id"], "2026-10-02", "10:48", 11.336)  # not to be reimbursed
    return {"charger": ohme, "a": a, "b": b, "c": c, "other": other, "private": private}


def test_preview_matches_lines_without_storing(client, home):
    r = _upload(client, eneco_csv(), path="/preview")
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["document"]["number"] == "BEX26-000001" and data["document"]["charger_id"] == home["charger"]["id"]  # found from the matches
    assert not data["already_imported"]
    assert [[s["id"] for s in ln["sessions"]] for ln in data["lines"]] == [[home["a"]["id"]], [home["b"]["id"]], [home["c"]["id"]], []]
    assert data["summary"] | {"sessions_amount": 0} == {
        "lines": 4, "matched": 3, "unmatched": 1, "sessions": 3, "grouped": 0, "document_amount": 7.86, "lines_amount": 7.86, "matched_amount": 7.86, "unmatched_amount": 0,
        "sessions_amount": 0, "kwh_mismatches": 0, "price_mismatches": 0, "time_offset_minutes": 0,
    }  # fmt: skip
    assert data["summary"]["sessions_amount"] == pytest.approx(7.86, abs=0.02)  # priced at the same tariff
    assert [s["id"] for s in data["unlisted_sessions"]] == [home["other"]["id"]]
    assert set(_statuses(client).values()) == {"recorded", None}  # nothing changed

    # A few cents because the two meters differ a little isn't a price difference; another tariff is
    client.put(f"/api/sessions/{home['b']['id']}", json={k: home["b"][k] for k in ("car_id", "charge_date", "start_time", "charger_id", "to_reimburse")} | {"kwh": 11.45})
    line = _upload(client, eneco_csv(), path="/preview").json()["lines"][1]
    assert (line["amount_diff"], line["kwh_mismatch"], line["price_mismatch"]) == (0.04, False, False)
    tariff = client.get("/api/tariffs").json()[0]
    client.put(f"/api/tariffs/{tariff['id']}", json={"valid_from": "2026-10-01", "valid_to": "2026-12-31", "price_per_kwh": 0.36})
    data = _upload(client, eneco_csv(), path="/preview").json()
    assert [ln["price_mismatch"] for ln in data["lines"]] == [True, True, True, False] and data["summary"]["price_mismatches"] == 3
    assert client.get(BASE).json()["documents"] == []

    assert _upload(client, b"date;kwh\n1;2\n", path="/preview").status_code == 422
    assert _upload(client, eneco_csv(), path="/preview", charger_id=999).status_code == 422


def test_import_then_payment(client, home):
    late = eneco_csv().replace(b"24-11-2026", b"24-11-2025")  # due a year ago
    assert _upload(client, late).status_code == 422  # the charger of the document is mandatory
    assert _upload(client, late, charger_id=999).status_code == 422
    r = _upload(client, late, name="BEX26-000001.csv", charger_id=home["charger"]["id"])
    assert r.status_code == 201, r.text
    doc = r.json()["document"]
    assert (doc["status"], doc["overdue"], doc["amount"], doc["matched"], doc["lines"], doc["charger_name"]) == ("to_receive", True, 7.86, 3, 4, "Ohme")
    st = _statuses(client)
    assert [st[home[k]["id"]] for k in ("a", "b", "c", "other", "private")] == ["invoiced", "invoiced", "invoiced", "recorded", None]
    assert _upload(client, eneco_csv(), charger_id=home["charger"]["id"]).status_code == 409  # once only
    assert client.delete(f"/api/chargers/{home['charger']['id']}").status_code == 409
    assert _upload(client, eneco_csv(), path="/preview").json()["already_imported"]

    listing = client.get(BASE).json()
    assert listing["totals"] == {"to_receive": {"documents": 1, "amount": 7.86}, "overdue": {"documents": 1, "amount": 7.86}, "received": {"documents": 0, "amount": 0}}

    # Paid, a little less than expected
    r = client.put(f"{BASE}/{doc['id']}/payment", json={"paid_date": "2026-11-20", "paid_amount": 7.8, "notes": " bank ref 123 "})
    paid = r.json()["document"]
    assert (paid["status"], paid["overdue"], paid["paid_date"], paid["paid_amount"], paid["difference"], paid["notes"]) == ("paid", False, "2026-11-20", 7.8, -0.06, "bank ref 123")
    st = _statuses(client)
    assert [st[home[k]["id"]] for k in ("a", "b", "c", "other")] == ["paid", "paid", "paid", "recorded"]
    assert client.get(BASE).json()["totals"]["received"] == {"documents": 1, "amount": 7.8}
    assert client.put(f"{BASE}/{doc['id']}/payment", json={"paid_amount": -1}).status_code == 422

    # Not paid after all, then paid with the defaults (today, the amount of the document)
    assert client.delete(f"{BASE}/{doc['id']}/payment").json()["document"]["status"] == "to_receive"
    assert _statuses(client)[home["a"]["id"]] == "invoiced"
    again = client.put(f"{BASE}/{doc['id']}/payment", json={}).json()["document"]
    assert (again["paid_amount"], again["difference"], again["notes"]) == (7.86, 0, "bank ref 123")

    # Deleting the document frees its sessions
    assert client.delete(f"{BASE}/{doc['id']}").status_code == 204
    assert set(_statuses(client).values()) == {"recorded", None}
    assert client.get(f"{BASE}/{doc['id']}").status_code == 404
    assert _upload(client, eneco_csv(), charger_id=home["charger"]["id"]).status_code == 201  # and it can be imported again


def test_link_and_unlink_a_line_by_hand(client, car, home):
    data = _upload(client, eneco_csv(), charger_id=home["charger"]["id"]).json()
    doc_id, free_line = data["document"]["id"], data["lines"][3]
    assert free_line["sessions"] == [] and [s["id"] for s in data["unlisted_sessions"]] == [home["other"]["id"]]
    url = f"{BASE}/{doc_id}/lines"

    data = client.put(f"{url}/{free_line['id']}", json={"session_ids": [home["other"]["id"]]}).json()
    assert [s["id"] for s in data["lines"][3]["sessions"]] == [home["other"]["id"]] and data["lines"][3]["kwh_mismatch"]  # 8 kWh for 0.007
    assert data["summary"]["matched"] == 4 and data["unlisted_sessions"] == []
    assert _statuses(client)[home["other"]["id"]] == "invoiced"

    # A session is on one line only; a private session, one of another charger or an unknown one can't be linked
    first = data["lines"][0]
    wallbox = client.post("/api/chargers", json={"name": "Wall box"}).json()
    elsewhere = _session(client, car["id"], "2026-10-01", "09:30", 2.7, charger_id=wallbox["id"])
    for bad in (home["other"]["id"], home["private"]["id"], elsewhere["id"], 99999):
        assert client.put(f"{url}/{first['id']}", json={"session_ids": [home["a"]["id"], bad]}).status_code == 422
    assert client.put(f"{url}/99999", json={"session_ids": []}).status_code == 404
    assert client.put(f"{BASE}/{doc_id}/charger", json={"charger_id": wallbox["id"]}).status_code == 422  # its sessions are the Ohme's

    data = client.put(f"{url}/{free_line['id']}", json={"session_ids": []}).json()
    assert data["lines"][3]["sessions"] == [] and _statuses(client)[home["other"]["id"]] == "recorded"

    # Linked once the document is paid: the session is paid too; a deleted session leaves its line
    client.put(f"{BASE}/{doc_id}/payment", json={})
    client.put(f"{url}/{free_line['id']}", json={"session_ids": [home["other"]["id"]]})
    assert _statuses(client)[home["other"]["id"]] == "paid"
    assert client.delete(f"/api/sessions/{home['other']['id']}").status_code == 204
    assert client.get(f"{BASE}/{doc_id}").json()["summary"]["matched"] == 3


def test_a_line_grouping_several_sessions(client, car):
    """The company made one line of sessions we recorded separately: found when their energies add up, or added by hand."""
    client.post("/api/tariffs", json={"valid_from": "2026-10-01", "price_per_kwh": 0.3779})
    ohme = client.post("/api/chargers", json={"name": "Ohme"}).json()
    rows = [
        ("ID_1", "01-10-2026", "18:00:00", "02-10-2026", "07:00:00", "202610", "12,000", "-4,53"),  # three sessions in the night
        ("ID_2", "03-10-2026", "18:00:00", "04-10-2026", "07:00:00", "202610", "9,000", "-3,40"),  # two sessions, the second far from the total
    ]
    night = [
        _session(client, car["id"], day, start, kwh, charger_id=ohme["id"])
        for day, start, kwh in (("2026-10-01", "18:01", 5), ("2026-10-01", "23:30", 4), ("2026-10-02", "04:10", 3.02))
    ]
    x = _session(client, car["id"], "2026-10-03", "18:02", 5, charger_id=ohme["id"])
    y = _session(client, car["id"], "2026-10-04", "02:00", 2.5, charger_id=ohme["id"])

    data = _upload(client, eneco_csv(rows, total="-7,93"), path="/preview").json()
    one, two = data["lines"]
    assert [s["id"] for s in one["sessions"]] == [n["id"] for n in night]
    assert (one["sessions_kwh"], one["kwh_mismatch"], one["sessions_amount"], one["amount_diff"]) == (12.02, False, 4.54, 0.01)
    assert [s["id"] for s in two["sessions"]] == [x["id"]] and two["kwh_mismatch"]  # 5 + 2.5 isn't 9: only the session started with the line
    assert (data["summary"]["sessions"], data["summary"]["matched"], data["summary"]["grouped"]) == (4, 2, 1)
    assert [s["id"] for s in data["unlisted_sessions"]] == [y["id"]]

    data = _upload(client, eneco_csv(rows, total="-7,93"), charger_id=ohme["id"]).json()
    doc, two = data["document"], data["lines"][1]
    assert (doc["matched"], doc["sessions"]) == (2, 4) and set(_statuses(client).values()) == {"invoiced", "recorded"}

    # Add the second session of the line by hand, then pay: the five sessions are paid
    data = client.put(f"{BASE}/{doc['id']}/lines/{two['id']}", json={"session_ids": [x["id"], y["id"]]}).json()
    assert (data["lines"][1]["sessions_kwh"], data["summary"]["sessions"], data["summary"]["grouped"]) == (7.5, 5, 2)
    client.put(f"{BASE}/{doc['id']}/payment", json={})
    assert set(_statuses(client).values()) == {"paid"}

    # Remove one of the three sessions of the first line: it is free again, the others stay
    data = client.put(f"{BASE}/{doc['id']}/lines/{data['lines'][0]['id']}", json={"session_ids": [night[0]["id"], night[2]["id"]]}).json()
    assert [s["id"] for s in data["unlisted_sessions"]] == [night[1]["id"]] and data["lines"][0]["kwh_mismatch"]
    st = _statuses(client)
    assert (st[night[0]["id"]], st[night[1]["id"]], st[night[2]["id"]]) == ("paid", "recorded", "paid")
    assert client.delete(f"{BASE}/{doc['id']}").status_code == 204
    assert set(_statuses(client).values()) == {"recorded"}


def test_document_in_another_time_zone_and_chosen_charger(client, car):
    """Times written in UTC (two hours behind in summer) still match, and only the chosen charger's sessions are used."""
    ohme = client.post("/api/chargers", json={"name": "Ohme"}).json()
    wallbox = client.post("/api/chargers", json={"name": "Wall box"}).json()
    rows = [
        ("ID_1", "01-07-2026", "07:28:40", "01-07-2026", "08:42:32", "202607", "2,713", "-1,03"),
        ("ID_2", "02-07-2026", "08:48:14", "02-07-2026", "15:10:29", "202607", "11,336", "-4,29"),
    ]
    a = _session(client, car["id"], "2026-07-01", "09:28", 2.71, charger_id=ohme["id"])
    b = _session(client, car["id"], "2026-07-02", "10:48", 11.34, charger_id=ohme["id"])
    _session(client, car["id"], "2026-07-02", "08:50", 5, charger_id=wallbox["id"])  # at the document's clock time, on another charger
    data = _upload(client, eneco_csv(rows, total="-5,32"), path="/preview").json()
    assert data["summary"]["time_offset_minutes"] == 120 and data["document"]["charger_id"] == ohme["id"] and data["document"]["period"] == "2026-07"
    assert [[s["id"] for s in ln["sessions"]] for ln in data["lines"]] == [[a["id"]], [b["id"]]]
    assert data["unlisted_sessions"] == []  # the wall box isn't this document's charger

    data = _upload(client, eneco_csv(rows, total="-5,32"), path="/preview", charger_id=wallbox["id"]).json()
    assert data["summary"]["matched"] == 1 and data["lines"][1]["kwh_mismatch"]


def test_lms_pdf_import(client, car):
    zap = client.post("/api/chargers", json={"name": "Zaptec", "reimbursement_company": "50five"}).json()
    s = _session(client, car["id"], "2026-12-01", "17:30", 9.8, end_time="06:19", charger_id=zap["id"])
    data = _upload(client, minimal_pdf(LMS_TEXT), name="Document_de_remboursement.pdf", charger_id=zap["id"]).json()
    assert (data["document"]["source"], data["document"]["due_date"], data["document"]["overdue"], data["summary"]["matched"]) == ("lms", None, False, 1)
    assert data["lines"][0]["sessions"][0]["id"] == s["id"] and data["lines"][0]["started_at"] == "2026-12-01T17:34:00"


def test_documents_follow_the_car_permissions(client, car, make_driver, home):
    other_car = client.post("/api/cars", json={"name": "Model 3"}).json()
    dave = make_driver([other_car["id"]])
    viewer = make_driver([car["id"]], username="vera", role="Viewer", global_role="Viewer")
    assert viewer.get(BASE).status_code == 403 and _upload(viewer, eneco_csv(), path="/preview").status_code == 403

    # Dave doesn't manage the car of these sessions: nothing matches for him, and he can't see the administrator's document
    assert _upload(dave, eneco_csv(), path="/preview").json()["summary"]["matched"] == 0
    doc_id = _upload(client, eneco_csv(), charger_id=home["charger"]["id"]).json()["document"]["id"]
    assert dave.get(BASE).json()["documents"] == [] and dave.get(f"{BASE}/{doc_id}").status_code == 404
    assert dave.put(f"{BASE}/{doc_id}/payment", json={}).status_code == 404 and dave.delete(f"{BASE}/{doc_id}").status_code == 404
    assert client.get("/payments").status_code == 200 and "Payments" in client.get("/").text
    assert viewer.get("/payments", follow_redirects=False).status_code == 303 and 'href="/payments"' not in viewer.get("/").text


def test_migrations_create_the_document_tables_and_keep_the_links(tmp_path):
    eng = create_engine(f"sqlite:///{tmp_path / 'v7.db'}")
    run_migrations(eng, "007")
    assert "reimbursement_documents" not in inspect(eng).get_table_names()
    run_migrations(eng, "008")  # one session per line, linked from the line
    with eng.begin() as conn:
        conn.execute(text("INSERT INTO cars (id, name, active, created_at, updated_at) VALUES (1, 'EV6', 1, '2026-01-01', '2026-01-01')"))
        conn.execute(
            text(
                "INSERT INTO charge_sessions (id, car_id, charge_date, kwh, to_reimburse, created_at, updated_at) VALUES (7, 1, '2026-02-01', 12.5, 1, '2026-02-01', '2026-02-01')"
            )
        )
        conn.execute(text("INSERT INTO reimbursement_documents (id, source, number, amount, kwh, created_at) VALUES (1, 'eneco', 'X', 5, 13.5, '2026-03-01')"))
        conn.execute(
            text(
                "INSERT INTO reimbursement_lines (id, document_id, position, started_at, kwh, amount, session_id) "
                "VALUES (3, 1, 0, '2026-02-01 10:00:00', 12.5, 4, 7), (4, 1, 1, '2026-02-02 10:00:00', 1, 1, NULL)"
            )
        )
    run_migrations(eng)
    assert "session_id" not in {c["name"] for c in inspect(eng).get_columns("reimbursement_lines")}
    with eng.connect() as conn:
        assert conn.execute(text("SELECT id, reimbursement_line_id FROM charge_sessions")).all() == [(7, 3)]
        assert conn.execute(text("SELECT id, document_id FROM reimbursement_lines ORDER BY id")).all() == [(3, 1), (4, 1)]
    eng.dispose()
