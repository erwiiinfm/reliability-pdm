"""
diagnosis.py — Oil Sample Diagnosis Engine

Alur:
  Path A (abnormal): vendor severity != Normal
      → identifikasi elemen penyebab
      → konfirmasi dengan failure mode matrix
      → analisis tren (memburuk?)
      → narasi AI

  Path B (normal vendor): cari sinyal tersembunyi
      → cek tren elemen dari riwayat
      → cek jam operasi / PM terakhir
      → jika ada anomali → balik konfirmasi ke elemen SOS
      → narasi AI
"""

from __future__ import annotations
import json, math, statistics
from typing import Optional
from datetime import date, timedelta
import anthropic


# ── Element reference ranges (ppm) ────────────────────────────────────────────
# Threshold caution / critical per komponen, berdasarkan industry standard CAT SOS
ELEMENT_LIMITS = {
    'ENGINE': {
        'fe':  {'caution': 100, 'critical': 200},
        'cu':  {'caution': 30,  'critical': 60},
        'al':  {'caution': 20,  'critical': 40},
        'cr':  {'caution': 10,  'critical': 20},
        'pb':  {'caution': 20,  'critical': 40},
        'si':  {'caution': 20,  'critical': 40},
        'na':  {'caution': 20,  'critical': 40},
        'soot':{'caution': 2.0, 'critical': 4.0},
        'tbn': {'low_caution': 5.0, 'low_critical': 3.0},   # low = buruk
        'water_pct': {'caution': 0.1, 'critical': 0.3},
        'glycol': {'caution': 0.1, 'critical': 0.5},
        'fuel_dilution_pct': {'caution': 1.5, 'critical': 3.0},
    },
    'HYDRAULIC': {
        'fe':  {'caution': 40,  'critical': 100},
        'cu':  {'caution': 15,  'critical': 30},
        'al':  {'caution': 10,  'critical': 25},
        'si':  {'caution': 15,  'critical': 30},
        'water_pct': {'caution': 0.05, 'critical': 0.1},
    },
    'FINAL DRIVE': {
        'fe':  {'caution': 200, 'critical': 500},
        'cu':  {'caution': 50,  'critical': 100},
        'pb':  {'caution': 50,  'critical': 100},
    },
    'TRANSMISSION': {
        'fe':  {'caution': 100, 'critical': 250},
        'cu':  {'caution': 40,  'critical': 80},
        'al':  {'caution': 15,  'critical': 30},
    },
    'DEFAULT': {
        'fe':  {'caution': 100, 'critical': 200},
        'cu':  {'caution': 30,  'critical': 60},
        'al':  {'caution': 20,  'critical': 40},
        'si':  {'caution': 25,  'critical': 50},
        'na':  {'caution': 20,  'critical': 40},
        'water_pct': {'caution': 0.1, 'critical': 0.3},
        'glycol': {'caution': 0.1, 'critical': 0.5},
    },
}

# ── Failure mode matrix ────────────────────────────────────────────────────────
# Kombinasi elemen → failure mode
FAILURE_MODES = [
    {
        'id': 'bearing_wear',
        'label': 'Keausan Bearing',
        'signals': {'fe': 'high', 'cu': 'high', 'pb': 'any'},
        'min_match': 2,
        'detail': 'Peningkatan Fe dan Cu mengindikasikan keausan bearing/bushing. Konfirmasi dengan cek suara dan getaran.',
    },
    {
        'id': 'liner_ring_wear',
        'label': 'Keausan Ring/Liner',
        'signals': {'fe': 'high', 'cr': 'high', 'al': 'any'},
        'min_match': 2,
        'detail': 'Fe + Cr tinggi mengindikasikan keausan ring piston dan cylinder liner.',
    },
    {
        'id': 'dirt_contamination',
        'label': 'Kontaminasi Debu/Tanah',
        'signals': {'si': 'high', 'al': 'any', 'fe': 'any'},
        'min_match': 1,  # Si saja sudah cukup
        'detail': 'Silicon tinggi = masuk debu/tanah. Cek kondisi air filter dan seal.',
    },
    {
        'id': 'coolant_leak',
        'label': 'Kebocoran Coolant',
        'signals': {'na': 'high', 'glycol': 'any', 'water_pct': 'any'},
        'min_match': 1,
        'detail': 'Na/Glycol/Air tinggi = kebocoran coolant ke oli. SEGERA cek head gasket dan oil cooler.',
        'urgent': True,
    },
    {
        'id': 'fuel_dilution',
        'label': 'Pengenceran Bahan Bakar',
        'signals': {'fuel_dilution_pct': 'any'},
        'min_match': 1,
        'detail': 'Fuel dilution tinggi = injector bocor atau masalah pembakaran. Cek injector dan timing.',
        'urgent': True,
    },
    {
        'id': 'oil_degradation',
        'label': 'Degradasi Oli',
        'signals': {'tbn': 'low', 'soot': 'high'},
        'min_match': 1,
        'detail': 'TBN rendah atau soot tinggi = oli sudah melewati batas kemampuan. Segera ganti oli.',
    },
    {
        'id': 'piston_wear',
        'label': 'Keausan Piston/Housing',
        'signals': {'al': 'high', 'si': 'any'},
        'min_match': 1,
        'detail': 'Aluminum tinggi = keausan piston atau housing komponen.',
    },
]


