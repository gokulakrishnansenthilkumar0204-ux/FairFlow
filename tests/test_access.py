"""Sign-in flow (fingerprint registration, then fingerprint-only sign-in) and account
access management: approval, deactivation, device revocation, role changes, session
invalidation, rate limiting, mobile association files, routine version check, pool."""

from concurrent.futures import ThreadPoolExecutor

import pytest

import ratelimit
import setup_db
from conftest import TEST_URL, make_user, sql
from soft_authenticator import SoftAuthenticator


def bearer(session):
    return {"Authorization": f"Bearer {session['access_token']}"}


def signup(client, device, username, shop_id=1):
    options = client.post("/auth/passkey/signup/options", json={"username": username, "shop_id": shop_id}).json()
    return client.post("/auth/passkey/signup/verify", json={"credential": device.create(options)})


def login(client, device):
    options = client.post("/auth/passkey/login/options", json={}).json()
    return client.post("/auth/passkey/login/verify", json={"credential": device.get(options)})


def uid(username):
    return sql("SELECT id FROM users WHERE username = %s", (username,))[0][0]


def enroll(client, username, code, device):
    options = client.post("/auth/passkey/enroll/options",
                          json={"username": username, "activation_code": code}).json()
    return client.post("/auth/passkey/enroll/verify", json={"credential": device.create(options)})


# ---------- The flow: register with fingerprint, then sign in with fingerprint only ----------

def test_every_role_registers_once_then_signs_in_with_fingerprint_only(client, world):
    # Beneficiary: self-registration.
    ben_phone = SoftAuthenticator()
    assert signup(client, ben_phone, "lakshmi").status_code == 201
    # Officer and admin: created by an admin, first sign-in with the activation code.
    officer_phone, admin_phone = SoftAuthenticator(), SoftAuthenticator()
    for username, role, shop, device in (("officer7", "officer", 1, officer_phone), ("admin7", "admin", None, admin_phone)):
        code = client.post("/admin/users", json={"username": username, "role": role, "shop_id": shop},
                           headers=world.admin).json()["activation_code"]
        assert enroll(client, username, code, device).status_code == 201

    # From now on: no username, no password, no code - only the fingerprint.
    for device, expected in ((ben_phone, "beneficiary"), (officer_phone, "officer"), (admin_phone, "admin")):
        r = login(client, device)
        assert r.status_code == 200 and r.json()["user"]["role"] == expected
        assert set(r.json()["user"]) == {"id", "username", "role", "shop_id", "is_active", "approved"}


def test_login_options_reveal_nothing_about_accounts(client, world):
    assert signup(client, SoftAuthenticator(), "known").status_code == 201
    known = client.post("/auth/passkey/login/options", json={"username": "known"}).json()
    unknown = client.post("/auth/passkey/login/options", json={"username": "nobody-here"}).json()
    no_body = client.post("/auth/passkey/login/options").json()
    for options in (known, unknown, no_body):
        options.pop("challenge")
    assert known == unknown == no_body and known.get("allowCredentials", []) == []


def test_usernames_are_case_insensitive_and_validated(client, world):
    assert signup(client, SoftAuthenticator(), "Meena").status_code == 201
    taken = client.post("/auth/passkey/signup/options", json={"username": "meena", "shop_id": 1})
    assert taken.status_code == 400
    assert client.post("/auth/passkey/signup/options", json={"username": "  spaced  ", "shop_id": 1}).status_code == 200
    for bad in ("a b c", "x<script>", "ab"):
        assert client.post("/auth/passkey/signup/options", json={"username": bad, "shop_id": 1}).status_code == 422
    code = client.post(f"/users/{uid('Meena')}/activation-code", headers=world.officer).json()["activation_code"]
    assert enroll(client, "MEENA", code, SoftAuthenticator()).status_code == 201  # any case works


# ---------- Beneficiary approval ----------

