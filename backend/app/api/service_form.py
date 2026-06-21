"""
Service Form API
  GET  /api/form/last-smu/{unit_no}          — SMU terakhir unit (untuk ghost text)
  POST /api/form/analyze-photo               — AI rating dari foto inspeksi
  GET  /api/form/parts/search                — cari sparepart dari katalog
  GET  /api/form/parts/task/{task_no}        — sparepart default untuk task tertentu
  POST /api/form/backlog                     — simpan item backlog dengan sparepart
  GET  /api/form/backlog/{unit_no}           — list backlog unit
  POST /api/form/fmx/save                   — simpan record FMX PM ke PostgreSQL
  GET  /api/form/fmx/history/{unit_no}      — history PM per unit
  GET  /api/form/fmx/{record_id}            — detail satu record
"""

import json
import shutil
import sqlite3
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Optional
from fastapi import APIRouter, UploadFile, File, HTTPException, Query
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

import os
import psycopg2
import psycopg2.extras

from app.services.photo_analyzer import analyze_inspection_photo

router = APIRouter(prefix='/api/form', tags=['Service Form'])

DB_URL = os.getenv('DATABASE_URL', 'postgresql://pdm:pdm_secret@localhost:5432/pdm').replace('+asyncpg', '')

def get_pg():
    return psycopg2.connect(DB_URL, cursor_factory=psycopg2.extras.RealDictCursor)


# ── FMX PM Record Models ───────────────────────────────────────────────────────

class FMXRecord(BaseModel):
    unit_no: str
    wo: str
    form_type: Optional[str] = 'FMX_PM_DT'
    service_date: Optional[str] = None
    interval_pm: Optional[int] = None
    smu_hours: Optional[float] = None
    smu_prev: Optional[float] = None
    lokasi: Optional[str] = None
    supervisor: Optional[str] = None
    mechanic_pic: Optional[str] = None
    sub_section_head: Optional[str] = None
    section_head: Optional[str] = None
    start_time: Optional[str] = None
    end_time: Optional[str] = None
    manhour: Optional[float] = None
    tasks: list = []
    mechanics: list = []
    backlogs: list = []
    brake: dict = {}
    sos: dict = {}
    tyre: list = []
    photos: dict = {}
    signatures: dict = {}
    raw_st: dict = {}


@router.post('/fmx/save')
def save_fmx(body: FMXRecord):
    unit = body.unit_no.strip().upper()
    wo   = body.wo.strip()
    if not unit or not wo:
        raise HTTPException(400, 'unit_no dan wo wajib diisi')
    conn = get_pg()
    try:
        with conn.cursor() as cur:
            cur.execute('''
                INSERT INTO pm_service_records
                    (unit_no, wo, service_date, interval_pm, smu_hours, smu_prev,
                     lokasi, supervisor, mechanic_pic, sub_section_head, section_head,
                     start_time, end_time, manhour,
                     tasks, mechanics, backlogs, brake, sos, tyre, raw_st, photos, signatures, updated_at)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,NOW())
                ON CONFLICT (unit_no, wo) DO UPDATE SET
                    service_date=EXCLUDED.service_date, interval_pm=EXCLUDED.interval_pm,
                    smu_hours=EXCLUDED.smu_hours, smu_prev=EXCLUDED.smu_prev,
                    lokasi=EXCLUDED.lokasi, supervisor=EXCLUDED.supervisor,
                    mechanic_pic=EXCLUDED.mechanic_pic,
                    sub_section_head=EXCLUDED.sub_section_head,
                    section_head=EXCLUDED.section_head,
                    start_time=EXCLUDED.start_time, end_time=EXCLUDED.end_time,
                    manhour=EXCLUDED.manhour, tasks=EXCLUDED.tasks,
                    mechanics=EXCLUDED.mechanics, backlogs=EXCLUDED.backlogs,
                    brake=EXCLUDED.brake, sos=EXCLUDED.sos, tyre=EXCLUDED.tyre,
                    raw_st=EXCLUDED.raw_st, photos=EXCLUDED.photos,
                    signatures=EXCLUDED.signatures, updated_at=NOW()
                RETURNING id
            ''', (
                unit, wo, body.service_date or None, body.interval_pm, body.smu_hours, body.smu_prev,
                body.lokasi, body.supervisor, body.mechanic_pic, body.sub_section_head, body.section_head,
                body.start_time or None, body.end_time or None, body.manhour,
                json.dumps(body.tasks), json.dumps(body.mechanics), json.dumps(body.backlogs),
                json.dumps(body.brake), json.dumps(body.sos), json.dumps(body.tyre),
                json.dumps(body.raw_st), json.dumps(body.photos), json.dumps(body.signatures),
            ))
            row = cur.fetchone()
        conn.commit()
        return {'ok': True, 'id': row['id'], 'unit_no': unit, 'wo': wo}
    finally:
        conn.close()


