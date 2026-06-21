"""
SOS Upload API — Blok Data Oil Sampling
POST /api/sos/upload  → parse + preview (belum simpan ke DB)
POST /api/sos/confirm → simpan ke DB setelah user konfirmasi
GET  /api/sos/samples → list samples yang sudah tersimpan
GET  /api/sos/samples/{unit_code} → history per unit
"""

import os, shutil, tempfile, json
from pathlib import Path
from datetime import datetime
from typing import Optional
from fastapi import APIRouter, UploadFile, File, HTTPException, Query
from pydantic import BaseModel
import psycopg2
import psycopg2.extras

from app.services.oil_sample_parser import parse_oil_sample_file, OilSampleRecord
from app.services import onedrive_sync
from app.services.diagnosis import diagnose_sample, generate_ai_summary
import threading as _threading

router = APIRouter(prefix='/api/sos', tags=['Oil Sampling'])

DATABASE_URL = os.getenv('DATABASE_URL', '').replace('+asyncpg', '')

def get_db():
    if not DATABASE_URL:
        return None
    try:
        conn = psycopg2.connect(DATABASE_URL)
        return conn
    except Exception:
        return None

ASSET_REGISTRY = os.getenv(
    'ASSET_REGISTRY_PATH',
    str(Path(__file__).parents[2] / '1. Asset Management.xlsx')
)

# ---------------------------------------------------------------------------
# Response schemas
# ---------------------------------------------------------------------------

class ParsedRecord(BaseModel):
    ptba_unit_code: Optional[str]
    match_method: Optional[str]
    vendor: str
    unit_id_vendor: Optional[str]
    unit_serial: Optional[str]
    component: Optional[str]
    component_raw: Optional[str]
    sampled_at: Optional[datetime]
    smu_hours: Optional[float]
    oil_hours: Optional[float]
    oil_changed: Optional[bool]
    filter_changed: Optional[bool]
    oil_brand: Optional[str]
    oil_grade: Optional[str]
    iron_fe: Optional[float]
    copper_cu: Optional[float]
    aluminum_al: Optional[float]
    chromium_cr: Optional[float]
    lead_pb: Optional[float]
    tin_sn: Optional[float]
    nickel_ni: Optional[float]
    silicon_si: Optional[float]
    sodium_na: Optional[float]
    magnesium_mg: Optional[float]
    molybdenum_mo: Optional[float]
    phosphorus_p: Optional[float]
    zinc_zn: Optional[float]
    calcium_ca: Optional[float]
    boron_b: Optional[float]
    potassium_k: Optional[float]
    barium_ba: Optional[float]
    viscosity_40: Optional[float]
    viscosity_100: Optional[float]
    tan: Optional[float]
    tbn: Optional[float]
    water_pct: Optional[float]
    soot: Optional[float]
    oxidation: Optional[float]
    nitration: Optional[float]
    fuel_dilution_pct: Optional[float]
    glycol: Optional[bool]
    pq_index: Optional[float]
    iso_code: Optional[str]
    vendor_severity: Optional[str]
    vendor_notes: Optional[str]


class UploadPreview(BaseModel):
    session_id: str           # pakai sebagai key waktu confirm
    vendor: str
    filename: str
    total_parsed: int
    total_matched: int
    total_unmatched: int
    match_summary: dict
    warnings: list[str]
    error: Optional[str]
    preview_rows: list[ParsedRecord]   # 20 baris pertama untuk ditampilkan di UI
    severity_summary: dict             # {'extreme': 3, 'critical': 12, ...}
    component_summary: dict            # {'ENGINE': 400, 'HYDRAULIC': 200, ...}
    date_range: Optional[dict]         # {'from': ..., 'to': ...}


class ConfirmRequest(BaseModel):
    session_id: str
    skip_unmatched: bool = False  # simpan semua record termasuk unmatched


class ConfirmResult(BaseModel):
    inserted: int
    skipped_unmatched: int
    skipped_duplicate: int
    errors: list[str]


# ---------------------------------------------------------------------------
# Session store — pickle ke /tmp agar survive hot-reload uvicorn
# ---------------------------------------------------------------------------
import pickle, tempfile as _tf

_SESSION_DIR = Path('/tmp/sos_sessions')
_SESSION_DIR.mkdir(exist_ok=True)

def _session_save(session_id: str, records: list):
    with open(_SESSION_DIR / session_id, 'wb') as f:
        pickle.dump(records, f)

def _session_load(session_id: str):
    p = _SESSION_DIR / session_id
    if not p.exists():
        return None
    with open(p, 'rb') as f:
        return pickle.load(f)

def _session_delete(session_id: str):
    p = _SESSION_DIR / session_id
    if p.exists():
        p.unlink()


# ---------------------------------------------------------------------------
# Auto-insert helper
# ---------------------------------------------------------------------------

