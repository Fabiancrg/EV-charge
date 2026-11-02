# EV Charge

Record the charging sessions of your EV cars and report on them: energy, distance, consumption, cost, sessions to be reimbursed, and lease mileage.

Same stack and look as Flow Manager: FastAPI, SQLAlchemy + Alembic (SQLite), Jinja pages that load their data from a JSON API, and Chart.js for the charts (vendored, so it works offline).

## Users and roles

On first start, every page leads to **/setup**, where you create the first administrator. Everybody else gets an account from the **Users** page.

| Role | Can do |
|---|---|
| **Administrator** | Everything: cars, chargers, every session, tariffs, users. |
| **Driver** | Works only with the **cars assigned to them**, on the Users page. A driver can add, edit, delete, import and export those cars' sessions, and see their dashboard, reports and reimbursement statements. Their cars are read-only for them, and the Tariffs and Users pages aren't available. |

Details:

- **Sign-in:** username and password.
  - Passwords are hashed with scrypt and need at least 8 characters.
  - A sign-in lasts `session_days` (30 by default).
  - After 8 failed attempts in 15 minutes, sign-in for that username or address is blocked for a while.
- **Account changes:** everyone can change their own password (👤 menu), which signs out their other browsers. Disabling a user, or changing their password from the Users page, signs them out.
- **Safeguards:** the last active administrator can't be demoted, disabled or deleted, and you can't delete your own account. Deleting a user keeps their sessions.
- **Entered by:** each session records who entered it, shown in the sessions list and CSV export.
- **Locked out?** Run `ev-charge --reset-password USERNAME` on the server. It asks for the new password and reactivates the account.
- **Cross-site protection:** the cookie is HttpOnly and SameSite=Lax, and requests that change data are refused when they come from another site.

The app still listens on `127.0.0.1` by default. To reach it from your phone or another computer, set `host` to `0.0.0.0`, and use HTTPS: set `ssl_certfile`/`ssl_keyfile`, or put it behind a reverse proxy that terminates HTTPS.

## Chargers and reimbursement status

The **Chargers** page (administrators) lists the chargers whose sessions are reimbursed: a home wall box such as a Zaptec Go, a mobile charger such as an Ohme…

- **Fields:** name, brand, model, serial number, the **reimbursing company** (the one that pays the sessions back, e.g. *50five* or *Enco e-Mobility*), whether its sessions are priced at the home tariff (yes by default), notes, active flag.
- **In a session:** picking a charger makes the session *to be reimbursed* and, when the charger uses the home tariff, prices it at the CREG tariff of its date (see below). Location no longer matters.
- **Rules:** a charger used by sessions can't be deleted; mark it inactive. An inactive charger isn't offered for new sessions, and older sessions keep it.

Each session to be reimbursed has a **reimbursement status**:

