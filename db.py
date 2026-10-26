"""The single database gateway: FastAPI -> call() -> psycopg2 -> PostgreSQL routine.

Every call runs one ff_* routine in its own transaction (commit on success,
rollback on any error) and maps PostgreSQL errors to HTTP errors. Parameters are
always bound by psycopg2; only the validated routine name is placed in the SQL.
"""

import logging
import os
import re
import threading
from contextlib import contextmanager

import psycopg2
from fastapi import HTTPException
from psycopg2.extensions import parse_dsn
from psycopg2.extras import RealDictCursor
from psycopg2.pool import PoolError, ThreadedConnectionPool

log = logging.getLogger("fairflow.db")

_ROUTINE_NAME = re.compile(r"^ff_[a-z_]+$")
_TIMEZONE_NAME = re.compile(r"^[A-Za-z_]+(/[A-Za-z0-9_+\-]+)*$")
_RETRYABLE = {"40P01", "40001"}  # deadlock, serialization failure
_STATUS = {"FF400": 400, "FF403": 403, "FF404": 404, "FF409": 409}
_GENERIC = {
    "23505": (409, "Record already exists"),
    "23503": (404, "Referenced record not found"),
    "23514": (400, "Value violates a data rule"),
    "23502": (400, "Required value is missing"),
    "22003": (400, "Numeric value out of range"),
    "22007": (400, "Invalid date"),
    "22008": (400, "Invalid date"),
    "40P01": (409, "Concurrent update conflict; please retry"),
    "40001": (409, "Concurrent update conflict; please retry"),
}

_pool = None
_pool_lock = threading.Lock()


def database_url() -> str:
    url = os.getenv("DATABASE_URL")
    if not url:
        raise RuntimeError("DATABASE_URL environment variable is not set")
    return url


def app_timezone() -> str:
    """Time zone for CURRENT_DATE/now() in every session (distribution dates, 'same day')."""
    tz = os.getenv("APP_TIMEZONE", "Asia/Kolkata")
    if not _TIMEZONE_NAME.match(tz):
        raise RuntimeError(f"APP_TIMEZONE is not a valid time zone name: {tz!r}")
    return tz


def connect_kwargs(dsn: str) -> dict:
    """Keep any options already in the DSN and add the application time zone."""
    existing = parse_dsn(dsn).get("options", "")
    return {"options": f"{existing} -c timezone={app_timezone()}".strip()}


class PoolTimeout(Exception):
    """No database connection became free in time."""


class _BlockingPool:
    """ThreadedConnectionPool raises as soon as it is empty. FastAPI runs up to 40
    requests at once, so callers instead wait (up to DB_POOL_TIMEOUT seconds) for a
    free connection; only a real overload returns 503."""

    def __init__(self, maxconn: int, dsn: str):
        self._pool = ThreadedConnectionPool(1, maxconn, dsn, **connect_kwargs(dsn))
        self._slots = threading.BoundedSemaphore(maxconn)
        self._timeout = float(os.getenv("DB_POOL_TIMEOUT", "30"))

    def getconn(self):
        if not self._slots.acquire(timeout=self._timeout):
            raise PoolTimeout()
        try:
            return self._pool.getconn()
        except Exception:
            self._slots.release()
            raise

    def putconn(self, conn, close: bool = False):
        try:
            self._pool.putconn(conn, close=close)
        finally:
            self._slots.release()


def _get_pool() -> _BlockingPool:
    global _pool
    with _pool_lock:
        if _pool is None:
            _pool = _BlockingPool(int(os.getenv("DB_POOL_MAX", "10")), database_url())
        return _pool


@contextmanager
def transaction():
    """Yield a pooled connection inside one transaction."""
    pool = _get_pool()
    conn = pool.getconn()
    try:
        yield conn
        conn.commit()
    except Exception:
        if not conn.closed:
            conn.rollback()
        raise
    finally:
        pool.putconn(conn, close=bool(conn.closed))


def _http_error(routine: str, exc: psycopg2.Error) -> HTTPException:
    code = exc.pgcode
    if code in _STATUS:
        return HTTPException(_STATUS[code], exc.diag.message_primary)
    if code in _GENERIC:
        return HTTPException(*_GENERIC[code])
    if code == "42883":  # undefined function: routines missing or outdated
        log.error("Routine %s is not installed. Run: python setup_db.py (same DATABASE_URL)", routine)
        return HTTPException(503, "Database routines are not installed; run setup_db.py")
    # Log the routine and error class only: never parameters (may hold hashes/PII).
    log.error("Database error in %s: %s %s", routine, code, type(exc).__name__)
    if isinstance(exc, (psycopg2.OperationalError, PoolError)):
        return HTTPException(503, "Database unavailable")
    return HTTPException(500, "Database error")


def call(routine: str, *args, one: bool = False, attempts: int = 3):
    """Run SELECT * FROM routine(args...) and return rows as dicts (or one row/None)."""
    if not _ROUTINE_NAME.match(routine):
        raise ValueError(f"Invalid routine name: {routine!r}")
    sql = f"SELECT * FROM {routine}({', '.join(['%s'] * len(args))})"

    for attempt in range(1, attempts + 1):
        try:
            with transaction() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute(sql, args)
                rows = [dict(row) for row in cur.fetchall()]
            break
        except PoolTimeout:
            log.error("No database connection free for %s; raise DB_POOL_MAX or check load", routine)
            raise HTTPException(503, "Server is busy; please try again") from None
        except psycopg2.Error as exc:
            # The whole routine ran in one rolled-back transaction, so retrying is safe.
            if exc.pgcode in _RETRYABLE and attempt < attempts:
                continue
            raise _http_error(routine, exc) from None

    if one:
        return rows[0] if rows else None
    return rows
