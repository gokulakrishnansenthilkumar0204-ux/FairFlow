-- FairFlow database API: every runtime database operation used by the FastAPI
-- app is one of these functions. setup_db.py installs this file in one transaction
-- after schema.sql.
--
-- Error convention (mapped to HTTP status by db.py):
--   FF400 bad request, FF403 forbidden, FF404 not found, FF409 conflict.
-- Authorization decisions (who may call what) are made in FastAPI; these routines
-- enforce data rules, integrity and atomicity, and write the audit log.

-- Replace every ff_* routine (including the old procedures from procedures.sql)
-- so the installed set always matches this file exactly. Only routines are
-- dropped here; no table or data is touched.
DO $$
DECLARE r RECORD;
BEGIN
    FOR r IN SELECT p.oid::regprocedure AS sig, p.prokind
             FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
             WHERE n.nspname = current_schema() AND p.proname LIKE 'ff\_%'
    LOOP
        EXECUTE format('DROP %s %s',
                       CASE r.prokind WHEN 'p' THEN 'PROCEDURE' ELSE 'FUNCTION' END, r.sig);
    END LOOP;
END $$;

-- ---------- Internal ----------

CREATE FUNCTION ff_audit(p_actor INT, p_action TEXT, p_entity TEXT, p_entity_id INT,
                         p_details JSONB DEFAULT '{}')
RETURNS VOID LANGUAGE sql AS $$
    INSERT INTO audit_log (actor_id, action, entity, entity_id, details)
    VALUES (p_actor, p_action, p_entity, p_entity_id, COALESCE(p_details, '{}'));
$$;

-- One readiness rule, shared by readiness, scheduling and distribution.
-- state: ok | suspended | missing | insufficient
CREATE FUNCTION ff_village_requirement_check(p_village_id INT)
RETURNS TABLE (commodity_id INT, commodity_name TEXT, required_quantity NUMERIC,
               available_quantity NUMERIC, status TEXT, state TEXT)
LANGUAGE sql STABLE AS $$
    SELECT vr.commodity_id, c.name, vr.quantity, COALESCE(s.quantity, 0),
           COALESCE(lower(btrim(cs.status)), 'active'),
           CASE
               WHEN lower(btrim(cs.status)) = 'seasonally_suspended' THEN 'suspended'
               WHEN lower(btrim(cs.status)) = 'out_of_stock' OR s.id IS NULL THEN 'missing'
               WHEN s.quantity < vr.quantity THEN 'insufficient'
               ELSE 'ok'
           END
    FROM village_requirements vr
    JOIN villages v ON v.id = vr.village_id
    JOIN commodities c ON c.id = vr.commodity_id
    LEFT JOIN stock s ON s.shop_id = v.shop_id AND s.commodity_id = vr.commodity_id
    LEFT JOIN commodity_status cs ON cs.shop_id = v.shop_id AND cs.commodity_id = vr.commodity_id
    WHERE vr.village_id = p_village_id
    ORDER BY vr.commodity_id;
$$;

-- ---------- Users ----------

-- Case-insensitive; an exact-case match wins if legacy rows differ only in case.
CREATE FUNCTION ff_get_user_by_username(p_username TEXT)
RETURNS TABLE (id INT, username TEXT, hashed_password TEXT, role TEXT, shop_id INT,
               is_active BOOLEAN, approved BOOLEAN, token_version INT)
LANGUAGE sql STABLE AS $$
    SELECT id, username, hashed_password, role, shop_id, is_active, approved, token_version
    FROM users WHERE lower(username) = lower(btrim(p_username))
    ORDER BY (username = btrim(p_username)) DESC, id
    LIMIT 1;
$$;

CREATE FUNCTION ff_get_beneficiary(p_id INT)
RETURNS TABLE (id INT, username TEXT, shop_id INT)
LANGUAGE sql STABLE AS $$
    SELECT id, username, shop_id FROM users WHERE id = p_id AND role = 'beneficiary';
$$;

-- p_approved = FALSE for self-registered beneficiaries (their shop officer approves them).
CREATE FUNCTION ff_create_user(p_actor INT, p_username TEXT, p_hashed_password TEXT,
                               p_role TEXT, p_shop_id INT, p_phone TEXT, p_approved BOOLEAN DEFAULT TRUE)
RETURNS TABLE (id INT, username TEXT, role TEXT, shop_id INT)
LANGUAGE plpgsql AS $$
#variable_conflict use_column
DECLARE v_id INT;
BEGIN
    p_username := btrim(p_username);
    IF p_role NOT IN ('admin', 'officer', 'beneficiary') THEN
        RAISE EXCEPTION 'Invalid user role' USING ERRCODE = 'FF400';
    END IF;
    IF COALESCE(p_username, '') = '' THEN
        RAISE EXCEPTION 'Username is required' USING ERRCODE = 'FF400';
    END IF;
    -- Serialise concurrent signups for the same name ("Meena" vs "meena").
    PERFORM pg_advisory_xact_lock(hashtext('ff_user:' || lower(p_username)));
    IF EXISTS (SELECT 1 FROM users u WHERE lower(u.username) = lower(p_username)) THEN
        RAISE EXCEPTION 'Username already taken' USING ERRCODE = 'FF400';
    END IF;
    IF p_role = 'officer' AND p_shop_id IS NULL THEN
        RAISE EXCEPTION 'Officer must be assigned to a shop' USING ERRCODE = 'FF400';
    END IF;
    IF p_role = 'admin' AND p_shop_id IS NOT NULL THEN
        RAISE EXCEPTION 'Admin should not be assigned to a shop' USING ERRCODE = 'FF400';
    END IF;
    IF p_shop_id IS NOT NULL AND NOT EXISTS (SELECT 1 FROM shops s WHERE s.id = p_shop_id) THEN
        RAISE EXCEPTION 'Shop not found' USING ERRCODE = 'FF404';
    END IF;

    INSERT INTO users (username, hashed_password, role, shop_id, phone, approved)
    VALUES (p_username, p_hashed_password, p_role, p_shop_id, p_phone,
            CASE WHEN p_role = 'beneficiary' THEN COALESCE(p_approved, TRUE) ELSE TRUE END)
    RETURNING users.id INTO v_id;
    PERFORM ff_audit(COALESCE(p_actor, v_id), 'user.created', 'user', v_id,
                     jsonb_build_object('role', p_role, 'shop_id', p_shop_id));
    RETURN QUERY SELECT u.id, u.username, u.role, u.shop_id FROM users u WHERE u.id = v_id;
