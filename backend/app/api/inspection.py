"""
Inspection & Measurement API
-----------------------------
POST /api/inspection/engine          → simpan hasil pengukuran mesin
GET  /api/inspection/engine          → list semua records (filter unit/date)
GET  /api/inspection/engine/{id}     → detail satu record
DELETE /api/inspection/engine/{id}   → hapus record
GET  /api/inspection/engine/summary  → statistik per unit
"""

import os, json, re, tempfile
from pathlib import Path
from datetime import datetime, date
from typing import Optional, List
from fastapi import APIRouter, HTTPException, Query, UploadFile, File
from pydantic import BaseModel
import psycopg2
import psycopg2.extras

router = APIRouter(prefix='/api/inspection', tags=['Inspection & Measurement'])

DATABASE_URL = os.getenv('DATABASE_URL', '').replace('+asyncpg', '')

def get_db():
    try:
        conn = psycopg2.connect(DATABASE_URL)
        return conn
    except Exception as e:
        print(f'[DB] Koneksi gagal: {e}')
        return None

DDL = """
CREATE TABLE IF NOT EXISTS engine_measurements (
    id              SERIAL PRIMARY KEY,
    unit_code       TEXT NOT NULL,
    measurement_date DATE NOT NULL,
    hm_smu          REAL,
    mechanic        TEXT,
    location        TEXT,

    -- Compression test (per silinder, max 16 silinder)
    cylinders       INTEGER,
    comp_cyl        JSONB,          -- [bar, bar, ...] per silinder
    comp_limit_min  REAL,           -- spec min (bar)
    comp_limit_max  REAL,           -- spec max (bar)
    comp_max_diff   REAL,           -- max selisih antar silinder (bar)

    -- Blow-by
    blowby_value    REAL,           -- cfm atau lpm
    blowby_unit     TEXT DEFAULT 'cfm',
    blowby_limit    REAL,

    -- Oil pressure
    oil_press_idle  REAL,           -- bar
    oil_press_rated REAL,           -- bar
    oil_press_idle_limit_min  REAL,
    oil_press_idle_limit_max  REAL,
    oil_press_rated_limit_min REAL,
    oil_press_rated_limit_max REAL,

    -- Coolant temperature
    coolant_temp_idle    REAL,      -- °C
    coolant_temp_rated   REAL,      -- °C
    coolant_temp_limit   REAL,      -- max limit

    -- Turbo boost pressure
    boost_pressure       REAL,      -- bar
    boost_limit_min      REAL,
    boost_limit_max      REAL,

    -- RPM
    rpm_idle             REAL,
    rpm_high_idle        REAL,
    rpm_rated            REAL,
    rpm_idle_spec        REAL,
    rpm_high_idle_spec   REAL,
    rpm_rated_spec       REAL,

    -- Fuel consumption
    fuel_consumption     REAL,      -- L/jam
    fuel_consumption_limit REAL,

    -- Battery
    battery_voltage      REAL,      -- Volt
    battery_limit_min    REAL DEFAULT 12.0,
    battery_limit_max    REAL DEFAULT 14.8,

    -- Exhaust back pressure
    exhaust_backpress    REAL,      -- kPa
    exhaust_backpress_limit REAL,

    -- Fuel supply pressure
    fuel_supply_press    REAL,      -- bar
    fuel_supply_press_limit_min REAL,
    fuel_supply_press_limit_max REAL,

    -- Cylinder balancing (% injector compensation per silinder)
    cylinder_balancing  JSONB,      -- [%, %, ...] per silinder

    -- Injector shut-off test per silinder
    injector_shutoff    JSONB,      -- [{cyl, rpm, load_pct, fuel_lph}, ...]

    -- Clutch wear
    clutch_wear_new_mm      REAL,   -- X1: ketebalan kampas baru (mm)
    clutch_wear_current_mm  REAL,   -- X2: posisi saat ini (mm)
    clutch_wear_diff_mm     REAL,   -- X1-X2: selisih / wear (mm)
    clutch_wear_limit_mm    REAL,   -- batas service (mm)

    -- Source document
    source_doc          TEXT,       -- nama file PDF sumber

    -- Overall result & catatan
    overall_status  TEXT,           -- OK / NOT OK / NEEDS MONITORING
    findings        TEXT,
    recommendation  TEXT,

    created_at      TIMESTAMP DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS ix_em_unit_date
    ON engine_measurements (unit_code, measurement_date DESC);
"""

def ensure_table(conn):
    cur = conn.cursor()
    cur.execute(DDL)
    # Tambah kolom baru jika belum ada (idempotent)
    _new_cols = [
        ("cylinder_balancing", "JSONB"),
        ("injector_shutoff", "JSONB"),
        ("clutch_wear_new_mm", "REAL"),
        ("clutch_wear_current_mm", "REAL"),
        ("clutch_wear_diff_mm", "REAL"),
        ("clutch_wear_limit_mm", "REAL"),
        ("source_doc", "TEXT"),
    ]
    for col, typ in _new_cols:
        try:
            cur.execute(f"ALTER TABLE engine_measurements ADD COLUMN IF NOT EXISTS {col} {typ}")
        except Exception:
            pass
    conn.commit()
    cur.close()


