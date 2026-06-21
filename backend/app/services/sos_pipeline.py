"""
SOS Pipeline — Oil Sampling Data Block
Jalankan: python3 sos_pipeline.py

Ambil file vendor (4 format) → parse → bersihkan → output Excel terstruktur.
Output: output/SOS_Cleaned_<tanggal>.xlsx
"""

import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '../../..'))

import pandas as pd
import numpy as np
from pathlib import Path
from datetime import datetime
from dataclasses import asdict

from backend.app.services.oil_sample_parser import (
    parse_oil_sample_file, OilSampleRecord, load_asset_registry
)

BASE = Path('/Users/macbookprom1/Documents/Data Analyst/reliability-pdm')
ASSET_FILE = str(BASE / '1. Asset Management.xlsx')
OUTPUT_DIR = BASE / 'output'
OUTPUT_DIR.mkdir(exist_ok=True)

VENDOR_FILES = [
    '/Users/macbookprom1/Downloads/Tekenomiks.csv',
    '/Users/macbookprom1/Downloads/Indotruck Utama.xlsx',
    '/Users/macbookprom1/Downloads/Trakindo Utama.xlsx',
    '/Users/macbookprom1/Downloads/UT_PAP_export_16-06-2026_09-43-30.xls',
]

# Kolom final untuk output (urutan penting)
DISPLAY_COLS = [
    # Identitas
    'ptba_unit_code', 'match_method', 'vendor', 'source_file',
    'unit_id_vendor', 'unit_serial',
    # Komponen
    'component', 'component_raw',
    # Tanggal & referensi
    'sampled_at', 'lab_date', 'lab_reference',
    # HM & oli info
    'smu_hours', 'oil_hours', 'oil_changed', 'filter_changed',
    'oil_brand', 'oil_grade',
    # Logam aus (ppm)
    'iron_fe', 'copper_cu', 'aluminum_al', 'chromium_cr',
    'lead_pb', 'tin_sn', 'nickel_ni', 'silicon_si',
    'sodium_na', 'magnesium_mg', 'molybdenum_mo', 'phosphorus_p',
    'zinc_zn', 'calcium_ca', 'boron_b', 'potassium_k', 'barium_ba',
    # Kondisi oli
    'viscosity_40', 'viscosity_100',
    'tan', 'tbn',
    'water_pct', 'soot', 'oxidation', 'nitration',
    'fuel_dilution_pct', 'glycol', 'pq_index', 'iso_code',
    # Assessment
    'vendor_severity', 'vendor_notes',
]

COLUMN_LABELS = {
    'ptba_unit_code':    'Kode Unit PTBA',
    'match_method':      'Metode Match',
    'vendor':            'Vendor Lab',
    'source_file':       'File Sumber',
    'unit_id_vendor':    'ID Unit (Vendor)',
    'unit_serial':       'Serial Chassis',
    'component':         'Komponen (Norm)',
    'component_raw':     'Komponen (Asli)',
    'sampled_at':        'Tgl Sampling',
    'lab_date':          'Tgl Laporan Lab',
    'lab_reference':     'No. Referensi Lab',
    'smu_hours':         'HM Unit (jam)',
    'oil_hours':         'HM Oli (jam)',
    'oil_changed':       'Ganti Oli',
    'filter_changed':    'Ganti Filter',
    'oil_brand':         'Merek Oli',
    'oil_grade':         'Grade Oli',
    'iron_fe':           'Fe (ppm)',
    'copper_cu':         'Cu (ppm)',
    'aluminum_al':       'Al (ppm)',
    'chromium_cr':       'Cr (ppm)',
    'lead_pb':           'Pb (ppm)',
    'tin_sn':            'Sn (ppm)',
    'nickel_ni':         'Ni (ppm)',
    'silicon_si':        'Si (ppm)',
    'sodium_na':         'Na (ppm)',
    'magnesium_mg':      'Mg (ppm)',
    'molybdenum_mo':     'Mo (ppm)',
    'phosphorus_p':      'P (ppm)',
    'zinc_zn':           'Zn (ppm)',
    'calcium_ca':        'Ca (ppm)',
    'boron_b':           'B (ppm)',
    'potassium_k':       'K (ppm)',
    'barium_ba':         'Ba (ppm)',
    'viscosity_40':      'Viskositas 40°C (cSt)',
    'viscosity_100':     'Viskositas 100°C (cSt)',
    'tan':               'TAN (mgKOH/g)',
    'tbn':               'TBN (mgKOH/g)',
    'water_pct':         'Air (%)',
    'soot':              'Jelaga / Soot',
    'oxidation':         'Oksidasi',
    'nitration':         'Nitrasi',
    'fuel_dilution_pct': 'Dilusi BBM (%)',
    'glycol':            'Glikol (kontam)',
    'pq_index':          'PQ Index',
    'iso_code':          'Kode ISO 4406',
    'vendor_severity':   'Status Vendor',
    'vendor_notes':      'Catatan Vendor',
}

