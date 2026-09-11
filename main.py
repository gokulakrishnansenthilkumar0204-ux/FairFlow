from datetime import date
from typing import Literal

from fastapi import Depends, FastAPI, HTTPException, status
from fastapi.security import OAuth2PasswordRequestForm
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from auth import (
    create_access_token,
    get_current_user,
    get_db,
    hash_password,
    verify_password,
)
from models import (
    Commodity,
    CommodityStatus,
    Shop,
    Stock,
    User,
    Village,
    VillageRequirement,
    DistributionHistory,
)

app = FastAPI()


# ---------- Pydantic schemas ----------

class ShopCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    location: str = Field(min_length=1, max_length=200)


class VillageCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    shop_id: int = Field(gt=0)


class CommodityCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)


class StockCreate(BaseModel):
    shop_id: int = Field(gt=0)
    commodity_id: int = Field(gt=0)
    quantity: float = Field(gt=0)


class StockUpdate(BaseModel):
    quantity: float = Field(gt=0)


class UserCreate(BaseModel):
    username: str = Field(min_length=3, max_length=50)
    password: str = Field(min_length=8, max_length=72)
    # Public signup always creates a beneficiary.
    shop_id: int | None = Field(default=None, gt=0)


class AdminUserCreate(BaseModel):
    username: str = Field(min_length=3, max_length=50)
    password: str = Field(min_length=8, max_length=72)
    role: Literal["officer", "admin"]
    shop_id: int | None = Field(default=None, gt=0)


class CommodityStatusCreate(BaseModel):
    shop_id: int = Field(gt=0)
    commodity_id: int = Field(gt=0)
    status: Literal["active", "seasonally_suspended", "out_of_stock"]
    reason: str | None = Field(default=None, max_length=500)


class MarkDistributed(BaseModel):
    village_id: int = Field(gt=0)


class ForecastHistoryCreate(BaseModel):
    shop_id: int = Field(gt=0)
    commodity_id: int = Field(gt=0)
    quantity: float = Field(gt=0)
    distribution_date: str = Field(
        min_length=10,
        max_length=10,
        pattern=r"^\d{4}-\d{2}-\d{2}$",
    )


class VillageRequirementCreate(BaseModel):
    commodity_id: int = Field(gt=0)
    quantity: float = Field(gt=0)


# ---------- Auth endpoints ----------

@app.post("/signup", status_code=status.HTTP_201_CREATED)
def signup(user: UserCreate, db: Session = Depends(get_db)):
    existing = db.query(User).filter(User.username == user.username).first()
    if existing:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Username already taken",
        )

    if user.shop_id is not None:
        shop = db.query(Shop).filter(Shop.id == user.shop_id).first()
        if not shop:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Shop not found",
            )

    new_user = User(
        username=user.username,
        hashed_password=hash_password(user.password),
        role="beneficiary",
        shop_id=user.shop_id,
    )

    db.add(new_user)
    db.commit()
    db.refresh(new_user)

    return {
        "id": new_user.id,
        "username": new_user.username,
        "role": new_user.role,
    }


@app.post("/login")
def login(
    form_data: OAuth2PasswordRequestForm = Depends(),
    db: Session = Depends(get_db),
):
    user = db.query(User).filter(User.username == form_data.username).first()

    if not user or not verify_password(
        form_data.password, user.hashed_password
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect username or password",
            headers={"WWW-Authenticate": "Bearer"},
        )

    token = create_access_token(
        data={"sub": user.username, "role": user.role}
    )

    return {
        "access_token": token,
        "token_type": "bearer",
    }


