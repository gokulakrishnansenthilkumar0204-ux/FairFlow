# FairFlow

FastAPI + PostgreSQL backend for Fair Price Shop (PDS) stock, village distribution,
demand forecasting, complaints and notifications.

```
Web app / Android app / iOS app ──HTTP/JSON──▶ FastAPI ──▶ db.call() ──psycopg2──▶ PostgreSQL ff_* functions ──▶ tables
                                (validation, passkeys, JWT, role/shop authorization) (data rules, transactions, audit log)
```

There is no ORM. Every runtime database operation is one PostgreSQL function in
`routines.sql`, run through the single helper `db.call()` with bound parameters.

## Files

| File | Purpose |
|---|---|
| `main.py` | API routes: validation (Pydantic), authorization, response shaping, forecasting maths |
| `passkeys.py` | Fingerprint sign-in (WebAuthn passkeys): registration, activation codes, fingerprint-only sign-in, devices, account access (approve / disable / revoke devices / role), mobile app association files |
| `auth.py` | JWT session creation/validation (with token version, so sessions can be ended), current-user dependency (password helpers only for test mode) |
| `ratelimit.py` | Per-client rate limit for the public sign-in/sign-up endpoints |
| `db.py` | Connection pool that waits for a free connection, app time zone, one transaction per call, rollback, deadlock retry, DB-error → HTTP mapping |
| `schema.sql` | Tables, constraints, indexes, safe in-place migrations from the old SQLAlchemy schema |
| `routines.sql` | All `ff_*` PostgreSQL functions (the database API) |
| `seed_demo.py` | Optional demo data through the same routines; re-runnable; refuses when `APP_ENV=production` |
| `setup_db.py` | Applies `schema.sql` + `routines.sql` in one transaction, verifies routines, optional first admin |
| `biometric.py` | Beneficiary identity-verification adapter (mock only; see below) |
| `notifications.py` | English + Tamil SMS templates, pluggable provider (mock/disabled only) |
| `frontend/` | Web app (HTML/CSS/JS modules, no build step), served by the API at `/` |
| `tests/` | pytest suite (needs a separate test database); `tests/soft_authenticator.py` is a software WebAuthn device |
| `e2e/browser_test.py` | Full browser walkthrough with Chrome's virtual fingerprint authenticator (Playwright) |

## Environment variables

| Variable | Required | Meaning |
|---|---|---|
| `DATABASE_URL` | yes | e.g. `postgresql://fairflow:pass@localhost:5432/fairflow_db`. Used by both setup and API |
| `JWT_SECRET_KEY` | yes | Random, ≥ 32 chars: `python -c "import secrets;print(secrets.token_urlsafe(48))"` |
| `ACCESS_TOKEN_EXPIRE_MINUTES` | no | Default 60 |
| `DB_POOL_MAX` | no | Max pooled connections, default 10. Requests wait for a free one |
| `DB_POOL_TIMEOUT` | no | Seconds a request waits for a connection before 503, default 30 |
| `APP_TIMEZONE` | no | Time zone for "today" (distribution dates, same-day rule, forecast months). Default `Asia/Kolkata` |
| `RATE_LIMIT_AUTH_PER_MINUTE` | no | Sign-in/sign-up attempts per client per minute, default 20 (`0` = off). Per process; see `ratelimit.py` |
| `RATE_LIMIT_SIGNUP_PER_HOUR` | no | Self-registrations per client per hour, default 10 (`0` = off) |
| `TRUSTED_PROXY_COUNT` | no | Reverse proxies in front of the API, default 0. Set to 1 behind one proxy so limits count real client IPs from `X-Forwarded-For` |
| `MAX_PENDING_SIGNUPS_PER_SHOP` | no | Cap on accounts awaiting approval per shop, default 200 |
| `APP_ENV` | no | Set `production` in production (forbids the mock biometric adapter) |
| `BIOMETRIC_PROVIDER` | no | `disabled` (default, endpoint returns 503) or `mock` (local testing only) |
| `SMS_PROVIDER` | no | `disabled` (default) or `mock` (records "simulated"; sends nothing) |
| `CORS_ORIGINS` | no | Comma-separated origins, only for Flutter web / browsers |
| `ADMIN_USERNAME` | first run | `setup_db.py` creates this admin (no password) and prints a one-time activation code |
| `WEBAUTHN_RP_ID` | no | Domain passkeys belong to. Default `localhost`. In production: your domain, e.g. `fairflow.example.in` |
| `WEBAUTHN_ORIGIN` | no | Exact address users open, default `http://localhost:8000`. Production: `https://fairflow.example.in` (comma-separate several) |
| `WEBAUTHN_RP_NAME` | no | Name shown in the fingerprint prompt. Default `FairFlow` |
| `ANDROID_APP_PACKAGE` | mobile | Android app id, e.g. `in.fairflow.app` (published in `/.well-known/assetlinks.json`) |
| `ANDROID_APP_SHA256_CERT_FINGERPRINTS` | mobile | Comma-separated SHA-256 fingerprints of the app's signing certificates (`AB:CD:…`). Also adds the app's `android:apk-key-hash:` origin automatically |
| `IOS_APP_IDS` | mobile | Comma-separated `TEAMID.bundle.id` (published in `/.well-known/apple-app-site-association`) |
| `ACTIVATION_CODE_TTL_HOURS` | no | How long activation codes work. Default 24 |
| `ALLOW_PASSWORD_LOGIN` | no | Default `false`. Only for automated tests/emergencies; the app never shows a password form |
| `TEST_DATABASE_URL` | tests | Separate database; its name must contain `test` |

