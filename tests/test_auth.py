from datetime import datetime, timedelta, timezone

import jwt

from auth import ALGORITHM, SECRET_KEY
from conftest import make_user, sql


def test_signup_creates_beneficiary_only_and_login_works(client, world):
    r = client.post("/signup", json={"username": "newben", "password": "password123", "shop_id": 1,
                                     "role": "admin"})
    assert r.status_code == 201
    assert r.json() == {"id": r.json()["id"], "username": "newben", "role": "beneficiary"}
    stored = sql("SELECT role, hashed_password FROM users WHERE username = 'newben'")[0]
    assert stored[0] == "beneficiary" and stored[1].startswith("$2") and "password123" not in stored[1]

    login = client.post("/login", data={"username": "newben", "password": "password123"})
    assert login.status_code == 200 and login.json()["token_type"] == "bearer"


def test_signup_validation(client, world):
    assert client.post("/signup", json={"username": "ben1", "password": "password123"}).status_code == 400
    assert client.post("/signup", json={"username": "x1y", "password": "short"}).status_code == 422
    assert client.post("/signup", json={"username": "abc", "password": "é" * 40}).status_code == 422  # >72 bytes
    assert client.post("/signup", json={"username": "abc", "password": "password123", "shop_id": 99}).status_code == 404
    assert client.post("/signup", json={"username": "abc", "password": "password123",
                                        "phone": "12ab"}).status_code == 422


def test_wrong_password_and_unknown_user(client, world):
    assert client.post("/login", data={"username": "ben1", "password": "wrongpass1"}).status_code == 401
    assert client.post("/login", data={"username": "nobody", "password": "password123"}).status_code == 401


def test_invalid_expired_and_missing_tokens(client, world):
    assert client.get("/shops").status_code == 401
    assert client.get("/shops", headers={"Authorization": "Bearer not-a-jwt"}).status_code == 401
    forged = jwt.encode({"sub": "admin"}, "a-different-secret-key-of-32-chars!!", algorithm=ALGORITHM)
    assert client.get("/shops", headers={"Authorization": f"Bearer {forged}"}).status_code == 401
    expired = jwt.encode({"sub": "admin", "exp": datetime.now(timezone.utc) - timedelta(minutes=1)},
                         SECRET_KEY, algorithm=ALGORITHM)
    assert client.get("/shops", headers={"Authorization": f"Bearer {expired}"}).status_code == 401
    ghost = jwt.encode({"sub": "deleted-user", "exp": datetime.now(timezone.utc) + timedelta(minutes=5)},
                       SECRET_KEY, algorithm=ALGORITHM)
    assert client.get("/shops", headers={"Authorization": f"Bearer {ghost}"}).status_code == 401


def test_role_comes_from_database_not_token(client, world):
    token = jwt.encode({"sub": "ben1", "role": "admin", "exp": datetime.now(timezone.utc) + timedelta(minutes=5)},
                       SECRET_KEY, algorithm=ALGORITHM)
    r = client.post("/shops", json={"name": "X", "location": "Y"}, headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 403


def test_admin_user_creation_rules(client, world):
    payload = {"username": "off3", "password": "password123", "role": "officer"}
    assert client.post("/admin/users", json=payload, headers=world.officer).status_code == 403
    assert client.post("/admin/users", json=payload, headers=world.ben).status_code == 403
    assert client.post("/admin/users", json=payload, headers=world.admin).status_code == 400  # no shop
    assert client.post("/admin/users", json=payload | {"shop_id": 9}, headers=world.admin).status_code == 404
    r = client.post("/admin/users", json=payload | {"shop_id": 2}, headers=world.admin)
    body = r.json()
    assert r.status_code == 201 and {k: body[k] for k in ("username", "role", "shop_id")} == \
        {"username": "off3", "role": "officer", "shop_id": 2}
    assert len(body["activation_code"]) == 11 and body["activation_expires_at"]
    bad_admin = {"username": "adm2", "password": "password123", "role": "admin", "shop_id": 1}
    assert client.post("/admin/users", json=bad_admin, headers=world.admin).status_code == 400
    assert client.post("/admin/users", json=payload | {"role": "beneficiary", "shop_id": 1},
                       headers=world.admin).status_code == 422


def test_existing_passlib_style_hash_still_verifies(client, world):
    # Hashes written by the old passlib code are standard $2b$ bcrypt hashes.
    import bcrypt
    legacy = bcrypt.hashpw(b"legacy-pass1", bcrypt.gensalt(rounds=12)).decode()
    sql("INSERT INTO users (username, hashed_password, role) VALUES ('legacy', %s, 'beneficiary')", (legacy,))
    assert client.post("/login", data={"username": "legacy", "password": "legacy-pass1"}).status_code == 200


def test_user_without_shop_sees_nothing(client, world):
    loose = make_user(client, "loose", "beneficiary")
    for path in ("/shops", "/villages", "/stock"):
        assert client.get(path, headers=loose).json() == []