@router.get('/fmx/history/{unit_no}')
def fmx_history(unit_no: str, limit: int = 20):
    conn = get_pg()
    try:
        with conn.cursor() as cur:
            cur.execute('''
                SELECT id, unit_no, wo, service_date, interval_pm, smu_hours,
                       mechanic_pic, lokasi, manhour, updated_at
                FROM pm_service_records
                WHERE unit_no = %s
                ORDER BY service_date DESC NULLS LAST
                LIMIT %s
            ''', (unit_no.upper(), limit))
            rows = [dict(r) for r in cur.fetchall()]
        for r in rows:
            if r.get('updated_at'): r['updated_at'] = r['updated_at'].isoformat()[:10]
            if r.get('service_date'): r['service_date'] = str(r['service_date'])
        return {'unit_no': unit_no.upper(), 'total': len(rows), 'records': rows}
    finally:
        conn.close()


@router.get('/fmx/{record_id}')
def get_fmx(record_id: int):
    conn = get_pg()
    try:
        with conn.cursor() as cur:
            cur.execute('SELECT * FROM pm_service_records WHERE id = %s', (record_id,))
            row = cur.fetchone()
        if not row:
            raise HTTPException(404, 'Record tidak ditemukan')
        r = dict(row)
        if r.get('updated_at'): r['updated_at'] = r['updated_at'].isoformat()
        if r.get('created_at'): r['created_at'] = r['created_at'].isoformat()
        if r.get('service_date'): r['service_date'] = str(r['service_date'])
        return r
    finally:
        conn.close()