See `.env.example`. Never commit real values.

## Setup and run

```bash
python -m venv .venv && source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements.txt

# 1. Back up the existing database first (setup never drops data, but back up anyway)
pg_dump -Fc fairflow_db > fairflow_db_backup.dump

# 2. Configure (bash/zsh; on Windows use `set NAME=value`)
export DATABASE_URL=postgresql://USER:PASS@localhost:5432/fairflow_db
export JWT_SECRET_KEY=$(python -c "import secrets;print(secrets.token_urlsafe(48))")
export ADMIN_USERNAME=admin                                   # first run only

# 3. Create/migrate tables, install + verify routines; prints the admin's activation code
python setup_db.py

# 4. Optional: demo data; prints an activation code for each demo user
python seed_demo.py

# 5. Start API + web app
uvicorn main:app --reload
```

Open **http://localhost:8000** (use `localhost`, not `127.0.0.1`: passkeys are tied to
the domain name). Choose **Activate account**, enter `admin` and the code from step 3,
and touch your fingerprint sensor. API docs: http://localhost:8000/docs.

Lost the code or the device? `python setup_db.py --activation-code admin` prints a new one.

## Fingerprint sign-in (WebAuthn passkeys)

There is no password login, on the web or in the mobile app. Every role works the same way:

| Step | Beneficiary | Officer / admin |
|---|---|---|
| **1. First time: register the fingerprint** | *New beneficiary*: username, ration shop, mobile number, then touch the sensor (`/auth/passkey/signup/*`). The account is **waiting for approval** until the shop officer approves it (after checking the ration card): until then the person can sign in but sees no shop data and cannot file complaints. | The admin creates the account (*People & access*) and gets a one-time **activation code**. On their phone/laptop the person chooses *Activate account*, enters username + code, and touches the sensor (`/auth/passkey/enroll/*`). The first admin gets their code from `setup_db.py`. |
| **2. Every sign-in after that** | Touch *Sign in with fingerprint*: **fingerprint only**, no username, no password, no code (`/auth/passkey/login/*`). | Same. |

Passkeys use `userVerification: required` on the device's **built-in** authenticator
(`authenticatorAttachment: platform`) and are **discoverable** (`residentKey: required`), so the
device itself offers the FairFlow passkey and the server identifies the account from it. The
sign-in request is identical for everyone, so it reveals nothing about which accounts exist.

- **What is stored:** only a public key per device, a signature counter, and a random
  user handle. Fingerprint images/templates and private keys never leave the device;
  the server cannot receive them. Activation codes are stored as SHA-256 hashes.
- **What is checked** on every sign-in: a single-use server challenge (3 minutes),
  the exact origin and domain (stops phishing sites), the signature with the stored
  public key, the "user verified" flag, the signature counter (detects cloned keys), and that
  the account is enabled.
- **Taking access away** (admin for anyone; officer for beneficiaries of their shop):
  - **Lost/stolen phone:** *New activation code* with "the old phone is lost or stolen" ticked (the
    default) switches off every existing device of that person, signs them out, and gives a code for
    the new phone. *Switch off devices* does the first part alone.
  - **Disable** an account (e.g. an officer who left): they are signed out at once and cannot sign
    in or activate until enabled again. Nothing is deleted. The last active admin cannot be disabled
    or demoted.
  - **Change role/shop** (admin): the person is signed out and gets the new access at next sign-in.
  - All of these bump `users.token_version`; session tokens carry it, so older tokens stop working
    immediately (not only when they expire after `ACCESS_TOKEN_EXPIRE_MINUTES`).
