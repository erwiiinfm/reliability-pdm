"""Initial schema with TimescaleDB hypertable for ECM records

Revision ID: 001
Create Date: 2026-06-15
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID, JSONB

revision = "001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    # TimescaleDB extension
    op.execute("CREATE EXTENSION IF NOT EXISTS timescaledb CASCADE;")

    # --- organizations ---
    op.create_table(
        "organizations",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("code", sa.String(50), unique=True, nullable=False),
        sa.Column("contact_info", JSONB),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )

    # --- sites ---
    op.create_table(
        "sites",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("organization_id", UUID(as_uuid=True), sa.ForeignKey("organizations.id"), nullable=False),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("code", sa.String(50), nullable=False),
        sa.Column("location", sa.String(500)),
        sa.Column("timezone", sa.String(50), default="Asia/Jakarta"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )

    # --- units ---
    op.create_table(
        "units",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("site_id", UUID(as_uuid=True), sa.ForeignKey("sites.id"), nullable=False),
        sa.Column("unit_type", sa.String(50), nullable=False),
        sa.Column("serial_number", sa.String(100), unique=True, nullable=False),
        sa.Column("model", sa.String(200), nullable=False),
        sa.Column("manufacturer", sa.String(100), nullable=False),
        sa.Column("manufacture_year", sa.Integer),
        sa.Column("fleet_number", sa.String(50)),
        sa.Column("commissioned_at", sa.DateTime(timezone=True)),
        sa.Column("total_hours", sa.Float),
        sa.Column("is_active", sa.Boolean, default=True),
        sa.Column("health_status", sa.String(20), default="good"),
        sa.Column("health_score", sa.Float),
        sa.Column("health_updated_at", sa.DateTime(timezone=True)),
        sa.Column("specs", JSONB),
        sa.Column("operating_context", JSONB),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )

    # --- components ---
    op.create_table(
        "components",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("unit_id", UUID(as_uuid=True), sa.ForeignKey("units.id"), nullable=False),
        sa.Column("parent_component_id", UUID(as_uuid=True), sa.ForeignKey("components.id")),
        sa.Column("category", sa.String(50), nullable=False),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("part_number", sa.String(100)),
        sa.Column("serial_number", sa.String(100)),
        sa.Column("installed_at", sa.DateTime(timezone=True)),
        sa.Column("installed_hours", sa.Float),
        sa.Column("expected_life_hours", sa.Float),
        sa.Column("health_status", sa.String(20), default="good"),
        sa.Column("health_score", sa.Float),
        sa.Column("health_updated_at", sa.DateTime(timezone=True)),
        sa.Column("specs", JSONB),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )

    # --- ecm_records (will become hypertable) ---
    op.create_table(
        "ecm_records",
        sa.Column("id", sa.Integer, autoincrement=True, nullable=False),
        sa.Column("unit_id", UUID(as_uuid=True), sa.ForeignKey("units.id"), nullable=False),
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("smu_hours", sa.Float),
        sa.Column("engine_rpm", sa.Float),
        sa.Column("engine_load_pct", sa.Float),
        sa.Column("engine_oil_pressure", sa.Float),
        sa.Column("engine_oil_temp", sa.Float),
        sa.Column("engine_coolant_temp", sa.Float),
        sa.Column("boost_pressure", sa.Float),
        sa.Column("fuel_rate", sa.Float),
        sa.Column("fuel_pressure", sa.Float),
        sa.Column("exhaust_temp", sa.Float),
        sa.Column("hydraulic_oil_temp", sa.Float),
        sa.Column("hydraulic_pressure", sa.Float),
        sa.Column("transmission_oil_temp", sa.Float),
        sa.Column("extra", JSONB),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    # TimescaleDB requires partition col in primary key — use composite PK
    op.execute("ALTER TABLE ecm_records ADD PRIMARY KEY (id, recorded_at);")
    op.create_index("ix_ecm_unit_time", "ecm_records", ["unit_id", "recorded_at"])

    # Convert to TimescaleDB hypertable partitioned by time
    op.execute(
        "SELECT create_hypertable('ecm_records', 'recorded_at', chunk_time_interval => INTERVAL '1 day');"
    )

    # --- diagnosis_library ---
    op.create_table(
        "diagnosis_library",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("component_category", sa.String(50), nullable=False),
        sa.Column("fault_code", sa.String(100), nullable=False),
        sa.Column("fault_name", sa.String(300), nullable=False),
        sa.Column("fault_severity", sa.String(20), nullable=False),
        sa.Column("description", sa.Text),
        sa.Column("symptom_rules", JSONB, nullable=False),
        sa.Column("recommended_actions", JSONB, nullable=False),
        sa.Column("reference_source", sa.String(300)),
        sa.Column("confidence_weight", sa.Float, default=1.0),
        sa.Column("is_active", sa.Boolean, default=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )

    # --- diagnosis_cases ---
    op.create_table(
        "diagnosis_cases",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("unit_id", UUID(as_uuid=True), sa.ForeignKey("units.id"), nullable=False),
        sa.Column("component_id", UUID(as_uuid=True), sa.ForeignKey("components.id"), nullable=False),
        sa.Column("library_entry_id", UUID(as_uuid=True), sa.ForeignKey("diagnosis_library.id")),
        sa.Column("detected_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("smu_at_detection", sa.Float),
        sa.Column("fault_name", sa.String(300), nullable=False),
        sa.Column("fault_severity", sa.String(20), nullable=False),
        sa.Column("health_impact", sa.String(20), nullable=False),
        sa.Column("confidence_score", sa.Float, nullable=False),
        sa.Column("evidence_summary", JSONB, nullable=False),
        sa.Column("recommended_actions", JSONB),
        sa.Column("estimated_rul_hours", sa.Float),
        sa.Column("is_acknowledged", sa.Boolean, default=False),
        sa.Column("acknowledged_by", sa.String(200)),
        sa.Column("acknowledged_at", sa.DateTime(timezone=True)),
        sa.Column("is_resolved", sa.Boolean, default=False),
        sa.Column("resolved_at", sa.DateTime(timezone=True)),
        sa.Column("outcome", sa.String(30), default="pending"),
        sa.Column("outcome_notes", sa.Text),
        sa.Column("outcome_validated_by", sa.String(200)),
        sa.Column("outcome_validated_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )

    # --- confidence_matrix ---
    op.create_table(
        "confidence_matrix",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("library_entry_id", UUID(as_uuid=True), sa.ForeignKey("diagnosis_library.id"), unique=True, nullable=False),
        sa.Column("period_start", sa.DateTime(timezone=True)),
        sa.Column("period_end", sa.DateTime(timezone=True)),
        sa.Column("total_cases", sa.Integer, default=0),
        sa.Column("true_positives", sa.Integer, default=0),
        sa.Column("false_positives", sa.Integer, default=0),
        sa.Column("true_negatives", sa.Integer, default=0),
        sa.Column("false_negatives", sa.Integer, default=0),
        sa.Column("pending", sa.Integer, default=0),
        sa.Column("precision", sa.Float),
        sa.Column("recall", sa.Float),
        sa.Column("f1_score", sa.Float),
        sa.Column("accuracy", sa.Float),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )

    # --- oil_samples ---
    op.create_table(
        "oil_samples",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("unit_id", UUID(as_uuid=True), sa.ForeignKey("units.id"), nullable=False),
        sa.Column("component_id", UUID(as_uuid=True), sa.ForeignKey("components.id")),
        sa.Column("sampled_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("smu_at_sample", sa.Float),
        sa.Column("lab_reference", sa.String(100)),
        sa.Column("iron_fe", sa.Float), sa.Column("copper_cu", sa.Float),
        sa.Column("aluminum_al", sa.Float), sa.Column("chromium_cr", sa.Float),
        sa.Column("lead_pb", sa.Float), sa.Column("tin_sn", sa.Float),
        sa.Column("nickel_ni", sa.Float), sa.Column("silicon_si", sa.Float),
        sa.Column("viscosity_40", sa.Float), sa.Column("viscosity_100", sa.Float),
        sa.Column("tan", sa.Float), sa.Column("tbn", sa.Float),
        sa.Column("water_pct", sa.Float), sa.Column("soot_pct", sa.Float),
        sa.Column("severity", sa.String(20)),
        sa.Column("notes", sa.Text),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )

    # --- photo_inspections ---
    op.create_table(
        "photo_inspections",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("unit_id", UUID(as_uuid=True), sa.ForeignKey("units.id"), nullable=False),
        sa.Column("component_id", UUID(as_uuid=True), sa.ForeignKey("components.id")),
        sa.Column("photo_type", sa.String(50), nullable=False),
        sa.Column("taken_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("smu_at_photo", sa.Float),
        sa.Column("storage_key", sa.String(500), nullable=False),
        sa.Column("thumbnail_key", sa.String(500)),
        sa.Column("ai_severity", sa.String(20)),
        sa.Column("ai_confidence", sa.Float),
        sa.Column("ai_findings", JSONB),
        sa.Column("ai_model_version", sa.String(50)),
        sa.Column("human_severity", sa.String(20)),
        sa.Column("human_notes", sa.Text),
        sa.Column("validated_by", sa.String(200)),
        sa.Column("validated_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )

    # --- oil_topups ---
    op.create_table(
        "oil_topups",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("unit_id", UUID(as_uuid=True), sa.ForeignKey("units.id"), nullable=False),
        sa.Column("component_id", UUID(as_uuid=True), sa.ForeignKey("components.id")),
        sa.Column("topped_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("smu_at_topup", sa.Float),
        sa.Column("volume_liters", sa.Float, nullable=False),
        sa.Column("oil_grade", sa.String(100)),
        sa.Column("reason", sa.String(500)),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )

    # --- maintenance_records ---
    op.create_table(
        "maintenance_records",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("unit_id", UUID(as_uuid=True), sa.ForeignKey("units.id"), nullable=False),
        sa.Column("component_id", UUID(as_uuid=True), sa.ForeignKey("components.id")),
        sa.Column("performed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("smu_at_service", sa.Float),
        sa.Column("work_order", sa.String(100)),
        sa.Column("maintenance_type", sa.String(100), nullable=False),
        sa.Column("description", sa.Text),
        sa.Column("parts_replaced", JSONB),
        sa.Column("technician", sa.String(200)),
        sa.Column("labor_hours", sa.Float),
        sa.Column("cost", sa.Float),
        sa.Column("triggered_by_diagnosis_id", UUID(as_uuid=True), sa.ForeignKey("diagnosis_cases.id")),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )

    # --- measurements ---
    op.create_table(
        "measurements",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("unit_id", UUID(as_uuid=True), sa.ForeignKey("units.id"), nullable=False),
        sa.Column("component_id", UUID(as_uuid=True), sa.ForeignKey("components.id")),
        sa.Column("measurement_type", sa.String(50), nullable=False),
        sa.Column("measured_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("smu_at_measure", sa.Float),
        sa.Column("value", sa.Float),
        sa.Column("unit_of_measure", sa.String(50)),
        sa.Column("location_point", sa.String(200)),
        sa.Column("storage_key", sa.String(500)),
        sa.Column("raw_data", JSONB),
        sa.Column("severity", sa.String(20)),
        sa.Column("notes", sa.Text),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )


def downgrade() -> None:
    op.drop_table("measurements")
    op.drop_table("maintenance_records")
    op.drop_table("oil_topups")
    op.drop_table("photo_inspections")
    op.drop_table("oil_samples")
    op.drop_table("confidence_matrix")
    op.drop_table("diagnosis_cases")
    op.drop_table("diagnosis_library")
    op.drop_table("ecm_records")
    op.drop_table("components")
    op.drop_table("units")
    op.drop_table("sites")
    op.drop_table("organizations")