# ── Helpers ────────────────────────────────────────────────────────────────────
def _float(v):
    try: return float(v) if v is not None else None
    except: return None


def _get_limits(component: str) -> dict:
    comp = (component or '').upper()
    for key in ELEMENT_LIMITS:
        if key != 'DEFAULT' and key in comp:
            return ELEMENT_LIMITS[key]
    return ELEMENT_LIMITS['DEFAULT']


def _check_element(val, limits: dict, key: str) -> Optional[str]:
    """Return 'critical', 'caution', or None."""
    if val is None: return None
    if key == 'tbn':
        if val <= (limits.get('low_critical', 3.0)): return 'critical'
        if val <= (limits.get('low_caution', 5.0)):  return 'caution'
        return None
    crit = limits.get('critical') or limits.get('caution', 9999) * 2
    caut = limits.get('caution', 9999)
    if val >= crit: return 'critical'
    if val >= caut: return 'caution'
    return None


def _vendor_severity_level(sev: str) -> int:
    """0=normal, 1=caution, 2=critical"""
    s = (sev or '').lower()
    if any(x in s for x in ('critical','extreme','abnormal')): return 2
    if any(x in s for x in ('caution','warning','monitor','abnormal')): return 1
    return 0


def _calc_trend(history: list[dict], element: str) -> dict:
    """Analisis tren elemen dari riwayat (list dict dengan key element dan sampled_at)."""
    vals = [(r['sampled_at'], _float(r.get(element))) for r in history if _float(r.get(element)) is not None]
    if len(vals) < 2:
        return {'direction': 'insufficient_data', 'slope': None, 'last_3': [v for _, v in vals[-3:]]}
    vals.sort(key=lambda x: x[0])
    ys = [v for _, v in vals]
    xs = list(range(len(ys)))
    mean_x = statistics.mean(xs); mean_y = statistics.mean(ys)
    num = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys))
    den = sum((x - mean_x) ** 2 for x in xs) or 1
    slope = num / den
    pct_change = (slope / (mean_y or 1)) * 100
    direction = 'increasing' if pct_change > 5 else 'decreasing' if pct_change < -5 else 'stable'
    return {
        'direction': direction,
        'slope': round(slope, 2),
        'pct_change_per_sample': round(pct_change, 1),
        'last_3': [round(v, 1) for v in ys[-3:]],
        'n_samples': len(ys),
    }


