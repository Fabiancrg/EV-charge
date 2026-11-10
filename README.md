# EV Charge

Record the charging sessions of your EV cars and report on them: energy, distance, consumption, cost, sessions to be reimbursed, and lease mileage.

Same stack and look as Flow Manager: FastAPI, SQLAlchemy + Alembic (SQLite), Jinja pages that load their data from a JSON API, and Chart.js for the charts (vendored, so it works offline).

## Users, roles and permissions

On first start, every page leads to **/setup**, where you create the first administrator. Users are managed on **Settings → Users & car access**, roles on **Settings → Roles & permissions**.

**Roles are sets of permissions.** Each user has:
- a **global role**: its app-wide permissions apply. If the role is marked *all cars*, its car permissions apply to **every car**.
- a **role on each car** they work with, e.g. *Driver* on the EV6 and *Viewer* on the Model 3. Only the role's car permissions apply, on that car.

| Permission | Allows |
|---|---|
| `users:manage` | Manage users, their global role and their role on each car |
| `roles:manage` | Create and edit roles |
| `settings:manage` | Single sign-on settings |
| `audit:read` | Read the audit log |
| `cars:manage` | Add and delete cars |
| `chargers:manage` | Chargers, their connection, history import |
| `tariffs:manage` | Home charging tariffs, CREG import |
| `tokens:manage` | Manage API tokens, their access and cars (*Settings → API tokens*) |
| `car:view` *(per car)* | See the car, its sessions, reports, statements, live charger |
| `car:edit` *(per car)* | Edit the car (specs, lease) |
| `sessions:create` / `sessions:edit` / `sessions:import` *(per car)* | Add / edit and delete / import sessions |
| `reimbursement:manage` *(per car)* | Change the *to be reimbursed* flag and the reimbursement status |

- **Wildcards:** `*` means everything, and `sessions:*` means every sessions permission.
- **Built-in roles** can't be deleted, but their permissions can be changed:
  - **Administrator:** everything, all cars.
  - **Driver:** view, add, edit and import sessions, reimbursement.
  - **Viewer:** view only.
  - **Accountant:** view and reimbursement, all cars.
- **Upgrading:** former *admin* users become Administrators; former *drivers* get the Driver role, globally and on each car they had.
- **Safeguard:** there is always at least one active user whose role can manage users.
- **Admin-level permissions:** `users:manage`, `roles:manage` and `tokens:manage` let their holders give themselves, or a token, any permission, so keep them for administrators.
- **Reimbursement without the permission:** when someone without `reimbursement:manage` adds or edits a session, the reimbursement flag and status follow the defaults or stay unchanged.
- **Bulk changes** skip the sessions on cars where the caller lacks the permission, and the result tells how many were skipped.

**Sign-in.**
- **Password:** passwords are hashed with scrypt and need at least 8 characters. After 8 failed attempts in 15 minutes, sign-in for that username or address is blocked for a while.
- **Single sign-on:** see below.
- **Session length:** `session_days`, 30 by default.
- **Cookie:** HttpOnly and SameSite=Lax. Requests that change data are refused when they come from another site.
- **Locked out?** Run `ev-charge --reset-password USERNAME` on the server.

**My account** (👤 menu) lists **where you are signed in**, with browser, IP, last activity and method. You can sign out one session, or all the others. Administrators can *sign a user out everywhere*; disabling a user or changing their password does it too.

**API tokens** (*Settings → API tokens*, `tokens:manage`). Tokens for scripts and other tools, such as Home Assistant:
- **Use:** send `Authorization: Bearer evc_…`. The API is documented at `/docs`.
- **Access:** each token has its own, chosen when it's created and editable later without changing the secret:
  - **what it may do:** the permissions of a **role** (the token follows later changes to the role), or **chosen permissions**;
  - **on which cars:** **all cars**, including ones added later, or **chosen cars**.