def _auto_insert_records(records: list, source_file: str = ''):
    conn = get_db()
    if not conn:
        return
    cur = conn.cursor()
    for r in records:
        try:
            cur.execute(
                'SELECT 1 FROM sos_raw WHERE ptba_unit_code=%s AND component=%s AND sampled_at=%s LIMIT 1',
                (r.ptba_unit_code, r.component, r.sampled_at)
            )
            if cur.fetchone():
                continue
            cur.execute('''
                INSERT INTO sos_raw (
                    ptba_unit_code, unit_id_vendor, unit_serial, vendor, source_file, match_method,
                    component, component_raw, sampled_at, lab_date, lab_reference,
                    smu_hours, oil_hours, oil_changed, filter_changed, oil_brand, oil_grade,
                    fe, cu, al, cr, pb, sn, ni, si, na, mg, mo, p, zn, ca, b, k, ba,
                    viscosity_40, viscosity_100, visc_sae, tan, tbn,
                    water_pct, karl_fischer, soot, oxidation, nitration,
                    fuel_dilution_pct, glycol, pq_index, iso_code,
                    sox, fame, sulphur, dir_trans,
                    particles_4um, particles_6um, particles_15um,
                    location, branch, follow_up,
                    vendor_severity, vendor_notes, raw_data, confirmed
                ) VALUES (
                    %s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,
                    %s,%s,%s,%s,%s,%s,
                    %s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,
                    %s,%s,%s,%s,%s,
                    %s,%s,%s,%s,%s,
                    %s,%s,%s,%s,
                    %s,%s,%s,%s,
                    %s,%s,%s,
                    %s,%s,%s,%s,%s,%s,%s
                )
            ''', (
                r.ptba_unit_code, r.unit_id_vendor, r.unit_serial, r.vendor,
                source_file, r.match_method,
                r.component, r.component_raw, r.sampled_at, r.lab_date, r.lab_reference,
                r.smu_hours, r.oil_hours, r.oil_changed, r.filter_changed,
                r.oil_brand, r.oil_grade,
                r.iron_fe, r.copper_cu, r.aluminum_al, r.chromium_cr, r.lead_pb,
                r.tin_sn, r.nickel_ni, r.silicon_si, r.sodium_na, r.magnesium_mg,
                r.molybdenum_mo, r.phosphorus_p, r.zinc_zn, r.calcium_ca,
                r.boron_b, r.potassium_k, r.barium_ba,
                r.viscosity_40, r.viscosity_100, r.visc_sae, r.tan, r.tbn,
                r.water_pct, r.karl_fischer, r.soot, r.oxidation, r.nitration,
                r.fuel_dilution_pct, r.glycol, r.pq_index, r.iso_code,
                r.sox, r.fame, r.sulphur, r.dir_trans,
                r.particles_4um, r.particles_6um, r.particles_15um,
                r.location, r.branch, r.follow_up,
                r.vendor_severity, r.vendor_notes,
                json.dumps(r.raw_data) if r.raw_data else None,
                True,
            ))
        except Exception:
            pass
    conn.commit()
    cur.close()
    conn.close()


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@router.post('/upload', response_model=UploadPreview, summary='Upload file raw dari vendor')
async def upload_sos_file(file: UploadFile = File(...)):
    """
    Upload file download dari vendor (CSV, XLSX, XLS — format apapun).
    Vendor terdeteksi otomatis dari konten file.
    Kembalikan preview sebelum data disimpan ke database.
    """
    suffix = Path(file.filename).suffix.lower()
    if suffix not in ('.csv', '.xlsx', '.xls'):
        raise HTTPException(400, f'Format tidak didukung: {suffix}. Harus .csv, .xlsx, atau .xls')

    # Simpan sementara
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=suffix)
    try:
        shutil.copyfileobj(file.file, tmp)
        tmp.close()

        result = parse_oil_sample_file(tmp.name, ASSET_REGISTRY if Path(ASSET_REGISTRY).exists() else None)
    finally:
        os.unlink(tmp.name)

    if result.get('error'):
        raise HTTPException(422, result['error'])

    records: list[OilSampleRecord] = result['records']

    # Auto-insert ke database langsung (tidak perlu confirm terpisah)
    import uuid
    session_id = str(uuid.uuid4())
    _session_save(session_id, records)
    _auto_insert_records(records, file.filename)

    # Hitung ringkasan
    sev_summary = {}
    comp_summary = {}
    dates = []
    for r in records:
        sev_summary[r.vendor_severity or 'unknown'] = sev_summary.get(r.vendor_severity or 'unknown', 0) + 1
        comp_summary[r.component or 'OTHER'] = comp_summary.get(r.component or 'OTHER', 0) + 1
        if r.sampled_at:
            dates.append(r.sampled_at)

    date_range = None
    if dates:
        date_range = {'from': str(min(dates).date()), 'to': str(max(dates).date())}

    total_unmatched = sum(1 for r in records if r.match_method == 'unmatched')

    preview = [ParsedRecord(**{
        f: getattr(r, f, None) for f in ParsedRecord.model_fields
    }) for r in records[:20]]

    return UploadPreview(
        session_id=session_id,
        vendor=result['vendor'],
        filename=file.filename,
        total_parsed=len(records),
        total_matched=len(records) - total_unmatched,
        total_unmatched=total_unmatched,
        match_summary=result.get('match_summary', {}),
        warnings=result.get('warnings', []),
        error=None,
        preview_rows=preview,
        severity_summary=sev_summary,
        component_summary=comp_summary,
        date_range=date_range,
    )