END $$;

-- ---------- Shops, villages, commodities ----------

CREATE FUNCTION ff_create_shop(p_actor INT, p_name TEXT, p_location TEXT)
RETURNS SETOF shops LANGUAGE plpgsql AS $$
DECLARE r shops;
BEGIN
    INSERT INTO shops (name, location) VALUES (p_name, p_location) RETURNING * INTO r;
    PERFORM ff_audit(p_actor, 'shop.created', 'shop', r.id, jsonb_build_object('name', p_name));
    RETURN NEXT r;
END $$;

-- p_shop_id NULL = all shops (admin). 0 matches nothing (user without a shop).
CREATE FUNCTION ff_list_shops(p_shop_id INT)
RETURNS SETOF shops LANGUAGE sql STABLE AS $$
    SELECT * FROM shops WHERE p_shop_id IS NULL OR id = p_shop_id ORDER BY id;
$$;

CREATE FUNCTION ff_create_village(p_actor INT, p_name TEXT, p_shop_id INT)
RETURNS SETOF villages LANGUAGE plpgsql AS $$
DECLARE r villages;
BEGIN
    IF NOT EXISTS (SELECT 1 FROM shops WHERE id = p_shop_id) THEN
        RAISE EXCEPTION 'Shop not found' USING ERRCODE = 'FF404';
    END IF;
    INSERT INTO villages (name, shop_id) VALUES (p_name, p_shop_id) RETURNING * INTO r;
    PERFORM ff_audit(p_actor, 'village.created', 'village', r.id, jsonb_build_object('shop_id', p_shop_id));
    RETURN NEXT r;
END $$;

CREATE FUNCTION ff_list_villages(p_shop_id INT, p_village_id INT)
RETURNS SETOF villages LANGUAGE sql STABLE AS $$
    SELECT * FROM villages
    WHERE (p_shop_id IS NULL OR shop_id = p_shop_id)
      AND (p_village_id IS NULL OR id = p_village_id)
    ORDER BY id;
$$;

CREATE FUNCTION ff_create_commodity(p_actor INT, p_name TEXT)
RETURNS SETOF commodities LANGUAGE plpgsql AS $$
DECLARE r commodities;
BEGIN
    IF EXISTS (SELECT 1 FROM commodities WHERE lower(name) = lower(p_name)) THEN
        RAISE EXCEPTION 'Commodity already exists' USING ERRCODE = 'FF409';
    END IF;
    INSERT INTO commodities (name) VALUES (p_name) RETURNING * INTO r;
    PERFORM ff_audit(p_actor, 'commodity.created', 'commodity', r.id, jsonb_build_object('name', p_name));
    RETURN NEXT r;
END $$;

CREATE FUNCTION ff_list_commodities(p_commodity_id INT)
RETURNS SETOF commodities LANGUAGE sql STABLE AS $$
    SELECT * FROM commodities WHERE p_commodity_id IS NULL OR id = p_commodity_id ORDER BY id;
$$;

-- ---------- Stock ----------

CREATE FUNCTION ff_add_stock(p_actor INT, p_shop_id INT, p_commodity_id INT, p_quantity NUMERIC)
RETURNS SETOF stock LANGUAGE plpgsql AS $$
DECLARE r stock;
BEGIN
    IF p_quantity IS NULL OR p_quantity <= 0 THEN
        RAISE EXCEPTION 'Quantity must be greater than zero' USING ERRCODE = 'FF400';
    END IF;
    IF NOT EXISTS (SELECT 1 FROM shops WHERE id = p_shop_id) THEN
        RAISE EXCEPTION 'Shop not found' USING ERRCODE = 'FF404';
    END IF;
    IF NOT EXISTS (SELECT 1 FROM commodities WHERE id = p_commodity_id) THEN
        RAISE EXCEPTION 'Commodity not found' USING ERRCODE = 'FF404';
    END IF;
    INSERT INTO stock (shop_id, commodity_id, quantity)
    VALUES (p_shop_id, p_commodity_id, p_quantity)
    ON CONFLICT (shop_id, commodity_id) DO UPDATE SET quantity = stock.quantity + EXCLUDED.quantity
    RETURNING * INTO r;
    PERFORM ff_audit(p_actor, 'stock.added', 'stock', r.id,
                     jsonb_build_object('shop_id', p_shop_id, 'commodity_id', p_commodity_id,
                                        'added', p_quantity, 'new_quantity', r.quantity));
    RETURN NEXT r;
END $$;

CREATE FUNCTION ff_list_stock(p_shop_id INT, p_stock_id INT)
RETURNS SETOF stock LANGUAGE sql STABLE AS $$
    SELECT * FROM stock
    WHERE (p_shop_id IS NULL OR shop_id = p_shop_id)
      AND (p_stock_id IS NULL OR id = p_stock_id)
    ORDER BY id;
$$;

CREATE FUNCTION ff_update_stock(p_actor INT, p_stock_id INT, p_quantity NUMERIC)
RETURNS SETOF stock LANGUAGE plpgsql AS $$
DECLARE r stock;
BEGIN
    IF p_quantity IS NULL OR p_quantity <= 0 THEN
        RAISE EXCEPTION 'Quantity must be greater than zero' USING ERRCODE = 'FF400';
    END IF;
    UPDATE stock SET quantity = p_quantity WHERE id = p_stock_id RETURNING * INTO r;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'Stock item not found' USING ERRCODE = 'FF404';
    END IF;
    PERFORM ff_audit(p_actor, 'stock.updated', 'stock', r.id, jsonb_build_object('new_quantity', p_quantity));
    RETURN NEXT r;
END $$;

CREATE FUNCTION ff_delete_stock(p_actor INT, p_stock_id INT)
RETURNS SETOF stock LANGUAGE plpgsql AS $$
DECLARE r stock;
BEGIN
    DELETE FROM stock WHERE id = p_stock_id RETURNING * INTO r;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'Stock item not found' USING ERRCODE = 'FF404';
    END IF;
    PERFORM ff_audit(p_actor, 'stock.deleted', 'stock', r.id,
                     jsonb_build_object('shop_id', r.shop_id, 'commodity_id', r.commodity_id,
                                        'quantity', r.quantity));
    RETURN NEXT r;
END $$;

-- ---------- Commodity status ----------

CREATE FUNCTION ff_set_commodity_status(p_actor INT, p_shop_id INT, p_commodity_id INT,
                                        p_status TEXT, p_reason TEXT)
