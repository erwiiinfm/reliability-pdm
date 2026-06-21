"""
SOS Reliability Engine — Analisa mandiri dari data oil sampling.

Tidak bergantung pada kesimpulan vendor. Bekerja dari:
  1. Trend unsur logam aus (RCF — Rate of Change per 100 HM)
  2. Wear signature per komponen
  3. Deteksi kontaminan (Si, air, glikol, BBM)
  4. Deteksi anomali: vendor bilang OK tapi tren berkata lain

Output per sample chain (unit + komponen):
  - our_severity: good / normal / critical / extreme
  - findings: list temuan spesifik dengan reasoning
  - vendor_agree: apakah kita setuju dengan kesimpulan vendor
  - alert_flags: flag untuk dikonfirmasi oleh blok data lain (ECM, vibration)
  - confidence: 0.0–1.0 (makin banyak sample historis → makin tinggi)
"""

from dataclasses import dataclass, field
from typing import Optional
from datetime import datetime
import math


# ── Threshold library (bisa dikalibrasi per model alat nanti) ──────────────
#
# Semua threshold dalam ppm kecuali dinyatakan lain.
# Format: (WATCH, CRITICAL, EXTREME)
# Ini adalah baseline OEM/ISO — akan dioverride oleh ML setelah cukup data.

ABSOLUTE_THRESHOLDS: dict[str, dict[str, tuple]] = {
    'ENGINE': {
        'iron_fe':      (75,  150, 300),
        'copper_cu':    (20,   50, 100),
        'aluminum_al':  (20,   40,  80),
        'chromium_cr':  (10,   20,  40),
        'lead_pb':      (15,   30,  60),
        'tin_sn':       (10,   20,  40),
        'silicon_si':   (15,   25,  40),    # dirt ingestion
        'sodium_na':    (20,   40,  80),    # coolant
        'water_pct':    (0.10, 0.20, 0.50),
        'fuel_dilution_pct': (1.0, 2.5, 5.0),
        'viscosity_40': None,               # dihitung relatif dari baseline
        'tbn':          (3.0, 2.0, 1.0),   # TERBALIK — makin rendah makin buruk
    },
    'HYDRAULIC': {
        'iron_fe':      (50,  100, 200),
        'copper_cu':    (15,   30,  60),
        'aluminum_al':  (10,   20,  40),
        'silicon_si':   (10,   20,  35),
        'water_pct':    (0.05, 0.10, 0.30),
        'viscosity_40': None,
    },
    'TRANSMISSION': {
        'iron_fe':      (100, 200, 400),
        'copper_cu':    (30,   75, 150),
        'aluminum_al':  (20,   40,  80),
        'chromium_cr':  (10,   20,  40),
        'silicon_si':   (15,   25,  40),
        'water_pct':    (0.10, 0.20, 0.50),
    },
    'FINAL_DRIVE': {
        'iron_fe':      (150, 300, 600),
        'copper_cu':    (20,   50, 100),
        'chromium_cr':  (15,   30,  60),
        'silicon_si':   (15,   25,  40),
    },
    'SWING_GEARBOX': {
        'iron_fe':      (100, 200, 400),
        'copper_cu':    (25,   60, 120),
        'silicon_si':   (15,   25,  40),
    },
    'TRAVEL_GEARBOX': {
        'iron_fe':      (100, 200, 400),
        'copper_cu':    (25,   60, 120),
        'silicon_si':   (15,   25,  40),
    },
    'AXLE': {
        'iron_fe':      (200, 400, 800),
        'copper_cu':    (30,   75, 150),
        'silicon_si':   (20,   35,  60),
    },
}

# Wear signature: komponen → metal yang paling diagnostik
# Artinya: kalau metal ini naik, komponen inilah yang paling mungkin aus
WEAR_SIGNATURE: dict[str, dict] = {
    'ENGINE': {
        'iron_fe':   'Liner/ring piston atau crankshaft journal',
        'copper_cu': 'Bushing/bearing atau oil cooler',
        'aluminum_al': 'Piston atau bearing shell aluminum',
        'chromium_cr': 'Ring piston chrome-plated atau shaft',
        'lead_pb':   'Bearing shell timah-timbal',
        'tin_sn':    'Bearing shell bimetal',
    },
    'HYDRAULIC': {
        'iron_fe':   'Pompa hidrolik atau motor (gear/vane/piston)',
        'copper_cu': 'Bushing silinder atau valve bronze',
        'aluminum_al': 'Housing pompa atau control valve body',
        'silicon_si': 'Kontaminasi debu — seal/breather/filter bermasalah',
    },
    'TRANSMISSION': {
        'iron_fe':   'Gear atau bearing transmisi',
        'copper_cu': 'Clutch plate bronze atau thrust washer',
        'chromium_cr': 'Gear case atau shaft chrome',
    },
    'FINAL_DRIVE': {
        'iron_fe':   'Gear planet atau ring gear',
        'copper_cu': 'Thrust washer atau bearing cage',
    },
    'SWING_GEARBOX':  {'iron_fe': 'Gear swing', 'copper_cu': 'Bearing cage swing'},
    'TRAVEL_GEARBOX': {'iron_fe': 'Gear travel', 'copper_cu': 'Bearing travel'},
    'AXLE':           {'iron_fe': 'Gear diferensial atau axle shaft'},
}