- **Abuse protection:** the public sign-in/sign-up endpoints are rate limited per client
  (`RATE_LIMIT_AUTH_PER_MINUTE`); activation codes lock after 5 wrong tries.
- **Honest limit:** WebAuthn tells the server "this device verified its owner", not
  *how*. If a device has no fingerprint enrolled, the OS may accept its PIN/passcode instead;
  the server cannot distinguish the two. Requiring a fingerprint-capable device is a
  deployment/policy decision.

**Browser & device requirements:** a current Chrome, Edge, Safari or Firefox; a device
with a fingerprint sensor (or Windows Hello) and a screen lock set up. Passkeys work only
in a **secure context**: `http://localhost` on the same computer, or **HTTPS** everywhere
else. To use a phone browser, serve FairFlow over HTTPS on a real domain (for a demo, an HTTPS
tunnel such as `cloudflared tunnel --url http://localhost:8000` works) and set
`WEBAUTHN_RP_ID` to that domain and `WEBAUTHN_ORIGIN` to its `https://` address.
An IP address like `http://192.168.1.5:8000` will **not** work.

## Mobile app (Android / iOS)

The mobile app uses the same API and the same two-step flow. Native passkey APIs
(Android **Credential Manager**, iOS **AuthenticationServices**; in Flutter e.g. the
`credential_manager` or `passkeys` packages) take the JSON options from the server as-is and
return standard WebAuthn JSON, which the app posts back unchanged.

| Screen | Calls |
|---|---|
| Register (beneficiary) | `POST /auth/passkey/signup/options` `{username, shop_id, phone}` → `createCredential(options)` → `POST /auth/passkey/signup/verify` `{credential, device_label}` |
| Activate (staff / new phone) | `POST /auth/passkey/enroll/options` `{username, activation_code}` → `createCredential` → `POST /auth/passkey/enroll/verify` |
| Sign in (every time) | `POST /auth/passkey/login/options` `{}` → `getCredential(options)` → `POST /auth/passkey/login/verify` `{credential}` |
| After sign-in | Send `Authorization: Bearer <access_token>` on every request. On `401`, show the fingerprint sign-in again. `user.approved == false` → show "waiting for approval". |

Server configuration for the apps (the domain must be served over HTTPS):

1. `WEBAUTHN_RP_ID=fairflow.example.in` (the same domain the web app uses, so one passkey works in both).
2. **Android:** `ANDROID_APP_PACKAGE` and `ANDROID_APP_SHA256_CERT_FINGERPRINTS` (debug *and*
   release/Play signing certificates: `keytool -list -v -keystore …` or Play Console → App integrity).
   The server then serves `https://<RP_ID>/.well-known/assetlinks.json` and accepts the app's
   `android:apk-key-hash:` origin.
3. **iOS:** `IOS_APP_IDS=TEAMID.in.fairflow.app`; the server serves
   `https://<RP_ID>/.well-known/apple-app-site-association`. In Xcode add the Associated Domains
   capability `webcredentials:<RP_ID>`.

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
Passkey tests use a software authenticator that signs with real P-256 keys, so the
server-side verification is exercised exactly as with a phone (also with an Android app origin).
`setup_db.py` stamps the routines with a checksum of `routines.sql`; the API refuses to start
if the database has missing **or outdated** routines.

Browser walkthrough (all roles, real Chromium, virtual fingerprint authenticator), on a fresh database:

```bash
pip install playwright && playwright install chromium
ADMIN_USERNAME=admin python setup_db.py      # copy the activation code
uvicorn main:app --port 8000                 # second terminal
python e2e/browser_test.py /tmp/fairflow-shots <ACTIVATION-CODE>   # CHROME_PATH=… to use a specific Chromium
```

## Roles

| Role | Created by | Can |
|---|---|---|
| `admin` | `setup_db.py` (first) or another admin | Create shops/villages/commodities/users; change roles/shops; disable/enable accounts, switch off devices, issue activation codes; view everything; delete stock; set status; manage requirements; close complaints; view audit log |
| `officer` | admin (must have a shop) | Manage stock, status, requirements, history of **own shop**; distribute; approve, disable and recover (codes, switch off devices) beneficiaries of own shop; verify beneficiaries; handle complaints of own shop |
| `beneficiary` | self-signup with fingerprint, approved by the shop officer | After approval: read own shop's shops/villages/stock/status/readiness/schedule; file and track own complaints; read own notifications |