RETURNS SETOF commodity_status LANGUAGE plpgsql AS $$
DECLARE r commodity_status;
BEGIN
    IF p_status NOT IN ('active', 'seasonally_suspended', 'out_of_stock') THEN
        RAISE EXCEPTION 'Invalid commodity status' USING ERRCODE = 'FF400';
    END IF;
    IF p_status <> 'active' AND COALESCE(btrim(p_reason), '') = '' THEN
        RAISE EXCEPTION 'Reason is required when status is not active' USING ERRCODE = 'FF400';
    END IF;
    IF NOT EXISTS (SELECT 1 FROM shops WHERE id = p_shop_id) THEN
        RAISE EXCEPTION 'Shop not found' USING ERRCODE = 'FF404';
    END IF;
    IF NOT EXISTS (SELECT 1 FROM commodities WHERE id = p_commodity_id) THEN
        RAISE EXCEPTION 'Commodity not found' USING ERRCODE = 'FF404';
    END IF;
    INSERT INTO commodity_status (shop_id, commodity_id, status, reason)
    VALUES (p_shop_id, p_commodity_id, p_status, NULLIF(btrim(p_reason), ''))
    ON CONFLICT (shop_id, commodity_id)
    DO UPDATE SET status = EXCLUDED.status, reason = EXCLUDED.reason
    RETURNING * INTO r;
    PERFORM ff_audit(p_actor, 'commodity_status.set', 'commodity_status', r.id,
                     jsonb_build_object('shop_id', p_shop_id, 'commodity_id', p_commodity_id,
                                        'status', p_status));
    RETURN NEXT r;
END $$;

CREATE FUNCTION ff_list_commodity_status(p_shop_id INT)
RETURNS SETOF commodity_status LANGUAGE sql STABLE AS $$
    SELECT * FROM commodity_status WHERE shop_id = p_shop_id ORDER BY commodity_id;
$$;

-- ---------- Village requirements, readiness, scheduling ----------

CREATE FUNCTION ff_upsert_village_requirement(p_actor INT, p_village_id INT,
                                              p_commodity_id INT, p_quantity NUMERIC)
RETURNS SETOF village_requirements LANGUAGE plpgsql AS $$
DECLARE r village_requirements;
BEGIN
    IF p_quantity IS NULL OR p_quantity <= 0 THEN
        RAISE EXCEPTION 'Requirement quantity must be greater than zero' USING ERRCODE = 'FF400';
    END IF;
    IF NOT EXISTS (SELECT 1 FROM villages WHERE id = p_village_id) THEN
        RAISE EXCEPTION 'Village not found' USING ERRCODE = 'FF404';
    END IF;
    IF NOT EXISTS (SELECT 1 FROM commodities WHERE id = p_commodity_id) THEN
        RAISE EXCEPTION 'Commodity not found' USING ERRCODE = 'FF404';
    END IF;
    INSERT INTO village_requirements (village_id, commodity_id, quantity)
    VALUES (p_village_id, p_commodity_id, p_quantity)
    ON CONFLICT (village_id, commodity_id) DO UPDATE SET quantity = EXCLUDED.quantity
    RETURNING * INTO r;
    PERFORM ff_audit(p_actor, 'village_requirement.set', 'village', p_village_id,
                     jsonb_build_object('commodity_id', p_commodity_id, 'quantity', p_quantity));
    RETURN NEXT r;
END $$;

CREATE FUNCTION ff_list_village_requirements(p_village_id INT)
RETURNS TABLE (id INT, village_id INT, commodity_id INT, commodity_name TEXT, quantity NUMERIC)
LANGUAGE sql STABLE AS $$
    SELECT vr.id, vr.village_id, vr.commodity_id, c.name, vr.quantity
    FROM village_requirements vr JOIN commodities c ON c.id = vr.commodity_id
    WHERE vr.village_id = p_village_id
    ORDER BY vr.commodity_id;
$$;

CREATE FUNCTION ff_delete_village_requirement(p_actor INT, p_village_id INT, p_commodity_id INT)
RETURNS SETOF village_requirements LANGUAGE plpgsql AS $$
DECLARE r village_requirements;
BEGIN
    DELETE FROM village_requirements
    WHERE village_id = p_village_id AND commodity_id = p_commodity_id
    RETURNING * INTO r;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'This village has no requirement for that commodity' USING ERRCODE = 'FF404';
    END IF;
    PERFORM ff_audit(p_actor, 'village_requirement.removed', 'village', p_village_id,
                     jsonb_build_object('commodity_id', p_commodity_id));
    RETURN NEXT r;
END $$;

-- Never-served ready villages first, then oldest last distribution, then lowest id.
CREATE FUNCTION ff_next_village(p_shop_id INT)
RETURNS SETOF villages LANGUAGE sql STABLE AS $$
    SELECT v.* FROM villages v
    WHERE v.shop_id = p_shop_id
      AND EXISTS (SELECT 1 FROM village_requirements vr WHERE vr.village_id = v.id)
      AND NOT EXISTS (SELECT 1 FROM ff_village_requirement_check(v.id) c WHERE c.state <> 'ok')
    ORDER BY v.last_distribution_date ASC NULLS FIRST, v.id
    LIMIT 1;
$$;

-- ---------- Distribution (atomic) ----------
-- Locks the village row, its requirement rows and the matching stock rows (in
-- commodity order, so concurrent distributions cannot deadlock), re-validates every
-- requirement, then deducts stock, writes history and updates the village date.
-- Any exception rolls back the whole call.
CREATE FUNCTION ff_mark_distributed(p_actor INT, p_village_id INT)
RETURNS TABLE (commodity_id INT, commodity_name TEXT, distributed_quantity NUMERIC,
               remaining_stock NUMERIC, distribution_date DATE)
LANGUAGE plpgsql AS $$
#variable_conflict use_column
DECLARE
    v   villages;
    bad RECORD;