@router.post('/confirm', response_model=ConfirmResult, summary='Konfirmasi dan simpan ke database')
async def confirm_upload(req: ConfirmRequest):
    """
    Setelah user review preview, panggil endpoint ini untuk commit ke DB.
    Saat ini DB belum terhubung — akan dikembalikan hasil dry-run.
    """
    records = _session_load(req.session_id); _session_delete(req.session_id)
    if records is None:
        raise HTTPException(404, 'Session tidak ditemukan atau sudah expired. Upload ulang file.')

    to_insert = records
    skipped_unmatched = 0
    if req.skip_unmatched:
        matched = [r for r in records if r.match_method != 'unmatched']
        skipped_unmatched = len(records) - len(matched)
        to_insert = matched

    inserted = 0
    skipped_duplicate = 0
    errors = []

    conn = get_db()
    if conn:
        cur = conn.cursor()
        cur.execute('''
            CREATE TABLE IF NOT EXISTS sos_raw (
                id SERIAL PRIMARY KEY,
                ptba_unit_code TEXT,
                unit_id_vendor TEXT,
                unit_serial TEXT,
                vendor TEXT,
                source_file TEXT,
                match_method TEXT,
                component TEXT,
                component_raw TEXT,
                sampled_at TIMESTAMP,
                lab_date TIMESTAMP,
                lab_reference TEXT,
                smu_hours REAL,
                oil_hours REAL,
                oil_changed BOOLEAN,
                filter_changed BOOLEAN,
                oil_brand TEXT,
                oil_grade TEXT,
                fe REAL, cu REAL, al REAL, cr REAL, pb REAL, sn REAL, ni REAL,
                si REAL, na REAL, mg REAL, mo REAL, p REAL, zn REAL, ca REAL,
                b REAL, k REAL, ba REAL,
                viscosity_40 REAL, viscosity_100 REAL,
                tan REAL, tbn REAL,
                water_pct REAL, karl_fischer REAL, soot REAL, oxidation REAL, nitration REAL,
                fuel_dilution_pct REAL, glycol BOOLEAN, pq_index REAL, iso_code TEXT,
                sox REAL, fame REAL, sulphur REAL, dir_trans REAL,
                particles_4um REAL, particles_6um REAL, particles_15um REAL,
                visc_sae TEXT, location TEXT, branch TEXT, follow_up TEXT,
                vendor_severity TEXT, vendor_notes TEXT,
                raw_data JSONB,
                created_at TIMESTAMP DEFAULT NOW()
            )
        ''')
        cur.execute('''
            CREATE UNIQUE INDEX IF NOT EXISTS ix_sos_dedup
            ON sos_raw (ptba_unit_code, component, sampled_at)
            WHERE ptba_unit_code IS NOT NULL AND component IS NOT NULL AND sampled_at IS NOT NULL
        ''')
        conn.commit()
        for r in to_insert:
            try:
                cur.execute(
                    'SELECT 1 FROM sos_raw WHERE ptba_unit_code=%s AND component=%s AND sampled_at=%s LIMIT 1',
                    (r.ptba_unit_code, r.component, r.sampled_at)
                )
                if cur.fetchone():
                    skipped_duplicate += 1
                    continue
                cur.execute('''
                    INSERT INTO sos_raw (
                        ptba_unit_code, unit_id_vendor, unit_serial, vendor, source_file, match_method,
                        component, component_raw, sampled_at, lab_date, lab_reference,
                        smu_hours, oil_hours, oil_changed, filter_changed, oil_brand, oil_grade,
                        fe, cu, al, cr, pb, sn, ni, si, na, mg, mo, p, zn, ca, b, k, ba,
                        viscosity_40, viscosity_100, visc_sae, tan, tbn,
                        water_pct, karl_fischer, soot, oxidation, nitration,
                        fuel_dilution_pct, glycol, pq_index, iso_code,
                        sox, fame, sulphur, dir_trans,
                        particles_4um, particles_6um, particles_15um,
                        location, branch, follow_up,
                        vendor_severity, vendor_notes, raw_data
                    ) VALUES (
                        %s,%s,%s,%s,%s,%s,
                        %s,%s,%s,%s,%s,
                        %s,%s,%s,%s,%s,%s,
                        %s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,
                        %s,%s,%s,%s,%s,
                        %s,%s,%s,%s,%s,
                        %s,%s,%s,%s,
                        %s,%s,%s,%s,
                        %s,%s,%s,
                        %s,%s,%s,
                        %s,%s,%s
                    )
                ''', (
                    r.ptba_unit_code, r.unit_id_vendor, r.unit_serial, r.vendor,
                    r.source_file, r.match_method,
                    r.component, r.component_raw, r.sampled_at, r.lab_date, r.lab_reference,
                    r.smu_hours, r.oil_hours, r.oil_changed, r.filter_changed,
                    r.oil_brand, r.oil_grade,
                    r.iron_fe, r.copper_cu, r.aluminum_al, r.chromium_cr, r.lead_pb,
                    r.tin_sn, r.nickel_ni, r.silicon_si, r.sodium_na, r.magnesium_mg,
                    r.molybdenum_mo, r.phosphorus_p, r.zinc_zn, r.calcium_ca,
                    r.boron_b, r.potassium_k, r.barium_ba,
                    r.viscosity_40, r.viscosity_100, r.visc_sae, r.tan, r.tbn,
                    r.water_pct, r.karl_fischer, r.soot, r.oxidation, r.nitration,
                    r.fuel_dilution_pct, r.glycol, r.pq_index, r.iso_code,
                    r.sox, r.fame, r.sulphur, r.dir_trans,
                    r.particles_4um, r.particles_6um, r.particles_15um,
                    r.location, r.branch, r.follow_up,
                    r.vendor_severity, r.vendor_notes,
                    json.dumps(r.raw_data) if r.raw_data else None,
                ))
                inserted += 1
            except Exception as e:
                errors.append(str(e)[:200])
        conn.commit()
        cur.close()
        conn.close()
    else:
        inserted = len(to_insert)

    return ConfirmResult(
        inserted=inserted,
        skipped_unmatched=skipped_unmatched,
        skipped_duplicate=skipped_duplicate,
        errors=errors[:10],
    )