@router.get('/fmx/{record_id}/print', response_class=HTMLResponse)
def print_fmx(record_id: int):
    conn = get_pg()
    try:
        with conn.cursor() as cur:
            cur.execute('SELECT * FROM pm_service_records WHERE id = %s', (record_id,))
            row = cur.fetchone()
        if not row:
            raise HTTPException(404, 'Record tidak ditemukan')
        r = dict(row)
    finally:
        conn.close()

    tasks = r.get('tasks') or []
    photos = r.get('photos') or {}
    sigs = r.get('signatures') or {}
    mechanics = r.get('mechanics') or []
    sos = r.get('sos') or {}
    brake = r.get('brake') or {}
    tyre = r.get('tyre') or []

    def fmt_date(d):
        return str(d) if d else '-'

    # Build task rows HTML
    task_rows = ''
    for t in tasks:
        result_icon = '✅' if t.get('result') else '☐'
        comment = t.get('comment', '') or ''
        photo_html = ''
        task_no = t.get('task_no', '')
        if str(task_no) in photos:
            photo_html = f'<img src="{photos[str(task_no)]}" style="max-width:120px;max-height:80px;border:1px solid #ccc">'
        meas_html = ''
        for m in (t.get('measurements') or []):
            val = m.get('value', '') or ''
            unit = m.get('unit', '') or ''
            meas_html += f'<span style="margin-right:8px">{m.get("label","")}: <b>{val} {unit}</b></span>'
        task_rows += f'''<tr>
            <td style="text-align:center">{task_no}</td>
            <td>{t.get("desc","")}</td>
            <td style="text-align:center">{result_icon}</td>
            <td>{meas_html}{comment}</td>
            <td>{photo_html}</td>
        </tr>'''

    # Mechanic rows
    mech_rows = ''.join(f'<tr><td>{i+1}</td><td>{m.get("name","")}</td><td>{m.get("nip","")}</td><td>{m.get("jabatan","")}</td></tr>'
                        for i, m in enumerate(mechanics))

    # Signatures
    sig_html = ''
    for key, label in [('mech', 'Mekanik PIC'), ('ssh', 'Sub Section Head'), ('sh', 'Section Head')]:
        img = sigs.get(key) or ''
        if img:
            sig_html += f'<div style="display:inline-block;text-align:center;margin:0 20px"><p style="margin:0;font-size:12px">{label}</p><img src="{img}" style="width:160px;height:80px;border-bottom:1px solid #333;display:block"></div>'
        else:
            sig_html += f'<div style="display:inline-block;text-align:center;margin:0 20px"><p style="margin:0;font-size:12px">{label}</p><div style="width:160px;height:80px;border-bottom:1px solid #333"></div></div>'

    html = f'''<!DOCTYPE html>
<html lang="id">
<head>
<meta charset="UTF-8">
<title>Sheet Service Volvo FMX — {r.get("unit_no","")} / {r.get("wo","")}</title>
<style>
body{{font-family:Arial,sans-serif;font-size:13px;margin:20px;color:#222}}
h2{{text-align:center;margin-bottom:4px}}
.sub{{text-align:center;color:#555;margin-bottom:16px}}
table{{width:100%;border-collapse:collapse;margin-bottom:16px}}
th,td{{border:1px solid #bbb;padding:5px 8px;vertical-align:top}}
th{{background:#1a3a5c;color:#fff;text-align:left}}
.info-grid{{display:grid;grid-template-columns:1fr 1fr;gap:6px 24px;margin-bottom:16px}}
.info-item{{display:flex;gap:8px}}.info-item label{{font-weight:bold;min-width:120px;color:#555}}
.section-title{{background:#1a3a5c;color:#fff;padding:6px 10px;font-weight:bold;margin:12px 0 6px}}
.sig-area{{display:flex;justify-content:space-around;margin-top:24px;padding-top:16px}}
@media print{{body{{margin:0}}.no-print{{display:none}}}}
</style>
</head>
<body>
<button class="no-print" onclick="window.print()" style="position:fixed;top:10px;right:10px;padding:8px 16px;background:#1a3a5c;color:#fff;border:none;border-radius:4px;cursor:pointer;font-size:14px">🖨 Print / Save PDF</button>

<h2>Sheet Service Volvo FMX</h2>
<div class="sub">PT Bukit Asam Tbk — Reliability & Predictive Maintenance</div>

<div class="info-grid">
  <div class="info-item"><label>Unit No:</label><span>{r.get("unit_no","")}</span></div>
  <div class="info-item"><label>Work Order:</label><span>{r.get("wo","")}</span></div>
  <div class="info-item"><label>Tanggal Service:</label><span>{fmt_date(r.get("service_date"))}</span></div>
  <div class="info-item"><label>Interval PM:</label><span>{r.get("interval_pm","")} jam</span></div>
  <div class="info-item"><label>SMU Hours:</label><span>{r.get("smu_hours","")}</span></div>
  <div class="info-item"><label>SMU Prev:</label><span>{r.get("smu_prev","")}</span></div>
  <div class="info-item"><label>Lokasi:</label><span>{r.get("lokasi","")}</span></div>
  <div class="info-item"><label>Supervisor:</label><span>{r.get("supervisor","")}</span></div>
  <div class="info-item"><label>Mekanik PIC:</label><span>{r.get("mechanic_pic","")}</span></div>
  <div class="info-item"><label>Man Hour:</label><span>{r.get("manhour","")} jam</span></div>
</div>

<div class="section-title">Daftar Mekanik</div>
<table>
  <thead><tr><th>#</th><th>Nama</th><th>NIP</th><th>Jabatan</th></tr></thead>
  <tbody>{mech_rows}</tbody>
</table>

<div class="section-title">Task Pemeriksaan</div>
<table>
  <thead><tr><th style="width:40px">#</th><th>Deskripsi Task</th><th style="width:40px">OK</th><th>Pengukuran / Keterangan</th><th style="width:130px">Foto</th></tr></thead>
  <tbody>{task_rows}</tbody>
</table>

<div class="section-title">Tanda Tangan</div>
<div class="sig-area">{sig_html}</div>

<div class="section-title no-print" style="margin-top:24px">Semua Foto Tugas</div>
<div class="no-print" style="display:flex;flex-wrap:wrap;gap:12px;margin-bottom:16px">
{"".join(f'<div style="text-align:center"><p style="margin:0;font-size:11px">Task {k}</p><img src="{v}" style="max-width:200px;max-height:150px;border:1px solid #ccc"></div>' for k, v in photos.items())}
</div>

<p style="color:#888;font-size:11px;margin-top:24px">Dicetak: {r.get("created_at","")}</p>
</body>
</html>'''
    return HTMLResponse(content=html)


