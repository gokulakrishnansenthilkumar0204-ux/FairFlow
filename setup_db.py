"""Create/migrate the FairFlow schema and install + verify its PostgreSQL routines.

    python setup_db.py

Uses DATABASE_URL (the same variable as the API). Safe to re-run: tables are
created only if missing and migrated in place, never dropped or truncated.
Everything runs in one transaction, so a failure leaves the database unchanged.

If ADMIN_USERNAME and ADMIN_PASSWORD are set, the first admin is created when
that username does not exist yet (public signup can only create beneficiaries).
"""

import os
import re
from pathlib import Path

import psycopg2

from db import database_url

HERE = Path(__file__).parent


def routine_names() -> set[str]:
    sql = (HERE / "routines.sql").read_text()
    return set(re.findall(r"CREATE (?:OR REPLACE )?(?:FUNCTION|PROCEDURE) (ff_\w+)", sql))


def _installed(cur) -> set[str]:
    # Functions only: the old procedures.sql created PROCEDUREs with the same names.
    cur.execute(
        "SELECT p.proname FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace "
        "WHERE n.nspname = current_schema() AND p.prokind = 'f' AND p.proname LIKE 'ff\\_%'"
    )
    return {row[0] for row in cur.fetchall()}


def setup(dsn: str | None = None, **connect_kwargs) -> set[str]:
    conn = psycopg2.connect(dsn or database_url(), **connect_kwargs)
    try:
        with conn, conn.cursor() as cur:
            for name in ("schema.sql", "routines.sql"):
                cur.execute((HERE / name).read_text())
            installed = _installed(cur)
            missing = routine_names() - installed
            if missing:
                raise RuntimeError(f"Routines missing after setup: {sorted(missing)}")
    finally:
        conn.close()
    return installed


def check_installed(dsn: str | None = None) -> None:
    """Raise a clear error if the database the API uses lacks the current routines."""
    dsn = dsn or database_url()
    conn = psycopg2.connect(dsn)
    try:
        with conn, conn.cursor() as cur:
            missing = routine_names() - _installed(cur)
            cur.execute("SELECT current_database(), current_schema()")
            database, schema = cur.fetchone()
    finally:
        conn.close()
    if missing:
        raise RuntimeError(
            f"Database '{database}' (schema '{schema}') is missing {len(missing)} FairFlow routines, "
            f"e.g. {sorted(missing)[:3]}. Run `python setup_db.py` with the SAME DATABASE_URL "
            "as the API, then restart the server."
        )


def seed_admin(username: str, password: str) -> bool:
    from auth import hash_password  # needs JWT_SECRET_KEY like the API
    from db import call

    if call("ff_get_user_by_username", username, one=True):
        return False
    call("ff_create_user", None, username, hash_password(password), "admin", None, None)
    return True


if __name__ == "__main__":
    names = setup()
    print(f"Schema ready. {len(names)} routines installed and verified.")
    if os.getenv("ADMIN_USERNAME") and os.getenv("ADMIN_PASSWORD"):
        created = seed_admin(os.environ["ADMIN_USERNAME"], os.environ["ADMIN_PASSWORD"])
        print("Admin user created." if created else "Admin user already exists; unchanged.")