@router.get('/sessions/{session_id}/unmatched', summary='Lihat baris yang tidak tercocokkan')
async def get_unmatched(session_id: str):
    records = _session_load(session_id)
    if records is None:
        raise HTTPException(404, 'Session tidak ditemukan')
    unmatched = [r for r in records if r.match_method == 'unmatched']
    return {
        'count': len(unmatched),
        'rows': [{'unit_id_vendor': r.unit_id_vendor, 'unit_serial': r.unit_serial,
                  'component': r.component_raw, 'sampled_at': r.sampled_at,
                  'vendor': r.vendor} for r in unmatched]
    }


@router.get('/status', summary='Ringkasan database oil sample')
def sos_status():
    conn = get_db()
    if not conn:
        return {'total': 0, 'last_inserted_at': None, 'by_component': [], 'by_vendor': []}
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("SELECT COUNT(*) as total, MAX(created_at) as last_inserted_at FROM sos_raw")
    row = cur.fetchone()
    cur.execute("""
        SELECT component, COUNT(*) as cnt
        FROM sos_raw WHERE component IS NOT NULL
        GROUP BY component ORDER BY cnt DESC LIMIT 12
    """)
    by_component = [dict(r) for r in cur.fetchall()]
    cur.execute("""
        SELECT vendor, COUNT(*) as cnt
        FROM sos_raw WHERE vendor IS NOT NULL
        GROUP BY vendor ORDER BY cnt DESC LIMIT 6
    """)
    by_vendor = [dict(r) for r in cur.fetchall()]
    # Unit paling jarang di-sampling
    cur.execute("""
        SELECT ptba_unit_code as unit, COUNT(*) as cnt,
               MAX(sampled_at) as last_sampled,
               COUNT(DISTINCT component) as components
        FROM sos_raw WHERE ptba_unit_code IS NOT NULL
        GROUP BY ptba_unit_code ORDER BY cnt ASC LIMIT 15
    """)
    least_sampled = [dict(r) for r in cur.fetchall()]
    for u in least_sampled:
        if u['last_sampled']: u['last_sampled'] = u['last_sampled'].isoformat()[:10]
    # Trend sampling per bulan (12 bulan terakhir)
    cur.execute("""
        SELECT TO_CHAR(sampled_at, 'YYYY-MM') as month, COUNT(*) as cnt
        FROM sos_raw WHERE sampled_at >= NOW() - INTERVAL '12 months'
        GROUP BY month ORDER BY month
    """)
    monthly_trend = [dict(r) for r in cur.fetchall()]
    # Unit coverage
    cur.execute("SELECT COUNT(DISTINCT ptba_unit_code) as units FROM sos_raw WHERE ptba_unit_code IS NOT NULL")
    unit_count = cur.fetchone()['units']
    cur.close(); conn.close()
    return {
        'total': row['total'] or 0,
        'last_inserted_at': row['last_inserted_at'].isoformat() if row['last_inserted_at'] else None,
        'by_component': by_component,
        'by_vendor': by_vendor,
        'least_sampled': least_sampled,
        'monthly_trend': monthly_trend,
        'unit_count': unit_count,
    }