@app.post("/admin/users", status_code=status.HTTP_201_CREATED)
def create_user_by_admin(
    user: AdminUserCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if current_user.role != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only admin can create officer or admin users",
        )

    existing = db.query(User).filter(User.username == user.username).first()
    if existing:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Username already taken",
        )

    if user.role == "officer" and user.shop_id is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Officer must be assigned to a shop",
        )

    if user.role == "admin" and user.shop_id is not None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Admin should not be assigned to a shop",
        )

    if user.shop_id is not None:
        shop = db.query(Shop).filter(Shop.id == user.shop_id).first()
        if not shop:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Shop not found",
            )

    new_user = User(
        username=user.username,
        hashed_password=hash_password(user.password),
        role=user.role,
        shop_id=user.shop_id,
    )

    db.add(new_user)
    db.commit()
    db.refresh(new_user)

    return {
        "id": new_user.id,
        "username": new_user.username,
        "role": new_user.role,
        "shop_id": new_user.shop_id,
    }


# ---------- Shop endpoints ----------

@app.post("/shops")
def create_shop(
    shop: ShopCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if current_user.role != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only admin can create shops",
        )

    new_shop = Shop(name=shop.name, location=shop.location)
    db.add(new_shop)
    db.commit()
    db.refresh(new_shop)
    return new_shop


@app.get("/shops")
def get_shops(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    # Admin can view all shops.
    if current_user.role == "admin":
        return db.query(Shop).all()

    # Officers and beneficiaries can view only their assigned shop.
    if current_user.role in ("officer", "beneficiary"):
        if current_user.shop_id is None:
            return []

        return db.query(Shop).filter(
            Shop.id == current_user.shop_id
        ).all()

    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail="Not authorized to view shops",
    )


# ---------- Village endpoints ----------

@app.post("/villages")
def create_village(
    village: VillageCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if current_user.role != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only admin can create villages",
        )

    shop = db.query(Shop).filter(Shop.id == village.shop_id).first()
    if not shop:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Shop not found",
        )

    new_village = Village(
        name=village.name,
        shop_id=village.shop_id,
    )
    db.add(new_village)
    db.commit()
    db.refresh(new_village)
    return new_village


@app.get("/villages")
def get_villages(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    # Admin can view villages from all shops.
    if current_user.role == "admin":
        return db.query(Village).all()

    # Officers and beneficiaries can view only villages
    # belonging to their assigned shop.
    if current_user.role in ("officer", "beneficiary"):
        if current_user.shop_id is None:
            return []

        return db.query(Village).filter(
            Village.shop_id == current_user.shop_id
        ).all()

    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail="Not authorized to view villages",
    )


# ---------- Commodity endpoints ----------

@app.post("/commodities")
def create_commodity(
    commodity: CommodityCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if current_user.role != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only admin can create commodities",
        )

    new_commodity = Commodity(name=commodity.name)
    db.add(new_commodity)
    db.commit()
    db.refresh(new_commodity)
    return new_commodity


@app.get("/commodities")
def get_commodities(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    return db.query(Commodity).all()


# ---------- Stock endpoints ----------

@app.post("/stock", status_code=status.HTTP_201_CREATED)
def add_stock(
    item: StockCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if current_user.role != "officer":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only officers can add stock",
        )

    if current_user.shop_id != item.shop_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Officer can only manage stock for their assigned shop",
        )

    shop = db.query(Shop).filter(Shop.id == item.shop_id).first()
    if not shop:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Shop not found",
        )

    commodity = db.query(Commodity).filter(
        Commodity.id == item.commodity_id
    ).first()
    if not commodity:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Commodity not found",
        )

    # Keep one inventory balance per shop + commodity.
    stock = db.query(Stock).filter(
        Stock.shop_id == item.shop_id,
        Stock.commodity_id == item.commodity_id,
    ).first()

    if stock:
        stock.quantity += item.quantity
    else:
        stock = Stock(
            shop_id=item.shop_id,
            commodity_id=item.commodity_id,
            quantity=item.quantity,
        )
        db.add(stock)

    db.commit()
    db.refresh(stock)
    return stock


@app.get("/stock")
def get_all_stock(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    # Admin can view stock from all shops.
    if current_user.role == "admin":
        return db.query(Stock).all()

    # Officers and beneficiaries can view stock only
    # from their assigned shop.
    if current_user.role in ("officer", "beneficiary"):
        if current_user.shop_id is None:
            return []

        return db.query(Stock).filter(
            Stock.shop_id == current_user.shop_id
        ).all()

    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail="Not authorized to view stock",
    )