# ── Pydantic models ───────────────────────────────────────────────────────────

class EngineCompSpec(BaseModel):
    cylinders: int = 6
    comp_limit_min: Optional[float] = None
    comp_limit_max: Optional[float] = None
    comp_max_diff: Optional[float] = None

class EngineMeasurementIn(BaseModel):
    unit_code: str
    measurement_date: str           # YYYY-MM-DD
    hm_smu: Optional[float] = None
    mechanic: Optional[str] = None
    location: Optional[str] = None

    # Compression
    cylinders: Optional[int] = None
    comp_cyl: Optional[List[Optional[float]]] = None
    comp_limit_min: Optional[float] = None
    comp_limit_max: Optional[float] = None
    comp_max_diff: Optional[float] = None

    # Blow-by
    blowby_value: Optional[float] = None
    blowby_unit: Optional[str] = 'cfm'
    blowby_limit: Optional[float] = None

    # Oil pressure
    oil_press_idle: Optional[float] = None
    oil_press_rated: Optional[float] = None
    oil_press_idle_limit_min: Optional[float] = None
    oil_press_idle_limit_max: Optional[float] = None
    oil_press_rated_limit_min: Optional[float] = None
    oil_press_rated_limit_max: Optional[float] = None

    # Coolant
    coolant_temp_idle: Optional[float] = None
    coolant_temp_rated: Optional[float] = None
    coolant_temp_limit: Optional[float] = None

    # Boost
    boost_pressure: Optional[float] = None
    boost_limit_min: Optional[float] = None
    boost_limit_max: Optional[float] = None

    # RPM
    rpm_idle: Optional[float] = None
    rpm_high_idle: Optional[float] = None
    rpm_rated: Optional[float] = None
    rpm_idle_spec: Optional[float] = None
    rpm_high_idle_spec: Optional[float] = None
    rpm_rated_spec: Optional[float] = None

    # Fuel consumption
    fuel_consumption: Optional[float] = None
    fuel_consumption_limit: Optional[float] = None

    # Battery
    battery_voltage: Optional[float] = None
    battery_limit_min: Optional[float] = 12.0
    battery_limit_max: Optional[float] = 14.8

    # Exhaust
    exhaust_backpress: Optional[float] = None
    exhaust_backpress_limit: Optional[float] = None

    # Fuel supply
    fuel_supply_press: Optional[float] = None
    fuel_supply_press_limit_min: Optional[float] = None
    fuel_supply_press_limit_max: Optional[float] = None

    # Cylinder balancing
    cylinder_balancing: Optional[List[Optional[float]]] = None

    # Injector shut-off
    injector_shutoff: Optional[list] = None

    # Clutch wear
    clutch_wear_new_mm: Optional[float] = None
    clutch_wear_current_mm: Optional[float] = None
    clutch_wear_diff_mm: Optional[float] = None
    clutch_wear_limit_mm: Optional[float] = None

    # Source
    source_doc: Optional[str] = None

    # Result
    overall_status: Optional[str] = None
    findings: Optional[str] = None
    recommendation: Optional[str] = None


# ── Endpoints ─────────────────────────────────────────────────────────────────