BEGIN
    SELECT * INTO v FROM villages WHERE id = p_village_id FOR UPDATE;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'Village not found' USING ERRCODE = 'FF404';
    END IF;
    IF v.last_distribution_date = CURRENT_DATE THEN
        RAISE EXCEPTION 'Village has already received a distribution today' USING ERRCODE = 'FF409';
    END IF;

    PERFORM 1 FROM village_requirements WHERE village_id = p_village_id
    ORDER BY commodity_id FOR SHARE;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'Village requirements are not configured' USING ERRCODE = 'FF400';
    END IF;

    PERFORM 1 FROM stock
    WHERE shop_id = v.shop_id
      AND commodity_id IN (SELECT commodity_id FROM village_requirements WHERE village_id = p_village_id)
    ORDER BY commodity_id FOR UPDATE;

    -- Validate everything before changing anything.
    SELECT * INTO bad FROM ff_village_requirement_check(p_village_id) c WHERE c.state <> 'ok' LIMIT 1;
    IF FOUND THEN
        IF bad.status IN ('seasonally_suspended', 'out_of_stock') THEN
            RAISE EXCEPTION 'Cannot distribute: commodity ''%'' is %', bad.commodity_name, bad.status
                USING ERRCODE = 'FF400';
        END IF;
        RAISE EXCEPTION 'Insufficient stock for ''%'': required %, available %',
            bad.commodity_name, trim_scale(bad.required_quantity), trim_scale(bad.available_quantity)
            USING ERRCODE = 'FF409';
    END IF;

    -- Fully consumed rows are removed (stock rows must stay > 0); delete before update
    -- so a row reduced by the update can never be deleted by mistake.
    DELETE FROM stock s USING village_requirements vr
    WHERE vr.village_id = p_village_id AND s.shop_id = v.shop_id
      AND s.commodity_id = vr.commodity_id AND s.quantity = vr.quantity;
    UPDATE stock s SET quantity = s.quantity - vr.quantity
    FROM village_requirements vr
    WHERE vr.village_id = p_village_id AND s.shop_id = v.shop_id
      AND s.commodity_id = vr.commodity_id AND s.quantity > vr.quantity;

    INSERT INTO distribution_history (shop_id, village_id, commodity_id, quantity, distribution_date)
    SELECT v.shop_id, p_village_id, vr.commodity_id, vr.quantity, CURRENT_DATE
    FROM village_requirements vr WHERE vr.village_id = p_village_id;

    UPDATE villages SET last_distribution_date = CURRENT_DATE WHERE id = p_village_id;
    PERFORM ff_audit(p_actor, 'distribution.completed', 'village', p_village_id,
                     jsonb_build_object('shop_id', v.shop_id));

    RETURN QUERY
        SELECT vr.commodity_id, c.name, vr.quantity, COALESCE(s.quantity, 0), CURRENT_DATE
        FROM village_requirements vr
        JOIN commodities c ON c.id = vr.commodity_id
        LEFT JOIN stock s ON s.shop_id = v.shop_id AND s.commodity_id = vr.commodity_id
        WHERE vr.village_id = p_village_id
        ORDER BY vr.commodity_id;
END $$;

-- ---------- Distribution history & forecasting input ----------

CREATE FUNCTION ff_add_forecast_history(p_actor INT, p_shop_id INT, p_commodity_id INT,
                                        p_quantity NUMERIC, p_distribution_date DATE)
RETURNS SETOF distribution_history LANGUAGE plpgsql AS $$
DECLARE r distribution_history;
BEGIN
    IF p_quantity IS NULL OR p_quantity <= 0 THEN
        RAISE EXCEPTION 'Quantity must be greater than zero' USING ERRCODE = 'FF400';
    END IF;
    IF p_distribution_date IS NULL OR p_distribution_date > CURRENT_DATE THEN
        RAISE EXCEPTION 'Distribution date must not be in the future' USING ERRCODE = 'FF400';
    END IF;
    IF NOT EXISTS (SELECT 1 FROM shops WHERE id = p_shop_id) THEN
        RAISE EXCEPTION 'Shop not found' USING ERRCODE = 'FF404';
    END IF;
    IF NOT EXISTS (SELECT 1 FROM commodities WHERE id = p_commodity_id) THEN
        RAISE EXCEPTION 'Commodity not found' USING ERRCODE = 'FF404';
    END IF;
    INSERT INTO distribution_history (shop_id, commodity_id, quantity, distribution_date)
    VALUES (p_shop_id, p_commodity_id, p_quantity, p_distribution_date)
    RETURNING * INTO r;
    PERFORM ff_audit(p_actor, 'history.added', 'distribution_history', r.id,
                     jsonb_build_object('shop_id', p_shop_id, 'commodity_id', p_commodity_id));
    RETURN NEXT r;
END $$;

-- Monthly totals per commodity, one row per calendar month from the commodity's first
-- recorded month up to the CURRENT month (in APP_TIMEZONE). Months without distribution
-- are 0, so the regression's x-axis is real calendar time ending today; is_partial marks
-- the current, still-running month. p_commodity_id NULL = every commodity of the shop.
CREATE FUNCTION ff_monthly_distribution(p_shop_id INT, p_commodity_id INT)
RETURNS TABLE (commodity_id INT, commodity_name TEXT, month TEXT, quantity NUMERIC,
               current_stock NUMERIC, is_partial BOOLEAN)
LANGUAGE sql STABLE AS $$
    WITH monthly AS (
        SELECT dh.commodity_id, date_trunc('month', dh.distribution_date)::DATE AS m,
               SUM(dh.quantity) AS q
        FROM distribution_history dh
        WHERE dh.shop_id = p_shop_id
          AND (p_commodity_id IS NULL OR dh.commodity_id = p_commodity_id)
        GROUP BY 1, 2
    ), bounds AS (
        SELECT commodity_id, MIN(m) AS first_m,
               GREATEST(MAX(m), date_trunc('month', CURRENT_DATE)::DATE) AS last_m
        FROM monthly GROUP BY 1
    )
    SELECT b.commodity_id, c.name, to_char(g.m, 'YYYY-MM'), COALESCE(mo.q, 0),
           COALESCE(s.quantity, 0), g.m::DATE = date_trunc('month', CURRENT_DATE)::DATE
    FROM bounds b
    CROSS JOIN LATERAL generate_series(b.first_m, b.last_m, INTERVAL '1 month') AS g(m)
    JOIN commodities c ON c.id = b.commodity_id
    LEFT JOIN monthly mo ON mo.commodity_id = b.commodity_id AND mo.m = g.m::DATE
    LEFT JOIN stock s ON s.shop_id = p_shop_id AND s.commodity_id = b.commodity_id
    ORDER BY b.commodity_id, g.m;
$$;

-- ---------- Biometric verification results ----------

CREATE FUNCTION ff_record_biometric_verification(p_actor INT, p_beneficiary_id INT, p_shop_id INT,
                                                 p_provider TEXT, p_reference TEXT, p_verified BOOLEAN)