@router.get('/fmx')
def list_fmx(limit: int = 100, offset: int = 0):
    conn = get_pg()
    try:
        with conn.cursor() as cur:
            cur.execute('SELECT COUNT(*) as n FROM pm_service_records')
            total = cur.fetchone()['n']
            cur.execute('''SELECT id, unit_no, wo, service_date, interval_pm, smu_hours,
                mechanic_pic, lokasi, form_type, created_at
                FROM pm_service_records ORDER BY created_at DESC LIMIT %s OFFSET %s''',
                (limit, offset))
            rows = []
            for row in cur.fetchall():
                r = dict(row)
                if r.get('service_date'): r['service_date'] = str(r['service_date'])
                if r.get('created_at'): r['created_at'] = r['created_at'].isoformat()
                rows.append(r)
        return {'total': total, 'records': rows}
    finally:
        conn.close()


PARTS_FILE = Path(__file__).parents[2] / 'data' / 'spare_parts_773e.json'
DB_PATH = Path(__file__).parents[2] / 'data' / 'service_records.db'


def get_db():
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_db()
    conn.execute('''
        CREATE TABLE IF NOT EXISTS service_blocks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            unit_no TEXT NOT NULL,
            wo TEXT NOT NULL,
            date TEXT NOT NULL,
            smu REAL,
            smu_prev REAL,
            block_id TEXT UNIQUE,
            payload TEXT NOT NULL,
            created_at TEXT DEFAULT (datetime('now')),
            updated_at TEXT DEFAULT (datetime('now'))
        )
    ''')
    conn.execute('CREATE INDEX IF NOT EXISTS idx_unit ON service_blocks(unit_no)')
    conn.execute('CREATE INDEX IF NOT EXISTS idx_wo ON service_blocks(wo)')
    conn.commit()
    conn.close()


DB_PATH.parent.mkdir(parents=True, exist_ok=True)
init_db()

# In-memory store untuk demo (nanti → DB)
_service_history: dict[str, list[dict]] = {
    # Dummy history dari data nyata PTBA untuk demo
    'DT0034': [{'smu': 14250, 'date': '2026-03-10', 'type': 'PM 2000H', 'wo': 'WO-2024-0312'}],
    'DT0035': [{'smu': 12800, 'date': '2026-02-20', 'type': 'PM 2000H', 'wo': 'WO-2024-0285'}],
    'DT0036': [{'smu': 18900, 'date': '2026-04-01', 'type': 'PM 2000H', 'wo': 'WO-2024-0398'}],
    'DZ0034': [{'smu': 33200, 'date': '2026-05-15', 'type': 'PM 2000H', 'wo': 'WO-2024-0501'}],
    'DZ0038': [{'smu': 28100, 'date': '2026-04-22', 'type': 'PM 2000H', 'wo': 'WO-2024-0445'}],
}
_backlog_store: dict[str, list[dict]] = {}