@router.post('/engine', summary='Simpan hasil engine measurement')
def create_engine_measurement(payload: EngineMeasurementIn):
    conn = get_db()
    if not conn:
        raise HTTPException(503, 'Database tidak tersedia')
    ensure_table(conn)
    cur = conn.cursor()
    d = payload.dict()
    d['comp_cyl'] = json.dumps(d['comp_cyl']) if d.get('comp_cyl') else None
    d['cylinder_balancing'] = json.dumps(d['cylinder_balancing']) if d.get('cylinder_balancing') else None
    d['injector_shutoff'] = json.dumps(d['injector_shutoff']) if d.get('injector_shutoff') else None
    cur.execute("""
        INSERT INTO engine_measurements (
            unit_code, measurement_date, hm_smu, mechanic, location,
            cylinders, comp_cyl, comp_limit_min, comp_limit_max, comp_max_diff,
            blowby_value, blowby_unit, blowby_limit,
            oil_press_idle, oil_press_rated,
            oil_press_idle_limit_min, oil_press_idle_limit_max,
            oil_press_rated_limit_min, oil_press_rated_limit_max,
            coolant_temp_idle, coolant_temp_rated, coolant_temp_limit,
            boost_pressure, boost_limit_min, boost_limit_max,
            rpm_idle, rpm_high_idle, rpm_rated,
            rpm_idle_spec, rpm_high_idle_spec, rpm_rated_spec,
            fuel_consumption, fuel_consumption_limit,
            battery_voltage, battery_limit_min, battery_limit_max,
            exhaust_backpress, exhaust_backpress_limit,
            fuel_supply_press, fuel_supply_press_limit_min, fuel_supply_press_limit_max,
            cylinder_balancing, injector_shutoff,
            clutch_wear_new_mm, clutch_wear_current_mm, clutch_wear_diff_mm, clutch_wear_limit_mm,
            source_doc, overall_status, findings, recommendation
        ) VALUES (
            %(unit_code)s, %(measurement_date)s, %(hm_smu)s, %(mechanic)s, %(location)s,
            %(cylinders)s, %(comp_cyl)s, %(comp_limit_min)s, %(comp_limit_max)s, %(comp_max_diff)s,
            %(blowby_value)s, %(blowby_unit)s, %(blowby_limit)s,
            %(oil_press_idle)s, %(oil_press_rated)s,
            %(oil_press_idle_limit_min)s, %(oil_press_idle_limit_max)s,
            %(oil_press_rated_limit_min)s, %(oil_press_rated_limit_max)s,
            %(coolant_temp_idle)s, %(coolant_temp_rated)s, %(coolant_temp_limit)s,
            %(boost_pressure)s, %(boost_limit_min)s, %(boost_limit_max)s,
            %(rpm_idle)s, %(rpm_high_idle)s, %(rpm_rated)s,
            %(rpm_idle_spec)s, %(rpm_high_idle_spec)s, %(rpm_rated_spec)s,
            %(fuel_consumption)s, %(fuel_consumption_limit)s,
            %(battery_voltage)s, %(battery_limit_min)s, %(battery_limit_max)s,
            %(exhaust_backpress)s, %(exhaust_backpress_limit)s,
            %(fuel_supply_press)s, %(fuel_supply_press_limit_min)s, %(fuel_supply_press_limit_max)s,
            %(cylinder_balancing)s, %(injector_shutoff)s,
            %(clutch_wear_new_mm)s, %(clutch_wear_current_mm)s, %(clutch_wear_diff_mm)s, %(clutch_wear_limit_mm)s,
            %(source_doc)s, %(overall_status)s, %(findings)s, %(recommendation)s
        ) RETURNING id
    """, d)
    new_id = cur.fetchone()[0]
    conn.commit(); cur.close(); conn.close()
    return {'id': new_id, 'success': True}


@router.get('/engine/summary', summary='Statistik engine measurement per unit')
def engine_summary():
    conn = get_db()
    if not conn: return []
    ensure_table(conn)
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("""
        SELECT unit_code,
               COUNT(*) as total,
               MAX(measurement_date) as last_date,
               MAX(hm_smu) as last_hm,
               SUM(CASE WHEN overall_status='OK' THEN 1 ELSE 0 END) as ok_count,
               SUM(CASE WHEN overall_status='NOT OK' THEN 1 ELSE 0 END) as notok_count,
               SUM(CASE WHEN overall_status='NEEDS MONITORING' THEN 1 ELSE 0 END) as monitor_count
        FROM engine_measurements
        GROUP BY unit_code
        ORDER BY last_date DESC
    """)
    rows = [dict(r) for r in cur.fetchall()]
    for r in rows:
        if r['last_date']: r['last_date'] = r['last_date'].isoformat()
    cur.close(); conn.close()
    return rows


@router.get('/engine', summary='List engine measurement records')
def list_engine_measurements(
    unit_code: Optional[str] = Query(None),
    date_from: Optional[str] = Query(None),
    date_to:   Optional[str] = Query(None),
    limit: int = Query(100, le=1000),
    offset: int = Query(0),
):
    conn = get_db()
    if not conn: return {'data': [], 'total': 0}
    ensure_table(conn)
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    filters, params = [], {}
    if unit_code:
        filters.append("unit_code ILIKE %(unit_code)s")
        params['unit_code'] = f'%{unit_code}%'
    if date_from:
        filters.append("measurement_date >= %(date_from)s")
        params['date_from'] = date_from
    if date_to:
        filters.append("measurement_date <= %(date_to)s")
        params['date_to'] = date_to

    where = ('WHERE ' + ' AND '.join(filters)) if filters else ''
    cur.execute(f"SELECT COUNT(*) as total FROM engine_measurements {where}", params)
    total = cur.fetchone()['total']

    params['limit'] = limit
    params['offset'] = offset
    cur.execute(f"""
        SELECT * FROM engine_measurements
        {where}
        ORDER BY measurement_date DESC, id DESC
        LIMIT %(limit)s OFFSET %(offset)s
    """, params)
    rows = [dict(r) for r in cur.fetchall()]
    for r in rows:
        if r.get('measurement_date'): r['measurement_date'] = r['measurement_date'].isoformat()
        if r.get('created_at'):       r['created_at'] = r['created_at'].isoformat()
        if r.get('comp_cyl') and isinstance(r['comp_cyl'], str):
            r['comp_cyl'] = json.loads(r['comp_cyl'])
    cur.close(); conn.close()
    return {'data': rows, 'total': total}