# ── Main diagnosis function ────────────────────────────────────────────────────
def diagnose_sample(sample: dict, history: list[dict], pm_data: dict | None = None) -> dict:
    """
    sample   : dict dari sos_raw (semua kolom)
    history  : list dict, riwayat sample sebelumnya untuk unit+komponen yg sama (sorted by date)
    pm_data  : dict info PM terakhir (optional)

    Returns dict diagnosis lengkap.
    """
    component = (sample.get('component') or '').upper()
    limits = _get_limits(component)
    vendor_sev_level = _vendor_severity_level(sample.get('vendor_severity'))

    # ── Step 1: cek tiap elemen vs threshold ─────────────────────────────────
    element_map = {
        'fe': 'fe', 'cu': 'cu', 'al': 'al',
        'cr': 'cr', 'pb': 'pb', 'si': 'si',
        'na': 'na', 'tbn': 'tbn', 'soot': 'soot',
        'water_pct': 'water_pct', 'glycol': 'glycol',
        'fuel_dilution_pct': 'fuel_dilution_pct',
    }
    flags = {}
    worst_level = vendor_sev_level  # 0=normal,1=caution,2=critical

    for elem, col in element_map.items():
        val = _float(sample.get(col))
        if val is None: continue
        elem_limits = limits.get(elem, {})
        if not elem_limits: continue
        level = _check_element(val, elem_limits, elem)
        if level:
            flags[elem] = {
                'value': val,
                'status': level,
                'threshold': elem_limits,
                'unit': 'ppm' if elem not in ('tbn','soot','water_pct','glycol','fuel_dilution_pct') else '',
            }
            if level == 'critical': worst_level = max(worst_level, 2)
            elif level == 'caution': worst_level = max(worst_level, 1)

    # ── Step 2: trend analysis per elemen ────────────────────────────────────
    trend = {}
    for elem, col in element_map.items():
        if history:
            t = _calc_trend([dict(h, **{elem: h.get(col)}) for h in history], elem)
            if t['direction'] != 'insufficient_data':
                trend[elem] = t
                # Tren naik pada elemen flagged → tingkatkan kewaspadaan
                if elem in flags and t['direction'] == 'increasing':
                    flags[elem]['trend'] = 'increasing'
                    if flags[elem]['status'] == 'caution':
                        flags[elem]['note'] = 'Tren meningkat — pantau ketat'

    # ── Step 3: failure mode matching ────────────────────────────────────────
    detected_modes = []
    for mode in FAILURE_MODES:
        signals = mode['signals']
        matched = 0
        for elem, req in signals.items():
            val = _float(sample.get(element_map.get(elem, elem)))
            elem_flags = flags.get(elem)
            if req == 'any' and elem_flags: matched += 1
            elif req == 'high' and elem_flags and elem_flags['status'] in ('caution','critical'): matched += 1
            elif req == 'low' and elem_flags: matched += 1
        if matched >= mode['min_match']:
            detected_modes.append({
                'id': mode['id'],
                'label': mode['label'],
                'detail': mode['detail'],
                'urgent': mode.get('urgent', False),
                'match_score': matched,
            })
            if mode.get('urgent'): worst_level = max(worst_level, 2)

    # ── Step 4: Path B — jika normal, cek sinyal tersembunyi ─────────────────
    hidden_signals = []
    if worst_level == 0 and history:
        for elem, col in {'fe':'fe','cu':'cu','al':'al'}.items():
            t = trend.get(elem)
            if t and t['direction'] == 'increasing' and t.get('n_samples', 0) >= 3:
                hidden_signals.append({
                    'elem': elem.upper(),
                    'detail': f'{elem.upper()} menunjukkan tren naik meski masih di bawah threshold '
                              f'({t["last_3"]} — +{t["pct_change_per_sample"]}%/sampel). Pantau.',
                })
                worst_level = max(worst_level, 1)  # upgrade ke caution

    # ── Step 5: confidence score ──────────────────────────────────────────────
    confidence = 0.5
    if flags: confidence += 0.2
    if detected_modes: confidence += 0.2
    if len(history) >= 3: confidence += 0.1
    confidence = min(confidence, 1.0)

    severity_label = {0: 'NORMAL', 1: 'CAUTION', 2: 'CRITICAL'}[worst_level]

    result = {
        'severity': severity_label,
        'confidence': round(confidence, 2),
        'flags': flags,
        'failure_modes': detected_modes,
        'trend': trend,
        'hidden_signals': hidden_signals,
        'vendor_severity': sample.get('vendor_severity'),
        'data_sources': ['sos_raw', 'sos_history'] + (['pm_records'] if pm_data else []),
    }
    return result


