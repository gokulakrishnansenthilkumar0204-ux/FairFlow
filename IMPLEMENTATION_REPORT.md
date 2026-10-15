# FairFlow Backend: Refactor Report

Date: 2026-10-14 · Python 3.13 · PostgreSQL 16 · see `README.md` for setup and commands.

## 1. Files

**Inspected:** `main.py`, `auth.py`, `models.py`, `database.py`, `create_tables.py`,
`migrate_stock_management.py`, `procedures.sql`, `STORED_PROCEDURES_SETUP.md`,
`requirements.txt`, `.gitignore`. There were no tests.

| Status | File | Change |
|---|---|---|
| Modified | `main.py` | All ORM queries replaced by `db.call()` to `ff_*` functions. Same routes and response shapes; duplicate checks moved into the DB. Adds biometric, complaint, notification and audit-log endpoints. 1,273 → 566 lines. |
| Modified | `auth.py` | Uses `bcrypt` directly (passlib breaks with bcrypt 5), switched from python-jose to PyJWT, looks users up via `ff_get_user_by_username`. Missing token now returns 401 (was 403). Equalises timing for unknown usernames. Enforces a JWT secret of ≥ 32 characters. |
| Modified | `requirements.txt` | Pinned versions. Removed sqlalchemy, passlib and python-jose; added PyJWT. |
| Created | `db.py` | The single DB helper: pool, transaction, rollback, retry, error mapping. |
| Created | `schema.sql` | Tables, constraints, indexes, in-place migration. |
| Created | `routines.sql` | 31 functions (replaces `procedures.sql`). |
| Created | `setup_db.py` | Repeatable setup and verification, plus first-admin seeding. |
| Created | `biometric.py` | Adapter interface plus a clearly labelled mock. |
| Created | `notifications.py` | Bilingual templates and provider interface (mock/disabled). |
| Created | `tests/*` (6 files), `pytest.ini`, `requirements-dev.txt` | 45 tests. |
| Created | `README.md`, `.env.example`, this report | Documentation. |
| Deleted | `models.py`, `database.py`, `create_tables.py` | ORM removed. |
| Deleted | `migrate_stock_management.py` | Its stock-merge logic is now part of `schema.sql`. |
| Deleted | `procedures.sql`, `STORED_PROCEDURES_SETUP.md` | Replaced by `routines.sql` and `README.md`. |

## 2. SQLAlchemy removal: confirmed

No `.py` file imports or mentions SQLAlchemy, and it is not in `requirements.txt`. SQL
runs in exactly one runtime place, `db.call()`, which can only build
`SELECT * FROM ff_<name>(%s, …)` with bound parameters. `setup_db.py` executes the
`.sql` files at install time. Both facts are checked by
`tests/test_setup.py::test_no_sqlalchemy_and_sql_only_executed_by_db_call`. Another test
checks that every routine the code calls is installed, and that every installed routine
is used (`ff_audit` is used internally by the other routines).

## 3. Before / after

| Feature | Before | After |
|---|---|---|
| Fresh install | **Broken**: bcrypt 5 + passlib crash; SQLAlchemy 2.1 needs psycopg 3 | Works, pinned and tested |
| First admin | **Missing**: no way to create one | `setup_db.py` with `ADMIN_USERNAME`/`ADMIN_PASSWORD` |
| DB access | ORM for reads; procedures for writes | Functions for all reads and writes |
| DB errors (duplicates, bad reason, race) | **500** | 400 / 404 / 409 with messages |
| Distribution atomicity | Atomic, but unordered locks (deadlock risk) and duplicated Python pre-checks | One function with ordered locks and a single validation path; deadlock retry; tested under concurrency |
| Same-day double distribution | **Allowed** (duplicate history) | 409 |
| History dates | Text; `2026-13-45` accepted | `DATE`; invalid or future dates rejected |
| Forecast | Skipped empty months (distorted trend) | Empty months count as 0; regression unchanged |
| Quantities | `FLOAT` | `NUMERIC(12,3)`, max 3 decimals validated |
| Config | Hard-coded DB URL | `DATABASE_URL` env, shared by setup and API |
| Biometric verification | Missing | Adapter + mock (**real provider externally blocked**) |
| Notifications | Missing | Templates + mock provider (**real SMS externally blocked**) |
| Complaints | Missing | Submit / list / track / status updates with role rules |
| Audit log | Missing | Written by every write routine in the same transaction; admin endpoint |
| Tests | None | 45 passing |