SEVERITY_ORDER = {'extreme': 0, 'critical': 1, 'normal': 2, 'good': 3, None: 4}


def records_to_df(records: list) -> pd.DataFrame:
    rows = []
    for r in records:
        d = asdict(r)
        rows.append({k: d.get(k) for k in DISPLAY_COLS})
    df = pd.DataFrame(rows, columns=DISPLAY_COLS)
    df = df.rename(columns=COLUMN_LABELS)
    return df


def severity_color(val):
    colors = {
        'extreme': 'FFFF0000',   # merah
        'critical': 'FFFF6600',  # orange
        'normal': 'FFFFFF00',    # kuning
        'good': 'FF00CC00',      # hijau
    }
    return colors.get(str(val).lower() if val else '', None)


def write_excel(all_records: list, per_vendor: dict, summary_df: pd.DataFrame,
                unit_summary_df: pd.DataFrame, warnings_df: pd.DataFrame):
    today = datetime.now().strftime('%Y-%m-%d')
    outfile = OUTPUT_DIR / f'SOS_Cleaned_{today}.xlsx'

    with pd.ExcelWriter(str(outfile), engine='xlsxwriter') as writer:
        wb = writer.book

        # ── Format styles ──────────────────────────────────────────
        hdr_fmt = wb.add_format({
            'bold': True, 'bg_color': '#1F4E79', 'font_color': 'white',
            'border': 1, 'text_wrap': True, 'valign': 'vcenter', 'align': 'center',
        })
        hdr_unit_fmt = wb.add_format({
            'bold': True, 'bg_color': '#2E75B6', 'font_color': 'white',
            'border': 1, 'text_wrap': True, 'valign': 'vcenter',
        })
        num_fmt = wb.add_format({'num_format': '#,##0.00', 'border': 1})
        int_fmt = wb.add_format({'num_format': '#,##0', 'border': 1})
        date_fmt = wb.add_format({'num_format': 'dd/mm/yyyy', 'border': 1})
        txt_fmt = wb.add_format({'border': 1, 'text_wrap': False})
        wrap_fmt = wb.add_format({'border': 1, 'text_wrap': True})

        sev_fmt = {
            'extreme':  wb.add_format({'bg_color': '#FF0000', 'font_color': 'white', 'bold': True, 'border': 1, 'align': 'center'}),
            'critical': wb.add_format({'bg_color': '#FF6600', 'font_color': 'white', 'bold': True, 'border': 1, 'align': 'center'}),
            'normal':   wb.add_format({'bg_color': '#FFFF00', 'font_color': 'black', 'bold': True, 'border': 1, 'align': 'center'}),
            'good':     wb.add_format({'bg_color': '#00CC00', 'font_color': 'white', 'bold': True, 'border': 1, 'align': 'center'}),
            'none':     wb.add_format({'bg_color': '#D3D3D3', 'font_color': '#666666', 'border': 1, 'align': 'center'}),
        }

        match_fmt = {
            'direct':    wb.add_format({'bg_color': '#E2EFDA', 'border': 1}),
            'chassis':   wb.add_format({'bg_color': '#DDEEFF', 'border': 1}),
            'alias':     wb.add_format({'bg_color': '#FFF2CC', 'border': 1}),
            'unmatched': wb.add_format({'bg_color': '#FFE0E0', 'bold': True, 'border': 1}),
        }

        # ── Sheet 1: Ringkasan ─────────────────────────────────────
        ws = wb.add_worksheet('Ringkasan')
        writer.sheets['Ringkasan'] = ws
        ws.set_zoom(95)

        title_fmt = wb.add_format({'bold': True, 'font_size': 14, 'font_color': '#1F4E79'})
        subtitle_fmt = wb.add_format({'bold': True, 'font_size': 11, 'font_color': '#2E75B6'})
        val_fmt = wb.add_format({'bold': True, 'font_size': 13, 'align': 'center', 'border': 1})
        label_fmt = wb.add_format({'font_size': 10, 'align': 'center', 'border': 1, 'text_wrap': True})

        ws.write(0, 0, '📊 OIL SAMPLING — BLOK DATA SOS', title_fmt)
        ws.write(1, 0, f'PT Bukit Asam (Persero) Tbk — Unit Pertambangan Tanjung Enim', subtitle_fmt)
        ws.write(2, 0, f'Generated: {datetime.now().strftime("%d %B %Y, %H:%M")}', wb.add_format({'italic': True, 'font_color': '#666666'}))

        # KPI cards
        total = len(all_records)
        matched = sum(1 for r in all_records if r.match_method != 'unmatched')
        extremes = sum(1 for r in all_records if r.vendor_severity == 'extreme')
        criticals = sum(1 for r in all_records if r.vendor_severity == 'critical')

        kpi = [
            ('Total Records', total, '#1F4E79', 'white'),
            ('Unit Tercocokkan', matched, '#00B050', 'white'),
            ('Status Extreme', extremes, '#FF0000', 'white'),
            ('Status Critical', criticals, '#FF6600', 'white'),
        ]
        ws.set_row(4, 45)
        ws.set_row(5, 25)
        for i, (label, val, bg, fg) in enumerate(kpi):
            kpi_val_fmt = wb.add_format({'bold': True, 'font_size': 18, 'align': 'center',
                                          'valign': 'vcenter', 'bg_color': bg, 'font_color': fg, 'border': 2})
            kpi_lbl_fmt = wb.add_format({'font_size': 9, 'align': 'center', 'bg_color': '#F2F2F2', 'border': 1, 'text_wrap': True})
            ws.write(4, i, val, kpi_val_fmt)
            ws.write(5, i, label, kpi_lbl_fmt)
            ws.set_column(i, i, 22)

        # Per-vendor breakdown
        ws.write(7, 0, 'Rekap Per Vendor', subtitle_fmt)
        vendor_headers = ['Vendor', 'Total Records', 'Direct', 'Chassis', 'Alias', 'Unmatched', 'Extreme', 'Critical', 'Normal', 'Good']
        for ci, h in enumerate(vendor_headers):
            ws.write(8, ci, h, hdr_fmt)
        row = 9
        for v, recs in per_vendor.items():
            totv = len(recs)
            def cnt(m): return sum(1 for r in recs if r.match_method == m)
            def sev(s): return sum(1 for r in recs if r.vendor_severity == s)
            ws.write(row, 0, v.replace('_', ' ').title(), txt_fmt)
            ws.write(row, 1, totv, int_fmt)
            ws.write(row, 2, cnt('direct'), int_fmt)
            ws.write(row, 3, cnt('chassis'), int_fmt)
            ws.write(row, 4, cnt('alias'), int_fmt)
            ws.write(row, 5, cnt('unmatched'), int_fmt)
            ws.write(row, 6, sev('extreme'), sev_fmt['extreme'] if sev('extreme') else int_fmt)
            ws.write(row, 7, sev('critical'), sev_fmt['critical'] if sev('critical') else int_fmt)
            ws.write(row, 8, sev('normal'), int_fmt)
            ws.write(row, 9, sev('good'), int_fmt)
            row += 1

        # ── Sheet 2: Semua Data ────────────────────────────────────
        df_all = records_to_df(all_records)
        df_all = df_all.sort_values(
            ['Kode Unit PTBA', 'Komponen (Norm)', 'Tgl Sampling'],
            ascending=[True, True, False], na_position='last'
        )
        df_all.to_excel(writer, sheet_name='Semua Data', index=False, startrow=0)
        ws2 = writer.sheets['Semua Data']
        ws2.freeze_panes(1, 4)
        ws2.autofilter(0, 0, len(df_all), len(df_all.columns) - 1)

        # Header row styling
        col_names = list(df_all.columns)
        for ci, col in enumerate(col_names):
            ws2.write(0, ci, col, hdr_fmt)

        # Lebar kolom optimal
        col_widths = {
            'Kode Unit PTBA': 14, 'Metode Match': 12, 'Vendor Lab': 14, 'File Sumber': 30,
            'ID Unit (Vendor)': 20, 'Serial Chassis': 20, 'Komponen (Norm)': 16, 'Komponen (Asli)': 20,
            'Tgl Sampling': 13, 'Tgl Laporan Lab': 13, 'No. Referensi Lab': 18,
            'HM Unit (jam)': 13, 'HM Oli (jam)': 12, 'Ganti Oli': 10, 'Ganti Filter': 11,
            'Merek Oli': 16, 'Grade Oli': 12,
        }
        for ci, col in enumerate(col_names):
            w = col_widths.get(col, 10)
            ws2.set_column(ci, ci, w)

        # Color-code status dan match method di semua data
        sev_col = col_names.index('Status Vendor')
        match_col = col_names.index('Metode Match')
        for ri, row_data in enumerate(df_all.itertuples(index=False), start=1):
            sv = str(getattr(row_data, 'Status_Vendor', '') or '').lower()
            mm = str(getattr(row_data, 'Metode_Match', '') or '').lower()
            ws2.write(ri, sev_col, sv if sv != 'none' else '', sev_fmt.get(sv, sev_fmt['none']))
            ws2.write(ri, match_col, mm if mm != 'none' else '', match_fmt.get(mm, txt_fmt))

        # ── Sheet 3: Per Vendor ────────────────────────────────────
        for vendor_name, recs in per_vendor.items():
            sname = vendor_name.replace('_', ' ').title()[:31]
            df_v = records_to_df(recs)
            df_v = df_v.sort_values('Tgl Sampling', ascending=False, na_position='last')
            df_v.to_excel(writer, sheet_name=sname, index=False)
            wsv = writer.sheets[sname]
            wsv.freeze_panes(1, 3)
            wsv.autofilter(0, 0, len(df_v), len(df_v.columns) - 1)
            for ci, col in enumerate(df_v.columns):
                wsv.write(0, ci, col, hdr_fmt)
                wsv.set_column(ci, ci, col_widths.get(col, 10))

        # ── Sheet 4: Ringkasan Per Unit ────────────────────────────
        unit_summary_df.to_excel(writer, sheet_name='Per Unit', index=False)
        ws_u = writer.sheets['Per Unit']
        ws_u.freeze_panes(1, 2)
        ws_u.autofilter(0, 0, len(unit_summary_df), len(unit_summary_df.columns) - 1)
        for ci, col in enumerate(unit_summary_df.columns):
            ws_u.write(0, ci, col, hdr_unit_fmt)
            ws_u.set_column(ci, ci, 16)

        # ── Sheet 5: Tidak Tercocokkan ─────────────────────────────
        if not warnings_df.empty:
            warnings_df.to_excel(writer, sheet_name='Perlu Konfirmasi', index=False)
            ws_w = writer.sheets['Perlu Konfirmasi']
            for ci, col in enumerate(warnings_df.columns):
                ws_w.write(0, ci, col, wb.add_format({
                    'bold': True, 'bg_color': '#FF0000', 'font_color': 'white', 'border': 1
                }))
                ws_w.set_column(ci, ci, 22)

    print(f'\n✅ Output: {outfile}')
    return outfile


