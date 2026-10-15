-- FairFlow schema. Safe to run repeatedly (setup_db.py runs it in one transaction).
-- It creates missing tables and migrates tables created by the old SQLAlchemy
-- models in place. It never drops or truncates a table.

-- Setup-only helpers: pg_temp functions vanish when the setup session ends.
CREATE FUNCTION pg_temp.col_type(t TEXT, c TEXT) RETURNS TEXT LANGUAGE sql AS $$
    SELECT data_type FROM information_schema.columns
    WHERE table_schema = current_schema() AND table_name = t AND column_name = c
$$;

CREATE FUNCTION pg_temp.add_constraint(t TEXT, c_name TEXT, ddl TEXT) RETURNS VOID LANGUAGE plpgsql AS $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                   WHERE conname = c_name AND conrelid = to_regclass(format('%I.%I', current_schema(), t))) THEN
        EXECUTE format('ALTER TABLE %I ADD CONSTRAINT %I %s', t, c_name, ddl);
    END IF;
END $$;

CREATE FUNCTION pg_temp.set_not_null(t TEXT, c TEXT) RETURNS VOID LANGUAGE plpgsql AS $$
DECLARE has_nulls BOOLEAN;
BEGIN
    EXECUTE format('SELECT EXISTS (SELECT 1 FROM %I WHERE %I IS NULL)', t, c) INTO has_nulls;
    IF has_nulls THEN
        RAISE WARNING '%.% contains NULLs; NOT NULL not applied. Fix the data and re-run setup.', t, c;
    ELSE
        EXECUTE format('ALTER TABLE %I ALTER COLUMN %I SET NOT NULL', t, c);
    END IF;
END $$;

-- Parses the date formats old rows may contain; NULL if blank or not a real date.
-- Accepts YYYY-MM-DD (optionally followed by a time), YYYY/MM/DD, and DD/MM/YYYY or DD-MM-YYYY.
CREATE FUNCTION pg_temp.parse_date(raw TEXT) RETURNS DATE LANGUAGE plpgsql AS $$
DECLARE s TEXT := btrim(COALESCE(raw, ''));
BEGIN
    IF s ~ '^\d{4}[-/]\d{1,2}[-/]\d{1,2}([ T].*)?$' THEN
        s := split_part(split_part(s, ' ', 1), 'T', 1);
        RETURN replace(s, '/', '-')::DATE;  -- strict: 2026-02-30 raises -> NULL
    ELSIF s ~ '^\d{1,2}[-/]\d{1,2}[-/]\d{4}$' THEN
        RETURN make_date(split_part(replace(s, '/', '-'), '-', 3)::INT,
                         split_part(replace(s, '/', '-'), '-', 2)::INT,
                         split_part(replace(s, '/', '-'), '-', 1)::INT);
    END IF;
    RETURN NULL;
EXCEPTION WHEN others THEN
    RETURN NULL;
END $$;

-- ---------- Tables ----------

