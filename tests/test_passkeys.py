import json

import setup_db
from conftest import make_user, sql
from soft_authenticator import SoftAuthenticator


def bearer(session):
    return {"Authorization": f"Bearer {session['access_token']}"}


def signup(client, device, username="meena", shop_id=1, phone="+919876500001"):
    options = client.post("/auth/passkey/signup/options",
                          json={"username": username, "shop_id": shop_id, "phone": phone}).json()
    return client.post("/auth/passkey/signup/verify",
                       json={"credential": device.create(options), "device_label": "Test phone"})


def login(client, device, username=None):
    options = client.post("/auth/passkey/login/options", json={"username": username}).json()
    return client.post("/auth/passkey/login/verify", json={"credential": device.get(options)})


def test_signup_and_fingerprint_login_without_any_password(client, world):
    device = SoftAuthenticator()
    r = signup(client, device)
    assert r.status_code == 201, r.text
    assert r.json()["user"]["role"] == "beneficiary" and r.json()["user"]["shop_id"] == 1
    assert sql("SELECT hashed_password FROM users WHERE username = 'meena'") == [(None,)]

    # Only a public key is stored - no biometric data, no private key.
    columns = {c for (c,) in sql("SELECT column_name FROM information_schema.columns "
                                 "WHERE table_name = 'webauthn_credentials'")}
    assert columns == {"id", "user_id", "credential_id", "public_key", "sign_count", "transports",
                       "device_label", "created_at", "last_used_at"}

    session = login(client, device)  # usernameless (discoverable passkey)
    assert session.status_code == 200, session.text
    me = client.get("/me", headers=bearer(session.json())).json()
    assert me["username"] == "meena" and "hashed_password" not in me
    assert login(client, device, "meena").status_code == 200  # old clients may still send a username
    assert sql("SELECT sign_count FROM webauthn_credentials")[0][0] == 2
    assert sql("SELECT count(*) FROM audit_log WHERE action = 'auth.login'")[0][0] == 2


def test_login_options_require_user_verification_and_platform_sensor(client, world):
    reg = client.post("/auth/passkey/signup/options", json={"username": "kavya", "shop_id": 1}).json()
    # Discoverable ("resident") key: later sign-ins need only the fingerprint, no username.
    assert reg["authenticatorSelection"] == {"authenticatorAttachment": "platform", "residentKey": "required",
                                             "requireResidentKey": True, "userVerification": "required"}
    assert reg["rp"]["id"] == "localhost" and reg["user"]["name"] == "kavya"
    assert reg["user"]["id"] != "kavya"  # random handle, not the username
    auth = client.post("/auth/passkey/login/options", json={}).json()
    assert auth["userVerification"] == "required" and auth["rpId"] == "localhost"
    assert auth.get("allowCredentials", []) == []  # usernameless


def test_rejects_unverified_user_wrong_origin_replay_and_unknown_key(client, world):
    # Authenticator that did NOT verify the user (e.g. no fingerprint check) is refused.
    lazy = SoftAuthenticator(user_verified=False)
    assert signup(client, lazy, "lazy").status_code == 400
    assert sql("SELECT count(*) FROM users WHERE username = 'lazy'")[0][0] == 0

    # Phishing site: response signed for another origin is refused.
    phish = SoftAuthenticator()
    options = client.post("/auth/passkey/signup/options", json={"username": "phish", "shop_id": 1}).json()
    r = client.post("/auth/passkey/signup/verify",
                    json={"credential": phish.create(options, origin="https://evil.example")})
    assert r.status_code == 400

    device = SoftAuthenticator()
    assert signup(client, device, "real").status_code == 201
    options = client.post("/auth/passkey/login/options", json={}).json()
    assertion = device.get(options)
    assert client.post("/auth/passkey/login/verify", json={"credential": assertion}).status_code == 200
    replay = client.post("/auth/passkey/login/verify", json={"credential": assertion})
    assert replay.status_code == 400 and "expired or is invalid" in replay.json()["detail"]

    stranger = SoftAuthenticator()  # never registered
    assert login(client, stranger).status_code == 401

    # Tampered signature is refused.
    options = client.post("/auth/passkey/login/options", json={}).json()
    bad = device.get(options)
    bad["response"]["signature"] = bad["response"]["signature"][:-6] + "AAAAAA"
    assert client.post("/auth/passkey/login/verify", json={"credential": bad}).status_code == 401

    # Cloned authenticator (counter goes backwards) is refused.
    device.counter = 0
    assert login(client, device).status_code == 401


def test_signup_validation(client, world):
    device = SoftAuthenticator()
    assert client.post("/auth/passkey/signup/options", json={"username": "ben1", "shop_id": 1}).status_code == 400
    assert client.post("/auth/passkey/signup/options", json={"username": "newbie", "shop_id": 99}).status_code == 404
    assert client.post("/auth/passkey/signup/options", json={"username": "x", "shop_id": 1}).status_code == 422
    assert signup(client, device, "dup").status_code == 201
    second = SoftAuthenticator()
    second.credential_id = device.credential_id  # same credential cannot be registered twice
    assert signup(client, second, "dup2").status_code == 409