# RCF threshold (ppm per 100 HM) — laju perubahan yang mengkhawatirkan
# Independent dari nilai absolut — mendeteksi akselerasi keausan
RCF_THRESHOLDS: dict[str, tuple] = {
    # (WATCH, CRITICAL)
    'iron_fe':      (15, 40),
    'copper_cu':    (8,  20),
    'aluminum_al':  (8,  20),
    'chromium_cr':  (4,  12),
    'lead_pb':      (6,  15),
    'silicon_si':   (5,  15),
    'sodium_na':    (8,  20),
}


# ── Data structures ────────────────────────────────────────────────────────

@dataclass
class Finding:
    """Satu temuan spesifik dari analisa."""
    element: str           # Fe, Cu, Si, Water, dll
    severity: str          # normal / critical / extreme
    finding_type: str      # 'absolute', 'trend', 'contamination', 'viscosity', 'tbn_depletion'
    message: str           # penjelasan untuk teknisi
    value: Optional[float] = None
    threshold: Optional[float] = None
    rcf: Optional[float] = None           # rate of change per 100 HM
    alert_flag: Optional[str] = None      # flag untuk dikonfirmasi blok lain


@dataclass
class ReliabilityResult:
    """Hasil analisa reliability satu chain (unit + komponen)."""
    ptba_unit_code: str
    component: str
    analyzed_at: datetime
    n_samples_used: int       # berapa sampel historis yang dipakai
    confidence: float         # 0–1, makin banyak sampel historis makin tinggi

    # Kesimpulan kita sendiri
    our_severity: str         # good / normal / critical / extreme
    findings: list[Finding] = field(default_factory=list)

    # Perbandingan dengan vendor
    vendor_severity: Optional[str] = None
    vendor_agree: Optional[bool] = None   # None kalau vendor tidak kasih label
    vendor_disagree_reason: Optional[str] = None

    # Flag untuk dikonfirmasi blok data lain
    alert_flags: list[str] = field(default_factory=list)
    # Contoh: 'CHECK_ECM_COOLANT_TEMP', 'CHECK_BLOWBY', 'CHECK_FILTER_CONDITION'


_SEV_RANK = {'good': 0, 'normal': 1, 'critical': 2, 'extreme': 3}


def _worst(a: str, b: str) -> str:
    return a if _SEV_RANK.get(a, 0) >= _SEV_RANK.get(b, 0) else b


def _sev_from_value(val: float, thresholds: tuple, inverted=False) -> Optional[str]:
    """Mapping nilai ke severity berdasarkan 3 threshold (watch, critical, extreme)."""
    if thresholds is None:
        return None
    w, c, e = thresholds
    if inverted:  # untuk TBN: makin rendah makin buruk
        if val <= e: return 'extreme'
        if val <= c: return 'critical'
        if val <= w: return 'normal'
        return 'good'
    else:
        if val >= e: return 'extreme'
        if val >= c: return 'critical'
        if val >= w: return 'normal'
        return 'good'


# ── Core Analysis ──────────────────────────────────────────────────────────