def load_parts() -> list[dict]:
    with open(PARTS_FILE) as f:
        return json.load(f)['catalog']


# ── Endpoints ─────────────────────────────────────────────────────────────

@router.get('/units')
def get_units():
    return {'units': sorted(_service_history.keys())}


@router.get('/last-smu/{unit_no}')
def get_last_smu(unit_no: str):
    """
    Ambil HM terakhir unit dari history service.
    Dipakai untuk ghost text di field SMU saat form dibuka.
    """
    unit_no = unit_no.strip().upper()
    history = _service_history.get(unit_no, [])
    if not history:
        return {'unit_no': unit_no, 'last_smu': None, 'last_service': None,
                'message': 'Belum ada history service di sistem'}
    last = sorted(history, key=lambda x: x['smu'], reverse=True)[0]
    return {
        'unit_no': unit_no,
        'last_smu': last['smu'],
        'last_service': last,
        'message': f'PM terakhir: {last["smu"]:,} HM pada {last["date"]}'
    }


@router.post('/analyze-photo')
async def analyze_photo(
    file: UploadFile = File(...),
    sample_type: str = Query('magnetic_plug', description='magnetic_plug | filter_cut | screen')
):
    """
    Upload foto magnetic plug / filter cut / screen.
    AI analisa dan kembalikan rating A/B/C + detail temuan.
    """
    if not file.content_type.startswith('image/'):
        raise HTTPException(400, 'File harus berupa gambar (JPEG/PNG)')

    if sample_type not in ('magnetic_plug', 'filter_cut', 'screen'):
        raise HTTPException(400, 'sample_type harus: magnetic_plug, filter_cut, atau screen')

    image_bytes = await file.read()
    if len(image_bytes) > 20 * 1024 * 1024:
        raise HTTPException(413, 'Ukuran gambar maksimal 20 MB')

    result = analyze_inspection_photo(image_bytes, sample_type)
    if 'error' in result:
        raise HTTPException(422, result['error'])

    return result


@router.get('/parts/search')
def search_parts(
    q: str = Query('', description='Kata kunci nama/part number'),
    category: Optional[str] = Query(None, description='filter/oil/seal/breather/dll'),
    limit: int = Query(10, le=50)
):
    """
    Cari sparepart dari katalog. Dipakai untuk autocomplete di form.
    """
    parts = load_parts()
    q_lower = q.lower().strip()
    results = []
    for p in parts:
        match = (
            q_lower in p['part_no'].lower() or
            q_lower in p['description'].lower() or
            (category and p['category'] == category)
        )
        if not q_lower and not category:
            match = True
        if match:
            results.append(p)
    return {'total': len(results), 'parts': results[:limit]}


@router.get('/parts/task/{task_no}')
def parts_for_task(task_no: int):
    """
    Ambil sparepart default untuk nomor task tertentu.
    Misalnya task 28 (ganti oil filter) → part 1R-0716, 1R-0755
    """
    parts = load_parts()
    matched = [p for p in parts if task_no in p.get('tasks', [])]
    return {
        'task_no': task_no,
        'parts': matched,
        'message': f'{len(matched)} part ditemukan untuk task {task_no}'
    }


class BacklogItem(BaseModel):
    unit_no: str
    task_no: Optional[int] = None
    description: str
    finding: str                  # catatan temuan dari mekanik
    priority: str = 'P3'          # P1=immediate, P2=urgent, P3=scheduled, P4=monitor
    suggested_parts: list[dict] = []
    wo_reference: Optional[str] = None
    smu_found: Optional[float] = None


@router.post('/backlog')
def add_backlog(item: BacklogItem):
    """Tambah item backlog dari form service. Auto-suggest parts jika task_no diketahui."""
    unit = item.unit_no.upper()
    if not _backlog_store.get(unit):
        _backlog_store[unit] = []

    # Auto-suggest parts dari task_no
    auto_parts = []
    if item.task_no:
        parts = load_parts()
        auto_parts = [p for p in parts if item.task_no in p.get('tasks', [])]

    entry = item.model_dump()
    entry['auto_suggested_parts'] = auto_parts
    entry['status'] = 'open'

    _backlog_store[unit].append(entry)
    return {'message': 'Backlog disimpan', 'unit': unit,
            'total_open': len(_backlog_store[unit]), 'auto_parts': auto_parts}