CREATE TABLE IF NOT EXISTS shops (
    id       SERIAL PRIMARY KEY,
    name     TEXT NOT NULL,
    location TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS villages (
    id                     SERIAL PRIMARY KEY,
    name                   TEXT NOT NULL,
    shop_id                INTEGER NOT NULL REFERENCES shops(id),
    last_distribution_date DATE
);

CREATE TABLE IF NOT EXISTS commodities (
    id   SERIAL PRIMARY KEY,
    name TEXT NOT NULL UNIQUE
);

CREATE TABLE IF NOT EXISTS users (
    id              SERIAL PRIMARY KEY,
    username        TEXT NOT NULL UNIQUE,
    hashed_password TEXT NOT NULL,
    role            TEXT NOT NULL,
    shop_id         INTEGER REFERENCES shops(id),
    phone           TEXT
);

CREATE TABLE IF NOT EXISTS stock (
    id           SERIAL PRIMARY KEY,
    shop_id      INTEGER NOT NULL REFERENCES shops(id),
    commodity_id INTEGER NOT NULL REFERENCES commodities(id),
    quantity     NUMERIC(12, 3) NOT NULL
);

CREATE TABLE IF NOT EXISTS commodity_status (
    id           SERIAL PRIMARY KEY,
    shop_id      INTEGER NOT NULL REFERENCES shops(id),
    commodity_id INTEGER NOT NULL REFERENCES commodities(id),
    status       TEXT NOT NULL DEFAULT 'active',
    reason       TEXT
);

CREATE TABLE IF NOT EXISTS village_requirements (
    id           SERIAL PRIMARY KEY,
    village_id   INTEGER NOT NULL REFERENCES villages(id) ON DELETE CASCADE,
    commodity_id INTEGER NOT NULL REFERENCES commodities(id),
    quantity     NUMERIC(12, 3) NOT NULL
);

CREATE TABLE IF NOT EXISTS distribution_history (
    id                SERIAL PRIMARY KEY,
    shop_id           INTEGER NOT NULL REFERENCES shops(id),
    village_id        INTEGER REFERENCES villages(id),
    commodity_id      INTEGER NOT NULL REFERENCES commodities(id),
    quantity          NUMERIC(12, 3) NOT NULL,
    distribution_date DATE NOT NULL
);

-- Records only the outcome of a provider check. No fingerprint image,
-- template or capture payload is ever stored.
CREATE TABLE IF NOT EXISTS biometric_verifications (
    id                 SERIAL PRIMARY KEY,
    beneficiary_id     INTEGER NOT NULL REFERENCES users(id),
    shop_id            INTEGER NOT NULL REFERENCES shops(id),
    officer_id         INTEGER NOT NULL REFERENCES users(id),
    provider           TEXT NOT NULL,
    provider_reference TEXT NOT NULL,
    verified           BOOLEAN NOT NULL,
    verified_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS complaints (
    id              SERIAL PRIMARY KEY,
    beneficiary_id  INTEGER NOT NULL REFERENCES users(id),
    shop_id         INTEGER NOT NULL REFERENCES shops(id),
    category        TEXT NOT NULL,
    description     TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'open',
    resolution_note TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS notifications (
    id                 SERIAL PRIMARY KEY,
    user_id            INTEGER REFERENCES users(id),
    event              TEXT NOT NULL,
    message            TEXT NOT NULL,
    status             TEXT NOT NULL,
    provider_reference TEXT,
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS audit_log (
    id         BIGSERIAL PRIMARY KEY,
    actor_id   INTEGER REFERENCES users(id) ON DELETE SET NULL,
    action     TEXT NOT NULL,
    entity     TEXT NOT NULL,
    entity_id  INTEGER,
    details    JSONB NOT NULL DEFAULT '{}',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ---------- Migrations for tables created by the old SQLAlchemy models ----------

ALTER TABLE users ADD COLUMN IF NOT EXISTS phone TEXT;

-- VARCHAR -> TEXT (binary compatible, no data change) so routine return types match exactly.
DO $$
DECLARE r RECORD;
BEGIN
    FOR r IN SELECT table_name, column_name FROM information_schema.columns
             WHERE table_schema = current_schema() AND data_type = 'character varying'
               AND table_name IN ('shops', 'villages', 'commodities', 'users', 'commodity_status',
                                  'distribution_history')
    LOOP
        EXECUTE format('ALTER TABLE %I ALTER COLUMN %I TYPE TEXT', r.table_name, r.column_name);
    END LOOP;
END $$;

-- FLOAT -> NUMERIC(12,3): exact quantities, no binary rounding drift.
DO $$
DECLARE t TEXT;
BEGIN
    FOREACH t IN ARRAY ARRAY['stock', 'village_requirements', 'distribution_history'] LOOP
        IF pg_temp.col_type(t, 'quantity') <> 'numeric' THEN
            EXECUTE format('ALTER TABLE %I ALTER COLUMN quantity TYPE NUMERIC(12, 3)', t);
        END IF;
    END LOOP;
END $$;

-- Text dates -> DATE.
-- villages.last_distribution_date: values that cannot be parsed become NULL ("never
--   served", so the village is scheduled first); the original text is kept in
--   migration_invalid_dates so nothing is lost.
-- distribution_history.distribution_date: every row must parse (it feeds forecasting),
--   otherwise setup stops, lists the bad rows and changes nothing.
DO $$
DECLARE bad TEXT;
BEGIN
    IF pg_temp.col_type('distribution_history', 'distribution_date') <> 'date' THEN
        SELECT string_agg(format('id=%s value=%L', id, distribution_date), ', ')
        INTO bad FROM (SELECT id, distribution_date FROM distribution_history
                       WHERE pg_temp.parse_date(distribution_date) IS NULL ORDER BY id LIMIT 10) b;
        IF bad IS NOT NULL THEN
            RAISE EXCEPTION 'distribution_history has invalid distribution_date values (%). Fix them as YYYY-MM-DD and re-run setup', bad;
        END IF;
        ALTER TABLE distribution_history ALTER COLUMN distribution_date TYPE DATE
            USING pg_temp.parse_date(distribution_date);
    END IF;

    IF pg_temp.col_type('villages', 'last_distribution_date') <> 'date' THEN
        CREATE TABLE IF NOT EXISTS migration_invalid_dates (
            table_name     TEXT NOT NULL,
            row_id         INTEGER NOT NULL,
            column_name    TEXT NOT NULL,
            original_value TEXT,
            migrated_at    TIMESTAMPTZ NOT NULL DEFAULT now()
        );
        INSERT INTO migration_invalid_dates (table_name, row_id, column_name, original_value)
        SELECT 'villages', id, 'last_distribution_date', last_distribution_date FROM villages
        WHERE btrim(COALESCE(last_distribution_date, '')) <> ''
          AND pg_temp.parse_date(last_distribution_date) IS NULL;
        IF FOUND THEN
            RAISE WARNING 'Some villages.last_distribution_date values could not be read and were set to NULL; originals saved in migration_invalid_dates';
        END IF;
        ALTER TABLE villages ALTER COLUMN last_distribution_date TYPE DATE
            USING pg_temp.parse_date(last_distribution_date);
    END IF;
END $$;

-- Old models left these nullable.
SELECT pg_temp.set_not_null(t, c) FROM (VALUES
    ('shops', 'name'), ('shops', 'location'), ('villages', 'name'), ('villages', 'shop_id'),
    ('commodities', 'name'), ('users', 'username'), ('users', 'hashed_password'), ('users', 'role')
) AS v(t, c);

-- Merge duplicate stock rows (formerly migrate_stock_management.py) before adding the
-- unique constraint. Quantities are summed, so no stock is lost.
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'uq_stock_shop_commodity'
                   AND conrelid = to_regclass(format('%I.stock', current_schema()))) THEN
        WITH d AS (
            SELECT shop_id, commodity_id, MIN(id) AS keep_id, SUM(quantity) AS total
            FROM stock GROUP BY shop_id, commodity_id HAVING COUNT(*) > 1
        ), merged AS (
            UPDATE stock s SET quantity = d.total FROM d WHERE s.id = d.keep_id
        )
        DELETE FROM stock s USING d
        WHERE s.shop_id = d.shop_id AND s.commodity_id = d.commodity_id AND s.id <> d.keep_id;
    END IF;
END $$;

-- ---------- Constraints (idempotent) ----------
-- CHECKs on pre-existing tables are added NOT VALID: enforced for every new or changed
-- row, without rejecting legacy rows (e.g. old human-readable status values).

SELECT pg_temp.add_constraint(t, n, d) FROM (VALUES
    ('stock', 'uq_stock_shop_commodity', 'UNIQUE (shop_id, commodity_id)'),
    ('stock', 'ck_stock_quantity_positive', 'CHECK (quantity > 0)'),
    ('commodity_status', 'uq_status_shop_commodity', 'UNIQUE (shop_id, commodity_id)'),
    ('commodity_status', 'ck_status_value',
        'CHECK (status IN (''active'', ''seasonally_suspended'', ''out_of_stock'')) NOT VALID'),
    ('commodity_status', 'ck_status_reason',
        'CHECK (status = ''active'' OR btrim(COALESCE(reason, '''')) <> '''') NOT VALID'),
    ('village_requirements', 'uq_village_requirement_village_commodity', 'UNIQUE (village_id, commodity_id)'),
    ('village_requirements', 'ck_village_requirement_quantity_positive', 'CHECK (quantity > 0)'),
    ('distribution_history', 'ck_history_quantity_positive', 'CHECK (quantity > 0) NOT VALID'),
    ('users', 'ck_users_role', 'CHECK (role IN (''admin'', ''officer'', ''beneficiary'')) NOT VALID'),
    ('users', 'ck_users_role_shop',
        'CHECK ((role = ''officer'' AND shop_id IS NOT NULL) OR (role = ''admin'' AND shop_id IS NULL) OR role = ''beneficiary'') NOT VALID'),
    ('complaints', 'ck_complaint_status', 'CHECK (status IN (''open'', ''in_progress'', ''resolved'', ''rejected''))'),
    ('complaints', 'ck_complaint_category',
        'CHECK (category IN (''quality'', ''quantity'', ''availability'', ''behaviour'', ''other''))'),
    ('notifications', 'ck_notification_status',
        'CHECK (status IN (''simulated'', ''accepted'', ''failed'', ''skipped_no_phone'', ''disabled''))')
) AS v(t, n, d);

CREATE INDEX IF NOT EXISTS ix_history_shop_commodity_date
    ON distribution_history (shop_id, commodity_id, distribution_date);
CREATE INDEX IF NOT EXISTS ix_complaints_shop ON complaints (shop_id);
CREATE INDEX IF NOT EXISTS ix_notifications_user ON notifications (user_id);
CREATE INDEX IF NOT EXISTS ix_audit_log_created ON audit_log (created_at);