- **Limits:** a token works while its creator's account is active, and the sessions it adds are entered in the creator's name. It can't manage tokens or account settings, which need a browser sign-in.
- **Lifetime:** tokens expire after 30 days, 90 days, 1 year or never. They can be revoked, then deleted from the list. Only a hash is stored; the token is shown once, with a `curl` example.
- **Upgrade:** existing personal tokens keep their permissions, limited to the cars their creator could access.

## E-mail (SMTP)

*Settings → E-mail (SMTP)* (`settings:manage`) sets the SMTP server EV Charge sends e-mails through:
- **Server:** host and port, with security STARTTLS (587), SSL/TLS (465) or none (25), and an option to skip the TLS certificate check.
- **Login:** username and password, the password stored encrypted. Leave the username empty for no login.
- **Sender:** the from address and name.
- **Test:** *Send a test e-mail* uses the values in the form, saved or not, and reports the server's answer: refused login, unreachable server, TLS problem… Settings changes and tests are in the audit log.

**Audit log** (*Settings → Audit log*, `audit:read`). It records sign-ins and failed sign-ins (password or SSO), user, role, token and SSO-settings changes, and **every change made through the app or the API**, such as `cars.update cars:3`, `sessions.bulk` or `chargers.connection`. Each entry has the user, time, IP and details, and the log can be filtered and searched. Events older than `audit_retention_days` (730) are deleted at startup.

## Single sign-on (Authelia, OpenID Connect)

*Settings → Single sign-on* (`settings:manage`) connects EV Charge to an OpenID Connect provider: Authelia, Keycloak, Entra ID, Google… The sign-in page then shows **Sign in with Authelia**.

1. **In Authelia:** add EV Charge as a client. The page shows the `identity_providers.oidc.clients` entry to paste into `configuration.yml`, with the redirect URI `https://<ev-charge>/auth/sso/callback`. Store the secret's digest (`authelia crypto hash generate pbkdf2`).
2. **In EV Charge:** enter the issuer URL (e.g. `https://auth.example.com`), the client ID and the secret (stored encrypted), and click *Test the provider*. Set the *Redirect URI* when EV Charge is behind a reverse proxy with another public URL.
3. **Accounts:**
   - A user is found by their SSO account. Otherwise they're linked to an existing user with the same username (`preferred_username`) or e-mail. Otherwise they're created, if *auto-create* is on, with the chosen default global role.
   - **Group → role** mappings, from the `groups` claim, set the global role at each sign-in; the first match wins. The role on each car is given on the Users page.
4. **Password sign-in** can be turned off once you have signed in with SSO yourself; the app refuses to turn it off before, to avoid locking you out. If the provider is down, set `"allow_local_login": true` in config.json (or `ALLOW_LOCAL_LOGIN=1`) and restart.

Technically:
- the authorization code flow, with PKCE (S256), `state` and `nonce`;
- the flow state is kept in an encrypted, short-lived cookie;
- the ID token is received directly from the provider's token endpoint over TLS, so, as OpenID Connect allows, its signature isn't checked, but its issuer, audience, expiry and nonce are;
- `userinfo` adds the e-mail, name and groups.

## Chargers and reimbursement status

The **Chargers** page (administrators) lists the chargers whose sessions are reimbursed: a home wall box such as a Zaptec Go, a mobile charger such as an Ohme…

