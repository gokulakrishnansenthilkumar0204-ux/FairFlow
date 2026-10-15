import re
from pathlib import Path

import psycopg2

import setup_db
from conftest import TEST_URL, sql

ROOT = Path(__file__).resolve().parent.parent
PY_FILES = list(ROOT.glob("*.py"))


def installed_routines():
    return {r[0] for r in sql("SELECT p.proname FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace "
                              "WHERE n.nspname = 'public' AND p.proname LIKE 'ff\\_%'")}


def test_every_called_routine_is_installed_and_every_routine_is_used():
    code = "\n".join(p.read_text() for p in PY_FILES)
    called = set(re.findall(r"""["'](ff_[a-z_]+)["']""", code))
    defined = setup_db.routine_names()
    assert called <= defined == installed_routines()
    routines_sql = (ROOT / "routines.sql").read_text()
    internal = {name for name in defined - called if routines_sql.count(name) > 1}
    assert defined - called == internal == {"ff_audit"}


def test_no_sqlalchemy_and_sql_only_executed_by_db_call():
    executes = {}
    for path in PY_FILES:
        text = path.read_text()
        assert "sqlalchemy" not in text.lower(), path.name
        if ".execute(" in text:
            executes[path.name] = text.count(".execute(")
    # Runtime SQL runs in exactly one place: db.call(), which can only build
    # "SELECT * FROM ff_<name>(%s, ...)". setup_db.py runs the .sql files at install time.
    assert executes == {"db.py": 1, "setup_db.py": 3}
    db_source = (ROOT / "db.py").read_text()
    assert 'sql = f"SELECT * FROM {routine}(' in db_source and r'^ff_[a-z_]+$' in db_source
    for name in ("models.py", "database.py", "create_tables.py"):
        assert not (ROOT / name).exists()


def test_setup_is_idempotent_and_keeps_data(client, world):
    sql("INSERT INTO stock (shop_id, commodity_id, quantity) VALUES (1, 1, 42)")
    setup_db.setup(TEST_URL)
    setup_db.setup(TEST_URL)
    assert sql("SELECT count(*) FROM shops")[0][0] == 2
    assert float(sql("SELECT quantity FROM stock")[0][0]) == 42


LEGACY_SCHEMA = """
CREATE TABLE shops (id SERIAL PRIMARY KEY, name VARCHAR, location VARCHAR);
CREATE TABLE villages (id SERIAL PRIMARY KEY, name VARCHAR, shop_id INTEGER REFERENCES shops(id),
                       last_distribution_date VARCHAR);
CREATE TABLE commodities (id SERIAL PRIMARY KEY, name VARCHAR UNIQUE);
CREATE TABLE stock (id SERIAL PRIMARY KEY, quantity FLOAT NOT NULL,
                    shop_id INTEGER NOT NULL REFERENCES shops(id),
                    commodity_id INTEGER NOT NULL REFERENCES commodities(id));
CREATE TABLE users (id SERIAL PRIMARY KEY, username VARCHAR UNIQUE, hashed_password VARCHAR, role VARCHAR,
                    shop_id INTEGER REFERENCES shops(id));
CREATE TABLE commodity_status (id SERIAL PRIMARY KEY, shop_id INTEGER NOT NULL, commodity_id INTEGER NOT NULL,
                               status VARCHAR NOT NULL, reason VARCHAR);
CREATE TABLE distribution_history (id SERIAL PRIMARY KEY, shop_id INTEGER NOT NULL, village_id INTEGER,
                                   commodity_id INTEGER NOT NULL, quantity FLOAT NOT NULL,
                                   distribution_date VARCHAR NOT NULL);
CREATE TABLE village_requirements (id SERIAL PRIMARY KEY, village_id INTEGER NOT NULL REFERENCES villages(id),
                                   commodity_id INTEGER NOT NULL REFERENCES commodities(id),
                                   quantity FLOAT NOT NULL);
CREATE PROCEDURE ff_add_stock(p_shop_id INTEGER, p_commodity_id INTEGER, p_quantity DOUBLE PRECISION)
    LANGUAGE plpgsql AS $$ BEGIN END $$;
INSERT INTO shops (name, location) VALUES ('Old Shop', 'Guindy');
INSERT INTO villages (name, shop_id, last_distribution_date) VALUES ('Old Village', 1, '2026-05-01'),
    ('Timestamp', 1, '2026-06-10 00:00:00'), ('Indian format', 1, '10/06/2026'), ('Blank', 1, ''),
    ('Junk', 1, 'yesterday'), ('Impossible', 1, '2026-02-30'), ('Never', 1, NULL);
INSERT INTO commodities (name) VALUES ('Rice');
INSERT INTO stock (quantity, shop_id, commodity_id) VALUES (10.5, 1, 1), (4.5, 1, 1);
INSERT INTO commodity_status (shop_id, commodity_id, status) VALUES (1, 1, 'Available');
INSERT INTO distribution_history (shop_id, commodity_id, quantity, distribution_date)
    VALUES (1, 1, 7.25, '2026-05-01'), (1, 1, 2, '2026-06-11T09:30:00'), (1, 1, 3, '12-06-2026');
"""


