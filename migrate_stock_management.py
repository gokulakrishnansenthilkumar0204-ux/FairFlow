"""One-time Stock Management database migration.

Run with the backend stopped:
    python migrate_stock_management.py

Back up the database first. Duplicate stock rows are merged by summing
their quantities. Duplicate commodity-status rows are rejected so that
the application never guesses which status should survive.
"""

from sqlalchemy import text
from database import engine

with engine.begin() as conn:
    duplicates = conn.execute(text("""
        SELECT shop_id, commodity_id
        FROM stock
        GROUP BY shop_id, commodity_id
        HAVING COUNT(*) > 1
    """)).mappings().all()

    for row in duplicates:
        shop_id, commodity_id = row["shop_id"], row["commodity_id"]

        total = conn.execute(text("""
            SELECT COALESCE(SUM(quantity), 0)
            FROM stock
            WHERE shop_id = :shop_id AND commodity_id = :commodity_id
        """), {"shop_id": shop_id, "commodity_id": commodity_id}).scalar_one()

        keep_id = conn.execute(text("""
            SELECT id FROM stock
            WHERE shop_id = :shop_id AND commodity_id = :commodity_id
            ORDER BY id LIMIT 1
        """), {"shop_id": shop_id, "commodity_id": commodity_id}).scalar_one()

        conn.execute(text("""
            UPDATE stock SET quantity = :total WHERE id = :keep_id
        """), {"total": total, "keep_id": keep_id})

        conn.execute(text("""
            DELETE FROM stock
            WHERE shop_id = :shop_id AND commodity_id = :commodity_id
              AND id <> :keep_id
        """), {"shop_id": shop_id, "commodity_id": commodity_id, "keep_id": keep_id})

    status_duplicates = conn.execute(text("""
        SELECT shop_id, commodity_id, COUNT(*) AS n
        FROM commodity_status
        GROUP BY shop_id, commodity_id
        HAVING COUNT(*) > 1
    """)).mappings().all()

    if status_duplicates:
        raise RuntimeError(
            "Duplicate commodity_status rows exist. Resolve them manually "
            "before adding uq_status_shop_commodity."
        )

    # Add constraints only if they are not already present.
    existing = {
        row[0] for row in conn.execute(text("""
            SELECT constraint_name
            FROM information_schema.table_constraints
            WHERE table_name IN ('stock', 'commodity_status')
        """))
    }

    if "uq_stock_shop_commodity" not in existing:
        conn.execute(text("""
            ALTER TABLE stock
            ADD CONSTRAINT uq_stock_shop_commodity UNIQUE (shop_id, commodity_id)
        """))

    if "ck_stock_quantity_positive" not in existing:
        conn.execute(text("""
            ALTER TABLE stock
            ADD CONSTRAINT ck_stock_quantity_positive CHECK (quantity > 0)
        """))

    if "uq_status_shop_commodity" not in existing:
        conn.execute(text("""
            ALTER TABLE commodity_status
            ADD CONSTRAINT uq_status_shop_commodity UNIQUE (shop_id, commodity_id)
        """))

print("Stock Management migration completed successfully.")
