"""Populate the database with demo data for local development and demos.

    python seed_demo.py

Uses the same DATABASE_URL and ff_* routines as the API. Safe to re-run: anything
that already exists (matched by name/username) is left unchanged. Refuses to run
when APP_ENV=production. All demo users share the password printed at the end.
"""

import os
from datetime import date

from auth import hash_password
from db import call

PASSWORD = "Demo@1234"

SHOPS = [  # name, location, villages
    ("FPS Guindy", "Guindy, Chennai", ["Kotturpuram", "Saidapet", "Little Mount"]),
    ("FPS Tambaram", "Tambaram, Chengalpattu", ["Mudichur", "Perungalathur"]),
]
COMMODITIES = ["Rice", "Wheat", "Sugar", "Toor Dal", "Palm Oil"]
# Per-village requirement (kg/litre) and opening stock per shop.
REQUIREMENTS = {"Rice": 120, "Wheat": 40, "Sugar": 25, "Toor Dal": 15, "Palm Oil": 10}
STOCK = {"Rice": 500, "Wheat": 150, "Sugar": 90, "Toor Dal": 50, "Palm Oil": 60}
# Monthly totals for the last 6 complete months (oldest first), for forecasting.
HISTORY = {"Rice": [700, 740, 760, 810, 830, 880], "Wheat": [260, 250, 255, 240, 235, 230],
           "Sugar": [140, 150, 145, 155, 150, 160]}


def months_back(n: int) -> date:
    """10th of the month n months before the current month."""
    today = date.today()
    y, m = divmod(today.year * 12 + today.month - 1 - n, 12)
    return date(y, m + 1, 10)


def ensure_user(actor, username, role, shop_id=None, phone=None):
    if not call("ff_get_user_by_username", username, one=True):
        call("ff_create_user", actor, username, hash_password(PASSWORD), role, shop_id, phone)
    return call("ff_get_user_by_username", username, one=True)["id"]


def main():
    if os.getenv("APP_ENV") == "production":
        raise SystemExit("Refusing to seed demo data when APP_ENV=production")

    admin = ensure_user(None, "admin_demo", "admin")

    # Reuse existing commodities, matching names case-insensitively ("rice" == "Rice"),
    # the same rule ff_create_commodity enforces.
    existing = {c["name"].strip().lower(): c["id"] for c in call("ff_list_commodities", None)}
    commodities = {}
    for name in COMMODITIES:
        commodities[name] = existing.get(name.lower()) or \
            call("ff_create_commodity", admin, name, one=True)["id"]

    shops = {s["name"]: s for s in call("ff_list_shops", None)}
    for n, (shop_name, location, village_names) in enumerate(SHOPS, start=1):
        shop = shops.get(shop_name) or call("ff_create_shop", admin, shop_name, location, one=True)
        shop_id = shop["id"]

        officer = ensure_user(admin, f"officer{n}", "officer", shop_id)
        ensure_user(None, f"beneficiary{n}", "beneficiary", shop_id, f"+9198765432{n:02d}")

        existing = {v["name"]: v["id"] for v in call("ff_list_villages", shop_id, None)}
        for village_name in village_names:
            village_id = existing.get(village_name) or \
                call("ff_create_village", admin, village_name, shop_id, one=True)["id"]
            for commodity, qty in REQUIREMENTS.items():  # upsert: re-run safe
                call("ff_upsert_village_requirement", officer, village_id, commodities[commodity], qty)

        if not call("ff_list_stock", shop_id, None):  # only on first run
            for commodity, qty in STOCK.items():
                call("ff_add_stock", officer, shop_id, commodities[commodity], qty)
            if n == 2:  # one suspended commodity, so shop 2 shows "not ready" villages
                call("ff_set_commodity_status", officer, shop_id, commodities["Palm Oil"],
                     "seasonally_suspended", "Not supplied this season")

        if not call("ff_monthly_distribution", shop_id, None):  # only on first run
            for commodity, totals in HISTORY.items():
                for i, total in enumerate(totals):
                    call("ff_add_forecast_history", officer, shop_id, commodities[commodity],
                         total, months_back(len(totals) - i))

    print("Demo data ready.\n")
    print(f"All demo users have password: {PASSWORD}")
    print("  admin_demo                 admin")
    for n, (shop_name, *_rest) in enumerate(SHOPS, start=1):
        print(f"  officer{n} / beneficiary{n}   {shop_name}")
    print("\nShops:", ", ".join(f"{s['id']}={s['name']}" for s in call("ff_list_shops", None)))


if __name__ == "__main__":
    main()







    public | audit_log               | table | test
 public |  public | biometric_verifications | table | test
 | table | test
 public | commodities             | table | test
 public | commodity_status        | table | test
 public | complaints              | table | test
 public | distribution_history    | table | test
 public | migration_invalid_dates | table | test
 public | notifications           | table | test
 public | shops                   | table | test
 public | stock                   | table | test
 public | users                   | table | test
 public | village_requirements    | table | test
 public | villages                | table | test
(13 rows)