@app.get("/stock/{stock_id}")
def get_stock(
    stock_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    stock = db.query(Stock).filter(Stock.id == stock_id).first()

    if not stock:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Stock item not found",
        )

    # Admin can view any stock.
    if current_user.role == "admin":
        return stock

    # Officers and beneficiaries can view stock only
    # from their assigned shop.
    if (
        current_user.role in ("officer", "beneficiary")
        and stock.shop_id == current_user.shop_id
    ):
        return stock

    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail="Not authorized to access this stock",
    )


@app.put("/stock/{stock_id}")
def update_stock(
    stock_id: int,
    item: StockUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if current_user.role != "officer":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only officers can update stock",
        )

    stock = db.query(Stock).filter(Stock.id == stock_id).first()

    if not stock:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Stock item not found",
        )

    if stock.shop_id != current_user.shop_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Officer can only update stock for their assigned shop",
        )

    stock.quantity = item.quantity
    db.commit()
    db.refresh(stock)

    return stock


@app.delete("/stock/{stock_id}")
def delete_stock(
    stock_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if current_user.role not in ("officer", "admin"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only officers or admin can delete stock",
        )

    stock = db.query(Stock).filter(Stock.id == stock_id).first()

    if not stock:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Stock item not found",
        )

    if (
        current_user.role == "officer"
        and stock.shop_id != current_user.shop_id
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Officer can only delete stock for their assigned shop",
        )

    db.delete(stock)
    db.commit()

    return {"message": f"Stock item {stock_id} deleted"}


# ---------- Commodity status ----------

@app.post("/commodity-status", status_code=status.HTTP_201_CREATED)
def set_commodity_status(
    item: CommodityStatusCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if current_user.role not in ("officer", "admin"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only officers or admin can set commodity status",
        )

    if (
        current_user.role == "officer"
        and current_user.shop_id != item.shop_id
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Officer can only manage status for their assigned shop",
        )

    if item.status != "active" and not item.reason:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Reason is required when status is not active",
        )

    shop = db.query(Shop).filter(Shop.id == item.shop_id).first()
    if not shop:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Shop not found",
        )

    commodity = db.query(Commodity).filter(
        Commodity.id == item.commodity_id
    ).first()
    if not commodity:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Commodity not found",
        )

    existing = db.query(CommodityStatus).filter(
        CommodityStatus.shop_id == item.shop_id,
        CommodityStatus.commodity_id == item.commodity_id,
    ).first()

    if existing:
        existing.status = item.status
        existing.reason = item.reason
        db.commit()
        db.refresh(existing)
        return existing

    new_status = CommodityStatus(
        shop_id=item.shop_id,
        commodity_id=item.commodity_id,
        status=item.status,
        reason=item.reason,
    )

    db.add(new_status)
    db.commit()
    db.refresh(new_status)

    return new_status


@app.get("/commodity-status/{shop_id}")
def get_commodity_status(
    shop_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    # Beneficiaries can VIEW status, but only for their assigned shop.
    if current_user.role == "beneficiary":
        if current_user.shop_id != shop_id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Beneficiary can only view status for their assigned shop",
            )

    elif current_user.role == "officer":
        if current_user.shop_id != shop_id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Officer can only view status for their assigned shop",
            )

    elif current_user.role != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Not authorized to view commodity status",
        )

    shop = db.query(Shop).filter(Shop.id == shop_id).first()
    if not shop:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Shop not found",
        )

    return db.query(CommodityStatus).filter(
        CommodityStatus.shop_id == shop_id
    ).all()



# ---------- ML demand forecasting & stock recommendation ----------

