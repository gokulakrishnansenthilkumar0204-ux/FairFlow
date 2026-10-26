
import os
from contextlib import asynccontextmanager
from datetime import date
from decimal import Decimal
from typing import Annotated, Literal

from fastapi import BackgroundTasks, Depends, FastAPI, HTTPException, Query, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import OAuth2PasswordRequestForm
from pydantic import AfterValidator, BaseModel, Field

from auth import authenticate, create_access_token, get_current_user, hash_password
from biometric import get_biometric_adapter
from db import call
from notifications import notify
from passkeys import ACTIVATION_TTL_HOURS, RP_ID, Phone, Username, hash_code, new_activation_code
from passkeys import router as passkey_router
from ratelimit import limit
from setup_db import check_installed

@asynccontextmanager
async def lifespan(_app: FastAPI):
    check_installed()  # fail fast if setup_db.py was not run on this database
    yield


app = FastAPI(title="FairFlow API", lifespan=lifespan)


_origins = [o.strip() for o in os.getenv("CORS_ORIGINS", "").split(",") if o.strip()]
if _origins:
    app.add_middleware(CORSMiddleware, allow_origins=_origins, allow_methods=["*"],
                       allow_headers=["*"])

_CSP = ("default-src 'self'; script-src 'self'; style-src 'self' https://fonts.googleapis.com; "
        "font-src 'self' https://fonts.gstatic.com; img-src 'self' data:; connect-src 'self'; "
        "frame-ancestors 'none'; base-uri 'none'; form-action 'self'")


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    path = request.url.path
    if not (path.startswith("/docs") or path.startswith("/redoc")):  # Swagger UI loads from a CDN
        response.headers.setdefault("Content-Security-Policy", _CSP)
    return response


app.include_router(passkey_router)


def password_login_enabled() -> bool:
    
    return os.getenv("ALLOW_PASSWORD_LOGIN", "false").lower() in ("1", "true", "yes")


def require_password_login():
    if not password_login_enabled():
        raise HTTPException(status.HTTP_403_FORBIDDEN,
                            "Password sign-in is disabled. Use fingerprint (passkey) sign-in.")


User = Annotated[dict, Depends(get_current_user)]


# ---------- Helpers ----------

def forbid(detail: str):
    raise HTTPException(status.HTTP_403_FORBIDDEN, detail)


def require_role(user: dict, *roles: str, detail: str):
    if user["role"] not in roles:
        forbid(detail)


def get_or_404(routine: str, *args, detail: str) -> dict:
    row = call(routine, *args, one=True)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail)
    return row


def pending_approval(user: dict) -> bool:
    
    return user["role"] == "beneficiary" and not user.get("approved", True)


_PENDING = "Your account is waiting for approval by your shop officer"


def shop_scope(user: dict) -> int | None:
    
    if user["role"] == "admin":
        return None
    return 0 if pending_approval(user) else (user["shop_id"] or 0)


def check_shop_access(user: dict, shop_id: int, detail: str):
    
    if user["role"] == "admin":
        return
    if pending_approval(user):
        forbid(_PENDING)
    if user["shop_id"] != shop_id:
        forbid(detail)


# ---------- Pydantic schemas ----------

def _bcrypt_limit(password: str) -> str:
    if len(password.encode()) > 72:
        raise ValueError("Password must be at most 72 bytes")
    return password


Password = Annotated[str, Field(min_length=8, max_length=72), AfterValidator(_bcrypt_limit)]
Quantity = Annotated[Decimal, Field(gt=0, max_digits=12, decimal_places=3)]
PositiveId = Annotated[int, Field(gt=0)]


class ShopCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    location: str = Field(min_length=1, max_length=200)


class VillageCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    shop_id: PositiveId


class CommodityCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)


class StockCreate(BaseModel):
    shop_id: PositiveId
    commodity_id: PositiveId
    quantity: Quantity


class StockUpdate(BaseModel):
    quantity: Quantity


class UserCreate(BaseModel):
    username: Username
    password: Password
    # Public signup always creates a beneficiary; any "role" field sent is ignored.
    shop_id: PositiveId | None = None
    phone: Phone = None


class AdminUserCreate(BaseModel):
    username: Username
    password: Password | None = None  # optional: staff sign in with a fingerprint passkey
    role: Literal["officer", "admin"]
    shop_id: PositiveId | None = None
    phone: Phone = None