@router.get('/engine/{rec_id}', summary='Detail satu engine measurement')
def get_engine_measurement(rec_id: int):
    conn = get_db()
    if not conn: raise HTTPException(503, 'Database tidak tersedia')
    ensure_table(conn)
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("SELECT * FROM engine_measurements WHERE id=%s", (rec_id,))
    row = cur.fetchone()
    cur.close(); conn.close()
    if not row: raise HTTPException(404, f'Record {rec_id} tidak ditemukan')
    r = dict(row)
    if r.get('measurement_date'): r['measurement_date'] = r['measurement_date'].isoformat()
    if r.get('created_at'):       r['created_at'] = r['created_at'].isoformat()
    if r.get('comp_cyl') and isinstance(r['comp_cyl'], str):
        r['comp_cyl'] = json.loads(r['comp_cyl'])
    return r


@router.delete('/engine/{rec_id}', summary='Hapus engine measurement record')
def delete_engine_measurement(rec_id: int):
    conn = get_db()
    if not conn: raise HTTPException(503, 'Database tidak tersedia')
    cur = conn.cursor()
    cur.execute("DELETE FROM engine_measurements WHERE id=%s RETURNING id", (rec_id,))
    deleted = cur.fetchone()
    conn.commit(); cur.close(); conn.close()
    if not deleted: raise HTTPException(404, f'Record {rec_id} tidak ditemukan')
    return {'success': True, 'deleted_id': rec_id}


# ── PDF Import ────────────────────────────────────────────────────────────────

