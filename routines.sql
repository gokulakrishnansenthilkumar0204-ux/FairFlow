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

CREATE FUNCTION ff_get_user_by_username(p_username TEXT)
RETURNS TABLE (id INT, username TEXT, hashed_password TEXT, role TEXT, shop_id INT)
LANGUAGE sql STABLE AS $$
    SELECT id, username, hashed_password, role, shop_id FROM users WHERE username = p_username;
$$;

CREATE FUNCTION ff_get_beneficiary(p_id INT)
RETURNS TABLE (id INT, username TEXT, shop_id INT)
LANGUAGE sql STABLE AS $$
    SELECT id, username, shop_id FROM users WHERE id = p_id AND role = 'beneficiary';
$$;

CREATE FUNCTION ff_create_user(p_actor INT, p_username TEXT, p_hashed_password TEXT,
                               p_role TEXT, p_shop_id INT, p_phone TEXT)
RETURNS TABLE (id INT, username TEXT, role TEXT, shop_id INT)
LANGUAGE plpgsql AS $$
#variable_conflict use_column
DECLARE v_id INT;
BEGIN
    IF p_role NOT IN ('admin', 'officer', 'beneficiary') THEN
        RAISE EXCEPTION 'Invalid user role' USING ERRCODE = 'FF400';
    END IF;
    IF EXISTS (SELECT 1 FROM users u WHERE u.username = p_username) THEN
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

    INSERT INTO users (username, hashed_password, role, shop_id, phone)
    VALUES (p_username, p_hashed_password, p_role, p_shop_id, p_phone)
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

-- Monthly totals per commodity. Months with no distribution between a commodity's
-- first and last recorded month are returned as 0 so the regression's x-axis is
-- real calendar months. p_commodity_id NULL = every commodity of the shop.
CREATE FUNCTION ff_monthly_distribution(p_shop_id INT, p_commodity_id INT)
RETURNS TABLE (commodity_id INT, commodity_name TEXT, month TEXT, quantity NUMERIC,
               current_stock NUMERIC)
LANGUAGE sql STABLE AS $$
    WITH monthly AS (
        SELECT dh.commodity_id, date_trunc('month', dh.distribution_date)::DATE AS m,
               SUM(dh.quantity) AS q
        FROM distribution_history dh
        WHERE dh.shop_id = p_shop_id
          AND (p_commodity_id IS NULL OR dh.commodity_id = p_commodity_id)
        GROUP BY 1, 2
    ), bounds AS (
        SELECT commodity_id, MIN(m) AS first_m, MAX(m) AS last_m FROM monthly GROUP BY 1
    )
    SELECT b.commodity_id, c.name, to_char(g.m, 'YYYY-MM'), COALESCE(mo.q, 0),
           COALESCE(s.quantity, 0)
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
    WHERE (p_user_id IS NOT NULL AND id = p_user_id)
       OR (p_user_id IS NULL AND role = 'beneficiary' AND shop_id = p_shop_id)
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