class CommodityStatusCreate(BaseModel):
    shop_id: PositiveId
    commodity_id: PositiveId
    status: Literal["active", "seasonally_suspended", "out_of_stock"]
    reason: str | None = Field(default=None, max_length=500)


class MarkDistributed(BaseModel):
    village_id: PositiveId


class ForecastHistoryCreate(BaseModel):
    shop_id: PositiveId
    commodity_id: PositiveId
    quantity: Quantity
    distribution_date: date  # YYYY-MM-DD, must be a real calendar date


class VillageRequirementCreate(BaseModel):
    commodity_id: PositiveId
    quantity: Quantity


class BiometricVerificationRequest(BaseModel):
    capture_token: str = Field(min_length=1, max_length=8192)


class ComplaintCreate(BaseModel):
    category: Literal["quality", "quantity", "availability", "behaviour", "other"]
    description: str = Field(min_length=5, max_length=1000)


class ComplaintStatusUpdate(BaseModel):
    status: Literal["in_progress", "resolved", "rejected"]
    resolution_note: str | None = Field(default=None, max_length=1000)


# ---------- Auth endpoints ----------

@app.get("/config")
def public_config():
    """Non-secret settings the frontend needs before sign-in."""
    return {
        "webauthn_rp_id": RP_ID,
        "password_login_enabled": password_login_enabled(),
        "biometric_provider": os.getenv("BIOMETRIC_PROVIDER", "disabled"),
        "activation_code_ttl_hours": ACTIVATION_TTL_HOURS,
    }


@app.get("/public/shops")
def public_shops():
    """Shop names/locations for the signup form (public information)."""
    return call("ff_list_shops", None)


@app.post("/signup", status_code=status.HTTP_201_CREATED, dependencies=[Depends(limit("auth-options"))])
def signup(user: UserCreate):
    """Password signup (disabled by default; use /auth/passkey/signup/*)."""
    require_password_login()
    row = call("ff_create_user", None, user.username, hash_password(user.password),
               "beneficiary", user.shop_id, user.phone, False, one=True)
    return {"id": row["id"], "username": row["username"], "role": row["role"]}


@app.post("/login", dependencies=[Depends(limit("auth-verify"))])
def login(form_data: OAuth2PasswordRequestForm = Depends()):
    """Password sign-in (disabled by default; use /auth/passkey/login/*)."""
    require_password_login()
    user = authenticate(form_data.username, form_data.password)
    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect username or password",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return {"access_token": create_access_token(user), "token_type": "bearer"}


@app.get("/me")
def get_me(current_user: User):
    return current_user



@app.post("/admin/users", status_code=status.HTTP_201_CREATED)
def create_user_by_admin(user: AdminUserCreate, current_user: User):
    require_role(current_user, "admin", detail="Only admin can create officer or admin users")
    # Account and first activation code are created in one transaction.
    code = new_activation_code()
    row = call("ff_create_user_with_code", current_user["id"], user.username,
               hash_password(user.password) if user.password else None,
               user.role, user.shop_id, user.phone, hash_code(code), ACTIVATION_TTL_HOURS * 3600, one=True)
    # Shown once: the new user enters it to register their fingerprint passkey.
    expires_at = row.pop("expires_at")
    return row | {"activation_code": code, "activation_expires_at": expires_at}


# ---------- Shops, villages, commodities ----------

@app.post("/shops", status_code=status.HTTP_201_CREATED)
def create_shop(shop: ShopCreate, current_user: User):
    require_role(current_user, "admin", detail="Only admin can create shops")
    return call("ff_create_shop", current_user["id"], shop.name, shop.location, one=True)


@app.get("/shops")
def get_shops(current_user: User):
    return call("ff_list_shops", shop_scope(current_user))


@app.post("/villages", status_code=status.HTTP_201_CREATED)
def create_village(village: VillageCreate, current_user: User):
    require_role(current_user, "admin", detail="Only admin can create villages")
    return call("ff_create_village", current_user["id"], village.name, village.shop_id, one=True)


@app.get("/villages")
def get_villages(current_user: User):
    return call("ff_list_villages", shop_scope(current_user), None)


@app.post("/commodities", status_code=status.HTTP_201_CREATED)
def create_commodity(commodity: CommodityCreate, current_user: User):
    require_role(current_user, "admin", detail="Only admin can create commodities")
    return call("ff_create_commodity", current_user["id"], commodity.name, one=True)