RETURNS SETOF biometric_verifications LANGUAGE plpgsql AS $$
DECLARE r biometric_verifications;
BEGIN
    INSERT INTO biometric_verifications (beneficiary_id, shop_id, officer_id, provider,
                                         provider_reference, verified)
    VALUES (p_beneficiary_id, p_shop_id, p_actor, p_provider, p_reference, p_verified)
    RETURNING * INTO r;
    PERFORM ff_audit(p_actor, 'beneficiary.verification', 'user', p_beneficiary_id,
                     jsonb_build_object('provider', p_provider, 'verified', p_verified,
                                        'verification_id', r.id));
    RETURN NEXT r;
END $$;

-- ---------- Complaints ----------

CREATE FUNCTION ff_create_complaint(p_actor INT, p_category TEXT, p_description TEXT)
RETURNS SETOF complaints LANGUAGE plpgsql AS $$
DECLARE
    v_shop INT;
    r complaints;
BEGIN
    SELECT shop_id INTO v_shop FROM users WHERE id = p_actor AND role = 'beneficiary';
    IF v_shop IS NULL THEN
        RAISE EXCEPTION 'Beneficiary is not assigned to a shop' USING ERRCODE = 'FF400';
    END IF;
    INSERT INTO complaints (beneficiary_id, shop_id, category, description)
    VALUES (p_actor, v_shop, p_category, p_description)
    RETURNING * INTO r;
    PERFORM ff_audit(p_actor, 'complaint.created', 'complaint', r.id,
                     jsonb_build_object('shop_id', v_shop, 'category', p_category));
    RETURN NEXT r;
END $$;

CREATE FUNCTION ff_list_complaints(p_shop_id INT, p_beneficiary_id INT, p_complaint_id INT)
RETURNS SETOF complaints LANGUAGE sql STABLE AS $$
    SELECT * FROM complaints
    WHERE (p_shop_id IS NULL OR shop_id = p_shop_id)
      AND (p_beneficiary_id IS NULL OR beneficiary_id = p_beneficiary_id)
      AND (p_complaint_id IS NULL OR id = p_complaint_id)
    ORDER BY id DESC;
$$;

CREATE FUNCTION ff_update_complaint_status(p_actor INT, p_complaint_id INT, p_status TEXT, p_note TEXT)
RETURNS SETOF complaints LANGUAGE plpgsql AS $$
DECLARE r complaints;
BEGIN
    SELECT * INTO r FROM complaints WHERE id = p_complaint_id FOR UPDATE;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'Complaint not found' USING ERRCODE = 'FF404';
    END IF;
    IF r.status IN ('resolved', 'rejected') THEN
        RAISE EXCEPTION 'Complaint is already closed' USING ERRCODE = 'FF409';
    END IF;
    IF p_status IN ('resolved', 'rejected') AND COALESCE(btrim(p_note), '') = '' THEN
        RAISE EXCEPTION 'A resolution note is required to close a complaint' USING ERRCODE = 'FF400';
    END IF;
    UPDATE complaints
    SET status = p_status, resolution_note = COALESCE(NULLIF(btrim(p_note), ''), resolution_note),
        updated_at = now()
    WHERE id = p_complaint_id
    RETURNING * INTO r;
    PERFORM ff_audit(p_actor, 'complaint.status_changed', 'complaint', r.id,
                     jsonb_build_object('status', p_status));
    RETURN NEXT r;
END $$;

-- ---------- Notifications ----------

-- Recipients: one user (p_user_id) or every beneficiary of a shop (p_shop_id).
CREATE FUNCTION ff_notification_recipients(p_shop_id INT, p_user_id INT)
RETURNS TABLE (user_id INT, phone TEXT)
LANGUAGE sql STABLE AS $$
    SELECT id, phone FROM users
    WHERE is_active AND approved
      AND ((p_user_id IS NOT NULL AND id = p_user_id)
        OR (p_user_id IS NULL AND role = 'beneficiary' AND shop_id = p_shop_id))
    ORDER BY id;
$$;

CREATE FUNCTION ff_log_notification(p_user_id INT, p_event TEXT, p_message TEXT,
                                    p_status TEXT, p_reference TEXT)
RETURNS SETOF notifications LANGUAGE sql AS $$
    INSERT INTO notifications (user_id, event, message, status, provider_reference)
    VALUES (p_user_id, p_event, p_message, p_status, p_reference)
    RETURNING *;
$$;

CREATE FUNCTION ff_list_notifications(p_shop_id INT, p_user_id INT)
RETURNS SETOF notifications LANGUAGE sql STABLE AS $$
    SELECT n.* FROM notifications n LEFT JOIN users u ON u.id = n.user_id
    WHERE (p_shop_id IS NULL OR u.shop_id = p_shop_id)
      AND (p_user_id IS NULL OR n.user_id = p_user_id)
    ORDER BY n.id DESC
    LIMIT 200;
$$;

-- ---------- Audit log ----------

CREATE FUNCTION ff_list_audit_log(p_limit INT)
RETURNS SETOF audit_log LANGUAGE sql STABLE AS $$
    SELECT * FROM audit_log ORDER BY id DESC LIMIT LEAST(GREATEST(COALESCE(p_limit, 100), 1), 1000);
$$;

-- ---------- Passkey (WebAuthn) authentication ----------
-- Fingerprints never reach the server: the user's device verifies the fingerprint
-- locally and signs a challenge with a private key that never leaves the device.
-- These routines store only public keys, single-use challenges and hashed codes.

CREATE FUNCTION ff_list_users(p_shop_id INT, p_role TEXT, p_user_id INT)
RETURNS TABLE (id INT, username TEXT, role TEXT, shop_id INT, phone TEXT, passkey_count BIGINT,
               is_active BOOLEAN, approved BOOLEAN, token_version INT)
LANGUAGE sql STABLE AS $$
    SELECT u.id, u.username, u.role, u.shop_id, u.phone,
           (SELECT count(*) FROM webauthn_credentials w WHERE w.user_id = u.id),
           u.is_active, u.approved, u.token_version
    FROM users u
    WHERE (p_shop_id IS NULL OR u.shop_id = p_shop_id)
      AND (p_role IS NULL OR u.role = p_role)
      AND (p_user_id IS NULL OR u.id = p_user_id)
    ORDER BY u.id;
$$;