@app.post("/forecast/history", status_code=status.HTTP_201_CREATED)
def add_forecast_history(
    item: ForecastHistoryCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Add a historical distribution record for model training/testing.

    In normal operation, distribution history is created automatically by
    /villages/mark-distributed. This endpoint is useful for loading existing
    historical records into a new deployment.
    """
    if current_user.role not in ("admin", "officer"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only officers or admin can add forecast history",
        )

    if current_user.role == "officer" and current_user.shop_id != item.shop_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Officer can only add history for their assigned shop",
        )

    shop = db.query(Shop).filter(Shop.id == item.shop_id).first()
    if not shop:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Shop not found",
        )

    commodity = db.query(Commodity).filter(
        Commodity.id == item.commodity_id
    ).first()
    if not commodity:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Commodity not found",
        )

    record = DistributionHistory(
        shop_id=item.shop_id,
        commodity_id=item.commodity_id,
        quantity=item.quantity,
        distribution_date=item.distribution_date,
    )
    db.add(record)
    db.commit()
    db.refresh(record)
    return record


def _get_forecast_access(shop_id: int, current_user: User):
    """Allow admins and officers to view forecasts for an authorized shop."""
    if current_user.role == "admin":
        return

    if current_user.role == "officer" and current_user.shop_id == shop_id:
        return

    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail="Not authorized to view demand forecasts",
    )


def _linear_regression_forecast(values: list[float]) -> tuple[float, float, float]:
    """Train a simple univariate linear regression on sequential months.

    x = 1, 2, ..., n represents the month number and y represents the
    quantity distributed in that month. Returns (forecast, slope, intercept).
    """
    n = len(values)
    if n < 2:
        raise ValueError("At least 2 monthly observations are required")

    x_mean = (n + 1) / 2
    y_mean = sum(values) / n

    numerator = sum(
        (x - x_mean) * (y - y_mean)
        for x, y in enumerate(values, start=1)
    )
    denominator = sum(
        (x - x_mean) ** 2
        for x in range(1, n + 1)
    )

    slope = numerator / denominator if denominator else 0.0
    intercept = y_mean - slope * x_mean
    next_x = n + 1

    forecast = intercept + slope * next_x
    return max(0.0, forecast), slope, intercept


def _build_monthly_history(records: list[DistributionHistory]) -> list[dict]:
    """Aggregate distribution records into monthly quantities."""
    monthly = {}

    for record in records:
        month = record.distribution_date[:7]
        monthly[month] = monthly.get(month, 0.0) + float(record.quantity)

    return [
        {"month": month, "quantity": monthly[month]}
        for month in sorted(monthly)
    ]


@app.get("/forecast/{shop_id}/{commodity_id}")
def forecast_demand(
    shop_id: int,
    commodity_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Predict next-month demand and calculate a stock recommendation."""
    _get_forecast_access(shop_id, current_user)

    shop = db.query(Shop).filter(Shop.id == shop_id).first()
    if not shop:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Shop not found",
        )

    commodity = db.query(Commodity).filter(
        Commodity.id == commodity_id
    ).first()
    if not commodity:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Commodity not found",
        )

    records = db.query(DistributionHistory).filter(
        DistributionHistory.shop_id == shop_id,
        DistributionHistory.commodity_id == commodity_id,
    ).order_by(DistributionHistory.distribution_date).all()

    monthly_history = _build_monthly_history(records)

    if len(monthly_history) < 2:
        return {
            "shop_id": shop_id,
            "commodity_id": commodity_id,
            "commodity_name": commodity.name,
            "model": "Linear Regression",
            "status": "insufficient_data",
            "message": "At least 2 different months of distribution history are required",
            "months_available": len(monthly_history),
            "history": monthly_history,
        }

    values = [row["quantity"] for row in monthly_history]
    predicted_demand, slope, intercept = _linear_regression_forecast(values)

    stock_entry = db.query(Stock).filter(
        Stock.shop_id == shop_id,
        Stock.commodity_id == commodity_id,
    ).first()
    current_stock = float(stock_entry.quantity) if stock_entry else 0.0

    reorder_quantity = max(0.0, predicted_demand - current_stock)

    if current_stock < predicted_demand:
        recommendation = "RESTOCK"
    elif current_stock > predicted_demand:
        recommendation = "SUFFICIENT_STOCK"
    else:
        recommendation = "STOCK_MATCHES_FORECAST"

    return {
        "shop_id": shop_id,
        "commodity_id": commodity_id,
        "commodity_name": commodity.name,
        "model": "Linear Regression",
        "status": "success",
        "history": monthly_history,
        "predicted_next_month_demand": round(predicted_demand, 2),
        "current_stock": round(current_stock, 2),
        "reorder_quantity": round(reorder_quantity, 2),
        "recommendation": recommendation,
        "trend_slope": round(slope, 4),
        "intercept": round(intercept, 4),
    }