def analyze_chain(samples: list) -> Optional[ReliabilityResult]:
    """
    Analisa satu chain: list OilSampleRecord untuk unit + komponen yang sama,
    urut dari terlama ke terbaru.

    Returns ReliabilityResult atau None kalau sampel tidak cukup.
    """
    if not samples:
        return None

    # Urut ascending (terlama dulu, terbaru terakhir)
    ordered = sorted(
        [s for s in samples if s.sampled_at],
        key=lambda s: s.sampled_at
    )
    if not ordered:
        return None

    latest = ordered[-1]
    component = latest.component or 'OTHER'
    thresholds = ABSOLUTE_THRESHOLDS.get(component, ABSOLUTE_THRESHOLDS.get('ENGINE', {}))
    signature = WEAR_SIGNATURE.get(component, {})

    findings: list[Finding] = []
    alert_flags: set[str] = set()

    # ── 1. Analisa nilai absolut (sample terbaru) ──────────────────────────
    METALS = ['iron_fe', 'copper_cu', 'aluminum_al', 'chromium_cr',
              'lead_pb', 'tin_sn', 'nickel_ni', 'silicon_si', 'sodium_na', 'magnesium_mg']

    for metal in METALS:
        val = getattr(latest, metal, None)
        if val is None:
            continue
        thr = thresholds.get(metal)
        if not thr:
            continue
        sev = _sev_from_value(val, thr)
        if sev and sev != 'good':
            label = metal.replace('_', ' ').upper().split()[0]
            wear_desc = signature.get(metal, f'Komponen {component}')
            findings.append(Finding(
                element=label,
                severity=sev,
                finding_type='absolute',
                value=val,
                threshold=thr[0],  # watch threshold
                message=f'{label} = {val:.1f} ppm ({sev.upper()}). Indikasi keausan: {wear_desc}.',
            ))

    # ── 2. Analisa kontaminan ──────────────────────────────────────────────

    # Air
    water = latest.water_pct
    if water is not None:
        thr = thresholds.get('water_pct', (0.10, 0.20, 0.50))
        sev = _sev_from_value(water, thr)
        if sev and sev != 'good':
            findings.append(Finding(
                element='WATER',
                severity=sev,
                finding_type='contamination',
                value=water,
                message=f'Kadar air {water:.3f}% ({sev.upper()}). Periksa seal, breather, dan sistem pendingin.',
                alert_flag='CHECK_ECM_COOLANT_TEMP',
            ))
            alert_flags.add('CHECK_ECM_COOLANT_TEMP')

    # Glikol (kontaminasi antifreeze — langsung berbahaya)
    if getattr(latest, 'glycol', None) is True:
        findings.append(Finding(
            element='GLYCOL',
            severity='extreme',
            finding_type='contamination',
            message='Glikol terdeteksi — indikasi kebocoran head gasket atau oil cooler. STOP OPERASI untuk inspeksi.',
            alert_flag='CHECK_ECM_COOLANT_TEMP',
        ))
        alert_flags.add('CHECK_ECM_COOLANT_TEMP')

    # Sodium tinggi (coolant masuk)
    na = latest.sodium_na
    if na and na > 30:
        sev = 'extreme' if na > 80 else 'critical' if na > 40 else 'normal'
        findings.append(Finding(
            element='Na',
            severity=sev,
            finding_type='contamination',
            value=na,
            message=f'Sodium (Na) {na:.0f} ppm — indikasi kontaminasi coolant. Cross-cek suhu coolant ECM.',
            alert_flag='CHECK_ECM_COOLANT_TEMP',
        ))
        alert_flags.add('CHECK_ECM_COOLANT_TEMP')

    # Silikon tinggi (debu/dirt ingestion)
    si = latest.silicon_si
    if si is not None:
        thr = thresholds.get('silicon_si', (15, 25, 40))
        sev = _sev_from_value(si, thr)
        if sev and sev != 'good':
            findings.append(Finding(
                element='Si',
                severity=sev,
                finding_type='contamination',
                value=si,
                message=f'Silicon (Si) {si:.1f} ppm — indikasi ingesti debu. Periksa air filter, breather, dan seal.',
                alert_flag='CHECK_AIR_FILTER',
            ))
            alert_flags.add('CHECK_AIR_FILTER')

    # Fuel dilution
    fd = getattr(latest, 'fuel_dilution_pct', None)
    if fd is not None and fd > 0:
        thr = thresholds.get('fuel_dilution_pct', (1.0, 2.5, 5.0))
        sev = _sev_from_value(fd, thr)
        if sev and sev != 'good':
            findings.append(Finding(
                element='FUEL_DILUTION',
                severity=sev,
                finding_type='contamination',
                value=fd,
                message=f'Dilusi BBM {fd:.2f}% ({sev.upper()}). Periksa injector, ring piston, dan blowby.',
                alert_flag='CHECK_BLOWBY',
            ))
            alert_flags.add('CHECK_BLOWBY')
            alert_flags.add('CHECK_ECM_FUEL_RATE')

    # ── 3. TBN depletion ──────────────────────────────────────────────────
    tbn = latest.tbn
    if tbn is not None:
        sev = _sev_from_value(tbn, (3.0, 2.0, 1.0), inverted=True)
        if sev and sev != 'good':
            findings.append(Finding(
                element='TBN',
                severity=sev,
                finding_type='tbn_depletion',
                value=tbn,
                message=f'TBN {tbn:.1f} mgKOH/g — kapasitas oli menetralkan asam hampir habis. Ganti oli segera.',
            ))

    # ── 4. Viscosity shift ────────────────────────────────────────────────
    # Bandingkan dengan baseline 3 sampel sebelumnya
    vis_vals = [(s.viscosity_40, s.sampled_at) for s in ordered[:-1] if s.viscosity_40]
    cur_vis = latest.viscosity_40
    if cur_vis and len(vis_vals) >= 2:
        baseline = sum(v for v, _ in vis_vals[-3:]) / len(vis_vals[-3:])
        pct_change = (cur_vis - baseline) / baseline * 100
        if abs(pct_change) >= 15:
            sev = 'extreme' if abs(pct_change) >= 30 else 'critical'
            direction = 'naik' if pct_change > 0 else 'turun'
            cause = (
                'Kemungkinan oksidasi atau kontaminasi soot' if pct_change > 0
                else 'Kemungkinan dilusi BBM atau solvent'
            )
            findings.append(Finding(
                element='VISCOSITY',
                severity=sev,
                finding_type='viscosity',
                value=cur_vis,
                message=f'Viskositas {direction} {abs(pct_change):.1f}% dari baseline. {cause}.',
                alert_flag='CHECK_BLOWBY' if pct_change < 0 else None,
            ))
            if pct_change < 0:
                alert_flags.add('CHECK_BLOWBY')

    # ── 5. Trend analysis (RCF) — butuh minimal 2 sampel ─────────────────
    if len(ordered) >= 2:
        _analyze_trends(ordered, thresholds, signature, findings, alert_flags)

    # ── 6. Hitung severity akhir ──────────────────────────────────────────
    our_severity = 'good'
    for f in findings:
        our_severity = _worst(our_severity, f.severity)

    # ── 7. Bandingkan dengan vendor ───────────────────────────────────────
    vendor_sev = latest.vendor_severity
    vendor_agree = None
    vendor_disagree_reason = None

    if vendor_sev:
        our_rank = _SEV_RANK.get(our_severity, 0)
        vendor_rank = _SEV_RANK.get(vendor_sev, 0)

        if our_rank == vendor_rank:
            vendor_agree = True
        else:
            vendor_agree = False
            if our_rank > vendor_rank:
                vendor_disagree_reason = (
                    f'Vendor menilai {vendor_sev.upper()} tapi analisa tren kami menunjukkan '
                    f'{our_severity.upper()}. Perlu perhatian lebih.'
                )
            else:
                vendor_disagree_reason = (
                    f'Vendor menilai {vendor_sev.upper()} tapi tren kami menunjukkan '
                    f'{our_severity.upper()}. Mungkin vendor menggunakan threshold berbeda.'
                )

    # ── 8. Confidence score ───────────────────────────────────────────────
    # Makin banyak sampel historis → makin yakin dengan analisa tren
    n = len(ordered)
    confidence = min(1.0, 0.4 + (n - 1) * 0.12)   # 1 sample=0.4, 6+=1.0

    return ReliabilityResult(
        ptba_unit_code=latest.ptba_unit_code or '',
        component=component,
        analyzed_at=datetime.now(),
        n_samples_used=n,
        confidence=round(confidence, 2),
        our_severity=our_severity,
        findings=findings,
        vendor_severity=vendor_sev,
        vendor_agree=vendor_agree,
        vendor_disagree_reason=vendor_disagree_reason,
        alert_flags=sorted(alert_flags),
    )


