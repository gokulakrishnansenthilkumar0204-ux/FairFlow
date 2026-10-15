from conftest import add_stock, set_requirement, sql


def test_admin_management_and_reads(client, world):
    assert client.post("/shops", json={"name": "S", "location": "L"}, headers=world.officer).status_code == 403
    assert client.post("/commodities", json={"name": "rice"}, headers=world.admin).status_code == 409  # case-insensitive dup
    assert client.post("/villages", json={"name": "V", "shop_id": 99}, headers=world.admin).status_code == 404
    assert [s["id"] for s in client.get("/shops", headers=world.admin).json()] == [1, 2]
    assert [s["id"] for s in client.get("/shops", headers=world.officer).json()] == [1]
    assert {v["name"] for v in client.get("/villages", headers=world.ben).json()} == {"V1", "V2"}
    assert len(client.get("/commodities", headers=world.ben).json()) == 2


def test_stock_add_merges_update_delete(client, world):
    first = add_stock(client, world.officer, 1, 100)
    second = add_stock(client, world.officer, 1, 20.5)
    assert first["id"] == second["id"] and second["quantity"] == 120.5
    assert sql("SELECT count(*) FROM stock")[0][0] == 1

    r = client.put(f"/stock/{first['id']}", json={"quantity": 80}, headers=world.officer)
    assert r.status_code == 200 and r.json()["quantity"] == 80
    assert client.put(f"/stock/{first['id']}", json={"quantity": 0}, headers=world.officer).status_code == 422
    assert client.put(f"/stock/{first['id']}", json={"quantity": 0.0001}, headers=world.officer).status_code == 422
    assert client.put("/stock/999", json={"quantity": 5}, headers=world.officer).status_code == 404
    assert client.get(f"/stock/{first['id']}", headers=world.ben).status_code == 200

    r = client.delete(f"/stock/{first['id']}", headers=world.officer)
    assert r.status_code == 200 and r.json() == {"message": f"Stock item {first['id']} deleted"}
    assert client.delete(f"/stock/{first['id']}", headers=world.officer).status_code == 404


def test_stock_validation_and_missing_references(client, world):
    assert client.post("/stock", json={"shop_id": 1, "commodity_id": 9, "quantity": 5},
                       headers=world.officer).status_code == 404
    assert client.post("/stock", json={"shop_id": 1, "commodity_id": 1, "quantity": -5},
                       headers=world.officer).status_code == 422


def test_cross_shop_and_role_restrictions(client, world):
    s2 = add_stock(client, world.officer2, 1, 50, shop_id=2)
    body = {"shop_id": 2, "commodity_id": 1, "quantity": 5}
    assert client.post("/stock", json=body, headers=world.officer).status_code == 403
    assert client.post("/stock", json=body | {"shop_id": 1}, headers=world.admin).status_code == 403
    assert client.post("/stock", json=body | {"shop_id": 1}, headers=world.ben).status_code == 403
    assert client.get(f"/stock/{s2['id']}", headers=world.officer).status_code == 403
    assert client.get(f"/stock/{s2['id']}", headers=world.ben).status_code == 403
    assert client.put(f"/stock/{s2['id']}", json={"quantity": 1}, headers=world.officer).status_code == 403
    assert client.delete(f"/stock/{s2['id']}", headers=world.officer).status_code == 403
    assert client.delete(f"/stock/{s2['id']}", headers=world.ben).status_code == 403
    assert client.get("/villages/3/requirements", headers=world.officer).status_code == 403
    assert client.get("/villages/3/readiness", headers=world.ben).status_code == 403
    assert client.post("/villages/3/requirements", json={"commodity_id": 1, "quantity": 1},
                       headers=world.officer).status_code == 403
    assert client.post("/villages/1/requirements", json={"commodity_id": 1, "quantity": 1},
                       headers=world.ben).status_code == 403
    assert client.get("/commodity-status/2", headers=world.officer).status_code == 403
    assert client.get("/shops/2/next-village", headers=world.ben).status_code == 403
    assert client.get("/forecast/2", headers=world.officer).status_code == 403
    assert client.get("/forecast/1", headers=world.ben).status_code == 403
    assert [s["shop_id"] for s in client.get("/stock", headers=world.officer).json()] == []
    assert client.delete(f"/stock/{s2['id']}", headers=world.admin).status_code == 200


def test_commodity_status(client, world):
    body = {"shop_id": 1, "commodity_id": 1, "status": "out_of_stock", "reason": "   "}
    assert client.post("/commodity-status", json=body, headers=world.officer).status_code == 400
    assert client.post("/commodity-status", json=body | {"reason": "Truck delayed"}, headers=world.officer).status_code == 201
    r = client.post("/commodity-status", json=body | {"status": "active", "reason": None}, headers=world.officer)
    assert r.status_code == 201 and r.json()["status"] == "active"
    assert len(client.get("/commodity-status/1", headers=world.ben).json()) == 1  # upsert, no duplicate
    assert client.post("/commodity-status", json=body | {"shop_id": 2, "reason": "x"}, headers=world.officer).status_code == 403
    assert client.post("/commodity-status", json=body | {"reason": "x"}, headers=world.ben).status_code == 403
    assert client.get("/commodity-status/99", headers=world.admin).status_code == 404