@app.get("/commodities")
def get_commodities(current_user: User):
    return call("ff_list_commodities", None)


# ---------- Stock ----------

def _get_stock(stock_id: int) -> dict:
    return get_or_404("ff_list_stock", None, stock_id, detail="Stock item not found")


@app.post("/stock", status_code=status.HTTP_201_CREATED)
def add_stock(item: StockCreate, current_user: User):
    require_role(current_user, "officer", detail="Only officers can add stock")
    if current_user["shop_id"] != item.shop_id:
        forbid("Officer can only manage stock for their assigned shop")
    return call("ff_add_stock", current_user["id"], item.shop_id, item.commodity_id,
                item.quantity, one=True)


@app.get("/stock")
def get_all_stock(current_user: User):
    return call("ff_list_stock", shop_scope(current_user), None)


@app.get("/stock/{stock_id}")
def get_stock(stock_id: int, current_user: User):
    stock = _get_stock(stock_id)
    check_shop_access(current_user, stock["shop_id"], "Not authorized to access this stock")
    return stock


@app.put("/stock/{stock_id}")
def update_stock(stock_id: int, item: StockUpdate, current_user: User):
    require_role(current_user, "officer", detail="Only officers can update stock")
    if _get_stock(stock_id)["shop_id"] != current_user["shop_id"]:
        forbid("Officer can only update stock for their assigned shop")
    return call("ff_update_stock", current_user["id"], stock_id, item.quantity, one=True)


@app.delete("/stock/{stock_id}")
def delete_stock(stock_id: int, current_user: User):
    require_role(current_user, "officer", "admin", detail="Only officers or admin can delete stock")
    if current_user["role"] == "officer" and _get_stock(stock_id)["shop_id"] != current_user["shop_id"]:
        forbid("Officer can only delete stock for their assigned shop")
    call("ff_delete_stock", current_user["id"], stock_id)
    return {"message": f"Stock item {stock_id} deleted"}


# ---------- Commodity status ----------

@app.post("/commodity-status", status_code=status.HTTP_201_CREATED)
def set_commodity_status(item: CommodityStatusCreate, current_user: User):
    require_role(current_user, "officer", "admin",
                 detail="Only officers or admin can set commodity status")
    if current_user["role"] == "officer" and current_user["shop_id"] != item.shop_id:
        forbid("Officer can only manage status for their assigned shop")
    return call("ff_set_commodity_status", current_user["id"], item.shop_id, item.commodity_id,
                item.status, item.reason, one=True)


@app.get("/commodity-status/{shop_id}")
def get_commodity_status(shop_id: int, current_user: User):
    check_shop_access(current_user, shop_id, "User can only view status for their assigned shop")
    get_or_404("ff_list_shops", shop_id, detail="Shop not found")
    return call("ff_list_commodity_status", shop_id)


# ---------- ML demand forecasting & stock recommendation ----------

@app.post("/forecast/history", status_code=status.HTTP_201_CREATED)
def add_forecast_history(item: ForecastHistoryCreate, current_user: User):
    """Load a historical distribution record (normal history comes from mark-distributed)."""
    require_role(current_user, "admin", "officer",
                 detail="Only officers or admin can add forecast history")
    if current_user["role"] == "officer" and current_user["shop_id"] != item.shop_id:
        forbid("Officer can only add history for their assigned shop")
    return call("ff_add_forecast_history", current_user["id"], item.shop_id, item.commodity_id,
                item.quantity, item.distribution_date, one=True)


def _get_forecast_access(shop_id: int, current_user: dict):
    """Allow admins, and officers of the shop, to view forecasts."""
    if current_user["role"] == "admin":
        return
    if current_user["role"] == "officer" and current_user["shop_id"] == shop_id:
        return
    forbid("Not authorized to view demand forecasts")