def test_staff_activation_code_flow(client, world):
    created = client.post("/admin/users", json={"username": "officer9", "role": "officer", "shop_id": 1},
                          headers=world.admin)
    assert created.status_code == 201
    code = created.json()["activation_code"]
    assert sql("SELECT hashed_password FROM users WHERE username = 'officer9'") == [(None,)]
    assert code not in json.dumps(sql("SELECT * FROM enrollment_codes", None), default=str)  # only a hash

    phone = SoftAuthenticator()
    bad = client.post("/auth/passkey/enroll/options", json={"username": "officer9", "activation_code": "WRONG-CODE1"})
    assert bad.status_code == 400
    options = client.post("/auth/passkey/enroll/options",
                          json={"username": "officer9", "activation_code": code.lower()}).json()
    r = client.post("/auth/passkey/enroll/verify", json={"credential": phone.create(options)})
    assert r.status_code == 201 and r.json()["user"]["role"] == "officer"
    # One code = one passkey.
    again = client.post("/auth/passkey/enroll/options", json={"username": "officer9", "activation_code": code})
    assert again.status_code == 400

    session = login(client, phone)
    assert session.status_code == 200
    officer = bearer(session.json())
    assert client.post("/stock", json={"shop_id": 1, "commodity_id": 1, "quantity": 5}, headers=officer).status_code == 201


def test_activation_code_locks_after_five_wrong_attempts(client, world):
    code = client.post("/admin/users", json={"username": "admin2", "role": "admin"},
                       headers=world.admin).json()["activation_code"]
    for _ in range(5):
        assert client.post("/auth/passkey/enroll/options",
                           json={"username": "admin2", "activation_code": "AAAAA-AAAAA"}).status_code == 400
    assert client.post("/auth/passkey/enroll/options",
                       json={"username": "admin2", "activation_code": code}).status_code == 400


def test_activation_code_permissions(client, world):
    ben_id = sql("SELECT id FROM users WHERE username = 'ben1'")[0][0]
    officer2_id = sql("SELECT id FROM users WHERE username = 'officer2'")[0][0]
    assert client.post(f"/users/{ben_id}/activation-code", headers=world.officer).status_code == 201
    assert client.post(f"/users/{ben_id}/activation-code", headers=world.officer2).status_code == 403
    assert client.post(f"/users/{officer2_id}/activation-code", headers=world.officer).status_code == 403
    assert client.post(f"/users/{ben_id}/activation-code", headers=world.ben).status_code == 403
    assert client.post(f"/users/{officer2_id}/activation-code", headers=world.admin).status_code == 201
    assert client.post("/users/999/activation-code", headers=world.admin).status_code == 404
    assert [u["username"] for u in client.get("/users", headers=world.officer).json()] == ["ben1"]
    assert len(client.get("/users", headers=world.admin).json()) == 4
    assert client.get("/users", headers=world.ben).status_code == 403


def test_add_and_remove_devices(client, world):
    laptop, phone = SoftAuthenticator(), SoftAuthenticator()
    session = bearer(signup(client, laptop, "priya").json())
    options = client.post("/auth/passkey/add/options", headers=session).json()
    assert len(options["excludeCredentials"]) == 1  # the laptop key is excluded
    added = client.post("/auth/passkey/add/verify", json={"credential": phone.create(options), "device_label": "Phone"},
                        headers=session)
    assert added.status_code == 201
    keys = client.get("/auth/passkeys", headers=session).json()
    assert [k["device_label"] for k in keys] == ["Test phone", "Phone"] and "public_key" not in keys[0]
    assert login(client, phone).status_code == 200

    # Another user cannot finish someone else's add request.
    other = bearer(signup(client, SoftAuthenticator(), "other").json())
    options = client.post("/auth/passkey/add/options", headers=session).json()
    assert client.post("/auth/passkey/add/verify", json={"credential": SoftAuthenticator().create(options)},
                       headers=other).status_code == 403

    assert client.delete(f"/auth/passkeys/{keys[0]['id']}", headers=other).status_code == 404
    assert client.delete(f"/auth/passkeys/{keys[0]['id']}", headers=session).status_code == 200
    assert client.delete(f"/auth/passkeys/{keys[1]['id']}", headers=session).status_code == 409  # keep one


def test_password_login_is_disabled_by_default(client, world, monkeypatch):
    monkeypatch.setenv("ALLOW_PASSWORD_LOGIN", "false")
    assert client.post("/login", data={"username": "ben1", "password": "password123"}).status_code == 403
    assert client.post("/signup", json={"username": "pwuser", "password": "password123"}).status_code == 403
    config = client.get("/config").json()
    assert config["password_login_enabled"] is False and config["webauthn_rp_id"] == "localhost"
    assert [s["name"] for s in client.get("/public/shops").json()] == ["Shop One", "Shop Two"]


def test_first_admin_bootstrap_and_recovery_code(client):
    info = setup_db.seed_admin("rootadmin")
    assert info["username"] == "rootadmin" and setup_db.seed_admin("rootadmin") is None
    device = SoftAuthenticator()
    options = client.post("/auth/passkey/enroll/options",
                          json={"username": "rootadmin", "activation_code": info["activation_code"]}).json()
    assert client.post("/auth/passkey/enroll/verify", json={"credential": device.create(options)}).status_code == 201
    recovery = setup_db.new_activation_code_for("rootadmin")  # e.g. lost phone
    options = client.post("/auth/passkey/enroll/options",
                          json={"username": "rootadmin", "activation_code": recovery["activation_code"]}).json()
    new_phone = SoftAuthenticator()
    assert client.post("/auth/passkey/enroll/verify", json={"credential": new_phone.create(options)}).status_code == 201
    assert login(client, new_phone).json()["user"]["role"] == "admin"


def test_security_headers_on_frontend_and_api(client):
    r = client.get("/config")
    assert "default-src 'self'" in r.headers["content-security-policy"]
    assert r.headers["x-content-type-options"] == "nosniff"
    assert "content-security-policy" not in client.get("/docs").headers