def _analyze_trends(ordered: list, thresholds: dict, signature: dict,
                    findings: list, alert_flags: set):
    """Hitung Rate of Change Factor (RCF) per unsur."""

    TRACKED = ['iron_fe', 'copper_cu', 'aluminum_al', 'chromium_cr',
               'lead_pb', 'silicon_si', 'sodium_na']

    for metal in TRACKED:
        # Ambil pasangan (HM, nilai) yang valid
        points = []
        for s in ordered:
            val = getattr(s, metal, None)
            hm = s.smu_hours
            if val is not None and hm is not None:
                points.append((hm, val))

        if len(points) < 2:
            continue

        # RCF = rata-rata laju perubahan per 100 HM dari 3 pasang terakhir
        rcf_vals = []
        for i in range(max(0, len(points) - 3), len(points) - 1):
            hm1, v1 = points[i]
            hm2, v2 = points[i + 1]
            delta_hm = hm2 - hm1
            if delta_hm > 0:
                rcf_vals.append((v2 - v1) / delta_hm * 100)

        if not rcf_vals:
            continue

        rcf = sum(rcf_vals) / len(rcf_vals)
        thr_rcf = RCF_THRESHOLDS.get(metal)
        if not thr_rcf or rcf <= 0:
            continue

        watch_rcf, crit_rcf = thr_rcf
        if rcf >= crit_rcf:
            sev = 'critical'
        elif rcf >= watch_rcf:
            sev = 'normal'
        else:
            continue   # laju normal, tidak perlu dilaporkan

        label = metal.split('_')[0].upper()
        wear_desc = signature.get(metal, '')
        findings.append(Finding(
            element=f'{label}_TREND',
            severity=sev,
            finding_type='trend',
            rcf=round(rcf, 2),
            message=(
                f'Laju kenaikan {label} = {rcf:.1f} ppm/100HM ({sev.upper()}). '
                f'{wear_desc + ". " if wear_desc else ""}'
                f'Perlu sample berikutnya dipercepat.'
            ),
        ))

    # Deteksi akselerasi keausan (sudden spike pada sample terbaru)
    fe_points = [(s.smu_hours, s.iron_fe) for s in ordered
                 if s.iron_fe is not None and s.smu_hours is not None]
    if len(fe_points) >= 3:
        recent_rate = None
        prev_rate = None
        for i in range(len(fe_points) - 1, 0, -1):
            hm2, v2 = fe_points[i]
            hm1, v1 = fe_points[i - 1]
            dh = hm2 - hm1
            if dh > 0:
                rate = (v2 - v1) / dh * 100
                if recent_rate is None:
                    recent_rate = rate
                elif prev_rate is None:
                    prev_rate = rate
                    break

        if recent_rate is not None and prev_rate is not None and prev_rate > 0:
            acceleration = recent_rate / prev_rate
            if acceleration >= 3.0:
                findings.append(Finding(
                    element='Fe_SPIKE',
                    severity='extreme',
                    finding_type='trend',
                    rcf=round(recent_rate, 2),
                    message=(
                        f'AKSELERASI KEAUSAN Fe: laju naik {acceleration:.1f}× lebih cepat '
                        f'dari interval sebelumnya ({recent_rate:.1f} vs {prev_rate:.1f} ppm/100HM). '
                        f'Potensi kegagalan akut — prioritas inspeksi.'
                    ),
                    alert_flag='CHECK_ECM_OIL_PRESSURE',
                ))
                alert_flags.add('CHECK_ECM_OIL_PRESSURE')