@router.get('/backlog/{unit_no}')
def get_backlog(unit_no: str):
    unit = unit_no.upper()
    items = _backlog_store.get(unit, [])
    # Urutkan: P1 dulu
    priority_rank = {'P1': 0, 'P2': 1, 'P3': 2, 'P4': 3}
    items_sorted = sorted(items, key=lambda x: priority_rank.get(x.get('priority','P3'), 2))
    return {'unit_no': unit, 'total': len(items_sorted), 'items': items_sorted}


# ── Save complete service block ────────────────────────────────────────────


class ServiceBlock(BaseModel):
    unit_no: str
    wo: str
    date: str
    smu: float
    smu_prev: float = 0
    mechanics: list[dict] = []
    signatories: list[dict] = []
    tasks: list[dict] = []       # each task: {task_no, result, measurement, parts, backlog_status}
    backlogs: list[dict] = []    # {desc, priority, status, closed_action, parts}
    measurements: list[dict] = []  # {type, value, unit} for PDM time-series
    fluids: list[dict] = []      # merged into parts now, kept for legacy


@router.post('/save-service')
def save_service(block: ServiceBlock):
    """Simpan satu blok service lengkap (tasks, backlogs, measurements, parts)."""
    unit = block.unit_no.strip().upper()
    block_id = f"{unit}_{block.wo}_{block.date}"
    payload = json.dumps(block.model_dump())
    conn = get_db()
    try:
        conn.execute('''
            INSERT INTO service_blocks (unit_no, wo, date, smu, smu_prev, block_id, payload, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, datetime('now'))
            ON CONFLICT(block_id) DO UPDATE SET
                payload=excluded.payload,
                smu=excluded.smu,
                updated_at=datetime('now')
        ''', (unit, block.wo, block.date, block.smu, block.smu_prev, block_id, payload))
        conn.commit()
        # Update _service_history so last-smu stays current
        if unit not in _service_history:
            _service_history[unit] = []
        hist = _service_history[unit]
        existing = next((h for h in hist if h.get('wo') == block.wo), None)
        if existing:
            existing['smu'] = block.smu
        else:
            hist.append({'smu': block.smu, 'date': block.date, 'type': 'PM 2000H', 'wo': block.wo})
    finally:
        conn.close()
    return {'status': 'ok', 'block_id': block_id}


@router.get('/service-history/{unit_no}')
def get_service_history(unit_no: str):
    """Ambil 10 blok service terakhir untuk unit tertentu dari SQLite."""
    unit = unit_no.strip().upper()
    conn = get_db()
    try:
        rows = conn.execute(
            'SELECT block_id, wo, date, smu, updated_at FROM service_blocks WHERE unit_no=? ORDER BY date DESC LIMIT 10',
            (unit,)
        ).fetchall()
        return {'unit_no': unit, 'records': [dict(r) for r in rows]}
    finally:
        conn.close()


@router.get('/print/{block_id}')
def get_print_payload(block_id: str):
    """Return the full service block JSON for print rendering."""
    conn = get_db()
    try:
        row = conn.execute('SELECT payload FROM service_blocks WHERE block_id=?', (block_id,)).fetchone()
        if not row:
            raise HTTPException(404, f'Record {block_id} tidak ditemukan')
        return json.loads(row['payload'])
    finally:
        conn.close()