| Status | Meaning |
|---|---|
| ⏳ **In progress** | Still being recorded (reserved for a future automatic link with the charger) |
| ⚠ **Not recorded** | The session wasn't exported to the reimbursing company |
| ✓ **Recorded** | The charger recorded it with the reimbursing company (default for a charger's session) |
| 🧾 **Invoiced** | The invoice from the reimbursing company was received |
| 💰 **Paid** | The amount was paid |

- **Defaults:** a new session to be reimbursed starts as *Recorded* with a charger, *Not recorded* without one. A session not to be reimbursed has no status. After upgrading, sessions already flagged *to be reimbursed* start as *Not recorded*.
- **Sessions page:** charger and status columns, *Status* and *Charger* filters (*Not paid yet* shows everything but *Paid*), and bulk *Set status…* / *Set charger…* for the selected rows.
  - *☑ Select all N matching* selects every session matching the filters, on all pages; changing a filter starts a new selection.
  - Changes to more than 25 sessions ask for confirmation.
  - Typical flows: filter *Charger: No charger* plus a period or a search → select all matching → *Set charger…*. Or filter a quarter → select all matching → *Invoiced* when the invoice arrives → *Paid* when the money is in.
- **Reimbursement statement:** filters on status and **reimbursing company**, so the statement for 50five can be checked against its invoice. The charger and status are shown per session, totals per status, and both appear in the Excel and CSV exports.
- **Import:** a *Charger* column (matched on the charger's name or serial number) and a *Status* column are recognised. Statuses can be written as *Paid*, *payé*, *betaald*, *facturé*, *niet geregistreerd*… Without a charger column, one charger can be set for every row, and without a status column, a status for every row to be reimbursed.

## Home charging tariffs

The **Tariffs** page (administrators) holds the price per kWh of home charging, as periods (usually quarters: the CREG reimbursement rate).

- **Which sessions:** sessions made with a charger that uses the home tariff. Their cost is **kWh × the tariff valid on its charge date**, whatever cost was typed in; the form shows the tariff and the resulting amount.
- **Other sessions:** they keep the cost entered.
- **Repricing:** costs are computed when read. Adding or changing a tariff, or the charger of a session, reprices the matching sessions everywhere: the list, reports, statements and exports.
- **Periods:**
  - Periods can't overlap.
  - An empty end date means "until further notice".
  - *＋ Next quarter* pre-fills the quarter after the last period, with the last price.
- **⟳ Import from CREG:** reads the official quarterly amounts from the [CREG page](https://www.creg.be/fr/consommateurs/prix-et-tarifs/tarif-de-remboursement-pour-la-recharge-a-domicile-des-voitures-de-0). These are the maximum amounts per kWh an employer can pay back for home charging, set by the SPF Finances (circular 2024/C/77).
  - Choose the region of the driver's home: Wallonia, Brussels or Flanders. The choice is remembered.
  - Each published quarter is compared with your tariffs:
    - *new*: nothing covers it yet
    - *price differs*: same dates, other price
    - *up to date*
    - *overlaps*: one of your periods covers part of the quarter with other dates; edit or delete it first
  - New and changed quarters are pre-selected, and nothing is saved until you click *Apply*. Imported periods are labelled `CREG Q4 2026 (Wallonia)` and record their source and date.
  - The page is read on the server (Python standard library, honouring `HTTPS_PROXY`). If the server can't reach the internet, save the CREG page from your browser (HTML only) and use *Upload the saved page*. Its URL is the `creg_url` setting; the Dutch page works too.
  - Columns are found by their region names, so the import survives the columns being reordered. If the CREG changes the page layout, the import says so and you enter the tariff by hand.
- **Warnings:**
  - The dashboard reminds administrators when no tariff covers today, with a link to the import.
  - The Tariffs page shows gaps between periods, and a last period that has ended.
  - Charger sessions on a date with no tariff show 🏠 *no tariff* and count as 0.
  - The reports point those sessions out.

## Pages

| Page | What it does |
|---|---|
| **Dashboard** `/` | One card per active car: odometer, last charge, total kWh, and a **lease progress** block (days left; km driven against the allowance, with a marker at the pro-rata allowance to date and the projected km at lease end). ⚡ *Log a charge* opens the session form. Shows the 10 most recent sessions. |
| **Sessions** `/sessions` | Every session with filters (car, period presets including this/last quarter or custom dates, reimbursement, text search on location/provider/notes); filters are kept in the URL. Sortable columns, pages of 25/50/100 rows or all (remembered per browser), a totals footer for the whole filtered set (kWh, cost, to reimburse). Click a row (or ✏) to edit or delete it, and ⧉ to duplicate it as a new session. Select rows to mark them *to reimburse* / *not to reimburse* in bulk. ⬇ *Export CSV* exports the filtered rows (`;`-separated, opens in Excel). ⬆ *Import* starts the Excel/CSV import wizard. |
| **Reports** `/reports` | Car and period filters (default: last 12 months). Stat tiles (sessions, kWh, km, kWh/100 km, average per session, cost, average price per kWh, to reimburse). Charts: kWh per month (stacked by car), km per month, to reimburse vs. not, kWh by provider, kWh by location, and, when one car with a lease is selected, **lease mileage** (odometer readings against the allowance line). A monthly summary table repeats the numbers. |
| **Reimbursement** `/reimbursement` | A printable statement of the sessions flagged *to be reimbursed* in a period (default: last quarter) for a car. Each session is priced with the home tariff or the cost entered, and totals are split into home and other charging, with signature lines. *🖨 Print / PDF* uses the browser's print (save as PDF). *⬇ Excel* and *⬇ CSV* export the same rows. |
| **Chargers** `/chargers` | Chargers whose sessions are reimbursed, and the company paying them back (administrators, see above). |
| **Tariffs** `/tariffs` | Home charging price periods and the CREG import (administrators, see above). |
| **Users** `/users` | Accounts, roles and the cars assigned to each driver (administrators). |
| **Cars** `/cars` | Add and edit cars: identity (name, make, model, year, plate, VIN, colour), specs (usable battery, max AC/DC power, WLTP range and consumption), leasing (company, contract, start/end, km per year, monthly cost, odometer at start), notes, active flag. A car with sessions can't be deleted; mark it inactive instead. |

### Charging session fields

Car, **charge date**, odometer (km), location, provider, **kWh**, start time, end time, *to be reimbursed* flag and its **reimbursement status**. Charger, cost and notes are optional. An end time earlier than the start time means the session ended the next day.

Derived values: duration, average power (kW), price per kWh, and **Δ km** since the car's previous reading. A negative Δ km is flagged ⚠ in the list because it usually means a typo in the odometer.

The session form helps you fill it in:
- it suggests the locations and providers you used before
- picking a known location fills in the provider, charger and reimbursement flag used there last time
- picking a charger ticks *to be reimbursed*, sets the status to *Recorded* and shows the tariff amount
- it shows the car's last odometer reading
- *Save & add another* keeps the car and the date for the next entry

### Report calculations

- **Distance** comes from consecutive odometer readings of the same car. A pair counts in the month of its later reading, so the first session of a period still gets the distance since the reading before it.
- **Consumption** is the kWh charged at the later reading of each pair divided by the distance of the pair.
- **Lease allowance** is km/year × the contract length in calendar months, so a 36-month lease at 20,000 km/year gives exactly 60,000 km, leap years included. It counts from the *odometer at start*.
- **Car colours** follow the car (the order cars were created in), not the filter, so a car keeps its colour on every chart.

## Import from Excel / CSV

*Sessions → ⬆ Import* accepts `.xlsx`, `.xlsm` and `.csv` files. It uses the same wizard as Flow Manager's flow import, in three steps:

1. **Sheet & header row**
   - Pick the tab: the one that looks most like a session list is pre-selected.
   - Pick the header row: it is auto-detected; click a row in the preview to change it.
2. **Columns**
   - Each column is mapped from its header name. English, French and Dutch names are recognised (*Date / Datum*, *Lieu / Locatie*, *Énergie (kWh)*, *À rembourser / Terug te betalen*, *Bedrag (€)*…). Change any mapping, or set a column to *not imported*.
   - Required: a date and the kWh.
   - **Car**: if no column holds the car, choose the car for every row. A car column is matched on the car **name or license plate**. Tick *Create the cars that don't exist yet* to add unknown ones.
   - **Date order**: detected automatically (day/month/year unless the values say otherwise), or forced.
   - A **duration** column (`1:30`, `1h30`, `90`, `2,5 h`) gives the end time when there is no end-time column.
   - **Reimbursement**: with no reimbursement column, you can mark every imported row as *to be reimbursed*.
3. **Review & import**
   - The server parses every row; nothing is stored yet. Each row is ✓ OK, ≡ duplicate or ✖ error, with the reason (unknown car, unreadable date, missing kWh…).
   - Rows with errors are not imported. A totals row at the bottom of the sheet simply shows up as an error.
   - **Duplicates** are rows with the same car, date, kWh and odometer as a session already recorded, or as an earlier row in the file. They are skipped by default, so importing the same file twice does nothing.

Values are read leniently:

| Kind | Accepted |
|---|---|
| Numbers | `12,5`, `1.234,56`, `1,234.56`, `€ 12,50`, `30 kWh` |
| Dates | Excel dates, `31/12/2026`, `31.12.26`, `2026-12-31`. A date-time cell also gives the start time. |
| Times | `22:30`, `22h30`, `2230`, `7:05 pm`, Excel times |
| Yes / no | `yes`/`no`, `x`, `1`/`0`, `oui`/`non`, `ja`/`nee`, `true`/`false` |

API: `POST /api/import/sheet` (file → rows of text), `POST /api/import/sessions/preview`, `POST /api/import/sessions`. The spreadsheet reader (`spreadsheet.py`) is ported from Flow Manager; keep the two in step.

## Running

Requires Python 3.12+ and [uv](https://github.com/astral-sh/uv).

```bash
uv sync
```

```bash
uv run ev-charge
```

Then open http://127.0.0.1:8000. The interactive API docs are at `/docs`.

### Configuration

`config.json` in the working directory, `--config path`, or the `CONFIG_FILE` environment variable (see `config.example.json`). Every key is optional:

| Key | Default | |
|---|---|---|
| `host` / `port` | `127.0.0.1` / `8000` | `--host` / `--port` override them |
| `data_dir` | `~/.ev-charge` (Windows: `%LOCALAPPDATA%\ev-charge`) | Holds the database |
| `database_url` | `sqlite:///{data_dir}/ev_charge.db` | Also the `DATABASE_URL` env var |
| `currency` | `EUR` | ISO code used to display costs |
| `ssl_certfile` / `ssl_keyfile` | none | Serve HTTPS when both are set |
| `log_level` | `INFO` | |
| `session_days` | `30` | How long a sign-in lasts |
| `creg_url` | the CREG home-charging tariff page (FR) | Page read by *Import from CREG* |

The database schema is created and migrated (Alembic) at startup. To add a migration after changing `models.py`:

```bash
uv run alembic -c src/ev_charge/alembic.ini revision --autogenerate -m "describe the change"
```

### Single-file build

`./build_shiv.sh` builds `dist/ev_charge.pyz`, a self-contained zipapp, the same way as Flow Manager. Run it with:

```bash
python ev_charge.pyz --config config.json
```

## API

Every endpoint needs a signed-in user (session cookie); drivers only see and change their cars' data.

| Method & path | |
|---|---|
| `GET/POST /api/cars`, `GET/PUT/DELETE /api/cars/{id}` | Cars, with session count, total kWh, last odometer and lease status |
| `GET/POST /api/sessions`, `GET/PUT/DELETE /api/sessions/{id}` | Sessions. Filters: `car_id`, `status`, `charger_id` (repeatable; `0` = no charger), `date_from`, `date_to`, `to_reimburse`, `q`, `limit` |
| `GET /api/sessions/export.csv` | Same filters as the list |
| `GET /api/sessions/suggestions?car_id=` | Values that pre-fill the session form |
| `GET /api/reports` | Report data. Filters: `car_id`, `date_from`, `date_to` |
| `GET /api/reports/reimbursement` | Reimbursement statement. Same filters plus `status` (repeatable) and `company` (empty: sessions without a charger), `fmt=json\|csv\|xlsx` |
| `GET/POST /api/tariffs`, `PUT/DELETE /api/tariffs/{id}` | Tariff periods (changes: administrators) |
| `GET/POST /api/chargers`, `PUT/DELETE /api/chargers/{id}` | Chargers (changes: administrators) |
| `PATCH /api/sessions/bulk` | `{"ids": [...], "reimbursement_status": "paid", "charger_id": 1, "clear_charger": false, "to_reimburse": true}`: only the fields given change |
| `GET /api/tariffs/creg?region=`, `POST /api/tariffs/creg/upload`, `POST /api/tariffs/creg/apply` | CREG import: preview (from the website or a saved page), then apply the chosen quarters (administrators) |
| `POST /api/auth/login\|logout\|password\|setup`, `GET /api/auth/me` | Sign-in and own account |
| `GET/POST /api/users`, `PUT/DELETE /api/users/{id}` | Users (administrators) |
| `POST /api/import/...` | Import wizard (see above) |

## Development

```bash
uv run pytest
```

```bash
uv run ruff check . && uv run ruff format --check .
```