CREATE FUNCTION ff_create_auth_challenge(p_challenge TEXT, p_purpose TEXT, p_user_id INT,
                                         p_details JSONB, p_ttl_seconds INT)
RETURNS SETOF auth_challenges LANGUAGE plpgsql AS $$
DECLARE r auth_challenges;
BEGIN
    DELETE FROM auth_challenges WHERE expires_at < now();  -- housekeeping
    INSERT INTO auth_challenges (challenge, purpose, user_id, details, expires_at)
    VALUES (p_challenge, p_purpose, p_user_id, COALESCE(p_details, '{}'),
            now() + make_interval(secs => p_ttl_seconds))
    RETURNING * INTO r;
    RETURN NEXT r;
END $$;

-- Single use: the row is deleted as it is read, so a challenge can never be replayed.
CREATE FUNCTION ff_consume_auth_challenge(p_challenge TEXT, p_purpose TEXT)
RETURNS SETOF auth_challenges LANGUAGE plpgsql AS $$
DECLARE r auth_challenges;
BEGIN
    DELETE FROM auth_challenges WHERE challenge = p_challenge AND purpose = p_purpose
    RETURNING * INTO r;
    IF NOT FOUND OR r.expires_at < now() THEN
        RAISE EXCEPTION 'This fingerprint request has expired or is invalid. Please try again.'
            USING ERRCODE = 'FF400';
    END IF;
    RETURN NEXT r;
END $$;

CREATE FUNCTION ff_issue_enrollment_code(p_actor INT, p_user_id INT, p_code_hash TEXT, p_ttl_seconds INT)
RETURNS TABLE (user_id INT, username TEXT, expires_at TIMESTAMPTZ)
LANGUAGE plpgsql AS $$
#variable_conflict use_column
DECLARE v_exp TIMESTAMPTZ := now() + make_interval(secs => p_ttl_seconds);
BEGIN
    IF NOT EXISTS (SELECT 1 FROM users u WHERE u.id = p_user_id) THEN
        RAISE EXCEPTION 'User not found' USING ERRCODE = 'FF404';
    END IF;
    DELETE FROM enrollment_codes e WHERE e.user_id = p_user_id AND e.used_at IS NULL;  -- one active code
    INSERT INTO enrollment_codes (user_id, code_hash, created_by, expires_at)
    VALUES (p_user_id, p_code_hash, p_actor, v_exp);
    PERFORM ff_audit(p_actor, 'auth.activation_code_issued', 'user', p_user_id, '{}');
    RETURN QUERY SELECT u.id, u.username, v_exp FROM users u WHERE u.id = p_user_id;
END $$;

-- Staff issuing a code for an account that already has devices always switches those
-- devices off (otherwise a code could be used to add a rogue device unnoticed). Revoke
-- and issue happen in one transaction.
CREATE FUNCTION ff_issue_activation(p_actor INT, p_user_id INT, p_code_hash TEXT, p_ttl_seconds INT)
RETURNS TABLE (user_id INT, username TEXT, expires_at TIMESTAMPTZ, revoked BIGINT)
LANGUAGE plpgsql AS $$
#variable_conflict use_column
DECLARE v_revoked BIGINT := 0;
BEGIN
    IF EXISTS (SELECT 1 FROM webauthn_credentials w WHERE w.user_id = p_user_id) THEN
        SELECT r.revoked INTO v_revoked FROM ff_revoke_passkeys(p_actor, p_user_id) r;
    END IF;
    RETURN QUERY SELECT i.user_id, i.username, i.expires_at, v_revoked
                 FROM ff_issue_enrollment_code(p_actor, p_user_id, p_code_hash, p_ttl_seconds) i;
END $$;

-- Admin creates a staff account and its first activation code atomically.
CREATE FUNCTION ff_create_user_with_code(p_actor INT, p_username TEXT, p_hashed_password TEXT,
                                         p_role TEXT, p_shop_id INT, p_phone TEXT,
                                         p_code_hash TEXT, p_ttl_seconds INT)
RETURNS TABLE (id INT, username TEXT, role TEXT, shop_id INT, expires_at TIMESTAMPTZ)
LANGUAGE plpgsql AS $$
#variable_conflict use_column
DECLARE
    v_user RECORD;
    v_exp TIMESTAMPTZ;
BEGIN
    SELECT c.* INTO v_user FROM ff_create_user(p_actor, p_username, p_hashed_password, p_role,
                                               p_shop_id, p_phone) c;
    SELECT i.expires_at INTO v_exp FROM ff_issue_enrollment_code(p_actor, v_user.id, p_code_hash, p_ttl_seconds) i;
    RETURN QUERY SELECT v_user.id, v_user.username, v_user.role, v_user.shop_id, v_exp;
END $$;

-- Returns the account when the code is valid; returns no row otherwise (never raises,
-- so the failed-attempt counter is committed). 5 wrong tries invalidate the code.
CREATE FUNCTION ff_check_enrollment_code(p_username TEXT, p_code_hash TEXT)
RETURNS TABLE (id INT, username TEXT, role TEXT, shop_id INT)
LANGUAGE plpgsql AS $$
#variable_conflict use_column
DECLARE
    v_user INT;
    v_code enrollment_codes;
BEGIN
    SELECT u.id INTO v_user FROM ff_get_user_by_username(p_username) u WHERE u.is_active;
    IF v_user IS NULL THEN
        RETURN;  -- unknown or disabled account: same answer as a wrong code
    END IF;
    SELECT * INTO v_code FROM enrollment_codes e
    WHERE e.user_id = v_user AND e.used_at IS NULL AND e.expires_at > now() AND e.attempts < 5
    ORDER BY e.id DESC LIMIT 1 FOR UPDATE;
    IF v_code.id IS NULL THEN
        RETURN;
    END IF;
    IF v_code.code_hash <> p_code_hash THEN
        UPDATE enrollment_codes SET attempts = attempts + 1 WHERE enrollment_codes.id = v_code.id;
        RETURN;
    END IF;
    RETURN QUERY SELECT u.id, u.username, u.role, u.shop_id FROM users u WHERE u.id = v_user;
END $$;

CREATE FUNCTION ff_get_or_set_webauthn_handle(p_user_id INT, p_new_handle TEXT)
RETURNS TABLE (webauthn_handle TEXT) LANGUAGE sql AS $$
    UPDATE users SET webauthn_handle = COALESCE(users.webauthn_handle, p_new_handle)
    WHERE users.id = p_user_id
    RETURNING users.webauthn_handle;
$$;