def test_self_registered_beneficiary_waits_for_officer_approval(client, world):
    device = SoftAuthenticator()
    session = signup(client, device, "newfamily").json()
    assert session["user"]["approved"] is False
    ben = bearer(session)
    assert client.get("/me", headers=ben).json()["approved"] is False
    assert client.get("/stock", headers=ben).json() == []
    assert client.get("/villages", headers=ben).json() == []
    assert client.get("/commodity-status/1", headers=ben).status_code == 403
    assert client.get("/shops/1/next-village", headers=ben).status_code == 403
    assert client.post("/complaints", json={"category": "other", "description": "hello there"},
                       headers=ben).status_code == 403

    new_id = uid("newfamily")
    assert client.post(f"/users/{new_id}/approve", headers=world.officer2).status_code == 403  # other shop
    assert client.post(f"/users/{new_id}/approve", headers=world.ben).status_code == 403
    r = client.post(f"/users/{new_id}/approve", headers=world.officer)
    assert r.status_code == 200 and r.json()["approved"] is True
    assert client.get("/villages", headers=ben).json() != []  # same session, now approved
    assert client.post("/complaints", json={"category": "other", "description": "hello there"},
                       headers=ben).status_code == 201
    assert sql("SELECT count(*) FROM audit_log WHERE action = 'user.approved'")[0][0] == 1


def test_password_signup_is_also_pending(client, world):
    assert client.post("/signup", json={"username": "pwben", "password": "password123", "shop_id": 1}).status_code == 201
    assert sql("SELECT approved FROM users WHERE username = 'pwben'") == [(False,)]


# ---------- Taking access away ----------

def test_deactivated_user_is_locked_out_everywhere(client, world):
    phone = SoftAuthenticator()
    code = client.post("/admin/users", json={"username": "leaver", "role": "officer", "shop_id": 1},
                       headers=world.admin).json()["activation_code"]
    session = bearer(enroll(client, "leaver", code, phone).json())
    assert client.get("/stock", headers=session).status_code == 200

    leaver = uid("leaver")
    assert client.post(f"/users/{leaver}/deactivate", headers=world.officer).status_code == 403
    r = client.post(f"/users/{leaver}/deactivate", headers=world.admin)
    assert r.status_code == 200 and r.json()["is_active"] is False
    assert client.get("/stock", headers=session).status_code == 401           # open session ended
    assert login(client, phone).status_code == 403                            # fingerprint refused
    assert client.post(f"/users/{leaver}/activation-code", headers=world.admin).status_code == 409

    assert client.post(f"/users/{leaver}/reactivate", headers=world.admin).status_code == 200
    assert login(client, phone).status_code == 200


def test_lost_phone_revoke_and_new_code(client, world):
    lost, new = SoftAuthenticator(), SoftAuthenticator()
    session = bearer(signup(client, lost, "kumar").json())
    kumar = uid("kumar")
    client.post(f"/users/{kumar}/approve", headers=world.officer)

    # Issuing a code always switches off the old devices, so staff cannot add a hidden extra one.
    code = client.post(f"/users/{kumar}/activation-code", headers=world.officer).json()
    assert code["revoked_passkeys"] == 1
    assert login(client, lost).status_code == 401                 # lost phone no longer signs in
    assert client.get("/me", headers=session).status_code == 401  # its session ended too
    assert enroll(client, "kumar", code["activation_code"], new).status_code == 201
    assert login(client, new).status_code == 200


def test_revoke_all_devices_permissions(client, world):
    signup(client, SoftAuthenticator(), "sita")
    sita = uid("sita")
    officer2 = uid("officer2")
    assert client.delete(f"/users/{sita}/passkeys", headers=world.officer2).status_code == 403
    assert client.delete(f"/users/{officer2}/passkeys", headers=world.officer).status_code == 403
    assert client.delete(f"/users/{uid('officer1')}/passkeys", headers=world.officer).status_code == 403
    assert client.delete(f"/users/{uid('admin')}/passkeys", headers=world.admin).status_code == 400  # self
    r = client.delete(f"/users/{sita}/passkeys", headers=world.officer)
    assert r.status_code == 200 and r.json()["revoked"] == 1
    assert client.delete(f"/users/{officer2}/passkeys", headers=world.admin).status_code == 200