# ── AI narasi via Claude ───────────────────────────────────────────────────────
def generate_ai_summary(sample: dict, diagnosis: dict) -> tuple[str, str]:
    """Return (summary, recommendation). Fallback ke rule-based jika API tidak tersedia."""
    try:
        client = anthropic.Anthropic()

        flags_text = '\n'.join(
            f"  - {k.upper()}: {v['value']} (status: {v['status']}, threshold caution: {v['threshold'].get('caution','—')})"
            for k, v in diagnosis['flags'].items()
        ) or '  Tidak ada elemen yang melewati threshold.'

        modes_text = '\n'.join(
            f"  - {m['label']}: {m['detail']}" for m in diagnosis['failure_modes']
        ) or '  Tidak ada failure mode terdeteksi.'

        hidden_text = '\n'.join(
            f"  - {h['detail']}" for h in diagnosis.get('hidden_signals', [])
        ) or '  Tidak ada.'

        trend_text = '\n'.join(
            f"  - {k.upper()}: {v['direction']} (last 3: {v['last_3']})"
            for k, v in diagnosis['trend'].items()
            if v['direction'] != 'stable'
        ) or '  Semua stabil.'

        prompt = f"""Kamu adalah engineer reliability ahli heavy equipment tambang batubara PT Bukit Asam.
Analisis hasil oil sample berikut dan berikan diagnosis singkat dalam Bahasa Indonesia.

UNIT: {sample.get('ptba_unit_code','—')} | KOMPONEN: {sample.get('component','—')}
TANGGAL SAMPLE: {str(sample.get('sampled_at','—'))[:10]}
SMU: {sample.get('smu_hours','—')} jam | MODEL: {sample.get('raw_data',{}).get('Model ','—') if sample.get('raw_data') else '—'}

SEVERITY VENDOR: {sample.get('vendor_severity','—')}
SEVERITY DIAGNOSIS: {diagnosis['severity']} (confidence: {diagnosis['confidence']})

ELEMEN FLAGGED:
{flags_text}

TREN (berubah dari normal):
{trend_text}

FAILURE MODE TERDETEKSI:
{modes_text}

SINYAL TERSEMBUNYI:
{hidden_text}

Tulis dalam 2 bagian:
1. RINGKASAN (2-3 kalimat): Apa yang terjadi pada komponen ini? Elemen apa yang menjadi bukti utama?
2. REKOMENDASI (2-3 poin aksi): Apa yang harus dilakukan maintenance team?

Format jawaban:
RINGKASAN: [teks]
REKOMENDASI: [teks]"""

        msg = client.messages.create(
            model='claude-haiku-4-5-20251001',
            max_tokens=400,
            messages=[{'role':'user','content':prompt}]
        )
        text = msg.content[0].text.strip()
        summary = ''
        recommendation = ''
        if 'REKOMENDASI:' in text:
            parts = text.split('REKOMENDASI:', 1)
            summary = parts[0].replace('RINGKASAN:','').strip()
            recommendation = parts[1].strip()
        else:
            summary = text
        return summary, recommendation

    except Exception as e:
        # Fallback rule-based
        sev = diagnosis['severity']
        modes = [m['label'] for m in diagnosis['failure_modes']]
        hidden = [h['elem'] for h in diagnosis.get('hidden_signals', [])]

        if sev == 'CRITICAL':
            summary = (f"Komponen dalam kondisi KRITIS. "
                       f"Terdeteksi: {', '.join(modes) if modes else 'anomali elemen'}. "
                       f"Tindakan segera diperlukan.")
            rec = "1. Hentikan unit jika memungkinkan.\n2. Lakukan inspeksi fisik komponen.\n3. Ambil sampel ulang untuk konfirmasi."
        elif sev == 'CAUTION':
            summary = (f"Komponen perlu perhatian. "
                       f"{'Failure mode: ' + ', '.join(modes) + '.' if modes else ''} "
                       f"{'Tren meningkat pada: ' + ', '.join(hidden) + '.' if hidden else ''}")
            rec = "1. Persingkat interval oil sampling.\n2. Monitor parameter terkait.\n3. Jadwalkan inspeksi saat PM berikutnya."
        else:
            summary = "Komponen dalam kondisi normal. Tidak ada anomali signifikan terdeteksi."
            rec = "Lanjutkan interval sampling sesuai jadwal PM."
        return summary, rec