def legacy_connection_kwargs():
    return {"options": "-c search_path=legacy_mig"}


def test_migrates_old_sqlalchemy_schema_in_place():
    sql("DROP SCHEMA IF EXISTS legacy_mig CASCADE; CREATE SCHEMA legacy_mig")
    try:
        with psycopg2.connect(TEST_URL, **legacy_connection_kwargs()) as conn, conn.cursor() as cur:
            cur.execute(LEGACY_SCHEMA)
            cur.execute("INSERT INTO distribution_history (shop_id, commodity_id, quantity, distribution_date) "
                        "VALUES (1, 1, 1, '2026-13-45')")
        # Invalid stored date: setup must stop and change nothing.
        try:
            setup_db.setup(TEST_URL, **legacy_connection_kwargs())
            raise AssertionError("setup should have failed on invalid date")
        except psycopg2.Error as exc:
            assert "invalid distribution_date" in str(exc) and "value='2026-13-45'" in str(exc)
        assert sql("SELECT data_type FROM information_schema.columns WHERE table_schema = 'legacy_mig' "
                   "AND table_name = 'stock' AND column_name = 'quantity'")[0][0] == "double precision"
        assert sql("SELECT count(*) FROM legacy_mig.stock")[0][0] == 2

        sql("DELETE FROM legacy_mig.distribution_history WHERE distribution_date = '2026-13-45'")
        setup_db.setup(TEST_URL, **legacy_connection_kwargs())
        setup_db.setup(TEST_URL, **legacy_connection_kwargs())  # re-run is a no-op

        types = dict(sql("SELECT table_name || '.' || column_name, data_type FROM information_schema.columns "
                         "WHERE table_schema = 'legacy_mig' AND column_name IN "
                         "('quantity', 'distribution_date', 'last_distribution_date', 'name')"))
        assert types["stock.quantity"] == "numeric" and types["distribution_history.distribution_date"] == "date"
        assert types["villages.last_distribution_date"] == "date" and types["shops.name"] == "text"
        assert [float(r[0]) for r in sql("SELECT quantity FROM legacy_mig.stock")] == [15.0]  # duplicates merged
        assert sql("SELECT status FROM legacy_mig.commodity_status")[0][0] == "Available"  # legacy row kept
        assert sql("SELECT prokind FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace "
                   "WHERE n.nspname = 'legacy_mig' AND proname = 'ff_add_stock'") == [("f",)]  # old procedure replaced
        dates = dict(sql("SELECT name, last_distribution_date::text FROM legacy_mig.villages"))
        assert dates == {"Old Village": "2026-05-01", "Timestamp": "2026-06-10", "Indian format": "2026-06-10",
                         "Blank": None, "Junk": None, "Impossible": None, "Never": None}
        saved = sql("SELECT original_value FROM legacy_mig.migration_invalid_dates ORDER BY row_id")
        assert [r[0] for r in saved] == ["yesterday", "2026-02-30"]  # unreadable originals kept
        assert [r[0] for r in sql("SELECT distribution_date::text FROM legacy_mig.distribution_history ORDER BY id")] \
            == ["2026-05-01", "2026-06-11", "2026-06-12"]
        assert sql("SELECT count(*) FROM pg_tables WHERE schemaname = 'legacy_mig'")[0][0] == 13
    finally:
        sql("DROP SCHEMA IF EXISTS legacy_mig CASCADE")


def test_missing_routines_fail_fast_and_clearly(client):
    import pytest
    from fastapi.testclient import TestClient
    import main
    from db import call
    from fastapi import HTTPException

    setup_db.check_installed(TEST_URL)  # passes on a set-up database
    with TestClient(main.app):  # startup check passes
        pass
    sql("ALTER FUNCTION ff_get_user_by_username(TEXT) RENAME TO ff_renamed_for_test")
    try:
        with pytest.raises(RuntimeError, match="Run `python setup_db.py`"):
            setup_db.check_installed(TEST_URL)
        with pytest.raises(RuntimeError, match="missing 1 FairFlow routines"):
            with TestClient(main.app):
                pass
        with pytest.raises(HTTPException) as err:
            call("ff_get_user_by_username", "x")
        assert err.value.status_code == 503
    finally:
        sql("ALTER FUNCTION ff_renamed_for_test(TEXT) RENAME TO ff_get_user_by_username")


def test_seed_demo_is_rerunnable_and_usable(client):
    import seed_demo

    seed_demo.main()
    seed_demo.main()  # second run must not duplicate anything
    counts = sql("SELECT (SELECT count(*) FROM shops), (SELECT count(*) FROM villages), (SELECT count(*) FROM users), "
                 "(SELECT count(*) FROM stock), (SELECT count(*) FROM distribution_history)")[0]
    assert counts == (2, 5, 5, 10, 36)
    assert client.post("/signup", json={"username": "fresh", "password": "password123", "shop_id": 1}).status_code == 201
    token = client.post("/login", data={"username": "officer1", "password": seed_demo.PASSWORD}).json()["access_token"]
    forecast = client.get("/forecast/1", headers={"Authorization": f"Bearer {token}"}).json()
    assert forecast["forecast_count"] == 3