@router.get('/print-html/{block_id}', response_class=HTMLResponse)
def print_html(block_id: str):
    """Return a complete print-ready HTML page for a service record."""
    conn = get_db()
    try:
        row = conn.execute('SELECT payload FROM service_blocks WHERE block_id=?', (block_id,)).fetchone()
        if not row:
            raise HTTPException(404, f'Record {block_id} tidak ditemukan')
        data = json.loads(row['payload'])
    finally:
        conn.close()

    unit = data.get('unit_no', '')
    wo = data.get('wo', '')
    date = data.get('date', '')
    smu = data.get('smu', 0)
    mechanics = ', '.join(m.get('name', '') for m in data.get('mechanics', []) if m.get('name'))

    tasks_html = ''
    for t in data.get('tasks', []):
        if not t.get('result') and not t.get('comment') and not t.get('parts'):
            continue
        parts_str = ', '.join(
            f"{p.get('part_no', '')} {p.get('description', '')} x{p.get('qty', 1)}"
            for p in t.get('parts', [])
        )
        photo_html = f'<img src="{t["photo"]}" style="max-width:200px;max-height:150px">' if t.get('photo') else ''
        tasks_html += f'''<tr>
            <td>{t.get("task_no", "")}</td>
            <td>{t.get("desc", "")}</td>
            <td style="text-align:center">{"&#10003;" if t.get("result") else ""}</td>
            <td>{t.get("comment", "")}</td>
            <td style="font-size:9px">{parts_str}</td>
            <td>{photo_html}</td>
        </tr>'''

    sigs_html = ''
    for key, val in data.get('signatures', {}).items():
        label = key.replace('sup_sign_', '').replace('_', ' ').title()
        sigs_html += (
            f'<div style="display:inline-block;margin:8px 16px;text-align:center">'
            f'<img src="{val}" style="width:120px;height:60px;border-bottom:1px solid #000;display:block">'
            f'<div style="font-size:10px;margin-top:4px">{label}</div></div>'
        )

    try:
        smu_fmt = f'{float(smu):,.0f}'
    except (ValueError, TypeError):
        smu_fmt = str(smu)

    html = f'''<!DOCTYPE html><html><head><meta charset="UTF-8">
<title>Service Record {wo}</title>
<style>
  body{{font-family:Arial,sans-serif;font-size:11px;margin:20px}}
  h1{{font-size:14px;margin:0 0 4px}}
  table{{width:100%;border-collapse:collapse;margin:10px 0}}
  th,td{{border:1px solid #ccc;padding:4px 6px;vertical-align:top}}
  th{{background:#f3f4f6;font-weight:600}}
  .header-grid{{display:grid;grid-template-columns:repeat(4,1fr);gap:8px;margin-bottom:12px;border:1px solid #ccc;padding:8px}}
  .lbl{{font-size:9px;color:#666;text-transform:uppercase}}
  .val{{font-weight:700;font-size:12px}}
  @media print{{@page{{margin:15mm}} button{{display:none}}}}
</style></head><body>
<div style="text-align:center;margin-bottom:12px">
  <h1>773E PRB — PM 2000 HOURS TASK RECORD</h1>
  <div style="font-size:10px;color:#666">PT BUKIT ASAM (PERSERO) Tbk — Unit Pertambangan Tanjung Enim</div>
</div>
<div class="header-grid">
  <div><div class="lbl">Unit</div><div class="val">{unit}</div></div>
  <div><div class="lbl">WO Number</div><div class="val">{wo}</div></div>
  <div><div class="lbl">Tanggal</div><div class="val">{date}</div></div>
  <div><div class="lbl">SMU (HM)</div><div class="val">{smu_fmt}</div></div>
  <div><div class="lbl">Mekanik</div><div class="val">{mechanics}</div></div>
</div>
<button onclick="window.print()" style="margin-bottom:12px;padding:6px 16px;font-size:11px;cursor:pointer">&#128424; Print</button>
<table>
  <thead><tr><th width="30">#</th><th>Task</th><th width="40">Done</th><th width="150">Catatan</th><th width="180">Sparepart</th><th width="120">Foto</th></tr></thead>
  <tbody>{tasks_html}</tbody>
</table>
<div style="margin-top:16px"><b>Tanda Tangan:</b><br>{sigs_html}</div>
</body></html>'''
    return HTMLResponse(content=html)