def test_role_change_ends_sessions_and_last_admin_is_protected(client, world):
    o2 = uid("officer2")
    old_session = world.officer2
    r = client.patch(f"/users/{o2}", json={"role": "officer", "shop_id": 1}, headers=world.admin)
    assert r.status_code == 200 and r.json()["shop_id"] == 1
    assert client.get("/stock", headers=old_session).status_code == 401
    assert client.patch(f"/users/{o2}", json={"role": "officer"}, headers=world.admin).status_code == 400
    assert client.patch(f"/users/{o2}", json={"role": "admin", "shop_id": 1}, headers=world.admin).status_code == 400
    assert client.patch(f"/users/{o2}", json={"role": "officer", "shop_id": 1}, headers=world.officer).status_code == 403

    admin_id = uid("admin")
    assert client.post(f"/users/{admin_id}/deactivate", headers=world.admin).status_code == 400  # not yourself
    second = make_user(client, "admin2", "admin")
    assert client.post(f"/users/{admin_id}/deactivate", headers=second).status_code == 200
    # admin2 is now the only active admin and cannot be demoted by anyone.
    assert client.post(f"/users/{admin_id}/reactivate", headers=second).status_code == 200
    a2 = uid("admin2")
    assert client.post(f"/users/{admin_id}/deactivate", headers=second).status_code == 200
    with pytest.raises(Exception, match="At least one active administrator"):
        sql("SELECT ff_set_user_active(NULL, %s, FALSE)", (a2,))


def test_users_listing_hides_internal_fields(client, world):
    for u in client.get("/users", headers=world.admin).json():
        assert "token_version" not in u and {"is_active", "approved", "passkey_count"} <= set(u)


# ---------- Rate limiting ----------

def test_public_sign_in_endpoints_are_rate_limited(client, world, monkeypatch):
    monkeypatch.setenv("RATE_LIMIT_AUTH_PER_MINUTE", "5")
    ratelimit.reset()
    try:
        codes = [client.post("/auth/passkey/login/options", json={}).status_code for _ in range(7)]
        assert codes == [200] * 5 + [429] * 2
        # All "start a fingerprint request" endpoints share one budget (code guessing included) ...
        r = client.post("/auth/passkey/enroll/options", json={"username": "x" * 5, "activation_code": "AAAAA-AAAAA"})
        assert r.status_code == 429 and r.headers["retry-after"]
        # ... while finishing a request has its own budget.
        assert client.post("/auth/passkey/login/verify", json={"credential": {}}).status_code == 400
    finally:
        ratelimit.reset()


# ---------- Village requirements can be removed ----------

def test_remove_village_requirement(client, world):
    client.post("/villages/1/requirements", json={"commodity_id": 1, "quantity": 5}, headers=world.officer)
    assert client.delete("/villages/1/requirements/1", headers=world.ben).status_code == 403
    assert client.delete("/villages/3/requirements/1", headers=world.officer).status_code == 403  # other shop
    assert client.delete("/villages/1/requirements/1", headers=world.officer).status_code == 200
    assert client.get("/villages/1/requirements", headers=world.officer).json() == []
    assert client.delete("/villages/1/requirements/1", headers=world.officer).status_code == 404


# ---------- Mobile app association ----------

def test_mobile_association_files(client, monkeypatch):
    import passkeys
    assert client.get("/.well-known/assetlinks.json").status_code == 404  # not configured
    fp = ":".join(["AB"] * 32)
    monkeypatch.setattr(passkeys, "ANDROID_PACKAGE", "in.fairflow.app")
    monkeypatch.setattr(passkeys, "ANDROID_CERT_FINGERPRINTS", [fp])
    monkeypatch.setattr(passkeys, "IOS_APP_IDS", ["ABCDE12345.in.fairflow.app"])
    links = client.get("/.well-known/assetlinks.json").json()
    assert links[0]["target"] == {"namespace": "android_app", "package_name": "in.fairflow.app",
                                  "sha256_cert_fingerprints": [fp]}
    assert "delegate_permission/common.get_login_creds" in links[0]["relation"]
    aasa = client.get("/.well-known/apple-app-site-association")
    assert aasa.headers["content-type"].startswith("application/json")
    assert aasa.json() == {"webcredentials": {"apps": ["ABCDE12345.in.fairflow.app"]}}
    assert passkeys.android_origin(fp) == "android:apk-key-hash:" + "q6urq6urq6urq6urq6urq6urq6urq6urq6urq6urq6s"


