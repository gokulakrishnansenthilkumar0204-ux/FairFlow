"""Test fixtures. Tests run ONLY against TEST_DATABASE_URL, whose database name must
contain "test"; tables there are truncated between tests. The real database is never used."""

import os

import psycopg2
import pytest
from psycopg2.extensions import parse_dsn

TEST_URL = os.getenv("TEST_DATABASE_URL")
if not TEST_URL:
    pytest.exit("Set TEST_DATABASE_URL to a separate test database", returncode=2)
if "test" not in parse_dsn(TEST_URL).get("dbname", ""):
    pytest.exit("Refusing to run: TEST_DATABASE_URL database name must contain 'test'", returncode=2)

os.environ.update(
    DATABASE_URL=TEST_URL,
    JWT_SECRET_KEY="test-secret-key-that-is-at-least-32-characters",
    BIOMETRIC_PROVIDER="mock",
    SMS_PROVIDER="mock",
    APP_ENV="test",
)

from fastapi.testclient import TestClient  # noqa: E402

import main  # noqa: E402
import setup_db  # noqa: E402
from auth import hash_password  # noqa: E402
from db import call  # noqa: E402

TABLES = ("audit_log, notifications, complaints, biometric_verifications, distribution_history, "
          "village_requirements, commodity_status, stock, users, villages, commodities, shops")


def sql(query, params=None):
    with psycopg2.connect(TEST_URL) as conn, conn.cursor() as cur:
        cur.execute(query, params)
        return cur.fetchall() if cur.description else None


@pytest.fixture(scope="session", autouse=True)
def database():
    setup_db.setup(TEST_URL)


@pytest.fixture(autouse=True)
def clean_tables(database):
    sql(f"TRUNCATE {TABLES} RESTART IDENTITY CASCADE")


@pytest.fixture
def client():
    return TestClient(main.app)


def make_user(client, username, role, shop_id=None, phone=None, password="password123"):
    call("ff_create_user", None, username, hash_password(password), role, shop_id, phone)
    token = client.post("/login", data={"username": username, "password": password}).json()
    return {"Authorization": f"Bearer {token['access_token']}"}


class World:
    """Two shops; shop 1 has villages V1, V2 and an officer + beneficiary; shop 2 its own officer."""


@pytest.fixture
def world(client):
    w = World()
    w.admin = make_user(client, "admin", "admin")
    for name in ("Shop One", "Shop Two"):
        assert client.post("/shops", json={"name": name, "location": "Chennai"}, headers=w.admin).status_code == 201
    for name, shop in (("V1", 1), ("V2", 1), ("W1", 2)):
        assert client.post("/villages", json={"name": name, "shop_id": shop}, headers=w.admin).status_code == 201
    for name in ("Rice", "Wheat"):
        assert client.post("/commodities", json={"name": name}, headers=w.admin).status_code == 201
    w.officer = make_user(client, "officer1", "officer", 1)
    w.officer2 = make_user(client, "officer2", "officer", 2)
    w.ben = make_user(client, "ben1", "beneficiary", 1, phone="+919876543210")
    return w


def add_stock(client, headers, commodity_id, quantity, shop_id=1):
    r = client.post("/stock", json={"shop_id": shop_id, "commodity_id": commodity_id, "quantity": quantity},
                    headers=headers)
    assert r.status_code == 201, r.text
    return r.json()


def set_requirement(client, headers, village_id, commodity_id, quantity):
    r = client.post(f"/villages/{village_id}/requirements",
                    json={"commodity_id": commodity_id, "quantity": quantity}, headers=headers)
    assert r.status_code == 201, r.text
    return r.json()