def _linear_regression_forecast(values: list[float], steps_ahead: int = 1) -> tuple[float, float, float]:
    """Train a simple univariate linear regression on sequential months.

    x = 1, 2, ..., n represents the month number and y represents the
    quantity distributed in that month. Predicts x = n + steps_ahead.
    Returns (forecast, slope, intercept).
    """
    n = len(values)
    if n < 2:
        raise ValueError("At least 2 monthly observations are required")

    x_mean = (n + 1) / 2
    y_mean = sum(values) / n

    numerator = sum((x - x_mean) * (y - y_mean) for x, y in enumerate(values, start=1))
    denominator = sum((x - x_mean) ** 2 for x in range(1, n + 1))

    slope = numerator / denominator if denominator else 0.0
    intercept = y_mean - slope * x_mean
    forecast = intercept + slope * (n + steps_ahead)
    return max(0.0, forecast), slope, intercept


def _reorder_quantity(predicted_demand: float, current_stock: float) -> float:
    return max(0.0, predicted_demand - current_stock)


def _recommendation(predicted_demand: float, current_stock: float) -> str:
    if current_stock < predicted_demand:
        return "RESTOCK"
    if current_stock > predicted_demand:
        return "SUFFICIENT_STOCK"
    return "STOCK_MATCHES_FORECAST"


def _next_month(month: str) -> str:
    year, mon = map(int, month.split("-"))
    year, mon = divmod(year * 12 + mon, 12)  # mon is 1-based, so this is "+1 month"
    return f"{year:04d}-{mon + 1:02d}"


def _forecast(rows: list[dict]) -> dict:
    """rows: one commodity's calendar months up to the current month (gaps filled with 0
    by the DB). The current month is still running, so it is shown but not used for
    training; the forecast is for the NEXT calendar month after it."""
    history = [{"month": r["month"], "quantity": float(r["quantity"]), "partial": bool(r["is_partial"])}
               for r in rows]
    complete = [h["quantity"] for h in history if not h["partial"]]
    result = {"history": history, "months_used": len(complete)}
    if len(complete) < 2:
        return result
    # Steps from the last complete month to next month: 2 when the current month is in progress.
    steps_ahead = 1 + sum(1 for h in history if h["partial"])
    predicted, slope, intercept = _linear_regression_forecast(complete, steps_ahead)
    current_stock = float(rows[0]["current_stock"])
    return result | {
        "forecast_month": _next_month(history[-1]["month"]),
        "predicted_next_month_demand": round(predicted, 2),
        "current_stock": round(current_stock, 2),
        "reorder_quantity": round(_reorder_quantity(predicted, current_stock), 2),
        "recommendation": _recommendation(predicted, current_stock),
        "trend_slope": round(slope, 4),
        "intercept": round(intercept, 4),
    }


@app.get("/forecast/{shop_id}/{commodity_id}")
def forecast_demand(shop_id: int, commodity_id: int, current_user: User):
    """Predict next-month demand and calculate a stock recommendation."""
    _get_forecast_access(shop_id, current_user)
    get_or_404("ff_list_shops", shop_id, detail="Shop not found")
    commodity = get_or_404("ff_list_commodities", commodity_id, detail="Commodity not found")

    forecast = _forecast(call("ff_monthly_distribution", shop_id, commodity_id))
    base = {"shop_id": shop_id, "commodity_id": commodity_id, "commodity_name": commodity["name"],
            "model": "Linear Regression"}
    if "predicted_next_month_demand" not in forecast:
        return base | {
            "status": "insufficient_data",
            "message": "At least 2 complete months of distribution history are required",
            "months_available": forecast["months_used"],
            "history": forecast["history"],
        }
    return base | {"status": "success"} | forecast


@app.get("/forecast/{shop_id}")
def forecast_shop(shop_id: int, current_user: User):
    """Return demand forecasts and stock recommendations for all commodities."""
    _get_forecast_access(shop_id, current_user)
    get_or_404("ff_list_shops", shop_id, detail="Shop not found")

    by_commodity: dict[int, list[dict]] = {}
    for row in call("ff_monthly_distribution", shop_id, None):
        by_commodity.setdefault(row["commodity_id"], []).append(row)

    results = []
    for commodity_id, rows in by_commodity.items():
        forecast = _forecast(rows)
        if "predicted_next_month_demand" in forecast:
            forecast.pop("intercept")
            results.append({"commodity_id": commodity_id,
                            "commodity_name": rows[0]["commodity_name"]} | forecast)

    return {"shop_id": shop_id, "model": "Linear Regression",
            "forecast_count": len(results), "forecasts": results}


# ---------- Village requirements, readiness and scheduling ----------