def build_unit_summary(all_records: list, registry) -> pd.DataFrame:
    """Satu baris per unit × komponen, kolom = last sample + trend info."""
    from collections import defaultdict
    groups = defaultdict(list)
    for r in all_records:
        if r.ptba_unit_code:
            groups[(r.ptba_unit_code, r.component)].append(r)

    rows = []
    for (unit, comp), recs in sorted(groups.items()):
        recs_sorted = sorted(recs, key=lambda r: r.sampled_at or datetime.min, reverse=True)
        last = recs_sorted[0]
        n = len(recs_sorted)

        # Trend Fe: banding 3 last samples
        fe_vals = [r.iron_fe for r in recs_sorted[:3] if r.iron_fe is not None]
        fe_trend = None
        if len(fe_vals) >= 2:
            if fe_vals[0] > fe_vals[-1] * 1.2:
                fe_trend = '↑ Naik'
            elif fe_vals[0] < fe_vals[-1] * 0.8:
                fe_trend = '↓ Turun'
            else:
                fe_trend = '→ Stabil'

        rows.append({
            'Kode Unit PTBA':    unit,
            'Komponen':          comp,
            'Jumlah Sample':     n,
            'Tgl Sample Terakhir': last.sampled_at,
            'HM Terakhir':       last.smu_hours,
            'Status Terakhir':   last.vendor_severity,
            'Fe Terakhir (ppm)': last.iron_fe,
            'Cu Terakhir (ppm)': last.copper_cu,
            'Al Terakhir (ppm)': last.aluminum_al,
            'Si Terakhir (ppm)': last.silicon_si,
            'Vis40 Terakhir':    last.viscosity_40,
            'TBN Terakhir':      last.tbn,
            'Air % Terakhir':    last.water_pct,
            'Trend Fe (3 sample)': fe_trend,
            'Vendor':            last.vendor,
        })

    return pd.DataFrame(rows)


