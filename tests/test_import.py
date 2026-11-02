import io
from datetime import date, time

import openpyxl
import pytest

from ev_charge.importer import detect_date_order, parse_bool, parse_datetime, parse_duration, parse_number, parse_time


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("12,5", 12.5), ("12.5", 12.5), ("1.234,56", 1234.56), ("1,234.56", 1234.56), ("€ 12,50", 12.5), ("30 kWh", 30), ("45 123", 45123), ("", None)],
)
def test_parse_number(raw, expected):
    assert parse_number(raw) == expected


def test_parse_number_rejects_text():
    with pytest.raises(ValueError):
        parse_number("n/a")


def test_parse_dates_and_times():
    assert detect_date_order(["05/01/2026", "28/01/2026"]) == "dmy"
    assert detect_date_order(["01/28/2026"]) == "mdy"
    assert detect_date_order(["2026-01-28"]) == "ymd"
    assert parse_datetime("05/01/2026", "dmy") == (date(2026, 1, 5), None)
    assert parse_datetime("05.01.26 22:15", "dmy") == (date(2026, 1, 5), time(22, 15))
    assert parse_datetime("2026-01-05", "dmy") == (date(2026, 1, 5), None)  # year first wins
    assert parse_datetime("46027", "dmy") == (date(2026, 1, 5), None)  # Excel serial
    assert parse_time("22h30") == time(22, 30)
    assert parse_time("7:05 pm") == time(19, 5)
    assert parse_time("0.9375") == time(22, 30)
    assert parse_time("2230") == time(22, 30)
    assert parse_duration("1:30") == 90 and parse_duration("1h30") == 90 and parse_duration("45 min") == 45 and parse_duration("2,5 h") == 150
    assert parse_bool("Oui") is True and parse_bool("nee") is False and parse_bool("") is None


def test_sheet_preview_and_import(client, car):
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Charges"
    ws.append(["My charging log"])
    ws.append([])
    ws.append(["Date", "Voiture", "Kilométrage", "Lieu", "Fournisseur", "kWh", "Début", "Fin", "À rembourser", "Coût (€)"])
    ws.append([date(2026, 1, 5), "Ioniq 5", 1200, "Home", "Wallbox", 30.5, time(22, 0), time(2, 0), "oui", 8.4])
    ws.append([date(2026, 1, 9), "ioniq5", 1500, "Ionity A4", "Ionity", "41,2", "12:10", "12:40", "non", "28,02"])
    ws.append([date(2026, 1, 9), "Ioniq 5", 1500, "Ionity A4", "Ionity", 41.2, "12:10", "12:40", "non", 28.02])  # duplicate in the file
    ws.append([date(2026, 1, 12), "Model Y", 300, "Office", "Engie", 22, None, None, "x", None])  # unknown car
    ws.append(["Total", None, None, None, None, 93.7])  # totals row → error
    buf = io.BytesIO()
    wb.save(buf)

    sheet = client.post("/api/import/sheet", files={"file": ("log.xlsx", buf.getvalue())}).json()["sheets"][0]
    assert sheet["name"] == "Charges"
    header_row = 2
    rows = sheet["rows"][header_row + 1 :]
    mapping = {"charge_date": 0, "car": 1, "odometer": 2, "location": 3, "provider": 4, "kwh": 5, "start_time": 6, "end_time": 7, "to_reimburse": 8, "cost": 9}
    body = {"rows": rows, "first_row_number": header_row + 2, "mapping": mapping}

    prev = client.post("/api/import/sessions/preview", json=body).json()
    statuses = [(r["row"], r["status"]) for r in prev["rows"]]
    assert statuses == [(4, "ok"), (5, "ok"), (6, "duplicate"), (7, "error"), (8, "error")]
    first = prev["rows"][0]
    assert first["charge_date"] == "2026-01-05" and first["start_time"] == "22:00" and first["to_reimburse"] is True and first["cost"] == 8.4
    assert prev["rows"][1]["kwh"] == 41.2 and prev["rows"][1]["car_name"] == "Ioniq 5"
    assert prev["summary"]["to_import"] == 2

    # Creating the unknown car makes its row importable
    prev = client.post("/api/import/sessions/preview", json=body | {"create_missing_cars": True}).json()
    assert prev["summary"]["new_cars"] == ["Model Y"] and prev["summary"]["to_import"] == 3

    res = client.post("/api/import/sessions", json=body | {"create_missing_cars": True}).json()
    assert res["imported"] == 3 and res["cars_created"] == ["Model Y"]
    assert len(client.get("/api/sessions").json()) == 3

    # Importing the same file again: everything is a duplicate
    again = client.post("/api/import/sessions/preview", json=body).json()
    assert [r["status"] for r in again["rows"]][:3] == ["duplicate", "duplicate", "duplicate"]
    assert client.post("/api/import/sessions", json=body).json()["imported"] == 0


def test_import_with_fixed_car_and_duration(client, car):
    body = {
        "rows": [["2026-02-01 21:30", "35,5", "3h"], ["", "", ""]],
        "mapping": {"charge_date": 0, "kwh": 1, "duration": 2},
        "car_id": car["id"],
        "default_to_reimburse": True,
    }
    rows = client.post("/api/import/sessions/preview", json=body).json()["rows"]
    assert len(rows) == 1  # blank row skipped
    assert rows[0]["start_time"] == "21:30" and rows[0]["end_time"] == "00:30" and rows[0]["to_reimburse"] is True
    assert client.post("/api/import/sessions/preview", json={"rows": [], "mapping": {"kwh": 0}}).status_code == 422  # no car


def test_import_rejects_bad_file(client):
    r = client.post("/api/import/sheet", files={"file": ("x.xls", b"garbage")})
    assert r.status_code == 422 and ".xlsx" in r.json()["detail"]
