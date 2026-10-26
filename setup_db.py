"""Create/migrate the FairFlow schema and install + verify its PostgreSQL routines.

    python setup_db.py

Uses DATABASE_URL (the same variable as the API). Safe to re-run: tables are
created only if missing and migrated in place, never dropped or truncated.
Everything runs in one transaction, so a failure leaves the database unchanged.

If ADMIN_USERNAME is set, the first admin is created when that username does not
exist yet, and a one-time activation code is printed: the admin enters it in the
app to register their fingerprint passkey. Lost device / new code:

    python setup_db.py --activation-code USERNAME
"""

import hashlib
import os
import re
from pathlib import Path

import psycopg2

from db import database_url

HERE = Path(__file__).parent
_CHECKSUM_PLACEHOLDER = "__ROUTINES_SHA256__"


def routine_names() -> set[str]:
    sql = (HERE / "routines.sql").read_text()
    return set(re.findall(r"CREATE (?:OR REPLACE )?(?:FUNCTION|PROCEDURE) (ff_\w+)", sql))


def routines_checksum() -> str:
    return hashlib.sha256((HERE / "routines.sql").read_bytes()).hexdigest()


def _sql_file(name: str) -> str:
    text = (HERE / name).read_text()
    # Stamp the routines with the checksum of this exact file (see ff_routines_checksum).
    return text.replace(_CHECKSUM_PLACEHOLDER, routines_checksum()) if name == "routines.sql" else text


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
                cur.execute(_sql_file(name))
            installed = _installed(cur)
            missing = routine_names() - installed
            if missing:
                raise RuntimeError(f"Routines missing after setup: {sorted(missing)}")
    finally:
        conn.close()
    return installed


def check_installed(dsn: str | None = None) -> None:
    """Raise a clear error if the database the API uses lacks the current routines,
    or has routines installed from a different (older/newer) routines.sql."""
    dsn = dsn or database_url()
    conn = psycopg2.connect(dsn)
    try:
        with conn, conn.cursor() as cur:
            installed = _installed(cur)
            missing = routine_names() - installed
            # Read the stamped checksum from the catalog (works even if the function is absent).
            cur.execute("SELECT current_database(), current_schema(), "
                        "(SELECT p.prosrc FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace "
                        " WHERE n.nspname = current_schema() AND p.proname = 'ff_routines_checksum' LIMIT 1)")
            database, schema, checksum_source = cur.fetchone()
    finally:
        conn.close()
    if missing:
        raise RuntimeError(
            f"Database '{database}' (schema '{schema}') is missing {len(missing)} FairFlow routines, "
            f"e.g. {sorted(missing)[:3]}. Run `python setup_db.py` with the SAME DATABASE_URL "
            "as the API, then restart the server."
        )
    if routines_checksum() not in (checksum_source or ""):
        raise RuntimeError(
            f"Database '{database}' (schema '{schema}') has FairFlow routines from a different "
            "version of routines.sql. Run `python setup_db.py` with the SAME DATABASE_URL "
            "as the API, then restart the server."
        )


def seed_admin(username: str, password: str | None = None) -> dict | None:
    """Create the first admin (no password needed) and return a one-time activation code."""
    from auth import hash_password  # needs JWT_SECRET_KEY like the API
    from db import call
    from passkeys import issue_activation_code

    if call("ff_get_user_by_username", username, one=True):
        return None
    row = call("ff_create_user", None, username, hash_password(password) if password else None,
               "admin", None, None, one=True)
    return issue_activation_code(None, row["id"])


def new_activation_code_for(username: str) -> dict:
    """Recovery: fresh one-time code for any account (e.g. lost phone)."""
    from db import call
    from passkeys import issue_activation_code

    user = call("ff_get_user_by_username", username, one=True)
    if not user:
        raise SystemExit(f"No user named {username!r}")
    return issue_activation_code(None, user["id"])


def _print_code(info: dict) -> None:
    print(f"\n  Activation code for {info['username']}:  {info['activation_code']}")
    print(f"  Valid until {info['expires_at']:%Y-%m-%d %H:%M %Z}. Open the app, choose")
    print("  'Activate account', enter the username and this code, then touch your fingerprint.\n")


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--activation-code", metavar="USERNAME",
                        help="issue a new one-time activation code for USERNAME and exit")
    args = parser.parse_args()

    if args.activation_code:
        _print_code(new_activation_code_for(args.activation_code))
        raise SystemExit(0)

    names = setup()
    print(f"Schema ready. {len(names)} routines installed and verified.")
    if os.getenv("ADMIN_USERNAME"):
        info = seed_admin(os.environ["ADMIN_USERNAME"], os.getenv("ADMIN_PASSWORD") or None)
        if info:
            print("Admin user created.")
            _print_code(info)
        else:
            print("Admin user already exists; unchanged. "
                  "New code: python setup_db.py --activation-code " + os.environ["ADMIN_USERNAME"])
