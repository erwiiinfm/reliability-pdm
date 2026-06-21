"""
All input measurement types. Each maps to a component or unit.
ECMRecord uses TimescaleDB hypertable for high-throughput time-series.
"""
import uuid
import enum
from datetime import datetime
from typing import Optional
from sqlalchemy import String, Float, ForeignKey, Enum as SAEnum, Text, Boolean, DateTime, Integer
from sqlalchemy.dialects.postgresql import UUID, JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .base import Base, UUIDMixin, TimestampMixin
from .asset import HealthStatus


class PhotoType(str, enum.Enum):
    MAGNETIC_PLUG = "magnetic_plug"
    FILTER_CUT = "filter_cut"
    VISUAL_INSPECTION = "visual_inspection"
    COMPONENT_PHOTO = "component_photo"


# ---------------------------------------------------------------------------
# ECM / Sensor Data  (high-volume, converted to hypertable)
# ---------------------------------------------------------------------------

class ECMRecord(Base, TimestampMixin):
    """
    Time-series ECM snapshot from telematics or manual download.
    Will be converted to TimescaleDB hypertable on `recorded_at`.
    No UUID PK — composite (unit_id, recorded_at) for performance.
    """
    __tablename__ = "ecm_records"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    unit_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("units.id"), nullable=False, index=True
    )
    recorded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    smu_hours: Mapped[Optional[float]] = mapped_column(Float, nullable=True)  # service meter unit

    # Engine vitals
    engine_rpm: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    engine_load_pct: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    engine_oil_pressure: Mapped[Optional[float]] = mapped_column(Float, nullable=True)  # kPa
    engine_oil_temp: Mapped[Optional[float]] = mapped_column(Float, nullable=True)      # °C
    engine_coolant_temp: Mapped[Optional[float]] = mapped_column(Float, nullable=True)  # °C
    boost_pressure: Mapped[Optional[float]] = mapped_column(Float, nullable=True)       # kPa
    fuel_rate: Mapped[Optional[float]] = mapped_column(Float, nullable=True)            # L/h
    fuel_pressure: Mapped[Optional[float]] = mapped_column(Float, nullable=True)        # kPa
    exhaust_temp: Mapped[Optional[float]] = mapped_column(Float, nullable=True)         # °C

    # Hydraulic
    hydraulic_oil_temp: Mapped[Optional[float]] = mapped_column(Float, nullable=True)   # °C
    hydraulic_pressure: Mapped[Optional[float]] = mapped_column(Float, nullable=True)   # bar

    # Transmission
    transmission_oil_temp: Mapped[Optional[float]] = mapped_column(Float, nullable=True)  # °C

    # Additional sensors stored as JSON for flexibility
    extra: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)

    unit: Mapped["Unit"] = relationship(back_populates="ecm_records")


# ---------------------------------------------------------------------------
# Oil Sample (lab analysis results)
# ---------------------------------------------------------------------------

class OilSample(Base, UUIDMixin, TimestampMixin):
    __tablename__ = "oil_samples"

    unit_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("units.id"), nullable=False)
    component_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True), ForeignKey("components.id"), nullable=True)
    sampled_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    smu_at_sample: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    lab_reference: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)

    # Wear metals (ppm)
    iron_fe: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    copper_cu: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    aluminum_al: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    chromium_cr: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    lead_pb: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    tin_sn: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    nickel_ni: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    silicon_si: Mapped[Optional[float]] = mapped_column(Float, nullable=True)  # dirt contamination

    # Oil condition
    viscosity_40: Mapped[Optional[float]] = mapped_column(Float, nullable=True)   # cSt at 40°C
    viscosity_100: Mapped[Optional[float]] = mapped_column(Float, nullable=True)  # cSt at 100°C
    tan: Mapped[Optional[float]] = mapped_column(Float, nullable=True)            # total acid number
    tbn: Mapped[Optional[float]] = mapped_column(Float, nullable=True)            # total base number
    water_pct: Mapped[Optional[float]] = mapped_column(Float, nullable=True)      # % water content
    soot_pct: Mapped[Optional[float]] = mapped_column(Float, nullable=True)

    # AI-derived severity from lab values (computed)
    severity: Mapped[Optional[HealthStatus]] = mapped_column(SAEnum(HealthStatus), nullable=True)
    notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    unit: Mapped["Unit"] = relationship(back_populates="oil_samples")