# ── Batch processor ────────────────────────────────────────────────────────

def analyze_all(records: list) -> list[ReliabilityResult]:
    """
    Kelompokkan records per (unit, komponen) lalu analisa masing-masing chain.
    """
    from collections import defaultdict
    chains: dict[tuple, list] = defaultdict(list)
    for r in records:
        if r.ptba_unit_code:
            chains[(r.ptba_unit_code, r.component or 'OTHER')].append(r)

    results = []
    for (unit, comp), recs in chains.items():
        result = analyze_chain(recs)
        if result:
            results.append(result)

    # Urut: extreme dulu, lalu confidence rendah (perlu lebih banyak data)
    results.sort(key=lambda r: (-_SEV_RANK.get(r.our_severity, 0), r.confidence))
    return results


def summarize(results: list[ReliabilityResult]) -> dict:
    """Ringkasan statistik untuk ditampilkan di dashboard."""
    total = len(results)
    sev_count = {'good': 0, 'normal': 0, 'critical': 0, 'extreme': 0}
    disagree_count = sum(1 for r in results if r.vendor_agree is False)
    flags: dict[str, int] = {}

    for r in results:
        sev_count[r.our_severity] = sev_count.get(r.our_severity, 0) + 1
        for f in r.alert_flags:
            flags[f] = flags.get(f, 0) + 1

    return {
        'total_chains': total,
        'severity_distribution': sev_count,
        'vendor_disagree_count': disagree_count,
        'alert_flags': dict(sorted(flags.items(), key=lambda x: -x[1])),
        'avg_confidence': round(sum(r.confidence for r in results) / total, 2) if total else 0,
    }