def test_requirements_upsert(client, world):
    set_requirement(client, world.officer, 1, 1, 60)
    r = set_requirement(client, world.officer, 1, 1, 70)
    assert r["quantity"] == 70
    reqs = client.get("/villages/1/requirements", headers=world.ben).json()
    assert reqs == [{"id": r["id"], "village_id": 1, "commodity_id": 1, "commodity_name": "Rice", "quantity": 70}]
    assert client.post("/villages/99/requirements", json={"commodity_id": 1, "quantity": 1},
                       headers=world.admin).status_code == 404


def test_readiness_states(client, world):
    no_reqs = client.get("/villages/1/readiness", headers=world.officer).json()
    assert no_reqs["is_ready"] is False and no_reqs["reason"] == "Village requirements are not configured"

    set_requirement(client, world.officer, 1, 1, 50)
    set_requirement(client, world.officer, 1, 2, 30)
    r = client.get("/villages/1/readiness", headers=world.officer).json()
    assert not r["is_ready"] and {c["commodity_id"] for c in r["missing_commodities"]} == {1, 2}

    add_stock(client, world.officer, 1, 50)
    add_stock(client, world.officer, 2, 10)
    r = client.get("/villages/1/readiness", headers=world.officer).json()
    assert r["insufficient_stock"] == [{"commodity_id": 2, "commodity_name": "Wheat", "required_quantity": 30,
                                        "available_quantity": 10}]
    add_stock(client, world.officer, 2, 20)
    assert client.get("/villages/1/readiness", headers=world.ben).json()["is_ready"] is True

    client.post("/commodity-status", json={"shop_id": 1, "commodity_id": 2, "status": "seasonally_suspended",
                                           "reason": "Off season"}, headers=world.officer)
    r = client.get("/villages/1/readiness", headers=world.officer).json()
    assert not r["is_ready"] and r["seasonally_suspended"][0]["commodity_id"] == 2

    client.post("/commodity-status", json={"shop_id": 1, "commodity_id": 2, "status": "out_of_stock",
                                           "reason": "Spoiled"}, headers=world.officer)
    r = client.get("/villages/1/readiness", headers=world.officer).json()
    assert r["missing_commodities"] == [{"commodity_id": 2, "commodity_name": "Wheat", "required_quantity": 30,
                                         "available_quantity": 0}]


def test_legacy_human_readable_status_does_not_block(client, world):
    set_requirement(client, world.officer, 1, 1, 10)
    add_stock(client, world.officer, 1, 10)
    # Old rows may hold values like 'Available'; they are not blocking (pre-existing rule).
    sql("ALTER TABLE commodity_status DROP CONSTRAINT ck_status_value")
    try:
        sql("INSERT INTO commodity_status (shop_id, commodity_id, status, reason) VALUES (1, 1, 'Available', 'legacy')")
        assert client.get("/villages/1/readiness", headers=world.officer).json()["is_ready"] is True
    finally:
        sql("DELETE FROM commodity_status")
        sql("ALTER TABLE commodity_status ADD CONSTRAINT ck_status_value "
            "CHECK (status IN ('active', 'seasonally_suspended', 'out_of_stock')) NOT VALID")


def test_next_village_policy(client, world):
    assert client.get("/shops/1/next-village", headers=world.officer).json() == {
        "message": "No village is currently ready for distribution"}
    for v in (1, 2):
        set_requirement(client, world.officer, v, 1, 10)
    add_stock(client, world.officer, 1, 100)

    # Both never served -> lowest id first (stable tie-breaker).
    assert client.get("/shops/1/next-village", headers=world.ben).json()["village_id"] == 1
    # Served village moves behind a never-served one.
    sql("UPDATE villages SET last_distribution_date = '2026-01-10' WHERE id = 1")
    assert client.get("/shops/1/next-village", headers=world.officer).json()["village_id"] == 2
    # Both served -> oldest date first.
    sql("UPDATE villages SET last_distribution_date = '2026-02-10' WHERE id = 2")
    nxt = client.get("/shops/1/next-village", headers=world.officer).json()
    assert nxt == {"village_id": 1, "village_name": "V1", "last_distribution_date": "2026-01-10"}
    # Equal dates -> lowest id.
    sql("UPDATE villages SET last_distribution_date = '2026-02-10' WHERE id = 1")
    assert client.get("/shops/1/next-village", headers=world.officer).json()["village_id"] == 1
    # Not-ready villages are skipped.
    set_requirement(client, world.officer, 1, 2, 5)
    assert client.get("/shops/1/next-village", headers=world.officer).json()["village_id"] == 2
    assert client.get("/shops/99/next-village", headers=world.admin).status_code == 404
