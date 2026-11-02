# EV Charge

Record the charging sessions of your EV cars and report on them: energy, distance, consumption, cost, sessions to be reimbursed, and lease mileage.

Same stack and look as Flow Manager: FastAPI, SQLAlchemy + Alembic (SQLite), Jinja pages that load their data from a JSON API, and Chart.js for the charts (vendored, so it works offline).

## Users and roles

On first start, every page leads to **/setup**, where you create the first administrator. Everybody else gets an account from the **Users** page.

| Role | Can do |
|---|---|
| **Administrator** | Everything: cars, every session, tariffs and home locations, users. |
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

## Home charging tariffs

The **Tariffs** page (administrators) holds the price per kWh of home charging, as periods (usually quarters, e.g. the CREG reimbursement rate) and the list of **home locations**.

- **Which sessions:** a session whose location matches a home location is home charging. Matching ignores case and extra spaces. Its cost is **kWh × the tariff valid on its charge date**, whatever cost was typed in; the form shows the tariff and the resulting amount.
- **Other sessions:** they keep the cost entered.
- **Repricing:** costs are computed when read. Adding or changing a tariff, or a home location, reprices the matching sessions everywhere: the list, reports, statements and exports.
- **Periods:**
  - Periods can't overlap.
  - An empty end date means "until further notice".
  - *＋ Next quarter* pre-fills the quarter after the last period, with the last price.
- **Warnings:**
  - The Tariffs page shows gaps between periods, and a last period that has ended.
  - Home sessions on a date with no tariff show 🏠 *no tariff* and count as 0.
  - The reports point those sessions out.

## Pages

| Page | What it does |
|---|---|
| **Dashboard** `/` | One card per active car: odometer, last charge, total kWh, and a **lease progress** block (days left; km driven against the allowance, with a marker at the pro-rata allowance to date and the projected km at lease end). ⚡ *Log a charge* opens the session form. Shows the 10 most recent sessions. |
| **Sessions** `/sessions` | Every session with filters (car, period presets including this/last quarter or custom dates, reimbursement, text search on location/provider/notes); filters are kept in the URL. Sortable columns, pages of 25/50/100 rows or all (remembered per browser), a totals footer for the whole filtered set (kWh, cost, to reimburse). Click a row (or ✏) to edit or delete it, and ⧉ to duplicate it as a new session. Select rows to mark them *to reimburse* / *not to reimburse* in bulk. ⬇ *Export CSV* exports the filtered rows (`;`-separated, opens in Excel). ⬆ *Import* starts the Excel/CSV import wizard. |
| **Reports** `/reports` | Car and period filters (default: last 12 months). Stat tiles (sessions, kWh, km, kWh/100 km, average per session, cost, average price per kWh, to reimburse). Charts: kWh per month (stacked by car), km per month, to reimburse vs. not, kWh by provider, kWh by location, and, when one car with a lease is selected, **lease mileage** (odometer readings against the allowance line). A monthly summary table repeats the numbers. |
| **Reimbursement** `/reimbursement` | A printable statement of the sessions flagged *to be reimbursed* in a period (default: last quarter) for a car. Each session is priced with the home tariff or the cost entered, and totals are split into home and other charging, with signature lines. *🖨 Print / PDF* uses the browser's print (save as PDF). *⬇ Excel* and *⬇ CSV* export the same rows. |
| **Tariffs** `/tariffs` | Home charging price periods and home locations (administrators, see above). |
| **Users** `/users` | Accounts, roles and the cars assigned to each driver (administrators). |
| **Cars** `/cars` | Add and edit cars: identity (name, make, model, year, plate, VIN, colour), specs (usable battery, max AC/DC power, WLTP range and consumption), leasing (company, contract, start/end, km per year, monthly cost, odometer at start), notes, active flag. A car with sessions can't be deleted; mark it inactive instead. |

### Charging session fields

Car, **charge date**, odometer (km), location, provider, **kWh**, start time, end time, *to be reimbursed* flag. Cost and notes are optional. An end time earlier than the start time means the session ended the next day.

Derived values: duration, average power (kW), price per kWh, and **Δ km** since the car's previous reading. A negative Δ km is flagged ⚠ in the list because it usually means a typo in the odometer.

The session form helps you fill it in:
- it suggests the locations and providers you used before
- picking a known location fills in the provider and the reimbursement flag used there last time
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
| `GET/POST /api/sessions`, `GET/PUT/DELETE /api/sessions/{id}` | Sessions. Filters: `car_id` (repeatable), `date_from`, `date_to`, `to_reimburse`, `q`, `limit` |
| `PATCH /api/sessions/reimburse` | `{"ids": [...], "to_reimburse": true}` |
| `GET /api/sessions/export.csv` | Same filters as the list |
| `GET /api/sessions/suggestions?car_id=` | Values that pre-fill the session form |
| `GET /api/reports` | Report data. Filters: `car_id`, `date_from`, `date_to` |
| `GET /api/reports/reimbursement` | Reimbursement statement. Same filters, `fmt=json\|csv\|xlsx` |
| `GET/POST /api/tariffs`, `PUT/DELETE /api/tariffs/{id}`, `GET/PUT /api/tariffs/home-locations` | Tariff periods and home locations (changes: administrators) |
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
