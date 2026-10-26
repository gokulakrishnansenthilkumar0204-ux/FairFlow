from datetime import date, datetime
from zoneinfo import ZoneInfo

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
    # Two steps ahead (skipping the running month): x = 5.
    assert _linear_regression_forecast([100, 200, 300], steps_ahead=2)[0] == pytest.approx(500.0)
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


def month_start(n_back: int):
    """First day of the month n_back months before the current month (APP_TIMEZONE)."""
    today = datetime.now(ZoneInfo("Asia/Kolkata")).date()
    y, m = divmod(today.year * 12 + today.month - 1 - n_back, 12)
    return date(y, m + 1, 1)


def ym(n_back: int) -> str:
    return month_start(n_back).strftime("%Y-%m")


def test_forecast_endpoint_with_known_data(client, world):
    insufficient = client.get("/forecast/1/1", headers=world.officer).json()
    assert insufficient["status"] == "insufficient_data" and insufficient["months_available"] == 0

    # Last three COMPLETE months: 100, 200, 300 (first one split over two records).
    for n_back, day, qty in ((3, 5, 60), (3, 20, 40), (2, 10, 200), (1, 10, 300)):
        d = month_start(n_back).replace(day=day).isoformat()
        assert add_history(client, world.officer, d, qty).status_code == 201
    add_stock(client, world.officer, 1, 150)

    f = client.get("/forecast/1/1", headers=world.admin).json()
    assert f["history"] == [{"month": ym(3), "quantity": 100.0, "partial": False},
                            {"month": ym(2), "quantity": 200.0, "partial": False},
                            {"month": ym(1), "quantity": 300.0, "partial": False},
                            {"month": ym(0), "quantity": 0.0, "partial": True}]  # running month: not trained on
    # y = 100x on complete months; next month is x = 5 (current month is x = 4).
    assert f["forecast_month"] == ym(-1) and f["months_used"] == 3
    assert (f["predicted_next_month_demand"], f["trend_slope"], f["intercept"]) == (500.0, 100.0, 0.0)
    assert (f["current_stock"], f["reorder_quantity"], f["recommendation"]) == (150.0, 350.0, "RESTOCK")

    shop = client.get("/forecast/1", headers=world.officer).json()
    assert shop["forecast_count"] == 1
    assert shop["forecasts"][0]["predicted_next_month_demand"] == 500.0
    assert "intercept" not in shop["forecasts"][0]


def test_missing_months_count_as_zero(client, world):
    # 3 months ago = 100, 2 months ago = nothing, last month = 300 -> 100, 0, 300.
    add_history(client, world.officer, month_start(3).replace(day=10).isoformat(), 100)
    add_history(client, world.officer, month_start(1).replace(day=10).isoformat(), 300)
    f = client.get("/forecast/1/1", headers=world.officer).json()
    assert [h["quantity"] for h in f["history"]] == [100.0, 0.0, 300.0, 0.0]
    expected = _linear_regression_forecast([100, 0, 300], steps_ahead=2)[0]
    assert f["predicted_next_month_demand"] == round(expected, 2)


def test_old_history_is_projected_to_the_real_next_month(client, world):
    # Data stopped 6 months ago: the months since then count as zero demand, and the
    # forecast is for next calendar month - not for the month after the last record.
    add_history(client, world.officer, month_start(8).replace(day=10).isoformat(), 500)
    add_history(client, world.officer, month_start(7).replace(day=10).isoformat(), 500)
    f = client.get("/forecast/1/1", headers=world.officer).json()
    assert f["history"][-1] == {"month": ym(0), "quantity": 0.0, "partial": True}
    assert f["forecast_month"] == ym(-1) and f["months_used"] == 8
    assert f["predicted_next_month_demand"] == 0.0  # clear downward trend, clipped at zero


def test_current_month_alone_is_not_enough(client, world):
    add_history(client, world.officer, month_start(0).isoformat(), 50)
    f = client.get("/forecast/1/1", headers=world.officer).json()
    assert f["status"] == "insufficient_data" and f["months_available"] == 0


def test_distribution_feeds_forecast_history(client, world):
    client.post("/villages/1/requirements", json={"commodity_id": 1, "quantity": 10}, headers=world.officer)
    add_stock(client, world.officer, 1, 100)
    assert client.post("/villages/mark-distributed", json={"village_id": 1}, headers=world.officer).status_code == 200
    rows = sql("SELECT shop_id, village_id, quantity FROM distribution_history")
    assert [(r[0], r[1], float(r[2])) for r in rows] == [(1, 1, 10.0)]