**API changes the Flutter app may notice:**
- `POST /shops`, `/villages` and `/commodities` return **201** (were 200).
- Missing token returns **401** (was 403).
- Errors that used to be 500 now return 4xx.
- Repeat distribution on the same day returns 409.
- Readiness without requirements now also includes `shop_id`.
- `/forecast/{shop}` can return `STOCK_MATCHES_FORECAST`.
- Forecast history includes zero months.
- Signup and admin user creation accept an optional `phone`.
- The 403 message for `/commodity-status/{shop}` is unified.

## 4. Test results (actual run)

```
pytest -v  ->  45 passed   (PostgreSQL 16, isolated database fairflow_test)
```

| Area | Tests |
|---|---|
| Auth | Signup only creates beneficiaries (an injected `role` is ignored); password rules incl. the 72-byte limit; wrong/unknown login; missing, malformed, forged, expired and deleted-user tokens; role taken from the DB, not the token; admin user-creation rules; old passlib hashes still verify; users without a shop see nothing |
| Operations | Admin management; stock merge, update, delete and validation; 16 cross-shop and role checks; commodity status; requirement upsert; readiness states; legacy status values; scheduling policy (never-served first, oldest date, id tie-break, skips not-ready villages) |
| Distribution | Success with exact response, history and date; same-day repeat; insufficient or suspended stock changes nothing; authorization; **forced failure after stock deduction rolls everything back**; concurrent competing villages (one 200, one 409, no over-distribution); 6 concurrent requests for the same village (exactly one succeeds); 8 concurrent multi-commodity distributions with reversed requirement order (no deadlock, history matches stock movement) |
| Forecast | Known-value regression, zero-clipping, reorder rule and recommendations, date/quantity validation, API with known data, zero-filled months, distribution feeds history |
| Modules | Mock biometric match/no-match, nothing sensitive stored, not a login, restrictions, disabled and production refusal; bilingual simulated SMS; SMS failure does not undo distribution; complaint lifecycle and access; audit log contents with no secrets |
| Setup | Routines installed and used; no ORM; idempotent re-run keeps data; **migration of an old SQLAlchemy schema** (refuses invalid dates without changing anything, then converts types, merges duplicate stock, keeps legacy rows, replaces old procedures) |

The concurrency tests were run 3 more times: all passed. Unresolved failures: none.
Not tested: real biometric hardware/provider and real SMS delivery (externally blocked),
and load beyond the concurrency tests above.

Also verified by hand: `setup_db.py` on the database from the original code, including
its invalid date. It stopped, the date was fixed, then it migrated and re-ran cleanly.
`uvicorn` then served `/login`, `/stock`, `/forecast` and `/docs`.

## 5. Remaining work

1. **Biometric:** a real provider adapter. This needs official authorisation, the API specification, credentials and certified devices (README, "Biometric verification").
2. **SMS:** a real gateway adapter (provider account, sender ID, DLT templates). Have a native speaker review the Tamil text.
3. **Beneficiary–shop link:** beneficiaries still choose their shop at signup (existing behaviour, kept). Before production, an officer should confirm it, e.g. against the ration-card record.
4. Beneficiaries are linked to shops, not villages, so "distribution completed" SMS goes to every beneficiary of the shop.
5. **Forecast:** the current (incomplete) month is included, and no accuracy evaluation has been done.
6. Not done (out of scope, not invented): failed-login audit or rate limiting, refresh tokens, pagination beyond fixed limits.
7. Before moving to a hosted DB, create a least-privilege DB role that can only `EXECUTE` the `ff_*` functions.

## 6. Short explanation for the professor

> We moved all database access into PostgreSQL. FastAPI no longer uses an ORM. Each operation (creating stock, checking readiness, completing a distribution, reading a forecast series) is a named PostgreSQL function, called through one helper with bound parameters. Data rules are enforced by the database itself through constraints, so they apply even outside the API. Distribution is one transaction with ordered row locks: stock is checked and deducted, history is written and the village date is updated together or not at all, and the audit log entry is written in the same transaction. FastAPI does only request validation, JWT authentication, role and shop authorization, and HTTP responses.
>
> On biometrics: password login is kept only for the mobile app. Beneficiary identity at the shop counter goes through a separate verification adapter, which stores only the result and never fingerprint data. The real provider requires government authorisation and certified devices, so it is not connected yet. A clearly labelled mock is used for testing.
