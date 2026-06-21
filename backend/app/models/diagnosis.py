"""
Diagnosis library and case tracking.
Library: curated symptom → fault → recommendation rules (OEM/ISO-based).
Case: actual diagnosis instances with confidence scoring and outcome validation.
Confidence matrix tracks true/false positive/negative to measure logic sharpness.
"""
import uuid
import enum
from datetime import datetime
from typing import Optional, List
from sqlalchemy import String, Float, ForeignKey, Enum as SAEnum, Text, Boolean, DateTime, Integer
from sqlalchemy.dialects.postgresql import UUID, JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .base import Base, UUIDMixin, TimestampMixin
from .asset import HealthStatus, ComponentCategory


class FaultSeverity(str, enum.Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class ConfidenceOutcome(str, enum.Enum):
    PENDING = "pending"           # not yet validated
    TRUE_POSITIVE = "true_positive"   # predicted fault, fault confirmed
    FALSE_POSITIVE = "false_positive"  # predicted fault, no fault found
    TRUE_NEGATIVE = "true_negative"    # predicted OK, was OK
    FALSE_NEGATIVE = "false_negative"  # predicted OK, fault existed


# ---------------------------------------------------------------------------
# Diagnosis Library (the knowledge base)
# ---------------------------------------------------------------------------

class DiagnosisLibraryEntry(Base, UUIDMixin, TimestampMixin):
    """
    Curated rule: if symptoms match → probable fault → recommended action.
    Built incrementally; each entry can be activated/deactivated.
    Validated entries gain confidence weight over time.
    """
    __tablename__ = "diagnosis_library"

    component_category: Mapped[ComponentCategory] = mapped_column(
        SAEnum(ComponentCategory), nullable=False, index=True
    )
    fault_code: Mapped[str] = mapped_column(String(100), nullable=False)
    fault_name: Mapped[str] = mapped_column(String(300), nullable=False)
    # e.g. "Engine Bearing Wear", "Hydraulic Pump Internal Leak"

    fault_severity: Mapped[FaultSeverity] = mapped_column(SAEnum(FaultSeverity), nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    # Symptom triggers — stored as structured JSON for flexible matching
    symptom_rules: Mapped[dict] = mapped_column(JSONB, nullable=False)
    # Example:
    # {
    #   "operator": "AND",
    #   "conditions": [
    #     {"source": "oil_sample", "field": "iron_fe", "op": "gt", "threshold": 150, "trend": "rising"},
    #     {"source": "oil_sample", "field": "copper_cu", "op": "gt", "threshold": 20},
    #     {"source": "ecm", "field": "engine_oil_pressure", "op": "lt", "threshold": 250, "trend": "falling"}
    #   ]
    # }

    recommended_actions: Mapped[dict] = mapped_column(JSONB, nullable=False)
    # [
    #   {"priority": 1, "action": "Inspect bearing clearance", "urgency_hours": 100},
    #   {"priority": 2, "action": "Schedule oil flush", "urgency_hours": 250}
    # ]

    # Source / reference
    reference_source: Mapped[Optional[str]] = mapped_column(String(300), nullable=True)
    # e.g. "CAT SIS 390F", "ISO 4406", "ASTM D7844", "internal experience"
    confidence_weight: Mapped[float] = mapped_column(Float, default=1.0)
    # multiplier based on historical validation accuracy (updated by feedback loop)

    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    cases: Mapped[List["DiagnosisCase"]] = relationship(back_populates="library_entry")


# ---------------------------------------------------------------------------
# Diagnosis Case (actual detection event)
# ---------------------------------------------------------------------------

class DiagnosisCase(Base, UUIDMixin, TimestampMixin):
    """
    One detected diagnosis event on a specific component.
    Tracks confidence, outcome validation, and links to evidence.
    """
    __tablename__ = "diagnosis_cases"

    unit_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("units.id"), nullable=False)
    component_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("components.id"), nullable=False)
    library_entry_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("diagnosis_library.id"), nullable=True
    )  # null = AI-generated, not yet in library

    detected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    smu_at_detection: Mapped[Optional[float]] = mapped_column(Float, nullable=True)

    fault_name: Mapped[str] = mapped_column(String(300), nullable=False)
    fault_severity: Mapped[FaultSeverity] = mapped_column(SAEnum(FaultSeverity), nullable=False)
    health_impact: Mapped[HealthStatus] = mapped_column(SAEnum(HealthStatus), nullable=False)

    # Confidence scoring
    confidence_score: Mapped[float] = mapped_column(Float, nullable=False)  # 0.0–1.0
    # Breakdown of which evidence contributed
    evidence_summary: Mapped[dict] = mapped_column(JSONB, nullable=False)
    # {
    #   "triggered_conditions": ["iron_fe > 150 (actual: 178)", "oil_pressure declining trend"],
    #   "data_sources": ["oil_sample:uuid", "ecm_trend:last_30d"],
    #   "model": "rule_engine_v1"
    # }

    recommended_actions: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)
    estimated_rul_hours: Mapped[Optional[float]] = mapped_column(Float, nullable=True)  # remaining useful life

    # Status tracking
    is_acknowledged: Mapped[bool] = mapped_column(Boolean, default=False)
    acknowledged_by: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)
    acknowledged_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    is_resolved: Mapped[bool] = mapped_column(Boolean, default=False)
    resolved_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    # Outcome validation (filled after maintenance)
    outcome: Mapped[ConfidenceOutcome] = mapped_column(
        SAEnum(ConfidenceOutcome), default=ConfidenceOutcome.PENDING
    )
    outcome_notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    outcome_validated_by: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)
    outcome_validated_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    component: Mapped["Component"] = relationship(back_populates="diagnoses")
    library_entry: Mapped[Optional["DiagnosisLibraryEntry"]] = relationship(back_populates="cases")


# ---------------------------------------------------------------------------
# Confidence Matrix (aggregated per library entry / model)
# ---------------------------------------------------------------------------

class ConfidenceMatrix(Base, UUIDMixin, TimestampMixin):
    """
    Aggregated accuracy metrics per diagnosis library entry.
    Updated by feedback loop whenever an outcome is validated.
    Used to weight and rank reliability of each rule.
    """
    __tablename__ = "confidence_matrix"

    library_entry_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("diagnosis_library.id"), unique=True, nullable=False
    )
    period_start: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    period_end: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    total_cases: Mapped[int] = mapped_column(Integer, default=0)
    true_positives: Mapped[int] = mapped_column(Integer, default=0)
    false_positives: Mapped[int] = mapped_column(Integer, default=0)
    true_negatives: Mapped[int] = mapped_column(Integer, default=0)
    false_negatives: Mapped[int] = mapped_column(Integer, default=0)
    pending: Mapped[int] = mapped_column(Integer, default=0)

    # Derived metrics (computed and stored for fast dashboard query)
    precision: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    # TP / (TP + FP)
    recall: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    # TP / (TP + FN)
    f1_score: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    accuracy: Mapped[Optional[float]] = mapped_column(Float, nullable=True)

    library_entry: Mapped["DiagnosisLibraryEntry"] = relationship()
