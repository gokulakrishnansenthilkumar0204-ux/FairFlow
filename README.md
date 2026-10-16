# FairFlow Backend

FastAPI + PostgreSQL backend for Fair Price Shop (PDS) stock, village distribution,
demand forecasting, complaints and notifications.

```
Flutter app ──HTTP/JSON──▶ FastAPI ──▶ db.call() ──psycopg2──▶ PostgreSQL ff_* functions ──▶ tables
            (validation, JWT, role/shop authorization)        (data rules, transactions, audit log)
```

There is no ORM. Every runtime database operation is one PostgreSQL function in
`routines.sql`, run through the single helper `db.call()` with bound parameters.

## Files

| File | Purpose |
|---|---|
| `main.py` | API routes: validation (Pydantic), authorization, response shaping, forecasting maths |
| `auth.py` | bcrypt password hashing, JWT creation/validation, current-user dependency |
| `db.py` | Connection pool, one transaction per call, rollback, deadlock retry, DB-error → HTTP mapping |
| `schema.sql` | Tables, constraints, indexes, safe in-place migrations from the old SQLAlchemy schema |
| `routines.sql` | All `ff_*` PostgreSQL functions (the database API) |
| `seed_demo.py` | Optional demo data through the same routines; re-runnable; refuses when `APP_ENV=production` |
| `setup_db.py` | Applies `schema.sql` + `routines.sql` in one transaction, verifies routines, optional first admin |
| `biometric.py` | Beneficiary identity-verification adapter (mock only; see below) |
| `notifications.py` | English + Tamil SMS templates, pluggable provider (mock/disabled only) |
| `tests/` | pytest suite (needs a separate test database) |

## Environment variables

| Variable | Required | Meaning |
|---|---|---|
| `DATABASE_URL` | yes | e.g. `postgresql://fairflow:pass@localhost:5432/fairflow_db`. Used by both setup and API |
| `JWT_SECRET_KEY` | yes | Random, ≥ 32 chars: `python -c "import secrets;print(secrets.token_urlsafe(48))"` |
| `ACCESS_TOKEN_EXPIRE_MINUTES` | no | Default 60 |
| `DB_POOL_MAX` | no | Max pooled connections, default 10 |
| `APP_ENV` | no | Set `production` in production (forbids the mock biometric adapter) |
| `BIOMETRIC_PROVIDER` | no | `disabled` (default, endpoint returns 503) or `mock` (local testing only) |
| `SMS_PROVIDER` | no | `disabled` (default) or `mock` (records "simulated"; sends nothing) |
| `CORS_ORIGINS` | no | Comma-separated origins, only for Flutter web / browsers |
| `ADMIN_USERNAME`, `ADMIN_PASSWORD` | no | If set, `setup_db.py` creates this first admin when absent |
| `TEST_DATABASE_URL` | tests | Separate database; its name must contain `test` |

See `.env.example`. Never commit real values.

## Setup and run

```bash
python -m venv .venv && source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements.txt

# 1. Back up the existing database first (setup never drops data, but back up anyway)
pg_dump -Fc fairflow_db > fairflow_db_backup.dump

# 2. Configure (bash; on Windows use `set NAME=value` or a .env loader)
export DATABASE_URL=postgresql://USER:PASS@localhost:5432/fairflow_db
export JWT_SECRET_KEY=<random 48+ chars>
export ADMIN_USERNAME=admin ADMIN_PASSWORD=<strong password>   # first run only

# 3. Create/migrate tables and install + verify all routines (safe to re-run)
python setup_db.py

# 4. Optional: demo data (2 shops, 5 villages, users, stock, 6 months of history)
python seed_demo.py        # safe to re-run; prints the demo logins (password Demo@1234)

# 5. Start the API and Frontend (web app at http://localhost:8000, docs at http://localhost:8000/docs)
uvicorn main:app --reload
```

Visiting `http://localhost:8000/` will load the FairFlow Web Frontend automatically. You can also open `frontend/index.html` directly in your browser.


`setup_db.py` runs everything in one transaction. If any step fails (for example an
old `distribution_history` row has an invalid date such as `2026-13-45`), it stops with
a clear message and leaves the database unchanged; fix the data and re-run.

What the migration does to an existing database: `VARCHAR` → `TEXT`, `FLOAT`
quantities → `NUMERIC(12,3)`, text dates → `DATE`, adds `users.phone`, merges duplicate
stock rows (summing quantities), adds missing constraints (CHECKs on old tables are
`NOT VALID`: enforced for new/changed rows without rejecting legacy rows), creates the
new tables, and replaces the old `ff_*` procedures with the new functions.

## Tests

```bash
pip install -r requirements-dev.txt
createdb fairflow_test
export TEST_DATABASE_URL=postgresql://USER:PASS@localhost:5432/fairflow_test
pytest -v
```

The suite refuses to run unless the database name contains `test`. It installs the
schema there and truncates its tables between tests. It never touches `DATABASE_URL`.

## Roles

| Role | Created by | Can |
|---|---|---|
| `admin` | `setup_db.py` (first) or another admin | Create shops/villages/commodities/users; view everything; delete stock; set status; manage requirements; close complaints; view audit log |
| `officer` | admin (must have a shop) | Manage stock, status, requirements, history of **own shop**; distribute; verify beneficiaries; handle complaints of own shop |
| `beneficiary` | public `/signup` | Read own shop's shops/villages/stock/status/readiness/schedule; file and track own complaints; read own notifications |

## Routines and the APIs that call them