def _get_village(village_id: int, current_user: dict) -> dict:
    village = get_or_404("ff_list_villages", None, village_id, detail="Village not found")
    check_shop_access(current_user, village["shop_id"],
                      "User can only access villages for their assigned shop")
    return village


@app.post("/villages/{village_id}/requirements", status_code=status.HTTP_201_CREATED)
def add_village_requirement(village_id: int, item: VillageRequirementCreate, current_user: User):
    """Create or replace a village's requirement for one commodity."""
    require_role(current_user, "admin", "officer",
                 detail="Only officers or admin can manage village requirements")
    _get_village(village_id, current_user)
    return call("ff_upsert_village_requirement", current_user["id"], village_id,
                item.commodity_id, item.quantity, one=True)


@app.delete("/villages/{village_id}/requirements/{commodity_id}")
def remove_village_requirement(village_id: int, commodity_id: int, current_user: User):
    """Stop giving a commodity to a village (removes its per-round requirement)."""
    require_role(current_user, "admin", "officer",
                 detail="Only officers or admin can manage village requirements")
    _get_village(village_id, current_user)
    call("ff_delete_village_requirement", current_user["id"], village_id, commodity_id)
    return {"message": "Requirement removed", "village_id": village_id, "commodity_id": commodity_id}


@app.get("/villages/{village_id}/requirements")
def get_village_requirements(village_id: int, current_user: User):
    _get_village(village_id, current_user)
    return call("ff_list_village_requirements", village_id)


@app.get("/villages/{village_id}/readiness")
def check_village_readiness(village_id: int, current_user: User):
    """Check whether the assigned shop has enough usable stock for the village."""
    village = _get_village(village_id, current_user)
    checks = call("ff_village_requirement_check", village_id)

    def item(c, with_available=True):
        out = {"commodity_id": c["commodity_id"], "commodity_name": c["commodity_name"],
               "required_quantity": c["required_quantity"]}
        if with_available:
            out["available_quantity"] = 0 if c["state"] == "missing" else c["available_quantity"]
        return out

    response = {
        "village_id": village_id,
        "village_name": village["name"],
        "shop_id": village["shop_id"],
        "is_ready": bool(checks) and all(c["state"] == "ok" for c in checks),
        "missing_commodities": [item(c) for c in checks if c["state"] == "missing"],
        "insufficient_stock": [item(c) for c in checks if c["state"] == "insufficient"],
        "seasonally_suspended": [item(c, False) for c in checks if c["state"] == "suspended"],
    }
    if not checks:
        response["reason"] = "Village requirements are not configured"
    return response


@app.get("/shops/{shop_id}/village-overview")
def get_village_overview(shop_id: int, current_user: User):
    """Requirements and readiness of every village of a shop in one request."""
    check_shop_access(current_user, shop_id, "User can only view villages of their assigned shop")
    out = []
    for v in call("ff_list_villages", shop_id, None):
        checks = call("ff_village_requirement_check", v["id"])
        out.append({
            "village_id": v["id"],
            "requirements": call("ff_list_village_requirements", v["id"]),
            "is_ready": bool(checks) and all(c["state"] == "ok" for c in checks),
            "configured": bool(checks),
        })
    return out


@app.get("/shops/{shop_id}/next-village")
def get_next_village(shop_id: int, current_user: User):
    check_shop_access(current_user, shop_id, "User can only view scheduling for their assigned shop")
    get_or_404("ff_list_shops", shop_id, detail="Shop not found")
    village = call("ff_next_village", shop_id, one=True)
    if village is None:
        return {"message": "No village is currently ready for distribution"}
    return {"village_id": village["id"], "village_name": village["name"],
            "last_distribution_date": village["last_distribution_date"]}


@app.post("/villages/mark-distributed")
def mark_distributed(item: MarkDistributed, current_user: User, background: BackgroundTasks):
    """Complete a distribution atomically, then notify beneficiaries (best-effort)."""
    require_role(current_user, "officer", detail="Only officers can mark distribution complete")
    village = get_or_404("ff_list_villages", None, item.village_id, detail="Village not found")
    if village["shop_id"] != current_user["shop_id"]:
        forbid("Officer can only distribute for their assigned shop")

    rows = call("ff_mark_distributed", current_user["id"], village["id"])
    distribution_date = rows[0]["distribution_date"]

    # Runs after this response; the distribution is already committed.
    background.add_task(notify, "distribution_completed", shop_id=village["shop_id"],
                        village=village["name"], date=distribution_date.isoformat())
    return {
        "message": "Distribution completed and stock updated",
        "village_id": village["id"],
        "village_name": village["name"],
        "distribution_date": distribution_date,
        "distributed_items": [
            {"commodity_id": r["commodity_id"], "commodity_name": r["commodity_name"],
             "distributed_quantity": r["distributed_quantity"],
             "remaining_stock": r["remaining_stock"]}
            for r in rows
        ],
    }


