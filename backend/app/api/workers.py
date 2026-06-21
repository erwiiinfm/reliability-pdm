from fastapi import APIRouter, Query, HTTPException
from typing import Optional
from pydantic import BaseModel
import psycopg2
import psycopg2.extras
import os

router = APIRouter(prefix='/api/workers', tags=['Workers'])

DB_URL = os.getenv('DATABASE_URL', 'postgresql://pdm:pdm_secret@localhost:5432/pdm')

def get_conn():
    # Support both asyncpg URL format and plain psycopg2
    url = DB_URL.replace('postgresql+asyncpg://', 'postgresql://')
    return psycopg2.connect(url, cursor_factory=psycopg2.extras.RealDictCursor)


@router.get('')
def list_workers(
    tipe: Optional[str] = Query(None, description='ORGANIK or ALIH DAYA'),
    search: Optional[str] = Query(None),
    lokasi: Optional[str] = Query(None),
    jabatan: Optional[str] = Query(None),
    divisi: Optional[str] = Query(None),
    limit: int = Query(5000, ge=1, le=10000),
    offset: int = Query(0, ge=0),
):
    conditions = []
    params = []

    if tipe:
        conditions.append("tipe = %s")
        params.append(tipe)
    if search:
        conditions.append("(UPPER(nama) LIKE %s OR nip_nopeg LIKE %s)")
        params += [f'%{search.upper()}%', f'%{search}%']
    if lokasi:
        conditions.append("UPPER(lokasi_kerja) LIKE %s")
        params.append(f'%{lokasi.upper()}%')
    if jabatan:
        conditions.append("UPPER(jabatan) LIKE %s")
        params.append(f'%{jabatan.upper()}%')
    if divisi:
        conditions.append("UPPER(divisi) LIKE %s")
        params.append(f'%{divisi.upper()}%')

    where = ('WHERE ' + ' AND '.join(conditions)) if conditions else ''

    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(f"SELECT COUNT(*) as cnt FROM workers {where}", params)
            total = cur.fetchone()['cnt']
            cur.execute(
                f"SELECT * FROM workers {where} ORDER BY tipe, nama LIMIT %s OFFSET %s",
                params + [limit, offset]
            )
            data = [dict(r) for r in cur.fetchall()]

    return {'total': total, 'offset': offset, 'limit': limit, 'data': data}


@router.get('/stats')
def worker_stats():
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT tipe, COUNT(*) as n FROM workers GROUP BY tipe")
            by_tipe = {r['tipe']: r['n'] for r in cur.fetchall()}

            cur.execute("SELECT lokasi_kerja, COUNT(*) as n FROM workers WHERE lokasi_kerja != '' GROUP BY lokasi_kerja ORDER BY n DESC LIMIT 15")
            by_lokasi = [dict(r) for r in cur.fetchall()]

            cur.execute("SELECT jabatan, COUNT(*) as n FROM workers WHERE jabatan != '' GROUP BY jabatan ORDER BY n DESC LIMIT 20")
            by_jabatan = [dict(r) for r in cur.fetchall()]

            cur.execute("SELECT pendidikan, COUNT(*) as n FROM workers WHERE pendidikan != '' GROUP BY pendidikan ORDER BY n DESC")
            by_pendidikan = [dict(r) for r in cur.fetchall()]

            cur.execute("SELECT jenis_kelamin, COUNT(*) as n FROM workers WHERE jenis_kelamin != '' GROUP BY jenis_kelamin")
            by_gender = [dict(r) for r in cur.fetchall()]

            cur.execute("""
                SELECT badan_hukum, tipe, COUNT(*) as n
                FROM workers
                WHERE badan_hukum IS NOT NULL AND badan_hukum != ''
                GROUP BY badan_hukum, tipe
                ORDER BY badan_hukum, tipe
            """)
            by_company = [dict(r) for r in cur.fetchall()]

    return {
        'total': sum(by_tipe.values()),
        'by_tipe': by_tipe,
        'by_lokasi': by_lokasi,
        'by_jabatan': by_jabatan,
        'by_pendidikan': by_pendidikan,
        'by_gender': by_gender,
        'by_company': by_company,
    }


class WorkerUpdate(BaseModel):
    nama: Optional[str] = None
    nip_nopeg: Optional[str] = None
    jabatan: Optional[str] = None
    divisi: Optional[str] = None
    departemen: Optional[str] = None
    lokasi_kerja: Optional[str] = None
    jenis_kelamin: Optional[str] = None
    tempat_lahir: Optional[str] = None
    tanggal_lahir: Optional[str] = None
    pendidikan: Optional[str] = None
    tmt: Optional[str] = None
    level: Optional[str] = None
    badan_hukum: Optional[str] = None
    email: Optional[str] = None
    sec_head: Optional[str] = None
    nomor_kontrak: Optional[str] = None
    nomor_spph: Optional[str] = None


@router.get('/{worker_id}')
def get_worker(worker_id: int):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM workers WHERE id = %s", [worker_id])
            row = cur.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail='Worker not found')
    return dict(row)


@router.put('/{worker_id}')
def update_worker(worker_id: int, body: WorkerUpdate):
    fields = {k: v for k, v in body.model_dump().items() if v is not None}
    if not fields:
        raise HTTPException(status_code=400, detail='No fields to update')
    set_clause = ', '.join(f"{k} = %s" for k in fields)
    values = list(fields.values()) + [worker_id]
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(f"UPDATE workers SET {set_clause} WHERE id = %s RETURNING id", values)
            if not cur.fetchone():
                raise HTTPException(status_code=404, detail='Worker not found')
        conn.commit()
    return {'ok': True}