@router.get('/health-matrix', summary='Health matrix per unit — data teragregasi untuk dashboard')
def health_matrix():
    """Return latest sample per unit per component, sudah diagregasi di server."""
    conn = get_db()
    if not conn:
        return {'units': [], 'components': []}
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("""
        WITH ranked AS (
            SELECT *,
                ROW_NUMBER() OVER (
                    PARTITION BY ptba_unit_code, component
                    ORDER BY sampled_at DESC NULLS LAST
                ) AS rn
            FROM sos_raw
            WHERE ptba_unit_code IS NOT NULL AND component IS NOT NULL
        )
        SELECT
            ptba_unit_code AS unit,
            component,
            sampled_at,
            smu_hours,
            vendor_severity AS severity,
            fe, cu, al, si, tbn, viscosity_40
        FROM ranked
        WHERE rn = 1
        ORDER BY ptba_unit_code, component
    """)
    rows = [dict(r) for r in cur.fetchall()]
    cur.close(); conn.close()

    # Convert timestamps to string
    for r in rows:
        if r.get('sampled_at'):
            r['sampled_at'] = r['sampled_at'].isoformat()[:10]

    units = sorted(set(r['unit'] for r in rows))
    components = sorted(set(r['component'] for r in rows))
    return {'units': units, 'components': components, 'rows': rows}


@router.get('/by-component-type', summary='Jumlah sampel per komponen per tipe unit')
def by_component_type():
    from app.api.assets import _assets, load_assets
    load_assets()
    conn = get_db()
    if not conn: return []
    cur = conn.cursor()
    cur.execute("""
        SELECT component, ptba_unit_code, COUNT(*) as cnt
        FROM sos_raw
        WHERE component IS NOT NULL AND ptba_unit_code IS NOT NULL
        GROUP BY component, ptba_unit_code
        ORDER BY component, cnt DESC
    """)
    rows = cur.fetchall()
    cur.close(); conn.close()
    result = []
    for comp, unit, cnt in rows:
        model = _assets.get(unit, {}).get('Model') or unit.split('-')[0]
        result.append({'component': comp, 'model': model, 'unit': unit, 'cnt': int(cnt)})
    return result


@router.get('/samples', summary='List semua oil samples tersimpan')
async def list_samples(
    unit_code: Optional[str] = Query(None, description='Filter by kode unit PTBA'),
    component: Optional[str] = Query(None),
    severity: Optional[str] = Query(None),
    limit: int = Query(100, le=50000),
    offset: int = Query(0),
):
    conn = get_db()
    if not conn:
        return {'message': 'DB tidak tersedia', 'data': [], 'total': 0}
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    conditions, params = [], []
    if unit_code:
        conditions.append('ptba_unit_code ILIKE %s'); params.append(f'%{unit_code}%')
    if component:
        conditions.append('component ILIKE %s'); params.append(f'%{component}%')
    if severity:
        conditions.append('our_severity = %s'); params.append(severity)
    where = ('WHERE ' + ' AND '.join(conditions)) if conditions else ''
    cur.execute(f'SELECT COUNT(*) as n FROM sos_raw {where}', params)
    total = cur.fetchone()['n']
    cur.execute(f'SELECT * FROM sos_raw {where} ORDER BY sampled_at DESC LIMIT %s OFFSET %s',
                params + [limit, offset])
    rows = [dict(r) for r in cur.fetchall()]
    cur.close(); conn.close()
    return {'total': total, 'data': rows}


@router.get('/samples/{unit_code}', summary='History oil sample per unit')
async def unit_sample_history(unit_code: str, component: Optional[str] = Query(None)):
    conn = get_db()
    if not conn:
        return {'unit_code': unit_code, 'samples': [], 'total': 0}
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    conditions = ['ptba_unit_code = %s']
    params = [unit_code.upper()]
    if component:
        conditions.append('component = %s'); params.append(component)
    where = 'WHERE ' + ' AND '.join(conditions)
    cur.execute(f'SELECT * FROM sos_raw {where} ORDER BY sampled_at DESC LIMIT 200', params)
    rows = [dict(r) for r in cur.fetchall()]
    cur.close(); conn.close()
    return {'unit_code': unit_code, 'total': len(rows), 'samples': rows}


# ── Download folder sync ────────────────────────────────────────────────────

DOWNLOADS_DIR = Path('/downloads')