## Routines and the APIs that call them

| API | Routine(s) |
|---|---|
| `POST /auth/passkey/signup/options`, `…/signup/verify` | `ff_get_user_by_username`, `ff_list_shops`, `ff_create_auth_challenge` / `ff_consume_auth_challenge`, `ff_signup_with_passkey` |
| `POST /auth/passkey/enroll/options`, `…/enroll/verify` | `ff_check_enrollment_code`, `ff_get_or_set_webauthn_handle`, `ff_list_passkeys`, challenges, `ff_add_passkey`, `ff_list_users` |
| `POST /auth/passkey/login/options`, `…/login/verify` | `ff_list_passkeys`, challenges, `ff_get_passkey_for_login`, `ff_record_passkey_login` |
| `POST /auth/passkey/add/options`, `…/add/verify`, `GET/DELETE /auth/passkeys` | `ff_get_or_set_webauthn_handle`, `ff_list_passkeys`, `ff_add_passkey`, `ff_delete_passkey` |
| `GET /users`, `POST /users/{id}/activation-code` (always switches off the account's existing devices) | `ff_list_users`, `ff_issue_activation` |
| `POST /users/{id}/approve`, `…/deactivate`, `…/reactivate`, `PATCH /users/{id}`, `DELETE /users/{id}/passkeys` | `ff_list_users` + `ff_set_beneficiary_approval` / `ff_set_user_active` / `ff_update_user` / `ff_revoke_passkeys` (`ff_assert_admin_remains` internally) |
| `GET /.well-known/assetlinks.json`, `GET /.well-known/apple-app-site-association` | (settings) |
| `GET /config`, `GET /public/shops` | (settings), `ff_list_shops` |
| `POST /admin/users` | `ff_create_user` + `ff_issue_enrollment_code` |
| `POST /signup`, `POST /login` (disabled unless `ALLOW_PASSWORD_LOGIN=true`) | `ff_create_user`, `ff_get_user_by_username` |
| every authenticated request, `GET /me` | `ff_get_user_by_username` |
| `POST /shops`, `GET /shops` | `ff_create_shop`, `ff_list_shops` |
| `POST /villages`, `GET /villages` | `ff_create_village`, `ff_list_villages` |
| `POST /commodities`, `GET /commodities` | `ff_create_commodity`, `ff_list_commodities` |
| `POST /stock` | `ff_add_stock` |
| `GET /stock`, `GET /stock/{id}` | `ff_list_stock` |
| `PUT /stock/{id}`, `DELETE /stock/{id}` | `ff_list_stock` (ownership check) + `ff_update_stock` / `ff_delete_stock` |
| `POST /commodity-status`, `GET /commodity-status/{shop_id}` | `ff_set_commodity_status`, `ff_list_shops` + `ff_list_commodity_status` |
| `POST /villages/{id}/requirements`, `GET …/requirements`, `DELETE …/requirements/{commodity_id}` | `ff_list_villages` + `ff_upsert_village_requirement` / `ff_list_village_requirements` / `ff_delete_village_requirement` |
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
| (internal) | `ff_audit` (called by every write routine), `ff_village_requirement_check` (also used by `ff_next_village`, `ff_mark_distributed`), `ff_routines_checksum` (startup version check) |

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
the original. The series runs from a commodity's first recorded month **up to the current
month** (in `APP_TIMEZONE`); months with no distribution count as **0**. The current month is
still running, so it is shown (`partial: true`) but **not used for training**, and the forecast
is for the **next calendar month** (`forecast_month`). So a commodity whose last distribution
was months ago correctly trends towards 0 instead of predicting "next month" from stale data.
Needs ≥ 2 complete months. Negative predictions are clipped to 0.
`reorder_quantity = max(0, predicted_demand − current_stock)`.
No accuracy is claimed: the model has not been evaluated on held-out real data.

## Biometric verification

**Status: architecture only. No real biometric provider is integrated.**

- `POST /beneficiaries/{id}/biometric-verification` (officer, own shop) sends an opaque
  `capture_token` to the configured adapter and stores **only** the outcome
  (`verified`, provider name, provider transaction reference, officer, shop, time).
- No fingerprint image, template or capture payload is stored or logged.
- Verification is separate from login: it issues no JWT and changes no permissions.
  (Signing in to the app is done with the person's own device passkey, see above.)
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