- **Fields:** name, brand, model, serial number, the **reimbursing company** (the one that pays the sessions back, e.g. *50five* or *Enco e-Mobility*), whether its sessions are priced at the home tariff (yes by default), notes, active flag.
- **Defaults for its sessions:** car, location, and **provider**. The provider is who supplies the electricity, e.g. your home energy supplier. Don't use the reimbursing company: it pays you back but doesn't supply the electricity.
- **Where the defaults apply:**
  - sessions created live or imported from the charger's history get them
  - picking the charger in the session form fills in the provider
  - setting the charger in bulk fills in empty providers
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
- **Sessions page:** charger and status columns, *Status* and *Charger* filters (*Not paid yet* shows everything but *Paid*), and bulk *Set status…* / *Set charger…* / *Set provider…* for the selected rows. An empty provider clears it.
  - *☑ Select all N matching* selects every session matching the filters, on all pages; changing a filter starts a new selection.
  - Changes to more than 25 sessions ask for confirmation.
  - Typical flows: filter *Charger: No charger* plus a period or a search → select all matching → *Set charger…*. Or filter a quarter → select all matching → *Invoiced* when the invoice arrives → *Paid* when the money is in (the **Payments** page does this from the document itself, see below).
- **Reimbursement statement:** filters on status and **reimbursing company**, so the statement for 50five can be checked against its invoice. The charger and status are shown per session, totals per status, and both appear in the Excel and CSV exports.
- **Import:** a *Charger* column (matched on the charger's name or serial number) and a *Status* column are recognised. Statuses can be written as *Paid*, *payé*, *betaald*, *facturé*, *niet geregistreerd*… Without a charger column, one charger can be set for every row, and without a status column, a status for every row to be reimbursed.

## Reimbursement documents and payments to receive

The **Payments** page (for users with *Change the reimbursement flag and status* on at least one car) reconciles the documents sent by the reimbursing companies with your sessions, then follows the payments.

1. **⬆ Import a document.** Two formats are read:
   - **Eneco eMobility**: the CSV *Credit Reimbursement Document* (one row per session);
   - **50five / Last Mile Solutions**: the PDF *Document de remboursement* with its annex of sessions.
2. **Choose the charger and check the preview.** A document is about **one charger** (mandatory): its lines are matched with that charger's sessions only. The charger is proposed from the first matches; change it if it is wrong. Each line is matched with the session that started at the same time (within an hour), or, for a session without a start time, on the same day with the same energy. Nothing is stored yet. The preview shows:
   - the lines **without a session** (a session you never recorded) and **your sessions of the period that aren't on the document**;
   - lines whose **energy** differs from your session(s), and lines that aren't at your **price per kWh** (a tariff to check). A few cents of difference caused by a slightly different energy on the two meters are shown but not flagged;
   - the lines that cover **several sessions** (⧉): when the company grouped sessions in one line, the sessions started between the start and the end of the line are taken together if their energies add up to the line's.
   - A document written in another time zone (UTC) is recognised: the shift in whole hours that matches best is applied and reported.
3. **Import.** The matched sessions become **🧾 Invoiced** and the document is a **payment to receive** (amount, due date; *Overdue* once the due date is passed). A document is imported once.
4. **Fix by hand if needed.** Open the document to link a line without a session to one of your sessions, to **add more sessions to a line** whose energy doesn't match (a line can have several sessions; a session is on one line only), or to unlink a wrong match (✕).
5. **💰 Payment received.** Enter the date and the amount received: the document is *Paid*, its sessions become **💰 Paid**, and a difference with the amount of the document is shown. *Not paid after all* puts everything back to *Invoiced*.

- **Which sessions can be matched:** sessions of the document's charger, *to be reimbursed*, not *In progress*, on no other document, on a car you manage.
- A charger with reimbursement documents can't be deleted; mark it inactive.
- **Deleting a document** (or unlinking a session) puts its sessions back to their default status: *Recorded* with a charger, *Not recorded* without one.
- The tiles show the total **to receive**, of which **overdue**, and **received**.

## Charger connection: live sessions and history

A charger can be connected to its cloud account (🔌 on the **Chargers** page, administrators). Supported: **Zaptec**, **Ohme** and **Home Assistant** (smart plug sensors). Other chargers can be added in `src/ev_charge/connectors/`.

- **Zaptec** (Go, Go 2, Pro…):
  - Uses Zaptec's **official API** ([docs.zaptec.com](https://docs.zaptec.com)) with the Zaptec Portal / app e-mail and password.
  - The login method is Zaptec's OAuth "password" grant, which Zaptec plans to replace one day.
  - If the account holds several chargers, give the serial number (e.g. `ZAP049123`).
  - **Live state:** the charger's operation mode, power, and energy of the current session.
  - **History:** the exact sessions (start, end, kWh), up to 365 days back.
- **Ohme:**
  - Ohme has no public API. EV Charge uses the open-source [`ohme`](https://github.com/dan-r/ohmepy) library (MIT), the one behind the Home Assistant integration.
  - It logs in with the **Ohme app e-mail and password**; one Ohme account is one charger.
  - The API is unofficial, so Ohme can change it at any time. A broken connection shows its error on the Chargers page and the dashboard; sessions can still be entered by hand.
- **Home Assistant** (a charger on a smart plug, e.g. an Ohme whose cloud is unreliable):
  - Reads the plug's **power** sensor (W or kW) and **energy** sensor (kWh or Wh, cumulative or a daily utility meter that resets: a drop is taken as a reset), e.g. `sensor.carplug2_relay_power` and `sensor.ve_plug_2_energy_daily`.
  - Needs the HA URL and a **long-lived access token** (HA profile → Security). EV Charge must be able to reach Home Assistant.
  - **Live:** *charging* while the power is above the threshold (100 W by default). The session's energy is what the energy sensor added since charging started, read from HA's recorded history, so it is right even if EV Charge restarts mid-charge, and the session starts when charging really started.
  - **End of a session:** a plug can't see the car being unplugged, so a session ends once the energy has stopped increasing for the idle time (60 minutes by default, so short pauses don't split it). The plug's standby use (a few W) is ignored.
  - **History:** the energy per hour from HA's long-term statistics (kept forever for sensors with a state class), else from the recorded history (about 10 days). Sessions are rebuilt as for Ohme.
  - To switch an existing Ohme charger, open its 🔌 connection and choose *Home Assistant*: its sessions and settings are kept.
- **Credentials:**
  - They're checked when saved and stored **encrypted**, with a key in `{data_dir}/secret.key` or the `EV_CHARGE_SECRET_KEY` environment variable.
  - Losing the key only means entering the credentials again.
  - The password is never sent back to the browser.
- **Default car and location:** set them on the charger. Sessions created from the charger go to that car at that location.

**Live session.** While *Follow the charger* is ticked, EV Charge reads it every `charger_poll_seconds` (60 by default):
- **Charging starts:** as soon as the car is plugged in and energy is delivered, a session is created for the default car with status ⏳ **In progress**. It starts when charging started and is priced at the CREG tariff. Its kWh and end time follow the charger.
- **Car unplugged:** the session becomes ✓ **Recorded**. If the energy counter restarts without unplugging, the session is closed and a new one starts.
- **Edits:** the session can be edited at any time to add the odometer or notes.
- **Dashboard:** a **Chargers** section shows each connected charger's state (charging, plugged in, finished…), power, energy of the current session, car battery % and last update. It refreshes every 30 s, and ⟳ reads the charger at once.
- **Drivers:** they see the chargers whose default car is theirs.

**History import.** 📜 on a connected charger fetches past sessions for a period and a car:
- **How:** Zaptec lists its sessions, which are taken as they are. Ohme gives energy per **hour**, not a list of sessions. Consecutive hours with energy, or separated by one empty hour, make one session. Times are therefore rounded to the hour, and the end is tightened with the active charging time.
- **Matching:** each rebuilt session is compared with the recorded ones. It's *new*, *matches* a recorded session (same day, kWh within 10 % or 1 kWh), or was *already imported*.
- **Applying:** the selected new sessions are added as ✓ Recorded. Selected matches that have no charger get this one, keeping their kWh. Importing the same period again adds nothing.



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
| **Payments** `/payments` | Import the reimbursement documents (Eneco CSV, 50five / Last Mile Solutions PDF), reconcile them with the sessions (*Invoiced*), and follow the payments to receive (*Paid*), see above. |
| **Chargers** `/chargers` | Chargers whose sessions are reimbursed, and the company paying them back (administrators, see above). |
| **Tariffs** `/tariffs` | Home charging price periods and the CREG import (administrators, see above). |
| **Users**, **Roles**, **Single sign-on**, **E-mail (SMTP)**, **API tokens**, **Audit log** (⚙ Settings menu) | Accounts with their global role and role per car; roles and permissions; OpenID Connect; SMTP; API tokens; audit log (see above). |
| **My account** `/account` | Where you are signed in, your permissions. |
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
| `allow_local_login` | `false` | Re-allow password sign-in when single sign-on is set to be the only way in (lock-out escape); also `ALLOW_LOCAL_LOGIN=1` |
| `audit_retention_days` | `730` | Audit events older than this are deleted at startup (`0`: keep all) |
| `charger_polling` / `charger_poll_seconds` | `true` / `60` | Read the connected chargers in the background (`CHARGER_POLLING=0` disables it) |
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

### Automatic deployment

On each merge to `main`, GitHub Actions (`.github/workflows/build.yml`) runs the tests, builds `ev_charge-py3.12.pyz`, `-py3.13` and `-py3.14` (Linux x86-64) and publishes them, with their SHA-256, to the rolling [`latest` release](https://github.com/Fabiancrg/EV-charge/releases/tag/latest). Pull requests only run the tests.

The server pulls the new build itself (nothing to open on your network): `deploy/ev-charge-update.sh`, run every 10 minutes by a systemd timer,
- downloads the checksum of the build for the server's Python, and stops there if it's the installed one;
- downloads the `.pyz`, checks its checksum and that it starts; while a build is being published (about a minute after a merge) the files are replaced one by one, so a missing file or a checksum mismatch is tried again (6 times, 20 s apart);
- stops EV Charge, keeps the old `.pyz` (`.prev`) and a copy of the database (`.pre-update`), puts the new one in place, starts EV Charge;
- waits for `/api/health`; if it doesn't answer within 90 s, puts the old `.pyz` and the database back.

Setup, once, as root (adapt the paths and the user to yours):

```bash
install -m 755 deploy/ev-charge-update.sh /usr/local/bin/ev-charge-update
mkdir -p /etc/ev-charge && cp deploy/update.conf.example /etc/ev-charge/update.conf   # then edit PYZ, HEALTH_URL, DB_FILE…
cp deploy/ev-charge-update.service deploy/ev-charge-update.timer /etc/systemd/system/
systemctl daemon-reload && systemctl enable --now ev-charge-update.timer
```

`ev-charge-update` runs it now, `FORCE=1 ev-charge-update` reinstalls, `journalctl -u ev-charge-update` shows what it did. The EV Charge service itself runs the `.pyz` from the fixed path set as `PYZ`, e.g.:

```ini
# /etc/systemd/system/ev-charge.service
[Service]
User=evcharge
ExecStart=/usr/bin/python3 /opt/ev-charge/ev_charge.pyz --config /etc/ev-charge/config.json
Restart=on-failure
```

## API

Every endpoint needs a signed-in user (session cookie) or an API token (`Authorization: Bearer evc_…`), and checks the permissions above: global ones, or per car.

| Method & path | |
|---|---|
| `GET/POST /api/cars`, `GET/PUT/DELETE /api/cars/{id}` | Cars, with session count, total kWh, last odometer and lease status |
| `GET/POST /api/sessions`, `GET/PUT/DELETE /api/sessions/{id}` | Sessions. Filters: `car_id`, `status`, `charger_id` (repeatable; `0` = no charger), `date_from`, `date_to`, `to_reimburse`, `q`, `limit` |
| `GET /api/sessions/export.csv` | Same filters as the list |
| `GET /api/sessions/suggestions?car_id=` | Values that pre-fill the session form |
| `GET /api/reports` | Report data. Filters: `car_id`, `date_from`, `date_to` |
| `GET /api/reports/reimbursement` | Reimbursement statement. Same filters plus `status` (repeatable) and `company` (empty: sessions without a charger), `fmt=json\|csv\|xlsx` |
| `GET /api/reimbursement-documents`, `POST /api/reimbursement-documents/preview`, `POST /api/reimbursement-documents` | Reimbursement documents with the totals to receive; preview the reconciliation of a file (multipart `file`, optional `?charger_id=`: without it a charger is proposed), then import it for a charger (`?charger_id=` required): the matched sessions become *invoiced* |
| `GET/DELETE /api/reimbursement-documents/{id}`, `PUT/DELETE /api/reimbursement-documents/{id}/payment`, `PUT /api/reimbursement-documents/{id}/lines/{line_id}`, `PUT /api/reimbursement-documents/{id}/charger` | A document with its lines; payment received `{"paid_date": "2026-11-20", "paid_amount": 51.92, "notes": "…"}` (sessions become *paid*) or cancelled; set the session(s) of a line `{"session_ids": [12, 13]}` (empty: unlink); set the charger of a document imported without one `{"charger_id": 1}` |
| `GET/POST /api/tariffs`, `PUT/DELETE /api/tariffs/{id}` | Tariff periods (changes: administrators) |
| `GET/POST /api/chargers`, `PUT/DELETE /api/chargers/{id}` | Chargers, with their live state (changes: administrators) |
| `GET /api/chargers/connectors`, `PUT /api/chargers/{id}/connection`, `POST /api/chargers/{id}/poll` | Charger APIs available, connect/disconnect a charger (tests the login), read it now |
| `GET /api/chargers/{id}/history?date_from=&date_to=&car_id=`, `POST /api/chargers/{id}/history/apply` | History import: preview, then add/link the chosen sessions (administrators) |
| `PATCH /api/sessions/bulk` | `{"ids": [...], "reimbursement_status": "paid", "charger_id": 1, "clear_charger": false, "provider": "Engie", "clear_provider": false, "to_reimburse": true}`: only the fields given change |
| `GET /api/tariffs/creg?region=`, `POST /api/tariffs/creg/upload`, `POST /api/tariffs/creg/apply` | CREG import: preview (from the website or a saved page), then apply the chosen quarters (administrators) |
| `POST /api/auth/login\|logout\|password\|setup`, `GET /api/auth/me` | Sign-in and own account (`me` includes the permissions, global and per car) |
| `GET /api/auth/sessions`, `DELETE /api/auth/sessions/{id}`, `POST /api/auth/sessions/revoke-others` | My sign-ins |
| `GET/POST /api/tokens`, `PUT/DELETE /api/tokens/{id}`, `POST /api/tokens/{id}/revoke` | API tokens: role or permissions, cars (`tokens:manage`, browser sign-in) |
| `GET/PUT /api/settings/email`, `POST /api/settings/email/test` | SMTP settings and test e-mail (`settings:manage`) |
| `GET/POST /api/users`, `PUT/DELETE /api/users/{id}`, `POST /api/users/{id}/signout` | Users, global role and role per car (`users:manage`) |
| `GET/POST /api/roles`, `PUT/DELETE /api/roles/{id}`, `GET /api/roles/permissions` | Roles and the permission catalogue (`roles:manage`) |
| `GET/PUT /api/settings/sso`, `POST /api/settings/sso/test` | Single sign-on settings (`settings:manage`) |
| `GET /api/audit` | Audit log (`audit:read`); filters `action` (or `user.` for a family), `username`, `q`, dates, `limit`/`offset` |
| `POST /api/import/...` | Import wizard (see above) |

## Development

```bash
uv run pytest
```

```bash
uv run ruff check . && uv run ruff format --check .
```