def test_android_app_passkey_is_accepted(client, world, monkeypatch):
    import passkeys
    origin = passkeys.android_origin(":".join(["CD"] * 32))
    monkeypatch.setattr(passkeys, "ORIGINS", passkeys.ORIGINS + [origin])
    app = SoftAuthenticator(origin=origin)  # what Android Credential Manager reports
    assert signup(client, app, "appuser").status_code == 201
    assert login(client, app).status_code == 200


# ---------- Outdated routines are refused ----------

def test_outdated_routines_fail_fast():
    setup_db.check_installed(TEST_URL)
    sql("CREATE OR REPLACE FUNCTION ff_routines_checksum() RETURNS TEXT LANGUAGE sql AS $$ SELECT 'old' $$")
    try:
        with pytest.raises(RuntimeError, match="different version of routines.sql"):
            setup_db.check_installed(TEST_URL)
    finally:
        setup_db.setup(TEST_URL)
    setup_db.check_installed(TEST_URL)


# ---------- Connection pool waits instead of failing ----------

def test_more_parallel_requests_than_connections(client, world, monkeypatch):
    import db
    monkeypatch.setenv("DB_POOL_MAX", "2")
    monkeypatch.setattr(db, "_pool", None)  # rebuild with 2 connections
    try:
        with ThreadPoolExecutor(30) as ex:
            codes = list(ex.map(lambda _: client.get("/stock", headers=world.admin).status_code, range(60)))
        assert codes == [200] * 60
    finally:
        db._pool = None


def test_officer_cannot_reenable_account_an_admin_disabled(client, world):
    signup(client, SoftAuthenticator(), "ravi")
    ravi = uid("ravi")
    client.post(f"/users/{ravi}/approve", headers=world.officer)
    assert client.post(f"/users/{ravi}/deactivate", headers=world.admin).status_code == 200
    assert client.post(f"/users/{ravi}/reactivate", headers=world.officer).status_code == 403
    assert client.post(f"/users/{ravi}/reactivate", headers=world.admin).status_code == 200


def test_notifications_skip_unapproved_and_disabled_accounts(client, world):
    for name in ("asha", "bala", "chitra"):
        signup(client, SoftAuthenticator(), name)
    client.post(f"/users/{uid('asha')}/approve", headers=world.officer)
    client.post(f"/users/{uid('bala')}/approve", headers=world.officer)
    client.post(f"/users/{uid('bala')}/deactivate", headers=world.admin)
    ids = {r[0] for r in sql("SELECT user_id FROM ff_notification_recipients(1, NULL)")}
    assert uid("asha") in ids and uid("bala") not in ids and uid("chitra") not in ids


def test_client_address_uses_forwarded_header_only_behind_trusted_proxy(monkeypatch):
    from types import SimpleNamespace
    request = SimpleNamespace(client=SimpleNamespace(host="10.0.0.1"),
                              headers={"x-forwarded-for": "6.6.6.6, 203.0.113.9"})
    monkeypatch.setenv("TRUSTED_PROXY_COUNT", "0")
    assert ratelimit.client_address(request) == "10.0.0.1"   # header ignored: could be forged
    monkeypatch.setenv("TRUSTED_PROXY_COUNT", "1")
    assert ratelimit.client_address(request) == "203.0.113.9"


def test_village_overview_gives_requirements_and_readiness_in_one_request(client, world):
    r = client.get("/shops/1/village-overview", headers=world.officer)
    assert r.status_code == 200
    assert all({"village_id", "requirements", "is_ready", "configured"} <= set(v) for v in r.json())
    assert client.get("/shops/2/village-overview", headers=world.officer).status_code == 403
