from sqlalchemy import create_engine, inspect, text

from ev_charge.database import run_migrations


def test_upgrade_with_existing_chargers_and_sessions(tmp_path):
    """Upgrading a database whose chargers are used by sessions (foreign keys on) must not fail."""
    eng = create_engine(f"sqlite:///{tmp_path / 'old.db'}")
    run_migrations(eng, "003")
    with eng.begin() as conn:
        conn.execute(text("PRAGMA foreign_keys=ON"))
        conn.execute(text("INSERT INTO cars (id, name, active, created_at, updated_at) VALUES (1, 'EV6', 1, '2026-01-01', '2026-01-01')"))
        conn.execute(text("INSERT INTO chargers (id, name, uses_home_tariff, active, created_at, updated_at) VALUES (1, 'Zaptec', 1, 1, '2026-01-01', '2026-01-01')"))
        conn.execute(
            text(
                "INSERT INTO charge_sessions (car_id, charge_date, kwh, to_reimburse, charger_id, reimbursement_status, created_at, updated_at) "
                "VALUES (1, '2026-02-01', 12.5, 1, 1, 'recorded', '2026-02-01', '2026-02-01')"
            )
        )
    run_migrations(eng)  # to head
    cols = {c["name"] for c in inspect(eng).get_columns("chargers")}
    assert {"connector", "connector_secret", "default_car_id", "poll_enabled", "live_state"} <= cols
    with eng.connect() as conn:
        assert conn.execute(text("SELECT charger_id, kwh FROM charge_sessions")).one() == (1, 12.5)
        assert conn.execute(text("PRAGMA foreign_keys")).scalar() == 1  # back on
    eng.dispose()


def test_old_admin_and_driver_become_roles(tmp_path):
    eng = create_engine(f"sqlite:///{tmp_path / 'v5.db'}")
    run_migrations(eng, "005")
    with eng.begin() as conn:
        conn.execute(text("INSERT INTO cars (id, name, active, created_at, updated_at) VALUES (1, 'EV6', 1, '2026-01-01', '2026-01-01')"))
        for uid, name, role in ((1, "boss", "admin"), (2, "dave", "driver")):
            conn.execute(
                text("INSERT INTO users (id, username, role, password_hash, active, created_at) VALUES (:i, :n, :r, 'x', 1, '2026-01-01')"), {"i": uid, "n": name, "r": role}
            )
        conn.execute(text("INSERT INTO user_cars (user_id, car_id) VALUES (2, 1)"))
    run_migrations(eng)
    with eng.connect() as conn:
        roles = dict(conn.execute(text("SELECT name, id FROM roles")).all())
        users = dict(conn.execute(text("SELECT username, role_id FROM users")).all())
        assert users == {"boss": roles["Administrator"], "dave": roles["Driver"]}
        assert conn.execute(text("SELECT role_id FROM user_cars")).scalar() == roles["Driver"]
    eng.dispose()