# ---------------------------------------------------------------------------
# Photo Inspection (magnetic plug, filter cut)
# ---------------------------------------------------------------------------

class PhotoInspection(Base, UUIDMixin, TimestampMixin):
    __tablename__ = "photo_inspections"

    unit_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("units.id"), nullable=False)
    component_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True), ForeignKey("components.id"), nullable=True)
    photo_type: Mapped[PhotoType] = mapped_column(SAEnum(PhotoType), nullable=False)
    taken_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    smu_at_photo: Mapped[Optional[float]] = mapped_column(Float, nullable=True)

    # Storage reference (MinIO object key)
    storage_key: Mapped[str] = mapped_column(String(500), nullable=False)
    thumbnail_key: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)

    # AI vision analysis results
    ai_severity: Mapped[Optional[HealthStatus]] = mapped_column(SAEnum(HealthStatus), nullable=True)
    ai_confidence: Mapped[Optional[float]] = mapped_column(Float, nullable=True)  # 0.0–1.0
    ai_findings: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)
    # e.g. {"particles": "metallic_debris", "color": "dark", "quantity": "moderate"}
    ai_model_version: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)

    # Human override / validation
    human_severity: Mapped[Optional[HealthStatus]] = mapped_column(SAEnum(HealthStatus), nullable=True)
    human_notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    validated_by: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)
    validated_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    unit: Mapped["Unit"] = relationship(back_populates="photo_inspections")


# ---------------------------------------------------------------------------
# Oil Top-Up Record
# ---------------------------------------------------------------------------

class OilTopUp(Base, UUIDMixin, TimestampMixin):
    __tablename__ = "oil_topups"

    unit_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("units.id"), nullable=False)
    component_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True), ForeignKey("components.id"), nullable=True)
    topped_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    smu_at_topup: Mapped[Optional[float]] = mapped_column(Float, nullable=True)

    volume_liters: Mapped[float] = mapped_column(Float, nullable=False)
    oil_grade: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    reason: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    # High consumption rate is itself a symptom — tracked by logic engine

    unit: Mapped["Unit"] = relationship(back_populates="oil_topups")


# ---------------------------------------------------------------------------
# Maintenance Record
# ---------------------------------------------------------------------------

class MaintenanceRecord(Base, UUIDMixin, TimestampMixin):
    __tablename__ = "maintenance_records"

    unit_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("units.id"), nullable=False)
    component_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True), ForeignKey("components.id"), nullable=True)
    performed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    smu_at_service: Mapped[Optional[float]] = mapped_column(Float, nullable=True)

    work_order: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    maintenance_type: Mapped[str] = mapped_column(String(100), nullable=False)
    # e.g. "PM 250H", "corrective", "predictive", "overhaul"

    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    parts_replaced: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)
    # [{"part_number": "...", "description": "...", "qty": 1}]
    technician: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)
    labor_hours: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    cost: Mapped[Optional[float]] = mapped_column(Float, nullable=True)

    # Was this triggered by a system diagnosis recommendation?
    triggered_by_diagnosis_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("diagnosis_cases.id"), nullable=True
    )

    unit: Mapped["Unit"] = relationship(back_populates="maintenance_records")


# ---------------------------------------------------------------------------
# Generic Measurement (vibration, thermography, ultrasonic, etc.)
# ---------------------------------------------------------------------------

class MeasurementType(str, enum.Enum):
    VIBRATION = "vibration"
    THERMOGRAPHY = "thermography"
    ULTRASONIC = "ultrasonic"
    NOISE_DB = "noise_db"
    BLOWBY = "blowby"
    OTHER = "other"


class Measurement(Base, UUIDMixin, TimestampMixin):
    __tablename__ = "measurements"

    unit_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("units.id"), nullable=False)
    component_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True), ForeignKey("components.id"), nullable=True)
    measurement_type: Mapped[MeasurementType] = mapped_column(SAEnum(MeasurementType), nullable=False)
    measured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    smu_at_measure: Mapped[Optional[float]] = mapped_column(Float, nullable=True)

    value: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    unit_of_measure: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)  # mm/s, °C, dB
    location_point: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)  # measurement point label

    # For image-based measurements (thermography photos)
    storage_key: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)

    raw_data: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)
    severity: Mapped[Optional[HealthStatus]] = mapped_column(SAEnum(HealthStatus), nullable=True)
    notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
