from sqlalchemy import (
    Column,
    Integer,
    String,
    Float,
    ForeignKey,
    UniqueConstraint,
    CheckConstraint,
)
from sqlalchemy.orm import relationship
from database import Base


class Shop(Base):
    __tablename__ = "shops"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, index=True)
    location = Column(String)

    villages = relationship("Village", back_populates="shop")
    stock_items = relationship("Stock", back_populates="shop")


class Village(Base):
    __tablename__ = "villages"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, index=True)
    shop_id = Column(Integer, ForeignKey("shops.id"))

    shop = relationship("Shop", back_populates="villages")
    requirements = relationship(
        "VillageRequirement",
        back_populates="village",
        cascade="all, delete-orphan",
    )
    last_distribution_date = Column(
        String,
        nullable=True,
    )  # e.g. "2026-06-10"


class Commodity(Base):
    __tablename__ = "commodities"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, unique=True, index=True)

    stock_items = relationship("Stock", back_populates="commodity")
    village_requirements = relationship(
        "VillageRequirement",
        back_populates="commodity",
    )


class Stock(Base):
    __tablename__ = "stock"
    __table_args__ = (
        UniqueConstraint("shop_id", "commodity_id", name="uq_stock_shop_commodity"),
        CheckConstraint("quantity > 0", name="ck_stock_quantity_positive"),
    )

    id = Column(Integer, primary_key=True, index=True)
    quantity = Column(Float, nullable=False)

    shop_id = Column(Integer, ForeignKey("shops.id"), nullable=False)
    commodity_id = Column(Integer, ForeignKey("commodities.id"), nullable=False)

    shop = relationship("Shop", back_populates="stock_items")
    commodity = relationship("Commodity", back_populates="stock_items")


class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)
    username = Column(String, unique=True, index=True)
    hashed_password = Column(String)
    role = Column(String)  # "beneficiary", "officer", or "admin"
    shop_id = Column(Integer, ForeignKey("shops.id"), nullable=True)


class CommodityStatus(Base):
    __tablename__ = "commodity_status"
    __table_args__ = (
        UniqueConstraint("shop_id", "commodity_id", name="uq_status_shop_commodity"),
    )

    id = Column(Integer, primary_key=True, index=True)
    shop_id = Column(Integer, ForeignKey("shops.id"), nullable=False)
    commodity_id = Column(Integer, ForeignKey("commodities.id"), nullable=False)
    status = Column(String, default="active", nullable=False)
    reason = Column(String, nullable=True)

    shop = relationship("Shop")
    commodity = relationship("Commodity")


class DistributionHistory(Base):
    """Historical quantity distributed for demand forecasting."""

    __tablename__ = "distribution_history"

    id = Column(Integer, primary_key=True, index=True)
    shop_id = Column(Integer, ForeignKey("shops.id"), nullable=False)
    village_id = Column(Integer, ForeignKey("villages.id"), nullable=True)
    commodity_id = Column(Integer, ForeignKey("commodities.id"), nullable=False)
    quantity = Column(Float, nullable=False)
    distribution_date = Column(String, nullable=False)

    shop = relationship("Shop")
    village = relationship("Village")
    commodity = relationship("Commodity")


class VillageRequirement(Base):
    """Commodity quantity required by a village for its next distribution."""

    __tablename__ = "village_requirements"
    __table_args__ = (
        UniqueConstraint(
            "village_id",
            "commodity_id",
            name="uq_village_requirement_village_commodity",
        ),
        CheckConstraint("quantity > 0", name="ck_village_requirement_quantity_positive"),
    )

    id = Column(Integer, primary_key=True, index=True)
    village_id = Column(Integer, ForeignKey("villages.id"), nullable=False)
    commodity_id = Column(Integer, ForeignKey("commodities.id"), nullable=False)
    quantity = Column(Float, nullable=False)

    village = relationship("Village", back_populates="requirements")
    commodity = relationship("Commodity", back_populates="village_requirements")