# ---------- Beneficiary identity verification (biometric adapter) ----------

@app.post("/beneficiaries/{beneficiary_id}/biometric-verification")
def verify_beneficiary(beneficiary_id: int, item: BiometricVerificationRequest, current_user: User):
    """Officer-initiated identity check at the counter. Does not log anyone in."""
    require_role(current_user, "officer", detail="Only officers can verify beneficiaries")
    beneficiary = get_or_404("ff_get_beneficiary", beneficiary_id, detail="Beneficiary not found")
    if beneficiary["shop_id"] != current_user["shop_id"]:
        forbid("Officer can only verify beneficiaries of their assigned shop")

    adapter = get_biometric_adapter()
    result = adapter(beneficiary_id, item.capture_token)
    row = call("ff_record_biometric_verification", current_user["id"], beneficiary_id,
               current_user["shop_id"], result.provider, result.reference, result.verified, one=True)
    return {
        "verification_id": row["id"],
        "beneficiary_id": beneficiary_id,
        "verified": result.verified,
        "provider": result.provider,
        "provider_reference": result.reference,
        "simulated": result.simulated,
        "verified_at": row["verified_at"],
    }


# ---------- Complaints ----------

def _get_complaint(complaint_id: int, current_user: dict) -> dict:
    complaint = get_or_404("ff_list_complaints", None, None, complaint_id, detail="Complaint not found")
    if current_user["role"] == "beneficiary":
        if complaint["beneficiary_id"] != current_user["id"]:
            forbid("Not authorized to access this complaint")
    else:
        check_shop_access(current_user, complaint["shop_id"], "Not authorized to access this complaint")
    return complaint


@app.post("/complaints", status_code=status.HTTP_201_CREATED)
def create_complaint(item: ComplaintCreate, current_user: User):
    require_role(current_user, "beneficiary", detail="Only beneficiaries can submit complaints")
    if pending_approval(current_user):
        forbid(_PENDING)
    return call("ff_create_complaint", current_user["id"], item.category, item.description, one=True)


@app.get("/complaints")
def list_complaints(current_user: User):
    if current_user["role"] == "beneficiary":
        return call("ff_list_complaints", None, current_user["id"], None)
    return call("ff_list_complaints", shop_scope(current_user), None, None)


@app.get("/complaints/{complaint_id}")
def get_complaint(complaint_id: int, current_user: User):
    return _get_complaint(complaint_id, current_user)


@app.patch("/complaints/{complaint_id}/status")
def update_complaint_status(complaint_id: int, item: ComplaintStatusUpdate, current_user: User,
                            background: BackgroundTasks):
    require_role(current_user, "officer", "admin",
                 detail="Only officers or admin can update complaints")
    _get_complaint(complaint_id, current_user)
    row = call("ff_update_complaint_status", current_user["id"], complaint_id, item.status,
               item.resolution_note, one=True)
    background.add_task(notify, "complaint_status", user_id=row["beneficiary_id"],
                        complaint_id=row["id"], status=row["status"])
    return row


# ---------- Notifications & audit log ----------

@app.get("/notifications")
def list_notifications(current_user: User):
    if current_user["role"] == "beneficiary":
        return call("ff_list_notifications", None, current_user["id"])
    return call("ff_list_notifications", shop_scope(current_user), None)


@app.get("/audit-logs")
def list_audit_logs(current_user: User, limit: int = Query(100, ge=1, le=1000)):
    require_role(current_user, "admin", detail="Only admin can view audit logs")
    return call("ff_list_audit_log", limit)


# ---------- Frontend UI ----------

_frontend_dir = os.path.join(os.path.dirname(__file__), "frontend")
if os.path.exists(_frontend_dir):
    from fastapi.staticfiles import StaticFiles

    app.mount("/", StaticFiles(directory=_frontend_dir, html=True), name="frontend")