CREATE FUNCTION ff_list_passkeys(p_user_id INT)
RETURNS TABLE (id INT, credential_id TEXT, transports TEXT[], device_label TEXT,
               created_at TIMESTAMPTZ, last_used_at TIMESTAMPTZ)
LANGUAGE sql STABLE AS $$
    SELECT w.id, w.credential_id, w.transports, w.device_label, w.created_at, w.last_used_at
    FROM webauthn_credentials w WHERE w.user_id = p_user_id ORDER BY w.id;
$$;

-- Adds a verified passkey to an existing account. With p_code_hash, the activation
-- code is consumed in the same transaction (so one code = one passkey).
CREATE FUNCTION ff_add_passkey(p_actor INT, p_user_id INT, p_credential_id TEXT, p_public_key BYTEA,
                               p_sign_count BIGINT, p_transports TEXT[], p_label TEXT, p_code_hash TEXT)
RETURNS TABLE (id INT, device_label TEXT, created_at TIMESTAMPTZ)
LANGUAGE plpgsql AS $$
#variable_conflict use_column
DECLARE v_id INT;
BEGIN
    IF p_code_hash IS NOT NULL THEN
        UPDATE enrollment_codes e SET used_at = now()
        WHERE e.user_id = p_user_id AND e.code_hash = p_code_hash AND e.used_at IS NULL
          AND e.expires_at > now() AND e.attempts < 5;
        IF NOT FOUND THEN
            RAISE EXCEPTION 'Invalid or expired activation code' USING ERRCODE = 'FF400';
        END IF;
    END IF;
    IF EXISTS (SELECT 1 FROM webauthn_credentials w WHERE w.credential_id = p_credential_id) THEN
        RAISE EXCEPTION 'This passkey is already registered' USING ERRCODE = 'FF409';
    END IF;
    INSERT INTO webauthn_credentials (user_id, credential_id, public_key, sign_count, transports, device_label)
    VALUES (p_user_id, p_credential_id, p_public_key, p_sign_count, COALESCE(p_transports, '{}'),
            COALESCE(NULLIF(btrim(p_label), ''), 'Passkey'))
    RETURNING webauthn_credentials.id INTO v_id;
    PERFORM ff_audit(COALESCE(p_actor, p_user_id), 'auth.passkey_added', 'user', p_user_id,
                     jsonb_build_object('passkey_id', v_id));
    RETURN QUERY SELECT w.id, w.device_label, w.created_at FROM webauthn_credentials w WHERE w.id = v_id;
END $$;

-- Public beneficiary signup: account + first passkey are created atomically, no password.
CREATE FUNCTION ff_signup_with_passkey(p_username TEXT, p_shop_id INT, p_phone TEXT, p_handle TEXT,
                                       p_credential_id TEXT, p_public_key BYTEA, p_sign_count BIGINT,
                                       p_transports TEXT[], p_label TEXT, p_max_pending INT DEFAULT 200)
RETURNS TABLE (id INT, username TEXT, role TEXT, shop_id INT)
LANGUAGE plpgsql AS $$
#variable_conflict use_column
DECLARE v_user INT;
BEGIN
    -- Bound the queue of accounts awaiting approval so signups cannot be mass-produced.
    IF (SELECT count(*) FROM users u WHERE u.shop_id = p_shop_id AND u.role = 'beneficiary'
        AND NOT u.approved AND u.is_active) >= p_max_pending THEN
        RAISE EXCEPTION 'Too many registrations are waiting for approval at this shop. Ask the shop officer.'
            USING ERRCODE = 'FF409';
    END IF;
    -- Self-registered: can sign in at once, sees shop data after the shop officer approves.
    SELECT c.id INTO v_user FROM ff_create_user(NULL, p_username, NULL, 'beneficiary', p_shop_id, p_phone, FALSE) c;
    UPDATE users SET webauthn_handle = p_handle WHERE users.id = v_user;
    PERFORM ff_add_passkey(v_user, v_user, p_credential_id, p_public_key, p_sign_count,
                           p_transports, p_label, NULL);
    RETURN QUERY SELECT u.id, u.username, u.role, u.shop_id FROM users u WHERE u.id = v_user;
END $$;

CREATE FUNCTION ff_get_passkey_for_login(p_credential_id TEXT)
RETURNS TABLE (passkey_id INT, user_id INT, username TEXT, role TEXT, shop_id INT,
               public_key BYTEA, sign_count BIGINT, webauthn_handle TEXT,
               is_active BOOLEAN, approved BOOLEAN, token_version INT)
LANGUAGE sql STABLE AS $$
    SELECT w.id, u.id, u.username, u.role, u.shop_id, w.public_key, w.sign_count, u.webauthn_handle,
           u.is_active, u.approved, u.token_version
    FROM webauthn_credentials w JOIN users u ON u.id = w.user_id
    WHERE w.credential_id = p_credential_id;
$$;

CREATE FUNCTION ff_record_passkey_login(p_passkey_id INT, p_sign_count BIGINT)
RETURNS TABLE (user_id INT) LANGUAGE plpgsql AS $$
#variable_conflict use_column
DECLARE v_user INT;
BEGIN
    UPDATE webauthn_credentials w SET sign_count = p_sign_count, last_used_at = now()
    WHERE w.id = p_passkey_id RETURNING w.user_id INTO v_user;
    IF v_user IS NULL THEN
        RAISE EXCEPTION 'Passkey not found' USING ERRCODE = 'FF404';
    END IF;
    PERFORM ff_audit(v_user, 'auth.login', 'user', v_user, jsonb_build_object('method', 'passkey'));
    RETURN QUERY SELECT v_user;
END $$;

CREATE FUNCTION ff_delete_passkey(p_actor INT, p_passkey_id INT)
RETURNS TABLE (id INT) LANGUAGE plpgsql AS $$
#variable_conflict use_column
BEGIN
    IF NOT EXISTS (SELECT 1 FROM webauthn_credentials w WHERE w.id = p_passkey_id AND w.user_id = p_actor) THEN
        RAISE EXCEPTION 'Passkey not found' USING ERRCODE = 'FF404';
    END IF;
    IF (SELECT count(*) FROM webauthn_credentials w WHERE w.user_id = p_actor) <= 1 THEN
        RAISE EXCEPTION 'You cannot remove your only passkey' USING ERRCODE = 'FF409';
    END IF;
    DELETE FROM webauthn_credentials w WHERE w.id = p_passkey_id;
    PERFORM ff_audit(p_actor, 'auth.passkey_removed', 'user', p_actor, jsonb_build_object('passkey_id', p_passkey_id));
    RETURN QUERY SELECT p_passkey_id;