def _parse_mip_pdf(pdf_path: str, filename: str) -> dict:
    """
    Parse PDF laporan Performance Test dari MIP/Indotruck.
    Format: Technical Information Report – PT Indotruck Utama
    """
    try:
        import fitz  # PyMuPDF
    except ImportError:
        raise HTTPException(500, 'PyMuPDF tidak terinstall di container.')

    try:
        import pytesseract
        from PIL import Image
        import io as _io
        HAS_OCR = True
    except ImportError:
        HAS_OCR = False

    doc = fitz.open(pdf_path)

    # Kumpulkan teks native dan OCR terpisah
    native_pages = []
    ocr_pages    = []
    for page in doc:
        native = page.get_text()
        native_pages.append(native)
        if HAS_OCR:
            pix = page.get_pixmap(dpi=250)
            img = Image.open(_io.BytesIO(pix.tobytes('png')))
            ocr_text = pytesseract.image_to_string(img, lang='eng')
            ocr_pages.append(ocr_text)
        else:
            ocr_pages.append(native)

    # full_text untuk pencarian header/general (native lebih akurat)
    full_text = '\n'.join(native_pages)
    # ocr_text untuk pencarian tabel yang ada di gambar screenshot
    ocr_full  = '\n'.join(ocr_pages)

    def find(pattern, text=full_text, default=None, flags=re.IGNORECASE):
        m = re.search(pattern, text, flags)
        return m.group(1).strip() if m else default

    def find_float(pattern, text=full_text, default=None):
        m = re.search(pattern, text, re.IGNORECASE)
        if not m: return default
        try: return float(m.group(1).replace(',', '.'))
        except: return default

    # ── Header info ──────────────────────────────────────────────────────────
    # Format PDF Indotruck: field dan value dipisah newline
    # Coba berbagai pola untuk unit_code
    unit_code = (
        find(r'UNIT CODE\s*[\n\r]+\s*[:\-]?\s*([A-Z]{1,3}[\d\-]+)', flags=re.IGNORECASE) or
        find(r'UNIT CODE\s*[:\-]\s*([A-Z]{1,3}[\d\-]+)', flags=re.IGNORECASE) or
        find(r'Unit Code\s*[:\-]?\s*([A-Z]{1,3}[\d\-]+)', flags=re.IGNORECASE) or
        # Fallback: cari pola kode unit umum seperti DT3010-11, WM0077, GS0046
        find(r'\b([A-Z]{2,3}[\d]+[-]?[\d]*)\b', flags=re.IGNORECASE)
    )
    unit_model = find(r'UNIT MODEL\s*[\n\r]*\s*[:\-]\s*(.+)', flags=re.IGNORECASE)
    hm_raw     = find(r'MILEAGE\s*/\s*HM\s*[\n\r]*\s*[:\-]\s*[\d.,]+\s*/\s*([\d.,]+)', flags=re.IGNORECASE)
    hm_smu     = float(hm_raw.replace(',', '.')) if hm_raw else None
    mechanic   = (
        find(r'REPORTED BY\s*[\n\r]+\s*[:\-]?\s*([A-Za-z ]{3,}?)(?:\n|$)', flags=re.IGNORECASE) or
        find(r'REPORTED BY\s*[:\-]\s*([A-Za-z ]{3,}?)(?:\n|$)', flags=re.IGNORECASE)
    )
    if mechanic: mechanic = mechanic.strip()
    date_raw   = (
        find(r'DATE OF CHECKED\s*[\n\r]*\s*[:\-]\s*(.+?)(?:\n)', flags=re.IGNORECASE) or
        find(r'DATE OF CHECKED\s*[:\-]\s*(.+?)(?:\n|UNIT)', flags=re.IGNORECASE)
    )

    # Parse tanggal — format: "14  Juni 2025" atau "14 June 2025"
    measurement_date = None
    if date_raw:
        bulan_id = {'januari':'01','februari':'02','maret':'03','april':'04',
                    'mei':'05','juni':'06','juli':'07','agustus':'08',
                    'september':'09','oktober':'10','november':'11','desember':'12',
                    'january':'01','february':'02','march':'03','may':'05',
                    'june':'06','july':'07','august':'08','october':'10','december':'12'}
        dm = re.search(r'(\d{1,2})\s+([A-Za-z]+)\s+(\d{4})', date_raw)
        if dm:
            day, mon, year = dm.group(1), dm.group(2).lower(), dm.group(3)
            mon_num = bulan_id.get(mon, '01')
            measurement_date = f'{year}-{mon_num}-{int(day):02d}'

    # ── Compression test (% per silinder dari OCR) ───────────────────────────
    # OCR menghasilkan: "CYLINDER NUMBER\n1 2 3 4 5 6\n98% 100% 96,5% 95% 94.5% 96,5%"
    comp_cyl = None
    # Cari di OCR (bukan native) karena nilai per silinder ada di gambar
    comp_block = re.search(
        r'(?:Cylinder\s*)?Compression\s*Test(.*?)(?:Cylinder\s*)?Balancing\s*Test',
        ocr_full, re.DOTALL | re.IGNORECASE)
    if comp_block:
        cb_text = comp_block.group(1)
        # Cari baris yang berisi ≥4 nilai % berurutan (baris nilai tabel kompresi)
        # Filter: abaikan 80% karena itu dari teks "above 80% (Good condition)"
        best_row = []
        for line in cb_text.splitlines():
            pcts_in_line = re.findall(r'(\d{2,3}(?:[.,]\d{1,2})?)\s*%', line)
            candidates_line = []
            for p in pcts_in_line:
                try:
                    v = float(p.replace(',', '.'))
                    if 85 <= v <= 120:   # kompresi wajar 85–120%, hindari 80% dari teks
                        candidates_line.append(v)
                except:
                    pass
            if len(candidates_line) >= len(best_row) and len(candidates_line) >= 4:
                best_row = candidates_line
        if best_row:
            comp_cyl = best_row[:16]

    # ── Cylinder balancing (% kompensasi dari OCR) ────────────────────────────
    # OCR: "1% -4% -1% 0% 1% 0%" atau baris terpisah
    cylinder_balancing = None
    bal_block = re.search(
        r'(?:Cylinder\s*)?Balancing\s*Test(.*?)(?:C\.\s*Fuel|Fuel\s*Pressure|©\s*Positive)',
        ocr_full, re.DOTALL | re.IGNORECASE)
    if bal_block:
        bb_text = bal_block.group(1)
        # Pola: angka signed (misal -4%, +1%, 0%) — range wajar -30 sampai +30
        # OCR kadang baca "- 1%" (spasi) atau "—1%" (em-dash) sebagai pengganti "-1%"
        # Cari pasangan: opsional tanda (inc. spasi) + angka + %
        bals_raw = re.findall(r'([+\-–—]?\s*\d{1,2}(?:[.,]\d)?)\s*%', bb_text)
        candidates_b = []
        for b in bals_raw:
            try:
                # Normalisasi: hapus spasi, ganti em-dash dengan minus
                b_clean = b.replace(' ', '').replace('–', '-').replace('—', '-').replace(',', '.')
                v = float(b_clean)
                if -30 <= v <= 30:
                    candidates_b.append(v)
            except:
                pass
        if len(candidates_b) >= 3:
            cylinder_balancing = candidates_b[:16]

    # ── Oil pressure ──────────────────────────────────────────────────────────
    # Text: "Result of oil pressure test @600Rpm : 320 kPa"
    oil_kpa = find_float(r'oil pressure test\s*@\s*600\s*[Rr]pm\s*[:/]\s*(?:Low idle\s*)?([\d.]+)\s*kPa')
    if oil_kpa is None:
        # fallback: "320 kPa" setelah "hasil tes tekanan oli"
        oil_kpa = find_float(r'[Hh]asil tes tekanan oli.*?([\d.]+)\s*kpa', flags=re.IGNORECASE)
    oil_press_idle     = round(oil_kpa / 100, 3) if oil_kpa else None  # kPa → bar
    oil_idle_limit_min = 2.0   # spec Volvo D13: min idle 200 kPa = 2 bar
    oil_idle_limit_max = 5.5   # spec Volvo D13: warm >1100rpm max 550 kPa

    # ── Fuel supply pressure ──────────────────────────────────────────────────
    # Text: "Result Fuel feed pressure check ... @600 Rpm : 384 kPa\n@1200 Rpm : 474 kPa"
    fuel_600  = find_float(r'@600\s*Rpm\s*:\s*([\d.]+)\s*kPa')
    fuel_1200 = find_float(r'@1200\s*Rpm\s*:\s*([\d.]+)\s*kPa')
    fuel_supply_press     = round(fuel_600  / 100, 3) if fuel_600  else None  # kPa → bar
    fuel_supply_press_hi  = round(fuel_1200 / 100, 3) if fuel_1200 else None

    # ── Injector shut-off (dari OCR tabel Evaluation per silinder) ────────────
    injector_shutoff = None
    shutoff_block = re.search(
        r'Injector\s*Shut\s*off\s*Manual(.*?)(?:F\.\s*Clutch|Clutch\s*wear)',
        ocr_full, re.DOTALL | re.IGNORECASE)
    if shutoff_block:
        sb = shutoff_block.group(1)
        # OCR pola: "NNN rpm  Engine speed" dan "N%  Engine load" dan "N.NNL/h  Engine fuel rate"
        # Cari blok per silinder — setiap silinder ada 1 set nilai
        # Pattern 1: "596 rpm\nEngine speed" style
        rpms  = [float(x) for x in re.findall(r'(\d{3,4})\s*(?:mpm|rpm)\s*Engine\s*speed', sb, re.IGNORECASE)]
        loads = [float(x) for x in re.findall(r'(\d{1,3})\s*%\s*Engine\s*load', sb, re.IGNORECASE)]
        fuels = [float(x.replace(',', '.')) for x in re.findall(r'([\d]+[.,][\d]+)\s*[Ll][/\\]h\s*Engine\s*fuel', sb, re.IGNORECASE)]
        # Pattern 2: "Engine speed\nNNN rpm" style
        if not rpms:
            rpms  = [float(x) for x in re.findall(r'Engine\s*speed[^\d]*([\d]{3,4})\s*rpm', sb, re.IGNORECASE)]
        if not loads:
            loads = [float(x) for x in re.findall(r'Engine\s*load[^\d]*(\d{1,3})\s*%', sb, re.IGNORECASE)]
        if not fuels:
            fuels = [float(x.replace(',', '.')) for x in re.findall(r'Engine\s*fuel\s*rate[^\d]*([\d]+[.,][\d]+)\s*L', sb, re.IGNORECASE)]
        n = max(len(rpms), len(loads), len(fuels), 0)
        if n >= 2:
            rows = []
            for i in range(min(n, 16)):
                rows.append({
                    'cyl': i + 1,
                    'rpm':      rpms[i]  if i < len(rpms)  else None,
                    'load_pct': loads[i] if i < len(loads) else None,
                    'fuel_lph': fuels[i] if i < len(fuels) else None,
                })
            if rows: injector_shutoff = rows

    # ── Clutch wear ───────────────────────────────────────────────────────────
    # Teks native: "Clutch wear position in 12,4 mm"
    clutch_raw     = find(r'[Cc]lutch wear position in\s*([\d,\.]+)\s*mm')
    clutch_current = float(clutch_raw.replace(',', '.')) if clutch_raw else None

    # OCR tabel clutch: X1 (kampas baru), X2 (posisi kini), X1-X2 (diff), limit
    # Format tabel 2-kolom — 250 DPI: label dan nilai di baris terpisah; 200 DPI: inline
    clutch_block = re.search(r'(?:CLUTCH WEAR|Clutch\s*wear\s*(?:check|DATA))(.*?)(?:REPORTED\s*BY|Information\b|$)',
                             ocr_full, re.DOTALL | re.IGNORECASE)
    clutch_new   = None
    clutch_diff  = None
    clutch_limit = None
    if clutch_block:
        cb = clutch_block.group(1)
        # Coba dulu inline (label & nilai di baris yang sama / berdekatan)
        m_new  = re.search(r'\(X1\)[^\d\n]*([\d]+[.,]?[\d]*)\s*mm', cb, re.IGNORECASE)
        m_x2   = re.search(r'\(X2\)[^\d\n]*([\d]+[.,]?[\d]*)\s*mm', cb, re.IGNORECASE)
        m_diff = re.search(r'X1[-—]X.\)[^\d\n]*([\d]+[.,][\d]+)\s*mm', cb, re.IGNORECASE)
        m_lim  = re.search(r'(?:at[/\\]?less\s*than|atless\s*than)[^\d\n]*([\d]+[.,][\d]+)\s*mm', cb, re.IGNORECASE)
        if m_new:  clutch_new     = float(m_new.group(1).replace(',', '.'))
        if m_x2:   clutch_current = float(m_x2.group(1).replace(',', '.'))
        if m_diff: clutch_diff    = float(m_diff.group(1).replace(',', '.'))
        if m_lim:  clutch_limit   = float(m_lim.group(1).replace(',', '.'))

        # Fallback: nilai mm muncul di baris terpisah dari label (250 DPI style)
        # Urutan OCR: X1(new) → X2(current) → diff → limit
        if not clutch_new:
            # Match angka dengan atau tanpa desimal sebelum "mm"
            all_mm = re.findall(r'[|\\]?([\d]+(?:[.,][\d]+)?)\s*mm', cb)
            mm_vals = []
            for v in all_mm:
                try:
                    f = float(v.replace(',', '.'))
                    if 1.0 <= f <= 30.0:
                        mm_vals.append(f)
                except:
                    pass
            # Deduplicate consecutive duplicates (OCR kadang baca nilai dua kali)
            mm_dedup = []
            for f in mm_vals:
                if not mm_dedup or abs(f - mm_dedup[-1]) > 0.1:
                    mm_dedup.append(f)
            if len(mm_dedup) >= 4:
                clutch_new, clutch_current, clutch_diff, clutch_limit = mm_dedup[:4]
            elif len(mm_dedup) == 3:
                clutch_new, clutch_current, clutch_diff = mm_dedup[:3]

        if not clutch_diff and clutch_new and clutch_current:
            clutch_diff = round(clutch_new - clutch_current, 2)

    # ── Overall status ────────────────────────────────────────────────────────
    note_text = full_text.upper()
    if 'ENGINE IN GOOD PERFORMANCE' in note_text or 'GOOD PERFORMANCE' in note_text:
        overall_status = 'OK'
    elif 'NOT OK' in note_text or 'FAILED' in note_text:
        overall_status = 'NOT OK'
    else:
        overall_status = None

    # ── Findings auto-generated ───────────────────────────────────────────────
    findings_parts = []
    if comp_cyl:
        mn, mx = min(comp_cyl), max(comp_cyl)
        findings_parts.append(f'Compression: {mn}–{mx}% ({"✓ semua >80%" if mn>=80 else "⚠ ada <80%"})')
    if cylinder_balancing:
        md = max(abs(v) for v in cylinder_balancing)
        findings_parts.append(f'Balancing max deviasi: {md}%')
    if oil_press_idle:
        findings_parts.append(f'Oil pressure idle: {oil_press_idle} bar')
    if fuel_supply_press:
        s = f'Fuel supply: {fuel_supply_press} bar @idle'
        if fuel_supply_press_hi:
            s += f', {fuel_supply_press_hi} bar @1200rpm'
        findings_parts.append(s)
    if clutch_current:
        cw = f'Clutch wear: {clutch_current} mm'
        if clutch_new:   cw += f' (baru {clutch_new} mm)'
        if clutch_limit: cw += f', limit {clutch_limit} mm, sisa {round(clutch_current-clutch_limit,1)} mm'
        findings_parts.append(cw)
    note_m = re.search(r'NOTE\s*[\n\r]+(.+)', full_text, re.IGNORECASE)
    if note_m:
        findings_parts.append(note_m.group(1).strip())
    findings = ' | '.join(findings_parts) if findings_parts else None

    return {
        'unit_code':                unit_code,
        'measurement_date':         measurement_date or date.today().isoformat(),
        'hm_smu':                   hm_smu,
        'mechanic':                 mechanic,
        'location':                 'PT Indotruck Utama',
        'cylinders':                len(comp_cyl) if comp_cyl else 6,
        'comp_cyl':                 comp_cyl,
        'comp_limit_min':           80.0,
        'cylinder_balancing':       cylinder_balancing,
        'oil_press_idle':           oil_press_idle,
        'oil_press_idle_limit_min': oil_idle_limit_min,
        'oil_press_idle_limit_max': 5.5,
        'fuel_supply_press':        fuel_supply_press,
        'fuel_supply_press_limit_min': 1.0,
        'exhaust_backpress':        None,
        'injector_shutoff':         injector_shutoff if injector_shutoff else None,
        'clutch_wear_new_mm':       clutch_new,
        'clutch_wear_current_mm':   clutch_current,
        'clutch_wear_diff_mm':      clutch_diff,
        'clutch_wear_limit_mm':     clutch_limit,
        'source_doc':               filename,
        'overall_status':           overall_status,
        'findings':                 findings,
        'recommendation':           None,
        # fields not in model but unused:
        'battery_limit_min':        12.0,
        'battery_limit_max':        14.8,
        'blowby_unit':              'cfm',
    }