| API | Routine(s) |
|---|---|
| `POST /signup`, `POST /admin/users` | `ff_create_user` |
| `POST /login`, every authenticated request | `ff_get_user_by_username` |
| `POST /shops`, `GET /shops` | `ff_create_shop`, `ff_list_shops` |
| `POST /villages`, `GET /villages` | `ff_create_village`, `ff_list_villages` |
| `POST /commodities`, `GET /commodities` | `ff_create_commodity`, `ff_list_commodities` |
| `POST /stock` | `ff_add_stock` |
| `GET /stock`, `GET /stock/{id}` | `ff_list_stock` |
| `PUT /stock/{id}`, `DELETE /stock/{id}` | `ff_list_stock` (ownership check) + `ff_update_stock` / `ff_delete_stock` |
| `POST /commodity-status`, `GET /commodity-status/{shop_id}` | `ff_set_commodity_status`, `ff_list_shops` + `ff_list_commodity_status` |
| `POST /villages/{id}/requirements`, `GET …/requirements` | `ff_list_villages` + `ff_upsert_village_requirement` / `ff_list_village_requirements` |
| `GET /villages/{id}/readiness` | `ff_list_villages` + `ff_village_requirement_check` |
| `GET /shops/{id}/next-village` | `ff_list_shops` + `ff_next_village` |
| `POST /villages/mark-distributed` | `ff_list_villages` + `ff_mark_distributed`; then `ff_notification_recipients`, `ff_log_notification` |
| `POST /forecast/history` | `ff_add_forecast_history` |
| `GET /forecast/{shop}/{commodity}`, `GET /forecast/{shop}` | `ff_list_shops`, `ff_list_commodities`, `ff_monthly_distribution` |
| `POST /beneficiaries/{id}/biometric-verification` | `ff_get_beneficiary` + `ff_record_biometric_verification` |
| `POST /complaints`, `GET /complaints`, `GET /complaints/{id}` | `ff_create_complaint`, `ff_list_complaints` |
| `PATCH /complaints/{id}/status` | `ff_list_complaints` + `ff_update_complaint_status`; then notification routines |
| `GET /notifications` | `ff_list_notifications` |
| `GET /audit-logs` | `ff_list_audit_log` |
| (internal) | `ff_audit` (called by every write routine), `ff_village_requirement_check` (also used by `ff_next_village`, `ff_mark_distributed`) |

## Distribution safety

`ff_mark_distributed` runs as one transaction:

1. Locks the village row (concurrent calls for the same village queue up), and rejects a second distribution on the same day.
2. Locks the village's requirement rows and the matching stock rows **in commodity order**, so concurrent distributions in one shop cannot deadlock.
3. Re-checks status and quantity for every requirement before changing anything.
4. Deducts stock (rows reaching 0 are removed, since stock must stay > 0), inserts history, updates `last_distribution_date`, writes the audit entry.
5. Any error rolls back all of it. `db.call()` retries a deadlock/serialization failure (safe, because nothing was committed).

SMS notifications are sent **after** the commit, in a background task, so they can never undo a distribution.

## Forecasting

Linear Regression on monthly totals (`x` = month number, `y` = quantity), unchanged from
the original. Months with no distribution between a commodity's first and last recorded
month count as **0** (previously they were skipped, which compressed the time axis).
Needs ≥ 2 months. Negative predictions are clipped to 0.
`reorder_quantity = max(0, predicted_demand − current_stock)`.
No accuracy is claimed: the model has not been evaluated on held-out real data.
Note: the current month is included even though it is still incomplete.

## Biometric verification

**Status: architecture only. No real biometric provider is integrated.**

- `POST /beneficiaries/{id}/biometric-verification` (officer, own shop) sends an opaque
  `capture_token` to the configured adapter and stores **only** the outcome
  (`verified`, provider name, provider transaction reference, officer, shop, time).
- No fingerprint image, template or capture payload is stored or logged.
- Verification is separate from login: it issues no JWT and changes no permissions.
  Password login stays for the mobile app (viewing stock, complaints).
- `BIOMETRIC_PROVIDER=mock` is a **simulation**: it "matches" only when
  `capture_token == "MOCK-MATCH"`. It does not read any fingerprint and is refused when
  `APP_ENV=production`.
- Phone fingerprint unlock is **not** identity verification and is not used.

A real integration still needs, from the authorities rather than from this code:
authorisation from the state food & civil supplies department / PDS authority to
authenticate beneficiaries; access to the identity-authentication service they use
(for Indian PDS this is typically Aadhaar-based authentication through UIDAI-authorised
agencies), with its official API specification, credentials and certificates;
certified fingerprint devices and their SDK, which produce the encrypted capture; a
lawful way to link each beneficiary to their ration-card/identity record; and
legal/privacy review. With those, add one adapter function to `ADAPTERS` in `biometric.py`.

## Notifications

`notifications.py` renders English + Tamil text (Tamil wording should be reviewed by a
native speaker) for `distribution_completed` (to the shop's beneficiaries) and
`complaint_status` (to the complainant). Every attempt is stored in `notifications` with
status `simulated`, `disabled`, `failed` or `skipped_no_phone`. **No SMS is actually
sent**: a real gateway needs an SMS provider account, sender ID and, in India,
DLT-registered templates. Add it as a function in `PROVIDERS`.

## Stored procedures vs ORM (for review)

The ORM generated SQL inside the Python process, so data rules lived in two places and
the database could be modified without them. Now PostgreSQL owns data access: each
operation is a named, parameterised function; integrity rules (quantities > 0, unique
stock per shop/commodity, valid statuses, role/shop rules) are constraints and checks
in the database; multi-step operations like distribution are a single atomic
transaction with row locking; and every write records an audit entry in the same
transaction. FastAPI is a thin, testable layer for validation, authentication,
authorization and HTTP responses. The trade-off is that business logic in SQL needs
its own versioning (`routines.sql`, re-applied by `setup_db.py`) and its own tests
(the pytest suite exercises every routine through the API).