END $$;

-- ---------- Access management (admin; officers for their shop's beneficiaries) ----------
-- Every change that should end existing sessions bumps users.token_version: the API
-- rejects tokens issued with an older version.

-- Refuses to leave the system without an active admin.
CREATE FUNCTION ff_assert_admin_remains(p_excluding INT)
RETURNS VOID LANGUAGE plpgsql AS $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM users WHERE role = 'admin' AND is_active AND id <> p_excluding) THEN
        RAISE EXCEPTION 'At least one active administrator must remain' USING ERRCODE = 'FF409';
    END IF;
END $$;

CREATE FUNCTION ff_update_user(p_actor INT, p_user_id INT, p_role TEXT, p_shop_id INT)
RETURNS TABLE (id INT, username TEXT, role TEXT, shop_id INT, is_active BOOLEAN, approved BOOLEAN)
LANGUAGE plpgsql AS $$
#variable_conflict use_column
DECLARE u users;
BEGIN
    SELECT * INTO u FROM users WHERE users.id = p_user_id FOR UPDATE;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'User not found' USING ERRCODE = 'FF404';
    END IF;
    IF p_role NOT IN ('admin', 'officer', 'beneficiary') THEN
        RAISE EXCEPTION 'Invalid user role' USING ERRCODE = 'FF400';
    END IF;
    IF p_role = 'officer' AND p_shop_id IS NULL THEN
        RAISE EXCEPTION 'Officer must be assigned to a shop' USING ERRCODE = 'FF400';
    END IF;
    IF p_role = 'admin' AND p_shop_id IS NOT NULL THEN
        RAISE EXCEPTION 'Admin should not be assigned to a shop' USING ERRCODE = 'FF400';
    END IF;
    IF p_shop_id IS NOT NULL AND NOT EXISTS (SELECT 1 FROM shops s WHERE s.id = p_shop_id) THEN
        RAISE EXCEPTION 'Shop not found' USING ERRCODE = 'FF404';
    END IF;
    IF u.role = 'admin' AND p_role <> 'admin' THEN
        PERFORM ff_assert_admin_remains(p_user_id);
    END IF;
    UPDATE users SET role = p_role, shop_id = p_shop_id, token_version = token_version + 1
    WHERE users.id = p_user_id;
    PERFORM ff_audit(p_actor, 'user.updated', 'user', p_user_id,
                     jsonb_build_object('role', p_role, 'shop_id', p_shop_id,
                                        'old_role', u.role, 'old_shop_id', u.shop_id));
    RETURN QUERY SELECT x.id, x.username, x.role, x.shop_id, x.is_active, x.approved
                 FROM users x WHERE x.id = p_user_id;
END $$;

CREATE FUNCTION ff_set_user_active(p_actor INT, p_user_id INT, p_active BOOLEAN)
RETURNS TABLE (id INT, username TEXT, role TEXT, shop_id INT, is_active BOOLEAN, approved BOOLEAN)
LANGUAGE plpgsql AS $$
#variable_conflict use_column
DECLARE u users;
BEGIN
    SELECT * INTO u FROM users WHERE users.id = p_user_id FOR UPDATE;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'User not found' USING ERRCODE = 'FF404';
    END IF;
    IF NOT p_active AND u.role = 'admin' THEN
        PERFORM ff_assert_admin_remains(p_user_id);
    END IF;
    UPDATE users SET is_active = p_active, token_version = token_version + 1 WHERE users.id = p_user_id;
    IF NOT p_active THEN  -- a disabled account must not be re-activated with an old code
        DELETE FROM enrollment_codes e WHERE e.user_id = p_user_id AND e.used_at IS NULL;
    END IF;
    PERFORM ff_audit(p_actor, CASE WHEN p_active THEN 'user.reactivated' ELSE 'user.deactivated' END,
                     'user', p_user_id, '{}');
    RETURN QUERY SELECT x.id, x.username, x.role, x.shop_id, x.is_active, x.approved
                 FROM users x WHERE x.id = p_user_id;
END $$;

CREATE FUNCTION ff_set_beneficiary_approval(p_actor INT, p_user_id INT, p_approved BOOLEAN)
RETURNS TABLE (id INT, username TEXT, role TEXT, shop_id INT, is_active BOOLEAN, approved BOOLEAN)
LANGUAGE plpgsql AS $$
#variable_conflict use_column
BEGIN
    UPDATE users SET approved = p_approved
    WHERE users.id = p_user_id AND users.role = 'beneficiary';
    IF NOT FOUND THEN
        RAISE EXCEPTION 'Beneficiary not found' USING ERRCODE = 'FF404';
    END IF;
    PERFORM ff_audit(p_actor, CASE WHEN p_approved THEN 'user.approved' ELSE 'user.unapproved' END,
                     'user', p_user_id, '{}');
    RETURN QUERY SELECT x.id, x.username, x.role, x.shop_id, x.is_active, x.approved
                 FROM users x WHERE x.id = p_user_id;
END $$;

-- Lost or stolen device: removes every passkey of the account and ends its sessions.
CREATE FUNCTION ff_revoke_passkeys(p_actor INT, p_user_id INT)
RETURNS TABLE (revoked BIGINT) LANGUAGE plpgsql AS $$
DECLARE v_count BIGINT;
BEGIN
    IF NOT EXISTS (SELECT 1 FROM users WHERE id = p_user_id) THEN
        RAISE EXCEPTION 'User not found' USING ERRCODE = 'FF404';
    END IF;
    WITH d AS (DELETE FROM webauthn_credentials WHERE user_id = p_user_id RETURNING 1)
    SELECT count(*) INTO v_count FROM d;
    UPDATE users SET token_version = token_version + 1 WHERE id = p_user_id;
    PERFORM ff_audit(p_actor, 'auth.passkeys_revoked', 'user', p_user_id, jsonb_build_object('count', v_count));
    RETURN QUERY SELECT v_count;
END $$;

-- Version marker: setup_db.py replaces the placeholder with the SHA-256 of this file,
-- so the API can refuse to start against outdated routines (not just missing ones).
CREATE FUNCTION ff_routines_checksum()
RETURNS TEXT LANGUAGE sql IMMUTABLE AS $$ SELECT '__ROUTINES_SHA256__'::TEXT $$;
