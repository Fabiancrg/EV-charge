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