@router.post('/engine/import-pdf', summary='Import PDF laporan Performance Test')
async def import_pdf(file: UploadFile = File(...)):
    if not file.filename.lower().endswith('.pdf'):
        raise HTTPException(400, 'File harus berformat PDF')

    # Simpan ke tmp
    suffix = Path(file.filename).suffix
    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
        tmp.write(await file.read())
        tmp_path = tmp.name

    try:
        parsed = _parse_mip_pdf(tmp_path, file.filename)
        if not parsed.get('unit_code'):
            # Ambil preview teks halaman pertama untuk diagnosa
            try:
                import fitz as _fitz
                _doc = _fitz.open(tmp_path)
                _preview = _doc[0].get_text()[:800]
            except Exception:
                _preview = '(tidak bisa membaca teks PDF)'
            raise HTTPException(422, {
                'error': 'Tidak dapat mengekstrak unit code dari PDF.',
                'hint': 'Pastikan PDF dari MIP/Indotruck dengan field UNIT CODE.',
                'text_preview': _preview,
            })
    finally:
        Path(tmp_path).unlink(missing_ok=True)

    # Buat payload sesuai model
    payload = EngineMeasurementIn(**{k: v for k, v in parsed.items() if k in EngineMeasurementIn.__fields__})

    conn = get_db()
    if not conn:
        raise HTTPException(503, 'Database tidak tersedia')
    ensure_table(conn)
    cur = conn.cursor()
    d = payload.dict()
    d['comp_cyl']          = json.dumps(d['comp_cyl'])          if d.get('comp_cyl')          else None
    d['cylinder_balancing'] = json.dumps(d['cylinder_balancing']) if d.get('cylinder_balancing') else None
    d['injector_shutoff']  = json.dumps(d['injector_shutoff'])  if d.get('injector_shutoff')  else None
    cur.execute("""
        INSERT INTO engine_measurements (
            unit_code, measurement_date, hm_smu, mechanic, location,
            cylinders, comp_cyl, comp_limit_min, comp_limit_max, comp_max_diff,
            blowby_value, blowby_unit, blowby_limit,
            oil_press_idle, oil_press_rated,
            oil_press_idle_limit_min, oil_press_idle_limit_max,
            oil_press_rated_limit_min, oil_press_rated_limit_max,
            coolant_temp_idle, coolant_temp_rated, coolant_temp_limit,
            boost_pressure, boost_limit_min, boost_limit_max,
            rpm_idle, rpm_high_idle, rpm_rated,
            rpm_idle_spec, rpm_high_idle_spec, rpm_rated_spec,
            fuel_consumption, fuel_consumption_limit,
            battery_voltage, battery_limit_min, battery_limit_max,
            exhaust_backpress, exhaust_backpress_limit,
            fuel_supply_press, fuel_supply_press_limit_min, fuel_supply_press_limit_max,
            cylinder_balancing, injector_shutoff,
            clutch_wear_new_mm, clutch_wear_current_mm, clutch_wear_diff_mm, clutch_wear_limit_mm,
            source_doc, overall_status, findings, recommendation
        ) VALUES (
            %(unit_code)s, %(measurement_date)s, %(hm_smu)s, %(mechanic)s, %(location)s,
            %(cylinders)s, %(comp_cyl)s, %(comp_limit_min)s, %(comp_limit_max)s, %(comp_max_diff)s,
            %(blowby_value)s, %(blowby_unit)s, %(blowby_limit)s,
            %(oil_press_idle)s, %(oil_press_rated)s,
            %(oil_press_idle_limit_min)s, %(oil_press_idle_limit_max)s,
            %(oil_press_rated_limit_min)s, %(oil_press_rated_limit_max)s,
            %(coolant_temp_idle)s, %(coolant_temp_rated)s, %(coolant_temp_limit)s,
            %(boost_pressure)s, %(boost_limit_min)s, %(boost_limit_max)s,
            %(rpm_idle)s, %(rpm_high_idle)s, %(rpm_rated)s,
            %(rpm_idle_spec)s, %(rpm_high_idle_spec)s, %(rpm_rated_spec)s,
            %(fuel_consumption)s, %(fuel_consumption_limit)s,
            %(battery_voltage)s, %(battery_limit_min)s, %(battery_limit_max)s,
            %(exhaust_backpress)s, %(exhaust_backpress_limit)s,
            %(fuel_supply_press)s, %(fuel_supply_press_limit_min)s, %(fuel_supply_press_limit_max)s,
            %(cylinder_balancing)s, %(injector_shutoff)s,
            %(clutch_wear_new_mm)s, %(clutch_wear_current_mm)s, %(clutch_wear_diff_mm)s, %(clutch_wear_limit_mm)s,
            %(source_doc)s, %(overall_status)s, %(findings)s, %(recommendation)s
        ) RETURNING id
    """, d)
    new_id = cur.fetchone()[0]
    conn.commit(); cur.close(); conn.close()
    return {
        'success': True,
        'id': new_id,
        'parsed': {k: v for k, v in parsed.items() if v is not None and k not in ('battery_limit_min','battery_limit_max','blowby_unit')},
    }
