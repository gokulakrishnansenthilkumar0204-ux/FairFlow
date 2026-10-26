from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from zoneinfo import ZoneInfo
from threading import Barrier

from fastapi.testclient import TestClient

import main
from conftest import add_stock, set_requirement, sql


def distribute(client, headers, village_id):
    return client.post("/villages/mark-distributed", json={"village_id": village_id}, headers=headers)


def stock_of(commodity_id, shop_id=1):
    rows = sql("SELECT quantity FROM stock WHERE shop_id = %s AND commodity_id = %s", (shop_id, commodity_id))
    return float(rows[0][0]) if rows else 0.0


def test_successful_distribution(client, world):
    set_requirement(client, world.officer, 1, 1, 70)
    set_requirement(client, world.officer, 1, 2, 30)
    add_stock(client, world.officer, 1, 120)
    add_stock(client, world.officer, 2, 30)

    r = distribute(client, world.officer, 1)
    assert r.status_code == 200, r.text
    today = datetime.now(ZoneInfo("Asia/Kolkata")).date().isoformat()  # APP_TIMEZONE
    assert r.json() == {
        "message": "Distribution completed and stock updated", "village_id": 1, "village_name": "V1",
        "distribution_date": today,
        "distributed_items": [
            {"commodity_id": 1, "commodity_name": "Rice", "distributed_quantity": 70, "remaining_stock": 50},
            {"commodity_id": 2, "commodity_name": "Wheat", "distributed_quantity": 30, "remaining_stock": 0},
        ],
    }
    assert stock_of(1) == 50 and stock_of(2) == 0  # fully used row removed (stock must stay > 0)
    history = sql("SELECT village_id, commodity_id, quantity, distribution_date::text FROM distribution_history ORDER BY commodity_id")
    assert [(h[0], h[1], float(h[2]), h[3]) for h in history] == [(1, 1, 70.0, today), (1, 2, 30.0, today)]
    assert sql("SELECT last_distribution_date::text FROM villages WHERE id = 1")[0][0] == today
    assert sql("SELECT count(*) FROM audit_log WHERE action = 'distribution.completed'")[0][0] == 1


def test_same_day_repeat_is_rejected(client, world):
    set_requirement(client, world.officer, 1, 1, 10)
    add_stock(client, world.officer, 1, 100)
    assert distribute(client, world.officer, 1).status_code == 200
    r = distribute(client, world.officer, 1)
    assert r.status_code == 409 and "already received" in r.json()["detail"]
    assert stock_of(1) == 90


def test_insufficient_or_blocked_stock_changes_nothing(client, world):
    set_requirement(client, world.officer, 1, 1, 10)
    set_requirement(client, world.officer, 1, 2, 30)
    add_stock(client, world.officer, 1, 100)
    add_stock(client, world.officer, 2, 20)

    r = distribute(client, world.officer, 1)
    assert r.status_code == 409
    assert r.json()["detail"] == "Insufficient stock for 'Wheat': required 30, available 20"
    assert (stock_of(1), stock_of(2)) == (100, 20)
    assert sql("SELECT count(*) FROM distribution_history")[0][0] == 0

    client.post("/commodity-status", json={"shop_id": 1, "commodity_id": 1, "status": "seasonally_suspended",
                                           "reason": "Off season"}, headers=world.officer)
    r = distribute(client, world.officer, 1)
    assert r.status_code == 400 and "seasonally_suspended" in r.json()["detail"]
    assert sql("SELECT last_distribution_date FROM villages WHERE id = 1")[0][0] is None


def test_no_requirements_and_authorization(client, world):
    assert distribute(client, world.officer, 1).status_code == 400
    assert distribute(client, world.officer, 99).status_code == 404
    assert distribute(client, world.officer2, 1).status_code == 403
    assert distribute(client, world.admin, 1).status_code == 403
    assert distribute(client, world.ben, 1).status_code == 403


def test_failure_after_deduction_rolls_everything_back(client, world):
    set_requirement(client, world.officer, 1, 1, 70)
    set_requirement(client, world.officer, 1, 2, 30)
    add_stock(client, world.officer, 1, 120)
    add_stock(client, world.officer, 2, 30)
    # Force the history insert (which runs after stock was deleted/updated) to fail.
    sql("ALTER TABLE distribution_history ADD CONSTRAINT ck_test_fail CHECK (commodity_id <> 2) NOT VALID")
    try:
        r = distribute(client, world.officer, 1)
        assert r.status_code == 400
    finally:
        sql("ALTER TABLE distribution_history DROP CONSTRAINT ck_test_fail")
    assert (stock_of(1), stock_of(2)) == (120, 30)
    assert sql("SELECT count(*) FROM distribution_history")[0][0] == 0
    assert sql("SELECT last_distribution_date FROM villages WHERE id = 1")[0][0] is None
    assert sql("SELECT count(*) FROM audit_log WHERE action = 'distribution.completed'")[0][0] == 0


def run_concurrently(headers, village_ids):
    barrier = Barrier(len(village_ids))

    def worker(village_id):
        client = TestClient(main.app)
        barrier.wait()
        return distribute(client, headers, village_id).status_code

    with ThreadPoolExecutor(len(village_ids)) as pool:
        return list(pool.map(worker, village_ids))


def test_concurrent_villages_cannot_over_distribute(client, world):
    for v in (1, 2):
        set_requirement(client, world.officer, v, 1, 60)
    add_stock(client, world.officer, 1, 100)  # enough for only one village

    codes = run_concurrently(world.officer, [1, 2])
    assert sorted(codes) == [200, 409]
    assert stock_of(1) == 40
    assert float(sql("SELECT sum(quantity) FROM distribution_history")[0][0]) == 60


def test_concurrent_same_village_distributes_once(client, world):
    set_requirement(client, world.officer, 1, 1, 10)
    add_stock(client, world.officer, 1, 1000)
    codes = run_concurrently(world.officer, [1] * 6)
    assert codes.count(200) == 1 and codes.count(409) == 5
    assert stock_of(1) == 990


def test_many_concurrent_multi_commodity_distributions_no_deadlock(client, world):
    village_ids = [1, 2]
    for i in range(6):
        r = client.post("/villages", json={"name": f"X{i}", "shop_id": 1}, headers=world.admin)
        village_ids.append(r.json()["id"])
    for n, v in enumerate(village_ids):
        # Insert requirements in alternating commodity order to provoke lock-order issues.
        for c in ((1, 2) if n % 2 else (2, 1)):
            set_requirement(client, world.officer, v, c, 5)
    add_stock(client, world.officer, 1, 1000)
    add_stock(client, world.officer, 2, 1000)

    codes = run_concurrently(world.officer, village_ids)
    assert codes == [200] * len(village_ids)
    assert stock_of(1) == stock_of(2) == 1000 - 5 * len(village_ids)
    # History is consistent with stock movement.
    totals = dict(sql("SELECT commodity_id, sum(quantity) FROM distribution_history GROUP BY 1"))
    assert {k: float(v) for k, v in totals.items()} == {1: 5.0 * len(village_ids), 2: 5.0 * len(village_ids)}