@app.get("/forecast/{shop_id}")
def forecast_shop(
    shop_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Return demand forecasts and stock recommendations for all commodities."""
    _get_forecast_access(shop_id, current_user)

    shop = db.query(Shop).filter(Shop.id == shop_id).first()
    if not shop:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Shop not found",
        )

    commodity_ids = [
        row[0]
        for row in db.query(DistributionHistory.commodity_id).filter(
            DistributionHistory.shop_id == shop_id
        ).distinct().all()
    ]

    results = []
    for commodity_id in sorted(commodity_ids):
        records = db.query(DistributionHistory).filter(
            DistributionHistory.shop_id == shop_id,
            DistributionHistory.commodity_id == commodity_id,
        ).order_by(DistributionHistory.distribution_date).all()

        monthly_history = _build_monthly_history(records)
        if len(monthly_history) < 2:
            continue

        values = [row["quantity"] for row in monthly_history]
        predicted_demand, slope, intercept = _linear_regression_forecast(values)

        stock_entry = db.query(Stock).filter(
            Stock.shop_id == shop_id,
            Stock.commodity_id == commodity_id,
        ).first()
        current_stock = float(stock_entry.quantity) if stock_entry else 0.0

        results.append({
            "commodity_id": commodity_id,
            "commodity_name": records[0].commodity.name,
            "predicted_next_month_demand": round(predicted_demand, 2),
            "current_stock": round(current_stock, 2),
            "reorder_quantity": round(max(0.0, predicted_demand - current_stock), 2),
            "recommendation": (
                "RESTOCK"
                if current_stock < predicted_demand
                else "SUFFICIENT_STOCK"
            ),
            "trend_slope": round(slope, 4),
            "history": monthly_history,
        })

    return {
        "shop_id": shop_id,
        "model": "Linear Regression",
        "forecast_count": len(results),
        "forecasts": results,
    }


# ---------- Village requirements, readiness and scheduling ----------


def _is_blocking_commodity_status(status_value: str) -> bool:
    """Return True when a commodity status prevents village distribution.

    Older database rows may contain human-readable values such as
    ``Available`` or ``Low Stock``. These remain usable when the actual
    stock quantity satisfies the village requirement. Only explicit
    unavailable statuses block readiness/distribution.
    """
    normalized = (status_value or "").strip().lower()
    return normalized in {"seasonally_suspended", "out_of_stock"}



def _check_village_access(village: Village, current_user: User):
    """Ensure the caller can access a village belonging to a shop."""
    if current_user.role == "admin":
        return

    if current_user.role in ("officer", "beneficiary"):
        if current_user.shop_id != village.shop_id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="User can only access villages for their assigned shop",
            )
        return

    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail="Not authorized to access village",
    )


@app.post("/villages/{village_id}/requirements", status_code=status.HTTP_201_CREATED)
def add_village_requirement(
    village_id: int,
    item: VillageRequirementCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Create or increase a village's commodity requirement.

    Admins can manage any village. Officers can manage requirements only for
    villages belonging to their assigned shop. Beneficiaries are read-only.
    """
    village = db.query(Village).filter(Village.id == village_id).first()
    if not village:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Village not found",
        )

    if current_user.role not in ("admin", "officer"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only officers or admin can manage village requirements",
        )

    if current_user.role == "officer" and current_user.shop_id != village.shop_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Officer can only manage requirements for their assigned shop",
        )

    commodity = db.query(Commodity).filter(Commodity.id == item.commodity_id).first()
    if not commodity:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Commodity not found",
        )

    existing = db.query(VillageRequirement).filter(
        VillageRequirement.village_id == village_id,
        VillageRequirement.commodity_id == item.commodity_id,
    ).first()

    if existing:
        existing.quantity = item.quantity
        db.commit()
        db.refresh(existing)
        return existing

    requirement = VillageRequirement(
        village_id=village_id,
        commodity_id=item.commodity_id,
        quantity=item.quantity,
    )
    db.add(requirement)
    db.commit()
    db.refresh(requirement)
    return requirement


