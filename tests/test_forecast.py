import pytest

from conftest import add_stock, sql
from main import _linear_regression_forecast, _recommendation, _reorder_quantity


def test_regression_known_values():
    # y = 100x exactly -> slope 100, intercept 0, next (x=4) = 400
    assert _linear_regression_forecast([100, 200, 300]) == pytest.approx((400.0, 100.0, 0.0))
    # Flat series -> forecast equals the level.
    assert _linear_regression_forecast([50, 50]) == pytest.approx((50.0, 0.0, 50.0))
    # Steep decline is clipped at zero.
    forecast, slope, _ = _linear_regression_forecast([300, 100])
    assert forecast == 0.0 and slope == -200
    with pytest.raises(ValueError):
        _linear_regression_forecast([10])


def test_reorder_rule_and_recommendation():
    assert _reorder_quantity(400, 150) == 250
    assert _reorder_quantity(100, 150) == 0
    assert _recommendation(400, 150) == "RESTOCK"
    assert _recommendation(100, 150) == "SUFFICIENT_STOCK"
    assert _recommendation(100, 100) == "STOCK_MATCHES_FORECAST"


def add_history(client, headers, day, qty, commodity_id=1):
    return client.post("/forecast/history", json={"shop_id": 1, "commodity_id": commodity_id, "quantity": qty,
                                                  "distribution_date": day}, headers=headers)


def test_history_validation(client, world):
    assert add_history(client, world.officer, "2026-13-45", 5).status_code == 422
    assert add_history(client, world.officer, "2026-02-30", 5).status_code == 422
    assert add_history(client, world.officer, "2999-01-01", 5).status_code == 400  # future
    assert add_history(client, world.officer, "2026-01-15", 0).status_code == 422
    assert add_history(client, world.ben, "2026-01-15", 5).status_code == 403
    r = client.post("/forecast/history", json={"shop_id": 2, "commodity_id": 1, "quantity": 5,
                                               "distribution_date": "2026-01-15"}, headers=world.officer)
    assert r.status_code == 403
    r = add_history(client, world.officer, "2026-01-15", 5)
    assert r.status_code == 201 and r.json()["distribution_date"] == "2026-01-15"


def test_forecast_endpoint_with_known_data(client, world):
    insufficient = client.get("/forecast/1/1", headers=world.officer).json()
    assert insufficient["status"] == "insufficient_data" and insufficient["months_available"] == 0

    for day, qty in (("2026-01-05", 60), ("2026-01-20", 40), ("2026-02-10", 200), ("2026-03-10", 300)):
        assert add_history(client, world.officer, day, qty).status_code == 201
    add_stock(client, world.officer, 1, 150)

    f = client.get("/forecast/1/1", headers=world.admin).json()
    assert f["history"] == [{"month": "2026-01", "quantity": 100.0}, {"month": "2026-02", "quantity": 200.0},
                            {"month": "2026-03", "quantity": 300.0}]
    assert (f["predicted_next_month_demand"], f["trend_slope"], f["intercept"]) == (400.0, 100.0, 0.0)
    assert (f["current_stock"], f["reorder_quantity"], f["recommendation"]) == (150.0, 250.0, "RESTOCK")

    shop = client.get("/forecast/1", headers=world.officer).json()
    assert shop["forecast_count"] == 1
    assert shop["forecasts"][0]["predicted_next_month_demand"] == 400.0
    assert "intercept" not in shop["forecasts"][0]


def test_missing_months_count_as_zero(client, world):
    # Jan=100, Mar=300, nothing in Feb. Calendar months: 100, 0, 300.
    add_history(client, world.officer, "2026-01-10", 100)
    add_history(client, world.officer, "2026-03-10", 300)
    f = client.get("/forecast/1/1", headers=world.officer).json()
    assert [h["quantity"] for h in f["history"]] == [100.0, 0.0, 300.0]
    expected = _linear_regression_forecast([100, 0, 300])[0]
    assert f["predicted_next_month_demand"] == round(expected, 2)


def test_distribution_feeds_forecast_history(client, world):
    client.post("/villages/1/requirements", json={"commodity_id": 1, "quantity": 10}, headers=world.officer)
    add_stock(client, world.officer, 1, 100)
    assert client.post("/villages/mark-distributed", json={"village_id": 1}, headers=world.officer).status_code == 200
    rows = sql("SELECT shop_id, village_id, quantity FROM distribution_history")
    assert [(r[0], r[1], float(r[2])) for r in rows] == [(1, 1, 10.0)]