@router.get('/downloads/scan', summary='Cari file xlsx terbaru di folder Downloads')
def scan_downloads():
    if not DOWNLOADS_DIR.exists():
        return {'files': [], 'error': 'Folder Downloads tidak terpasang'}
    files = sorted(
        [f for f in DOWNLOADS_DIR.glob('*.xlsx') if not f.name.startswith('~$')],
        key=lambda f: f.stat().st_mtime, reverse=True
    )
    return {'files': [{'name': f.name, 'size_kb': f.stat().st_size // 1024,
                       'modified': datetime.fromtimestamp(f.stat().st_mtime).isoformat()}
                      for f in files[:10]]}

@router.post('/downloads/process', summary='Proses file xlsx dari Downloads ke DB')
async def process_from_downloads(filename: str = Query(...)):
    filepath = DOWNLOADS_DIR / filename
    if not filepath.exists():
        raise HTTPException(404, f'File tidak ditemukan: {filename}')
    if not str(filepath.resolve()).startswith(str(DOWNLOADS_DIR.resolve())):
        raise HTTPException(400, 'Path tidak valid')

    result = parse_oil_sample_file(str(filepath), ASSET_REGISTRY if Path(ASSET_REGISTRY).exists() else None)
    if result.get('error'):
        raise HTTPException(422, result['error'])

    records = result['records']
    inserted = skipped_dup = skipped_unm = 0
    errors = []
    dup_preview = []
    unmatched_preview = []

    conn = get_db()
    if conn:
        import json as _json
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute('''CREATE TABLE IF NOT EXISTS sos_raw (
            id SERIAL PRIMARY KEY, ptba_unit_code TEXT, unit_id_vendor TEXT, unit_serial TEXT,
            vendor TEXT, source_file TEXT, match_method TEXT, component TEXT, component_raw TEXT,
            sampled_at TIMESTAMP, lab_date TIMESTAMP, lab_reference TEXT,
            smu_hours REAL, oil_hours REAL, oil_changed BOOLEAN, filter_changed BOOLEAN,
            oil_brand TEXT, oil_grade TEXT,
            fe REAL, cu REAL, al REAL, cr REAL, pb REAL, sn REAL, ni REAL,
            si REAL, na REAL, mg REAL, mo REAL, p REAL, zn REAL, ca REAL, b REAL, k REAL, ba REAL,
            viscosity_40 REAL, viscosity_100 REAL, visc_sae TEXT, tan REAL, tbn REAL,
            water_pct REAL, karl_fischer REAL, soot REAL, oxidation REAL, nitration REAL,
            fuel_dilution_pct REAL, glycol BOOLEAN, pq_index REAL, iso_code TEXT,
            sox REAL, fame REAL, sulphur REAL, dir_trans REAL,
            particles_4um REAL, particles_6um REAL, particles_15um REAL,
            location TEXT, branch TEXT, follow_up TEXT,
            vendor_severity TEXT, vendor_notes TEXT, raw_data JSONB,
            created_at TIMESTAMP DEFAULT NOW())''')
        cur.execute('''CREATE UNIQUE INDEX IF NOT EXISTS ix_sos_dedup
            ON sos_raw (ptba_unit_code, component, sampled_at)
            WHERE ptba_unit_code IS NOT NULL AND component IS NOT NULL AND sampled_at IS NOT NULL''')
        conn.commit()
        import pandas as _pd
        def _clean(v):
            if v is None: return None
            try:
                if _pd.isna(v): return None
            except Exception: pass
            return v

        for r in records:
            if r.match_method == 'unmatched':
                skipped_unm += 1
                if True:
                    unmatched_preview.append({
                        'unit': r.unit_id_vendor or r.ptba_unit_code or '—',
                        'component': r.component,
                        'sampled_at': str(r.sampled_at)[:10] if r.sampled_at else None,
                        'smu': r.smu_hours,
                        'reason': 'Unit tidak ditemukan di asset registry',
                    })
                continue
            try:
                cur.execute('SELECT 1 FROM sos_raw WHERE ptba_unit_code=%s AND component=%s AND sampled_at=%s LIMIT 1',
                            (r.ptba_unit_code, r.component, _clean(r.sampled_at)))
                existing = cur.fetchone()
                if existing:
                    skipped_dup += 1
                    dup_preview.append({
                        'unit': r.ptba_unit_code, 'component': r.component,
                        'sampled_at': str(r.sampled_at)[:10] if r.sampled_at else None,
                        'smu': r.smu_hours,
                        '_key': f'{r.ptba_unit_code}||{r.component}||{str(_clean(r.sampled_at))[:10]}',
                    })
                    continue
                cur.execute('''INSERT INTO sos_raw (
                    ptba_unit_code,unit_id_vendor,unit_serial,vendor,source_file,match_method,
                    component,component_raw,sampled_at,lab_date,lab_reference,
                    smu_hours,oil_hours,oil_changed,filter_changed,oil_brand,oil_grade,
                    fe,cu,al,cr,pb,sn,ni,si,na,mg,mo,p,zn,ca,b,k,ba,
                    viscosity_40,viscosity_100,visc_sae,tan,tbn,
                    water_pct,karl_fischer,soot,oxidation,nitration,
                    fuel_dilution_pct,glycol,pq_index,iso_code,
                    sox,fame,sulphur,dir_trans,particles_4um,particles_6um,particles_15um,
                    location,branch,follow_up,vendor_severity,vendor_notes,raw_data
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,
                    %s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,
                    %s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                RETURNING id''',
                    (r.ptba_unit_code,r.unit_id_vendor,r.unit_serial,r.vendor,
                     filename,r.match_method,r.component,r.component_raw,
                     _clean(r.sampled_at),_clean(r.lab_date),r.lab_reference,
                     r.smu_hours,r.oil_hours,r.oil_changed,r.filter_changed,r.oil_brand,r.oil_grade,
                     r.iron_fe,r.copper_cu,r.aluminum_al,r.chromium_cr,r.lead_pb,
                     r.tin_sn,r.nickel_ni,r.silicon_si,r.sodium_na,r.magnesium_mg,
                     r.molybdenum_mo,r.phosphorus_p,r.zinc_zn,r.calcium_ca,
                     r.boron_b,r.potassium_k,r.barium_ba,
                     r.viscosity_40,r.viscosity_100,r.visc_sae,r.tan,r.tbn,
                     r.water_pct,r.karl_fischer,r.soot,r.oxidation,r.nitration,
                     r.fuel_dilution_pct,r.glycol,r.pq_index,r.iso_code,
                     r.sox,r.fame,r.sulphur,r.dir_trans,
                     r.particles_4um,r.particles_6um,r.particles_15um,
                     r.location,r.branch,r.follow_up,
                     r.vendor_severity,r.vendor_notes,
                     _json.dumps(r.raw_data) if r.raw_data else None))
                new_id = cur.fetchone()['id'] if cur.description else None
                inserted += 1
                # Trigger diagnosis background
                if new_id and r.ptba_unit_code and r.component:
                    _threading.Thread(
                        target=_run_diagnosis_bg,
                        args=(r.ptba_unit_code, r.component, _clean(r.sampled_at), new_id),
                        kwargs={'use_ai': False},
                        daemon=True
                    ).start()
            except Exception as e:
                errors.append(str(e)[:200])
        # Batch-fetch reason for all dups (1 query instead of N queries)
        if dup_preview:
            keys = [(d['unit'], d['component'], d['sampled_at']) for d in dup_preview]
            placeholders = ','.join(['(%s,%s,%s::date)'] * len(keys))
            flat = [x for t in keys for x in t]
            cur.execute(
                f'SELECT ptba_unit_code,component,sampled_at,created_at,source_file FROM sos_raw '
                f'WHERE (ptba_unit_code,component,sampled_at) IN ({placeholders})',
                flat
            )
            orig_map = {}
            for row in cur.fetchall():
                k = f"{row['ptba_unit_code']}||{row['component']}||{str(row['sampled_at'])[:10]}"
                orig_map[k] = row
            for d in dup_preview:
                orig = orig_map.get(d.pop('_key', ''))
                if orig:
                    first_seen = orig['created_at'].strftime('%d %b %Y') if orig['created_at'] else '—'
                    src = (orig['source_file'] or '').split('::')[0].split('/')[-1]
                    d['reason'] = f'Unit+Komponen+Tgl identik · dicatat {first_seen} via {src}'
                else:
                    d['reason'] = 'Unit+Komponen+Tgl identik'

        conn.commit(); cur.close(); conn.close()

    return {'filename': filename, 'total_parsed': len(records),
            'inserted': inserted, 'skipped_duplicate': skipped_dup,
            'skipped_unmatched': skipped_unm, 'errors': errors[:5],
            'dup_preview': dup_preview, 'unmatched_preview': unmatched_preview}


