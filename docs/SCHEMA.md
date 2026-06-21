# Data Model — Predictive Maintenance System

## Entity Hierarchy

```
Organization
  └── Site
        └── Unit  (e.g. CAT 390F, serial: DJB00123)
              ├── Component  (Engine, Hydraulic, Transmission, Final Drive, ...)
              │     └── SubComponent  (Engine → Turbocharger, Engine → Bearings, ...)
              │           └── DiagnosisCase[]
              │
              ├── ECMRecord[]          ← high-volume, TimescaleDB hypertable
              ├── OilSample[]          ← lab results per component
              ├── PhotoInspection[]    ← magnetic plug / filter cut + AI vision
              ├── OilTopUp[]           ← consumption tracking
              ├── MaintenanceRecord[]  ← service history
              └── Measurement[]        ← vibration, thermography, etc.

DiagnosisLibrary   ← curated rules: symptom → fault → recommendation
ConfidenceMatrix   ← accuracy tracking per library rule (TP/FP/TN/FN)
```

## Health Status Matrix

| Status   | Color  | Meaning                                      |
|----------|--------|----------------------------------------------|
| good     | Green  | All parameters within normal range           |
| normal   | Yellow | Minor deviation, monitor closely             |
| critical | Orange | Significant anomaly, schedule maintenance    |
| extreme  | Red    | Immediate action required                    |

Health is tracked independently at **component level**, then aggregated to **unit level** (worst-component wins).

## Key Design Decisions

### ECM Records as Hypertable
`ecm_records` is converted to a TimescaleDB hypertable partitioned daily by `recorded_at`.
This enables millions of rows/day per unit with fast time-range queries and automatic data tiering.

### Flexible Measurement Schema
All sensor types store their core numeric values as columns but also carry an `extra` / `raw_data` JSONB
column so new sensor types can be added without schema migrations.

### Diagnosis Library — Incremental Logic
Rules in `diagnosis_library` are built and validated incrementally:
1. Start with OEM threshold rules (e.g. Fe > 150 ppm = watch, Fe > 250 ppm = critical)
2. Each triggered `diagnosis_case` gets a `confidence_score` (0–1)
3. After maintenance, outcome is recorded (TP/FP/TN/FN)
4. `ConfidenceMatrix` aggregates accuracy; poor-performing rules get lower `confidence_weight`

### Photo Analysis
`photo_inspections` stores MinIO object keys (not binary data).
AI vision model returns `ai_severity` + `ai_findings` JSON. Human validators can override with `human_severity`.

### Oil Consumption as Symptom
`oil_topups` is not just record-keeping — the logic engine monitors **consumption rate** (L per 100 HM)
as a leading indicator for internal leaks or ring wear.
