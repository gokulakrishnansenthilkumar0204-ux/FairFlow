import json

import notifications
from biometric import MOCK_MATCH_TOKEN
from conftest import add_stock, make_user, set_requirement, sql

BEN = 4  # world user ids: admin 1, officer1 2, officer2 3, ben1 4


# ---------- Biometric (mock adapter) ----------

def verify(client, headers, beneficiary_id, token):
    return client.post(f"/beneficiaries/{beneficiary_id}/biometric-verification",
                       json={"capture_token": token}, headers=headers)


def test_mock_biometric_match_and_no_match(client, world):
    ok = verify(client, world.officer, BEN, MOCK_MATCH_TOKEN)
    assert ok.status_code == 200
    body = ok.json()
    assert body["verified"] is True and body["simulated"] is True and body["provider"] == "mock"
    assert verify(client, world.officer, BEN, "something-else").json()["verified"] is False

    rows = sql("SELECT beneficiary_id, officer_id, shop_id, verified FROM biometric_verifications ORDER BY id")
    assert rows == [(BEN, 2, 1, True), (BEN, 2, 1, False)]
    # Nothing resembling the capture payload is stored anywhere.
    columns = {r[0] for r in sql("SELECT column_name FROM information_schema.columns "
                                 "WHERE table_name = 'biometric_verifications'")}
    assert not columns & {"capture_token", "template", "fingerprint", "image"}
    assert MOCK_MATCH_TOKEN not in json.dumps([str(r) for r in sql("SELECT * FROM audit_log")])


def test_biometric_is_not_a_login_and_is_restricted(client, world):
    assert "access_token" not in verify(client, world.officer, BEN, MOCK_MATCH_TOKEN).json()
    assert verify(client, world.officer2, BEN, MOCK_MATCH_TOKEN).status_code == 403  # other shop
    assert verify(client, world.ben, BEN, MOCK_MATCH_TOKEN).status_code == 403
    assert verify(client, world.admin, BEN, MOCK_MATCH_TOKEN).status_code == 403
    assert verify(client, world.officer, 2, MOCK_MATCH_TOKEN).status_code == 404  # user 2 is officer1


def test_biometric_provider_configuration(client, world, monkeypatch):
    monkeypatch.setenv("BIOMETRIC_PROVIDER", "disabled")
    assert verify(client, world.officer, BEN, MOCK_MATCH_TOKEN).status_code == 503
    monkeypatch.setenv("BIOMETRIC_PROVIDER", "mock")
    monkeypatch.setenv("APP_ENV", "production")
    assert verify(client, world.officer, BEN, MOCK_MATCH_TOKEN).status_code == 503
    assert sql("SELECT count(*) FROM biometric_verifications")[0][0] == 0


# ---------- Notifications ----------

def ready_village(client, world):
    set_requirement(client, world.officer, 1, 1, 10)
    add_stock(client, world.officer, 1, 100)


def test_distribution_sends_bilingual_simulated_sms(client, world):
    make_user(client, "ben2", "beneficiary", 1)  # no phone
    ready_village(client, world)
    assert client.post("/villages/mark-distributed", json={"village_id": 1}, headers=world.officer).status_code == 200
    rows = sql("SELECT user_id, status, message FROM notifications ORDER BY user_id")
    assert [(r[0], r[1]) for r in rows] == [(BEN, "simulated"), (5, "skipped_no_phone")]
    assert "Ration distribution for V1" in rows[0][2] and "ரேஷன் விநியோகம்" in rows[0][2]
    mine = client.get("/notifications", headers=world.ben).json()
    assert len(mine) == 1 and mine[0]["status"] == "simulated"


def test_notification_failure_does_not_undo_distribution(client, world, monkeypatch):
    def broken(phone, text):
        raise ConnectionError("gateway down")
    monkeypatch.setitem(notifications.PROVIDERS, "mock", broken)
    ready_village(client, world)
    assert client.post("/villages/mark-distributed", json={"village_id": 1}, headers=world.officer).status_code == 200
    assert sql("SELECT status FROM notifications")[0][0] == "failed"
    assert float(sql("SELECT quantity FROM stock")[0][0]) == 90

    # Even a crash inside notification processing itself is contained.
    monkeypatch.setattr(notifications, "render", lambda *a, **k: 1 / 0)
    notifications.notify("distribution_completed", shop_id=1, village="V1", date="2026-01-01")


# ---------- Complaints ----------

def test_complaint_lifecycle(client, world):
    r = client.post("/complaints", json={"category": "quantity", "description": "Received less rice"},
                    headers=world.ben)
    assert r.status_code == 201
    cid = r.json()["id"]
    assert r.json()["status"] == "open" and r.json()["shop_id"] == 1

    assert client.post("/complaints", json={"category": "quantity", "description": "x"}, headers=world.ben).status_code == 422
    assert client.post("/complaints", json={"category": "other", "description": "Officer cannot file"},
                       headers=world.officer).status_code == 403

    assert len(client.get("/complaints", headers=world.ben).json()) == 1
    assert len(client.get("/complaints", headers=world.officer).json()) == 1
    assert client.get("/complaints", headers=world.officer2).json() == []
    assert client.get(f"/complaints/{cid}", headers=world.officer2).status_code == 403
    other_ben = make_user(client, "ben9", "beneficiary", 1)
    assert client.get(f"/complaints/{cid}", headers=other_ben).status_code == 403

    path = f"/complaints/{cid}/status"
    assert client.patch(path, json={"status": "in_progress"}, headers=world.ben).status_code == 403
    assert client.patch(path, json={"status": "in_progress"}, headers=world.officer2).status_code == 403
    assert client.patch(path, json={"status": "in_progress"}, headers=world.officer).json()["status"] == "in_progress"
    assert client.patch(path, json={"status": "resolved"}, headers=world.officer).status_code == 400  # note needed
    r = client.patch(path, json={"status": "resolved", "resolution_note": "Shortfall supplied"}, headers=world.admin)
    assert r.status_code == 200 and r.json()["resolution_note"] == "Shortfall supplied"
    assert client.patch(path, json={"status": "in_progress"}, headers=world.officer).status_code == 409  # closed
    assert client.get("/complaints/999", headers=world.admin).status_code == 404

    events = [r[0] for r in sql("SELECT status FROM notifications WHERE event = 'complaint_status' ORDER BY id")]
    assert events == ["simulated", "simulated"]


def test_beneficiary_without_shop_cannot_complain(client, world):
    loose = make_user(client, "loose", "beneficiary")
    r = client.post("/complaints", json={"category": "other", "description": "No shop yet"}, headers=loose)
    assert r.status_code == 400


# ---------- Audit log ----------

def test_audit_log_records_actions_without_secrets(client, world):
    client.post("/signup", json={"username": "auditme", "password": "SuperSecret99"})
    ready_village(client, world)
    actions = {r[0] for r in sql("SELECT action FROM audit_log")}
    assert {"user.created", "shop.created", "village.created", "commodity.created", "stock.added",
            "village_requirement.set"} <= actions

    logs = client.get("/audit-logs?limit=500", headers=world.admin)
    assert logs.status_code == 200
    dump = json.dumps(logs.json())
    assert "SuperSecret99" not in dump and "$2b$" not in dump and "Bearer" not in dump
    stock_entry = next(e for e in logs.json() if e["action"] == "stock.added")
    assert stock_entry["actor_id"] == 2  # officer1
    assert client.get("/audit-logs", headers=world.officer).status_code == 403