def run():
    print('='*60)
    print('SOS PIPELINE — Oil Sampling Data Block')
    print('='*60)

    all_records = []
    per_vendor = {}
    unmatched_rows = []

    registry = None
    try:
        registry = load_asset_registry(ASSET_FILE)
        print(f'Asset registry: {len(registry)} unit loaded')
    except Exception as e:
        print(f'WARN: Asset registry gagal dibaca: {e}')

    for filepath in VENDOR_FILES:
        if not Path(filepath).exists():
            print(f'\nSKIP (tidak ada): {filepath}')
            continue

        result = parse_oil_sample_file(filepath, ASSET_FILE)
        vendor = result['vendor']
        recs = result['records']

        print(f'\n{"─"*50}')
        print(f'File  : {Path(filepath).name}')
        print(f'Vendor: {vendor}')
        print(f'Record: {len(recs):,}')
        print(f'Match : {result.get("match_summary", {})}')
        for w in result.get('warnings', []):
            print(f'WARN  : {w}')

        per_vendor[vendor] = recs
        all_records.extend(recs)

        # Kumpulkan yang unmatched untuk sheet konfirmasi
        for r in recs:
            if r.match_method == 'unmatched':
                unmatched_rows.append({
                    'Vendor': vendor,
                    'ID Unit (Vendor)': r.unit_id_vendor,
                    'Serial Chassis': r.unit_serial,
                    'Komponen': r.component_raw,
                    'Tgl Sampling': r.sampled_at,
                    'Status': r.vendor_severity,
                    'File': r.source_file,
                    'Keterangan': 'Tidak cocok ke Equipment Register PTBA',
                })

    print(f'\n{"="*50}')
    print(f'TOTAL RECORDS BERSIH: {len(all_records):,}')

    unit_summary = build_unit_summary(all_records, registry)
    warnings_df = pd.DataFrame(unmatched_rows) if unmatched_rows else pd.DataFrame()

    outfile = write_excel(all_records, per_vendor, None, unit_summary, warnings_df)
    print(f'Unit summary: {len(unit_summary)} baris (unit × komponen)')
    print(f'Perlu konfirmasi: {len(unmatched_rows)} baris')
    return outfile


if __name__ == '__main__':
    run()
