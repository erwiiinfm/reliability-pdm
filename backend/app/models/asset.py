"""
Asset hierarchy: Organization → Site → Unit → Component → SubComponent
Each level tracks its own health status independently.
"""
import uuid
import enum
from datetime import datetime
from typing import Optional, List
from sqlalchemy import String, Integer, Float, ForeignKey, Enum as SAEnum, Text, Boolean, DateTime
from sqlalchemy.dialects.postgresql import UUID, JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .base import Base, UUIDMixin, TimestampMixin


class HealthStatus(str, enum.Enum):
    GOOD = "good"           # Green  — operating within all normal parameters
    NORMAL = "normal"       # Yellow — minor deviations, monitor closely
    CRITICAL = "critical"   # Orange — significant anomaly, plan maintenance
    EXTREME = "extreme"     # Red    — immediate action required


class UnitType(str, enum.Enum):
    EXCAVATOR = "excavator"
    DUMP_TRUCK = "dump_truck"
    BULLDOZER = "bulldozer"
    GRADER = "grader"
    COMPACTOR = "compactor"
    CRANE = "crane"
    LOADER = "loader"
    GENSET = "genset"
    PUMP = "pump"
    COMPRESSOR = "compressor"
    OTHER = "other"


class ComponentCategory(str, enum.Enum):
    ENGINE = "engine"
    HYDRAULIC = "hydraulic"
    TRANSMISSION = "transmission"
    FINAL_DRIVE = "final_drive"
    UNDERCARRIAGE = "undercarriage"
    COOLING = "cooling"
    ELECTRICAL = "electrical"
    STRUCTURAL = "structural"
    OTHER = "other"


# ---------------------------------------------------------------------------
# Organization & Site
# ---------------------------------------------------------------------------

class Organization(Base, UUIDMixin, TimestampMixin):
    __tablename__ = "organizations"

    name: Mapped[str] = mapped_column(String(200), nullable=False)
    code: Mapped[str] = mapped_column(String(50), unique=True, nullable=False)
    contact_info: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)

    sites: Mapped[List["Site"]] = relationship(back_populates="organization")


class Site(Base, UUIDMixin, TimestampMixin):
    __tablename__ = "sites"

    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    code: Mapped[str] = mapped_column(String(50), nullable=False)
    location: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    timezone: Mapped[str] = mapped_column(String(50), default="Asia/Jakarta")

    organization: Mapped["Organization"] = relationship(back_populates="sites")
    units: Mapped[List["Unit"]] = relationship(back_populates="site")


# ---------------------------------------------------------------------------
# Unit (individual machine / equipment)
# ---------------------------------------------------------------------------

class Unit(Base, UUIDMixin, TimestampMixin):
    __tablename__ = "units"

    site_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("sites.id"), nullable=False
    )
    unit_type: Mapped[UnitType] = mapped_column(SAEnum(UnitType), nullable=False)

    # Identity
    serial_number: Mapped[str] = mapped_column(String(100), unique=True, nullable=False)
    model: Mapped[str] = mapped_column(String(200), nullable=False)       # e.g. "CAT 390F"
    manufacturer: Mapped[str] = mapped_column(String(100), nullable=False) # e.g. "Caterpillar"
    manufacture_year: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    fleet_number: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)

    # Operational
    commissioned_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    total_hours: Mapped[Optional[float]] = mapped_column(Float, nullable=True)   # current SMU / HM
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    # Overall health — aggregated from worst component status
    health_status: Mapped[HealthStatus] = mapped_column(
        SAEnum(HealthStatus), default=HealthStatus.GOOD, nullable=False
    )
    health_score: Mapped[Optional[float]] = mapped_column(Float, nullable=True)  # 0-100
    health_updated_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    # OEM specs & operating context stored as flexible JSON
    specs: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)
    operating_context: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)
    # e.g. {"application": "mining", "material": "overburden", "avg_daily_hours": 20}

    site: Mapped["Site"] = relationship(back_populates="units")
    components: Mapped[List["Component"]] = relationship(back_populates="unit", cascade="all, delete-orphan")
    ecm_records: Mapped[List["ECMRecord"]] = relationship(back_populates="unit")
    oil_samples: Mapped[List["OilSample"]] = relationship(back_populates="unit")
    maintenance_records: Mapped[List["MaintenanceRecord"]] = relationship(back_populates="unit")
    photo_inspections: Mapped[List["PhotoInspection"]] = relationship(back_populates="unit")
    oil_topups: Mapped[List["OilTopUp"]] = relationship(back_populates="unit")


# ---------------------------------------------------------------------------
# Component (major system within a unit)
# ---------------------------------------------------------------------------

class Component(Base, UUIDMixin, TimestampMixin):
    __tablename__ = "components"

    unit_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("units.id"), nullable=False
    )
    parent_component_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("components.id"), nullable=True
    )  # allows sub-components (e.g. Engine → Turbocharger)

    category: Mapped[ComponentCategory] = mapped_column(SAEnum(ComponentCategory), nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    # e.g. "Main Engine", "Swing Hydraulic Pump", "Left Final Drive"

    # Part identity (when replaced, create new component record)
    part_number: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    serial_number: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    installed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    installed_hours: Mapped[Optional[float]] = mapped_column(Float, nullable=True)  # unit SMU at install
    expected_life_hours: Mapped[Optional[float]] = mapped_column(Float, nullable=True)  # OEM spec

    health_status: Mapped[HealthStatus] = mapped_column(
        SAEnum(HealthStatus), default=HealthStatus.GOOD, nullable=False
    )
    health_score: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    health_updated_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    specs: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)

    unit: Mapped["Unit"] = relationship(back_populates="components")
    sub_components: Mapped[List["Component"]] = relationship(
        "Component", back_populates="parent_component"
    )
    parent_component: Mapped[Optional["Component"]] = relationship(
        "Component", back_populates="sub_components", remote_side="Component.id"
    )
    diagnoses: Mapped[List["DiagnosisCase"]] = relationship(back_populates="component")