# ── OneDrive Sync status (sync dilakukan oleh selenium_server.py di host) ────

@router.get('/onedrive/status', summary='Status sync OneDrive terakhir')
def onedrive_status():
    return onedrive_sync.get_sync_status()


# ── Diagnosis ──────────────────────────────────────────────────────────────────

def _run_diagnosis_bg(unit: str, component: str, sampled_at, sos_raw_id: int, use_ai: bool = False):
    """Jalankan diagnosis di background thread setelah insert."""
    try:
        conn = get_db();
        if not conn: return
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

        # Ambil sample yang baru saja diinsert
        cur.execute('SELECT * FROM sos_raw WHERE id=%s', (sos_raw_id,))
        sample = dict(cur.fetchone() or {})
        if not sample: return

        # Ambil riwayat 10 sample sebelumnya (unit+komponen sama)
        cur.execute('''
            SELECT fe,cu,al,cr,pb,si,na,tbn,soot,water_pct,glycol,fuel_dilution_pct,sampled_at
            FROM sos_raw WHERE ptba_unit_code=%s AND component=%s AND sampled_at < %s
            ORDER BY sampled_at DESC LIMIT 10
        ''', (unit, component, sampled_at))
        history = [dict(r) for r in cur.fetchall()]

        diag = diagnose_sample(sample, history)

        ai_summary = ai_rec = ''
        if use_ai and diag['severity'] != 'NORMAL':
            ai_summary, ai_rec = generate_ai_summary(sample, diag)

        cur.execute('''
            INSERT INTO sos_diagnosis
              (sos_raw_id, ptba_unit_code, component, sampled_at,
               severity, confidence, flags, failure_modes, trend,
               ai_summary, ai_recommendation, data_sources)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT DO NOTHING
        ''', (
            sos_raw_id, unit, component, sampled_at,
            diag['severity'], diag['confidence'],
            json.dumps(diag['flags']), json.dumps(diag['failure_modes']),
            json.dumps(diag['trend']),
            ai_summary, ai_rec, json.dumps(diag['data_sources']),
        ))
        conn.commit(); cur.close(); conn.close()
    except Exception as e:
        print(f'[diagnosis] error: {e}')