@app.get("/villages/{village_id}/requirements")
def get_village_requirements(
    village_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    village = db.query(Village).filter(Village.id == village_id).first()
    if not village:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Village not found",
        )

    _check_village_access(village, current_user)

    requirements = db.query(VillageRequirement).filter(
        VillageRequirement.village_id == village_id
    ).all()

    return [
        {
            "id": requirement.id,
            "village_id": requirement.village_id,
            "commodity_id": requirement.commodity_id,
            "commodity_name": requirement.commodity.name,
            "quantity": requirement.quantity,
        }
        for requirement in requirements
    ]


@app.get("/villages/{village_id}/readiness")
def check_village_readiness(
    village_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Check whether the assigned shop has enough usable stock for the village."""
    village = db.query(Village).filter(Village.id == village_id).first()

    if not village:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Village not found",
        )

    _check_village_access(village, current_user)

    requirements = db.query(VillageRequirement).filter(
        VillageRequirement.village_id == village_id
    ).all()

    # A village without configured requirements cannot safely be marked ready.
    if not requirements:
        return {
            "village_id": village_id,
            "village_name": village.name,
            "is_ready": False,
            "reason": "Village requirements are not configured",
            "missing_commodities": [],
            "insufficient_stock": [],
            "seasonally_suspended": [],
        }

    missing = []
    insufficient_stock = []
    suspended = []

    for requirement in requirements:
        commodity = requirement.commodity

        status_row = db.query(CommodityStatus).filter(
            CommodityStatus.shop_id == village.shop_id,
            CommodityStatus.commodity_id == requirement.commodity_id,
        ).first()
        status_value = status_row.status if status_row else "active"

        if status_value.strip().lower() == "seasonally_suspended":
            suspended.append({
                "commodity_id": commodity.id,
                "commodity_name": commodity.name,
                "required_quantity": requirement.quantity,
            })
            continue

        stock_entry = db.query(Stock).filter(
            Stock.shop_id == village.shop_id,
            Stock.commodity_id == requirement.commodity_id,
        ).first()

        if status_value.strip().lower() == "out_of_stock" or not stock_entry:
            missing.append({
                "commodity_id": commodity.id,
                "commodity_name": commodity.name,
                "required_quantity": requirement.quantity,
                "available_quantity": 0,
            })
            continue

        if stock_entry.quantity < requirement.quantity:
            insufficient_stock.append({
                "commodity_id": commodity.id,
                "commodity_name": commodity.name,
                "required_quantity": requirement.quantity,
                "available_quantity": stock_entry.quantity,
            })

    is_ready = not missing and not insufficient_stock and not suspended

    return {
        "village_id": village_id,
        "village_name": village.name,
        "shop_id": village.shop_id,
        "is_ready": is_ready,
        "missing_commodities": missing,
        "insufficient_stock": insufficient_stock,
        "seasonally_suspended": suspended,
    }


@app.get("/shops/{shop_id}/next-village")
def get_next_village(
    shop_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if current_user.role in ("officer", "beneficiary"):
        if current_user.shop_id != shop_id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="User can only view scheduling for their assigned shop",
            )
    elif current_user.role != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Not authorized to view village scheduling",
        )

    shop = db.query(Shop).filter(Shop.id == shop_id).first()
    if not shop:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Shop not found",
        )

    villages = db.query(Village).filter(Village.shop_id == shop_id).all()
    ready_villages = []

    for village in villages:
        requirements = db.query(VillageRequirement).filter(
            VillageRequirement.village_id == village.id
        ).all()
        if not requirements:
            continue

        # Evaluate readiness directly so scheduling never depends on an HTTP call.
        ready = True
        for requirement in requirements:
            status_row = db.query(CommodityStatus).filter(
                CommodityStatus.shop_id == shop_id,
                CommodityStatus.commodity_id == requirement.commodity_id,
            ).first()
            status_value = status_row.status if status_row else "active"

            stock_entry = db.query(Stock).filter(
                Stock.shop_id == shop_id,
                Stock.commodity_id == requirement.commodity_id,
            ).first()

            if (
                _is_blocking_commodity_status(status_value)
                or not stock_entry
                or stock_entry.quantity < requirement.quantity
            ):
                ready = False
                break

        if ready:
            ready_villages.append(village)

    if not ready_villages:
        return {"message": "No village is currently ready for distribution"}

    ready_villages.sort(
        key=lambda v: (
            v.last_distribution_date is not None,
            v.last_distribution_date or "",
            v.id,
        )
    )

    next_village = ready_villages[0]
    return {
        "village_id": next_village.id,
        "village_name": next_village.name,
        "last_distribution_date": next_village.last_distribution_date,
    }


@app.post("/villages/mark-distributed")
def mark_distributed(
    item: MarkDistributed,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Complete a distribution and deduct the village requirements from stock."""
    if current_user.role != "officer":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only officers can mark distribution complete",
        )

    village = db.query(Village).filter(Village.id == item.village_id).first()
    if not village:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Village not found",
        )

    if village.shop_id != current_user.shop_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Officer can only distribute for their assigned shop",
        )

    requirements = db.query(VillageRequirement).filter(
        VillageRequirement.village_id == village.id
    ).all()
    if not requirements:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Village requirements are not configured",
        )

    # Re-check everything immediately before changing stock.
    # This prevents a village from being distributed using stale readiness data.
    for requirement in requirements:
        status_row = db.query(CommodityStatus).filter(
            CommodityStatus.shop_id == village.shop_id,
            CommodityStatus.commodity_id == requirement.commodity_id,
        ).first()
        status_value = status_row.status if status_row else "active"

        if _is_blocking_commodity_status(status_value):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=(
                    f"Cannot distribute: commodity '{requirement.commodity.name}' "
                    f"is {status_value}"
                ),
            )

        stock_entry = db.query(Stock).filter(
            Stock.shop_id == village.shop_id,
            Stock.commodity_id == requirement.commodity_id,
        ).first()

        if not stock_entry or stock_entry.quantity < requirement.quantity:
            available = stock_entry.quantity if stock_entry else 0
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=(
                    f"Insufficient stock for '{requirement.commodity.name}': "
                    f"required {requirement.quantity}, available {available}"
                ),
            )

    # All checks passed, so deduct every required commodity in one transaction.
    distributed_items = []
    for requirement in requirements:
        stock_entry = db.query(Stock).filter(
            Stock.shop_id == village.shop_id,
            Stock.commodity_id == requirement.commodity_id,
        ).first()

        stock_entry.quantity -= requirement.quantity
        remaining = stock_entry.quantity
        distributed_items.append({
            "commodity_id": requirement.commodity_id,
            "commodity_name": requirement.commodity.name,
            "distributed_quantity": requirement.quantity,
            "remaining_stock": remaining,
        })

        # Keep an auditable historical record for demand forecasting.
        db.add(DistributionHistory(
            shop_id=village.shop_id,
            commodity_id=requirement.commodity_id,
            village_id=village.id,
            quantity=requirement.quantity,
            distribution_date=str(date.today()),
        ))

        # The existing Stock table enforces quantity > 0, so remove a row when
        # distribution consumes its entire balance.
        if remaining == 0:
            db.delete(stock_entry)

    village.last_distribution_date = str(date.today())
    db.commit()
    db.refresh(village)

    return {
        "message": "Distribution completed and stock updated",
        "village_id": village.id,
        "village_name": village.name,
        "distribution_date": village.last_distribution_date,
        "distributed_items": distributed_items,
    }