@router.get('/diagnosis/unit/{unit_code}', summary='Diagnosis terbaru per unit')
def get_unit_diagnosis(unit_code: str, limit: int = Query(20, le=100)):
    conn = get_db()
    if not conn: return {'data': []}
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute('''
        SELECT d.*, r.smu_hours, r.vendor_severity as vs_vendor,
               r.raw_data->>'Model ' as model
        FROM sos_diagnosis d
        LEFT JOIN sos_raw r ON r.id = d.sos_raw_id
        WHERE d.ptba_unit_code = %s
        ORDER BY d.sampled_at DESC LIMIT %s
    ''', (unit_code, limit))
    rows = [dict(r) for r in cur.fetchall()]
    for r in rows:
        if r.get('sampled_at'): r['sampled_at'] = str(r['sampled_at'])[:10]
        if r.get('created_at'): r['created_at'] = r['created_at'].isoformat()
    cur.close(); conn.close()
    return {'unit_code': unit_code, 'data': rows}


@router.get('/diagnosis/summary', summary='Ringkasan diagnosis semua unit')
def get_diagnosis_summary():
    """Satu baris per unit+komponen — kondisi terkini."""
    conn = get_db()
    if not conn: return {'data': []}
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute('''
        SELECT DISTINCT ON (ptba_unit_code, component)
            ptba_unit_code, component, sampled_at, severity, confidence,
            flags, failure_modes, ai_summary, ai_recommendation, created_at
        FROM sos_diagnosis
        ORDER BY ptba_unit_code, component, sampled_at DESC
    ''')
    rows = [dict(r) for r in cur.fetchall()]
    for r in rows:
        if r.get('sampled_at'): r['sampled_at'] = str(r['sampled_at'])[:10]
        if r.get('created_at'): r['created_at'] = r['created_at'].isoformat()
    cur.close(); conn.close()
    # Hitung agregat
    total = len(rows)
    critical = sum(1 for r in rows if r['severity']=='CRITICAL')
    caution  = sum(1 for r in rows if r['severity']=='CAUTION')
    normal   = total - critical - caution
    return {'total': total, 'critical': critical, 'caution': caution, 'normal': normal, 'data': rows}


@router.post('/diagnosis/run', summary='Jalankan ulang diagnosis untuk semua data')
def run_all_diagnosis(use_ai: bool = Query(False)):
    """Batch diagnosis — berguna setelah import data pertama."""
    conn = get_db()
    if not conn: raise HTTPException(500, 'DB tidak tersedia')
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    # Ambil sample yang belum didiagnosis (atau semua jika force)
    cur.execute('''
        SELECT r.id, r.ptba_unit_code, r.component, r.sampled_at
        FROM sos_raw r
        LEFT JOIN sos_diagnosis d ON d.sos_raw_id = r.id
        WHERE d.id IS NULL AND r.ptba_unit_code IS NOT NULL
        ORDER BY r.sampled_at DESC LIMIT 500
    ''')
    pending = cur.fetchall()
    cur.close(); conn.close()

    def _batch():
        for row in pending:
            _run_diagnosis_bg(row['ptba_unit_code'], row['component'],
                              row['sampled_at'], row['id'], use_ai=use_ai)

    t = _threading.Thread(target=_batch, daemon=True)
    t.start()
    return {'queued': len(pending), 'message': f'Menjalankan diagnosis untuk {len(pending)} sample di background.'}


