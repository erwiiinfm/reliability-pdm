"""
Oil Sample Parser — Smart multi-vendor pipeline.
Deteksi vendor dari KONTEN file (bukan nama file).
Output: list OilSampleRecord yang sudah dinormalisasi dan di-link ke kode unit PTBA.

Supported vendors (auto-detected by content fingerprint):
  - Tekenomiks    (.csv)  — row 0: "Techenomics Copyright"
  - Indotruck     (.xlsx) — sheet 'Data', col 0: 'Sample Number'
  - Trakindo      (.xlsx) — sheet 'OIL', col 0: 'Health'
  - United Tractors (.xls) — sheet 'sheet', col 0: 'grouploc', col 6: 'unit_id'
"""

import re
import pandas as pd
import xlrd
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional
from pathlib import Path

try:
    from rapidfuzz import fuzz, process as fuzz_process
    _HAS_RAPIDFUZZ = True
except ImportError:
    _HAS_RAPIDFUZZ = False


# ---------------------------------------------------------------------------
# Normalized output schema
# ---------------------------------------------------------------------------

@dataclass
class OilSampleRecord:
    vendor: str
    source_file: str

    # Unit identity — setelah matching ke asset register PTBA
    ptba_unit_code: Optional[str] = None   # Equipment Register (DZ0034, DT0005, ...)
    unit_serial: Optional[str] = None      # serial chassis dari vendor
    unit_id_vendor: Optional[str] = None   # kode unit versi vendor
    match_method: Optional[str] = None     # cara matching: 'direct', 'chassis', 'alias', 'unmatched'

    component: Optional[str] = None        # normalized: ENGINE, HYDRAULIC, ...
    component_raw: Optional[str] = None    # original string

    # Sample metadata
    sampled_at: Optional[datetime] = None
    lab_date: Optional[datetime] = None
    lab_reference: Optional[str] = None
    smu_hours: Optional[float] = None
    oil_hours: Optional[float] = None
    oil_changed: Optional[bool] = None
    filter_changed: Optional[bool] = None
    oil_brand: Optional[str] = None
    oil_grade: Optional[str] = None

    # Wear metals (ppm)
    iron_fe: Optional[float] = None
    copper_cu: Optional[float] = None
    aluminum_al: Optional[float] = None
    chromium_cr: Optional[float] = None
    lead_pb: Optional[float] = None
    tin_sn: Optional[float] = None
    nickel_ni: Optional[float] = None
    silicon_si: Optional[float] = None
    sodium_na: Optional[float] = None
    magnesium_mg: Optional[float] = None
    molybdenum_mo: Optional[float] = None
    phosphorus_p: Optional[float] = None
    zinc_zn: Optional[float] = None
    calcium_ca: Optional[float] = None
    boron_b: Optional[float] = None
    potassium_k: Optional[float] = None
    barium_ba: Optional[float] = None

    # Oil condition
    viscosity_40: Optional[float] = None
    viscosity_100: Optional[float] = None
    visc_sae: Optional[str] = None
    tan: Optional[float] = None
    tbn: Optional[float] = None
    water_pct: Optional[float] = None
    karl_fischer: Optional[float] = None
    soot: Optional[float] = None
    oxidation: Optional[float] = None
    nitration: Optional[float] = None
    fuel_dilution_pct: Optional[float] = None
    glycol: Optional[bool] = None
    pq_index: Optional[float] = None
    iso_code: Optional[str] = None
    sox: Optional[float] = None
    fame: Optional[float] = None
    sulphur: Optional[float] = None
    dir_trans: Optional[float] = None
    particles_4um: Optional[float] = None
    particles_6um: Optional[float] = None
    particles_15um: Optional[float] = None

    # Location / admin
    location: Optional[str] = None
    branch: Optional[str] = None
    follow_up: Optional[str] = None

    # Vendor assessment
    vendor_severity: Optional[str] = None
    vendor_notes: Optional[str] = None

    # Full raw row (semua kolom asli dari file vendor)
    raw_data: Optional[dict] = None


# ---------------------------------------------------------------------------
# Asset Registry Loader
# ---------------------------------------------------------------------------

_asset_registry: Optional[pd.DataFrame] = None

def load_asset_registry(filepath: str) -> pd.DataFrame:
    global _asset_registry
    if _asset_registry is not None:
        return _asset_registry

    df = pd.read_excel(filepath, sheet_name='APPT Profile', header=4, engine='openpyxl')
    df = df[['Equipment Register', 'Model', 'Unit/ Chassis', 'Engine']].dropna(subset=['Equipment Register'])
    df['eq_reg'] = df['Equipment Register'].astype(str).str.strip()
    df['chassis'] = df['Unit/ Chassis'].astype(str).str.strip().str.replace('.0', '', regex=False).str.upper()
    df['engine'] = df['Engine'].astype(str).str.strip().str.replace('.0', '', regex=False).str.upper()
    _asset_registry = df
    return _asset_registry


def _normalize_id(s: str) -> str:
    """Strip semua karakter non-alfanumerik, uppercase."""
    return re.sub(r'[^A-Z0-9]', '', str(s).upper())


# Mapping deskripsi vendor → prefix kode PTBA
_MODEL_PREFIX = {
    'EXCAVATOR': 'EX', 'EXCAV': 'EX',
    'DUMP TRUCK': 'DT', 'DUMPTRUCK': 'DT', 'DUMP': 'DT',
    'DOZER': 'DZ', 'BULLDOZER': 'DZ', 'BLADE': 'DZ',
    'GRADER': 'GD', 'MOTOR GRADER': 'GD',
    'COMPACTOR': 'CP',
    'HAULER': 'DT',
    'LOADER': 'LD', 'WHEEL LOADER': 'LD',
    'DRILLRIG': 'DR', 'DRILL': 'DR',
    'CRANE': 'CR',
}


def _model_to_prefix(desc: str) -> Optional[str]:
    """Coba ekstrak prefix kode PTBA dari deskripsi model vendor."""
    d = desc.upper().strip()
    # Coba dari terpanjang ke terpendek biar 'MOTOR GRADER' tidak match 'GRADER' dulu
    for key in sorted(_MODEL_PREFIX, key=len, reverse=True):
        if key in d:
            return _MODEL_PREFIX[key]
    return None


def _build_registry_index(registry: pd.DataFrame) -> dict:
    """
    Bangun beberapa index lookup dari registry.
    Di-cache per panggilan (bisa di-upgrade ke module-level cache jika perlu).
    """
    eq_list = registry['eq_reg'].tolist()
    chassis_map = {c: e for c, e in zip(registry['chassis'], registry['eq_reg'])
                   if c and c not in ('NAN', '')}
    engine_map  = {e2: e for e2, e in zip(registry['engine'], registry['eq_reg'])
                   if e2 and e2 not in ('NAN', '')}
    eq_norm = {_normalize_id(e): e for e in eq_list}

    # Bangun: prefix_letter → {num_suffix → eq_reg}
    # Contoh: 'EX' → {'07': 'EX3010-07', '08': 'EX3010-08', ...}
    prefix_num: dict[str, dict[str, str]] = {}
    for eq in eq_list:
        eq_up = eq.upper()
        # Ambil huruf di depan
        m = re.match(r'^([A-Z]{2,3})', eq_up)
        if not m:
            continue
        pref = m.group(1)
        # Ambil angka paling belakang
        nums = re.findall(r'\d+', eq_up)
        if nums:
            suffix = nums[-1].lstrip('0') or '0'   # '07' → '7', '034' → '34'
            if pref not in prefix_num:
                prefix_num[pref] = {}
            if suffix not in prefix_num[pref]:   # ambil yang pertama (lebih tua)
                prefix_num[pref][suffix] = eq

    return {
        'eq_list':    eq_list,
        'eq_set':     set(eq_list),
        'eq_norm':    eq_norm,
        'chassis':    chassis_map,
        'engine':     engine_map,
        'prefix_num': prefix_num,
    }


# Module-level cache — reset otomatis saat registry berubah
_registry_index_cache: Optional[dict] = None
_registry_cache_id: Optional[int] = None


def _get_index(registry: pd.DataFrame) -> dict:
    global _registry_index_cache, _registry_cache_id
    rid = id(registry)
    if _registry_cache_id != rid or _registry_index_cache is None:
        _registry_index_cache = _build_registry_index(registry)
        _registry_cache_id = rid
    return _registry_index_cache


def match_to_ptba(unit_id_vendor: str, unit_serial: str,
                  registry: pd.DataFrame,
                  unit_model: str = '') -> tuple[Optional[str], str]:
    """
    Match ke Equipment Register PTBA dengan 8 layer berurutan:
    1. Direct exact match
    2. Chassis serial exact match
    3. Engine serial exact match
    4. Alias — strip non-alphanumeric, compare
    5. Substring — eq_reg muncul di dalam string vendor
    6. Model+number — 'EXCAVATOR07' → prefix EX, num 7 → EX3010-07
    7. Fuzzy match (rapidfuzz WRatio ≥ 88, hanya dalam prefix yang sama)
    8. Fuzzy match cross-prefix sebagai last resort (≥ 92)
    """
    idx = _get_index(registry)
    eq_set    = idx['eq_set']
    eq_norm   = idx['eq_norm']
    chassis   = idx['chassis']
    engine    = idx['engine']
    prefix_num = idx['prefix_num']

    uid = str(unit_id_vendor or '').strip()
    ser = str(unit_serial or '').strip().upper()
    uid_up = uid.upper()
    uid_norm = _normalize_id(uid)
    ser_norm  = _normalize_id(ser)

    # ── 1. Direct exact (case-insensitive)
    if uid in eq_set:
        return uid, 'direct'
    if uid_up in eq_set:
        return uid_up, 'direct'

    # ── 2. Chassis exact
    if ser and ser in chassis:
        return chassis[ser], 'chassis'

    # ── 3. Engine exact
    if ser and ser in engine:
        return engine[ser], 'engine'

    # ── 4. Alias (strip non-alphanumeric)
    if uid_norm and uid_norm in eq_norm:
        return eq_norm[uid_norm], 'alias'
    if ser_norm and ser_norm in eq_norm:
        return eq_norm[ser_norm], 'alias'

    # ── 5. Substring — eq_reg muncul di dalam string vendor
    for eq in idx['eq_list']:
        eq_up2 = eq.upper()
        if len(eq_up2) >= 4 and (eq_up2 in uid_up or eq_up2 in ser.upper()):
            return eq, 'substring'

    # ── 6. Model description + nomor unit
    #    Contoh: 'EXCAVATOR07' → prefix 'EX', num '7' → cari di prefix_num['EX']['7']
    for candidate_str in [uid_up, ser.upper(), unit_model.upper()]:
        if not candidate_str:
            continue
        # Coba ekstrak angka di akhir
        m_num = re.search(r'(\d+)\s*$', candidate_str.strip())
        if not m_num:
            # Angka di mana saja
            m_num = re.search(r'(\d+)', candidate_str)
        if not m_num:
            continue
        num_raw = m_num.group(1).lstrip('0') or '0'

        # Cari prefix dari teks sebelum angka
        prefix_str = candidate_str[:m_num.start()].strip()
        derived_prefix = _model_to_prefix(prefix_str)

        # Juga coba 2-3 huruf pertama sebagai prefix langsung
        direct_prefix = re.match(r'^([A-Z]{2,3})', _normalize_id(candidate_str))
        direct_prefix = direct_prefix.group(1) if direct_prefix else None

        for pref in filter(None, [derived_prefix, direct_prefix]):
            if pref in prefix_num and num_raw in prefix_num[pref]:
                return prefix_num[pref][num_raw], 'model_num'

    # ── 7. Fuzzy match DALAM prefix yang sama (threshold 88%)
    if _HAS_RAPIDFUZZ and uid_norm and len(uid_norm) >= 3:
        # Ambil prefix 2 huruf dari uid_norm, hanya fuzzy dalam set tersebut
        pref2 = uid_norm[:2]
        same_prefix = {k: v for k, v in eq_norm.items() if k.startswith(pref2)}
        if same_prefix:
            result = fuzz_process.extractOne(
                uid_norm, list(same_prefix.keys()),
                scorer=fuzz.WRatio,
                score_cutoff=88
            )
            if result:
                matched_norm, score, _ = result
                return same_prefix[matched_norm], f'fuzzy({score:.0f}%)'

        # Juga coba serial dalam prefix yang sama
        if ser_norm and ser_norm != uid_norm and len(ser_norm) >= 3:
            pref2s = ser_norm[:2]
            same_prefix_s = {k: v for k, v in eq_norm.items() if k.startswith(pref2s)}
            if same_prefix_s:
                result = fuzz_process.extractOne(
                    ser_norm, list(same_prefix_s.keys()),
                    scorer=fuzz.WRatio,
                    score_cutoff=88
                )
                if result:
                    matched_norm, score, _ = result
                    return same_prefix_s[matched_norm], f'fuzzy_serial({score:.0f}%)'

    # ── 8. Last resort: fuzzy lintas prefix (threshold tinggi 92%)
    if _HAS_RAPIDFUZZ and uid_norm and len(uid_norm) >= 4:
        result = fuzz_process.extractOne(
            uid_norm, list(eq_norm.keys()),
            scorer=fuzz.WRatio,
            score_cutoff=92
        )
        if result:
            matched_norm, score, _ = result
            return eq_norm[matched_norm], f'fuzzy_broad({score:.0f}%)'

    return None, 'unmatched'


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _float(val) -> Optional[float]:
    """Parse numeric, handle '<1', '<0.50', etc."""
    if val is None or (isinstance(val, float) and pd.isna(val)):
        return None
    s = str(val).strip().replace('<', '').replace('>', '').replace(',', '.')
    try:
        v = float(s)
        return v if v >= 0 else None
    except (ValueError, TypeError):
        return None


def _date(val) -> Optional[datetime]:
    if val is None:
        return None
    if isinstance(val, datetime):
        return val
    if isinstance(val, pd.Timestamp):
        return val.to_pydatetime()
    s = str(val).strip()
    if not s or s in ('NaT', 'nan', 'None'):
        return None
    for fmt in ('%d/%m/%Y', '%m/%d/%Y', '%Y-%m-%d', '%d-%m-%Y', '%Y/%m/%d'):
        try:
            return datetime.strptime(s[:10], fmt)
        except ValueError:
            continue
    try:
        return pd.to_datetime(s).to_pydatetime()
    except Exception:
        return None


def _bool_yn(val) -> Optional[bool]:
    if val is None:
        return None
    s = str(val).strip().upper()
    if s in ('Y', 'YES', 'TRUE', '1'):
        return True
    if s in ('N', 'NO', 'FALSE', '0'):
        return False
    return None


def _normalize_component(raw: str) -> str:
    r = str(raw).upper().strip()
    if not r or r in ('NAN', 'NONE', '-', ''):
        return 'OTHER'
    if re.search(r'\bENGINE\b|\bENG\b|\bMESIN\b', r):
        return 'ENGINE'
    if re.search(r'HYDRAULIC|HIDROLIK|HYD\b', r):
        return 'HYDRAULIC'
    if re.search(r'TRANSMIS|TRANS\b|\bTR\b|\bGBOX\b', r):
        return 'TRANSMISSION'
    if re.search(r'FINAL.?DRIVE|FINAL.?DR|\bFD\b|\bFDR\b|\bFDL\b|RIGHT FINAL|LEFT FINAL', r):
        return 'FINAL DRIVE'
    if re.search(r'SWING|SLEW', r):
        return 'SWING GEARBOX'
    if re.search(r'TRAVEL|GRTV', r):
        return 'TRAVEL GEARBOX'
    if re.search(r'\bAXLE\b|\bAX\b|REAR AXLE|FRONT AXLE|DIFF', r):
        return 'AXLE'
    if re.search(r'COOLANT|RADIATOR|PENDINGIN', r):
        return 'COOLING'
    if re.search(r'TORQUE.?CONV|CONVERTER', r):
        return 'TORQUE CONVERTER'
    if re.search(r'STEERING|KEMUDI', r):
        return 'STEERING'
    if re.search(r'BRAKE|REM\b', r):
        return 'BRAKE'
    if re.search(r'COMP.?AIR|AIR COMP|KOMPRESOR', r):
        return 'COMPRESSOR'
    if re.search(r'PUMP|POMPA', r):
        return 'PUMP'
    if re.search(r'HOIST|DUMP', r):
        return 'HOIST'
    if re.search(r'SWING.?BEAR|SLEW.?BEAR|CIRCLE', r):
        return 'SWING BEARING'
    return r  # kembalikan raw yang sudah di-uppercase sebagai fallback, bukan 'OTHER'


def _map_severity(raw: str) -> Optional[str]:
    if not raw:
        return None
    r = str(raw).upper().strip()
    if re.search(r'NO ACTION|NORMAL|SATISFACT|^N$|^GOOD$', r):
        return 'good'
    if re.search(r'MONITOR|CAUTION|WATCH|^B$', r):
        return 'normal'
    if re.search(r'CRITICAL|SEVERE|IMMED|^C$', r):
        return 'critical'
    if re.search(r'EXTREME|DANGER|IMMEDIATE STOP|^X$|ABNORMAL', r):
        return 'extreme'
    if re.search(r'ACTION|ATTENTION|WARNING', r):
        return 'critical'
    return None


# ---------------------------------------------------------------------------
# Vendor detection by CONTENT (bukan filename)
# ---------------------------------------------------------------------------

def detect_vendor(filepath: str) -> str:
    path = Path(filepath)
    suffix = path.suffix.lower()

    if suffix == '.csv':
        with open(filepath, 'r', errors='ignore') as f:
            first_line = f.readline()
        if 'Techenomics' in first_line or 'Techenomics Copyright' in first_line:
            return 'tekenomiks'
        return 'unknown_csv'

    if suffix in ('.xlsx', '.xls'):
        try:
            if suffix == '.xlsx':
                xl = pd.ExcelFile(filepath, engine='openpyxl')
                sheets = xl.sheet_names

                if 'OIL' in sheets:
                    df = pd.read_excel(filepath, sheet_name='OIL', header=0,
                                       engine='openpyxl', nrows=1)
                    if 'Health' in df.columns and 'Asset Serial Number' in df.columns:
                        return 'trakindo'

                if 'Data' in sheets:
                    df = pd.read_excel(filepath, sheet_name='Data', header=0,
                                       engine='openpyxl', nrows=1)
                    if 'Sample Number' in df.columns and 'Unit ID' in df.columns:
                        return 'indotruck'

                # Format PTBA compiled: ada sheet vendor PTBA atau sheet cleaned
                PTBA_VENDOR_SHEETS = {'trakindo utama', 'united tractors', 'indotruck utama',
                                      'tekenomik', 'tekenomiks', 'all unit',
                                      'trakindo', 'indotruck', 'semua data', 'per unit',
                                      'perlu konfirmasi'}
                sheet_lower = {s.lower() for s in sheets}
                if sheet_lower & PTBA_VENDOR_SHEETS:
                    return 'ptba_compiled'

                # Cek kolom kunci PTBA di sheet pertama yang berisi data
                for sh in sheets[:4]:
                    try:
                        dfsh = pd.read_excel(filepath, sheet_name=sh, header=0,
                                             engine='openpyxl', nrows=1)
                        cols = set(dfsh.columns)
                        if 'No. Lambung Unit' in cols and 'Asset Serial Number' in cols:
                            return 'ptba_compiled'
                    except Exception:
                        continue

            else:
                # XLS binary
                book = xlrd.open_workbook(filepath, ignore_workbook_corruption=True)
                sh = book.sheet_by_index(0)
                if sh.name == 'sheet':
                    headers = sh.row_values(0)
                    if 'grouploc' in headers and 'UNIT_NO' in headers:
                        return 'united_tractors'
        except Exception:
            pass

    return 'unknown'


# ---------------------------------------------------------------------------
# Parser: Tekenomiks
# ---------------------------------------------------------------------------

def parse_tekenomiks(filepath: str, registry: Optional[pd.DataFrame] = None) -> list:
    records = []
    # Unit metadata di baris 2
    meta_df = pd.read_csv(filepath, header=None, nrows=4)
    unit_serial = None
    compartment_raw = None
    try:
        row2 = meta_df.iloc[2].fillna('').astype(str).tolist()
        if 'Unit' in row2:
            unit_serial = row2[row2.index('Unit') + 1].strip()
        if 'Compartment' in row2:
            compartment_raw = row2[row2.index('Compartment') + 1].strip()
    except Exception:
        pass

    # Header di row 3, skip row 4 (blank separator)
    df = pd.read_csv(filepath, header=3)
    df = df.dropna(subset=['Sample Date', 'Fe'], how='all')
    df = df[df['Sample Date'].notna()]

    for _, row in df.iterrows():
        comp_raw = compartment_raw or ''
        uid = unit_serial or ''
        ptba_code, method = match_to_ptba(uid, uid, registry) if registry is not None else (None, 'unmatched')

        rec = OilSampleRecord(
            vendor='tekenomiks', source_file=Path(filepath).name,
            ptba_unit_code=ptba_code, match_method=method,
            unit_id_vendor=uid, unit_serial=uid,
            component_raw=comp_raw, component=_normalize_component(comp_raw),
            sampled_at=_date(row.get('Sample Date')),
            smu_hours=_float(row.get('Equip Hours')),
            oil_hours=_float(row.get('Oil Hrs')),
            oil_changed=True if str(row.get('Oil Changed', '')).strip() in ('1', '2', 'Y') else None,
            oil_brand=str(row.get('Oil Type', '') or '').strip() or None,
            iron_fe=_float(row.get('Fe')), copper_cu=_float(row.get('Cu')),
            aluminum_al=_float(row.get('Al')), chromium_cr=_float(row.get('Cr')),
            lead_pb=_float(row.get('Pb')), tin_sn=_float(row.get('Sn')),
            nickel_ni=_float(row.get('Ni')), silicon_si=_float(row.get('Si')),
            sodium_na=_float(row.get('Na')), magnesium_mg=_float(row.get('Mg')),
            molybdenum_mo=_float(row.get('Mo')), phosphorus_p=_float(row.get('P')),
            zinc_zn=_float(row.get('Zn')), calcium_ca=_float(row.get('Ca')),
            boron_b=_float(row.get('B')),
            viscosity_40=_float(row.get('VIS-40')), viscosity_100=_float(row.get('VIS-100')),
            tbn=_float(row.get('TBN')), water_pct=_float(row.get('Water')),
            soot=_float(row.get('Soot')), oxidation=_float(row.get('Oxidation')),
            nitration=_float(row.get('Nitration')),
            fuel_dilution_pct=_float(row.get('Fuel Dilution')),
            pq_index=_float(row.get('PQ')),
            iso_code=str(row.get('ISOCode', '') or '').strip() or None,
            vendor_severity=_map_severity(str(row.get('Overall Eval', '') or '')),
        )
        records.append(rec)
    return records


# ---------------------------------------------------------------------------
# Parser: Indotruck
# ---------------------------------------------------------------------------

def parse_indotruck(filepath: str, registry: Optional[pd.DataFrame] = None) -> list:
    df = pd.read_excel(filepath, sheet_name='Data', header=0, engine='openpyxl')
    df = df.dropna(how='all')
    records = []

    for _, row in df.iterrows():
        uid = str(row.get('Unit ID', '') or '').strip()
        ser = str(row.get('Unit Serial', '') or '').strip()
        comp_raw = str(row.get('Component Type', '') or row.get('Component', '') or '').strip()
        ptba_code, method = match_to_ptba(uid, ser, registry) if registry is not None else (None, 'unmatched')

        sev_raw = str(row.get('Severity', '') or row.get('Condition', '') or '')
        rec = OilSampleRecord(
            vendor='indotruck', source_file=Path(filepath).name,
            ptba_unit_code=ptba_code, match_method=method,
            unit_id_vendor=uid or None, unit_serial=ser or None,
            component_raw=comp_raw or None, component=_normalize_component(comp_raw),
            sampled_at=_date(row.get('Date Sampled')),
            lab_date=_date(row.get('Date Received')),
            lab_reference=str(row.get('Analysis No.', '') or row.get('Sample Number', '') or '').strip() or None,
            smu_hours=_float(row.get('Component Age')),
            oil_hours=_float(row.get('Lube Age')),
            oil_brand=str(row.get('Lube Brand', '') or '').strip() or None,
            oil_grade=str(row.get('Lube Grade', '') or '').strip() or None,
            iron_fe=_float(row.get('Iron')), copper_cu=_float(row.get('Copper')),
            aluminum_al=_float(row.get('Aluminum')), chromium_cr=_float(row.get('Chromium')),
            lead_pb=_float(row.get('Lead')), tin_sn=_float(row.get('Tin')),
            nickel_ni=_float(row.get('Nickel')), silicon_si=_float(row.get('Silicon')),
            sodium_na=_float(row.get('Sodium')), magnesium_mg=_float(row.get('Magnesium')),
            molybdenum_mo=_float(row.get('Molybdenum')), phosphorus_p=_float(row.get('Phosphorus')),
            zinc_zn=_float(row.get('Zinc')), calcium_ca=_float(row.get('Calcium')),
            boron_b=_float(row.get('Boron')), barium_ba=_float(row.get('Barium')),
            potassium_k=_float(row.get('Potassium')),
            viscosity_40=_float(row.get('Viscosity 40 °C cSt')),
            viscosity_100=_float(row.get('Viscosity 100 °C cSt')),
            tan=_float(row.get('Total Acid Number mg KOH/g')),
            water_pct=_float(row.get('Water %')),
            soot=_float(row.get('Soot ABS/cm')), oxidation=_float(row.get('Oxidation ABS/cm')),
            nitration=_float(row.get('Nitration ABS/cm')),
            fuel_dilution_pct=_float(row.get('Fuel %')),
            pq_index=_float(row.get('PQ Index')),
            iso_code=str(row.get('ISO Code', '') or '').strip() or None,
            vendor_severity=_map_severity(sev_raw),
            vendor_notes=str(row.get('Recommendations', '') or '').strip() or None,
        )
        records.append(rec)
    return records


# ---------------------------------------------------------------------------
# Parser: Trakindo
# ---------------------------------------------------------------------------

def parse_trakindo(filepath: str, registry: Optional[pd.DataFrame] = None) -> list:
    df = pd.read_excel(filepath, sheet_name='OIL', header=0, engine='openpyxl')
    df = df.dropna(how='all').iloc[1:]  # row 1 is blank separator
    records = []

    for _, row in df.iterrows():
        ser = str(row.get('Asset Serial Number', '') or '').strip()
        uid = str(row.get('Asset ID', '') or '').strip()
        comp_raw = str(row.get('Component', '') or '').strip()
        ptba_code, method = match_to_ptba(uid, ser, registry) if registry is not None else (None, 'unmatched')

        rec = OilSampleRecord(
            vendor='trakindo', source_file=Path(filepath).name,
            ptba_unit_code=ptba_code, match_method=method,
            unit_id_vendor=uid or None, unit_serial=ser or None,
            component_raw=comp_raw or None, component=_normalize_component(comp_raw),
            sampled_at=_date(row.get('Sampled Date')),
            lab_date=_date(row.get('Lab Date')),
            lab_reference=str(row.get('Lab No.', '') or '').strip() or None,
            smu_hours=_float(row.get('Meter')),
            oil_hours=_float(row.get('Calculated Meter on Fluid')),
            oil_changed=_bool_yn(row.get('Fluid Changed')),
            filter_changed=_bool_yn(row.get('Filter Changed')),
            oil_brand=str(row.get('Fluid Brand', '') or '').strip() or None,
            oil_grade=str(row.get('Fluid Weight', '') or '').strip() or None,
            iron_fe=_float(row.get('Fe')), copper_cu=_float(row.get('Cu')),
            aluminum_al=_float(row.get('Al')), chromium_cr=_float(row.get('Cr')),
            lead_pb=_float(row.get('Pb')), tin_sn=_float(row.get('Sn')),
            nickel_ni=_float(row.get('Ni')), silicon_si=_float(row.get('Si')),
            sodium_na=_float(row.get('Na')), magnesium_mg=_float(row.get('Mg')),
            molybdenum_mo=_float(row.get('Mo')), phosphorus_p=_float(row.get('P')),
            zinc_zn=_float(row.get('Zn')), calcium_ca=_float(row.get('Ca')),
            boron_b=_float(row.get('B')), potassium_k=_float(row.get('K')),
            viscosity_40=_float(row.get('V40')), viscosity_100=_float(row.get('V100')),
            tan=_float(row.get('TAN')), tbn=_float(row.get('TBN')),
            water_pct=_float(row.get('Water %')),
            oxidation=_float(row.get('OXIDATION')), nitration=_float(row.get('NITRATION')),
            fuel_dilution_pct=_float(row.get('DILUTION')),
            glycol=True if str(row.get('GLYCOL', '') or '').strip().upper() in ('Y', 'YES', 'POSITIVE') else None,
            pq_index=_float(row.get('PQI')),
            iso_code=str(row.get('ISO', '') or '').strip() or None,
            vendor_severity=_map_severity(str(row.get('Health', '') or '')),
            vendor_notes=str(row.get('Interp. Text', '') or '').strip() or None,
        )
        records.append(rec)
    return records


# ---------------------------------------------------------------------------
# Parser: United Tractors
# ---------------------------------------------------------------------------

def parse_united_tractors(filepath: str, registry: Optional[pd.DataFrame] = None) -> list:
    eval_map = {'N': 'good', 'B': 'normal', 'C': 'critical', 'X': 'extreme'}

    book = xlrd.open_workbook(filepath, ignore_workbook_corruption=True)
    sh = book.sheet_by_index(0)
    headers = [str(h).strip() for h in sh.row_values(0)]

    def col(row_vals, name):
        try:
            return row_vals[headers.index(name)]
        except (ValueError, IndexError):
            return None

    def _xls_float(v):
        try:
            f = float(v)
            return f if f >= 0 else None
        except (TypeError, ValueError):
            return None

    def _xls_date(v):
        if not v:
            return None
        if isinstance(v, str) and v.strip():
            return _date(v.strip())
        try:
            return datetime(*xlrd.xldate_as_tuple(float(v), book.datemode))
        except Exception:
            return None

    def _serial(v):
        if not v:
            return None
        s = str(v).strip()
        return s.replace('.0', '') if s.endswith('.0') else s

    records = []
    for i in range(1, sh.nrows):
        row = sh.row_values(i)
        if not any(row):
            continue

        uid = str(col(row, 'UNIT_NO') or '').strip()
        ser = _serial(col(row, 'SERIAL_NO')) or ''
        comp_raw = str(col(row, 'COMPONENT') or '').strip()
        eval_code = str(col(row, 'EVAL_CODE') or '').strip().upper()

        ptba_code, method = match_to_ptba(uid, ser, registry) if registry is not None else (None, 'unmatched')

        notes = ' | '.join(filter(None, [
            str(col(row, 'RECOMM1') or '').strip(),
            str(col(row, 'RECOMM2') or '').strip(),
        ])) or None

        rec = OilSampleRecord(
            vendor='united_tractors', source_file=Path(filepath).name,
            ptba_unit_code=ptba_code, match_method=method,
            unit_id_vendor=uid or None, unit_serial=ser or None,
            component_raw=comp_raw or None, component=_normalize_component(comp_raw),
            sampled_at=_xls_date(col(row, 'SAMPL_DT1')),
            lab_date=_xls_date(col(row, 'RPT_DT1')),
            lab_reference=str(col(row, 'Lab_No') or '').strip() or None,
            smu_hours=_xls_float(col(row, 'HRS_KM_TOT')),
            oil_hours=_xls_float(col(row, 'HRS_KM_OC')),
            oil_changed=_bool_yn(col(row, 'oil_change')),
            oil_brand=str(col(row, 'OIL_TYPE') or '').strip() or None,
            oil_grade=str(col(row, 'ORIG_VISC') or '').strip() or None,
            iron_fe=_xls_float(col(row, 'IRON')), copper_cu=_xls_float(col(row, 'COPPER')),
            aluminum_al=_xls_float(col(row, 'ALUMINIUM')), chromium_cr=_xls_float(col(row, 'CHROMIUM')),
            lead_pb=_xls_float(col(row, 'LEAD')), tin_sn=_xls_float(col(row, 'TIN')),
            nickel_ni=_xls_float(col(row, 'NICKEL')), silicon_si=_xls_float(col(row, 'SILICON')),
            sodium_na=_xls_float(col(row, 'SODIUM')), magnesium_mg=_xls_float(col(row, 'MAGNESIUM')),
            molybdenum_mo=_xls_float(col(row, 'Molybdenum')), phosphorus_p=_xls_float(col(row, 'phosphor')),
            zinc_zn=_xls_float(col(row, 'ZINC')), calcium_ca=_xls_float(col(row, 'CALCIUM')),
            boron_b=_xls_float(col(row, 'Boron')), potassium_k=_xls_float(col(row, 'Potassium')),
            barium_ba=_xls_float(col(row, 'Barium')),
            viscosity_40=_xls_float(col(row, 'visc_40')),
            viscosity_100=_xls_float(col(row, 'VISC_CST')),
            tan=_xls_float(col(row, 'T_A_N')), tbn=_xls_float(col(row, 'T_B_N')),
            water_pct=_xls_float(col(row, 'WATER')),
            karl_fischer=_xls_float(col(row, 'KarlFischer')),
            oxidation=_xls_float(col(row, 'OXIDATION')), nitration=_xls_float(col(row, 'NITRATION')),
            fuel_dilution_pct=_xls_float(col(row, 'DILUTION')),
            glycol=True if str(col(row, 'GLYCOL') or '').strip().upper() in ('Y', 'YES', 'POSITIVE') else None,
            pq_index=_xls_float(col(row, 'PQIndex')),
            iso_code=str(col(row, 'ISO4406') or '').strip() or None,
            sox=_xls_float(col(row, 'SOX')),
            fame=_xls_float(col(row, 'FAME')),
            sulphur=_xls_float(col(row, 'sulphur')),
            dir_trans=_xls_float(col(row, 'DIR_TRANS')),
            particles_4um=_xls_float(col(row, '4um')),
            particles_6um=_xls_float(col(row, '6um')),
            particles_15um=_xls_float(col(row, '15um')),
            visc_sae=str(col(row, 'VISC_SAE') or '').strip() or None,
            location=str(col(row, 'grouploc') or '').strip() or None,
            branch=str(col(row, 'branch') or '').strip() or None,
            follow_up=str(col(row, 'follow_up') or '').strip() or None,
            vendor_severity=eval_map.get(eval_code),
            vendor_notes=notes,
            raw_data={h: (str(v) if v is not None else None) for h, v in zip(headers, row)},
        )
        records.append(rec)
    return records


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Parser: Format PTBA compiled (2. SOS.xlsx — sheet per vendor atau "All Unit")
# Kolom kunci: 'No. Lambung Unit', 'Asset Serial Number', 'Component Type',
#              'Sampel Date', 'HM Oil', plus kolom elemen (Fe, Cu, Al, ...)
# ---------------------------------------------------------------------------

def parse_ptba_compiled(filepath: str, registry: Optional[pd.DataFrame] = None) -> list:
    """
    Parser untuk format PTBA internal (file kompilasi SOS seperti '2. SOS.xlsx').
    Mendukung sheet apa saja dengan kolom 'No. Lambung Unit' atau serupa.
    """
    xl = pd.ExcelFile(filepath, engine='openpyxl')
    all_records = []

    # Hanya ambil sheet "All Unit" — satu sumber kebenaran, hindari duplikasi dari sheet per-vendor
    all_unit_sheets = [s for s in xl.sheet_names if s.lower() == 'all unit']
    sheet_order = all_unit_sheets if all_unit_sheets else [s for s in xl.sheet_names if s.lower() not in ('sheet1', 'rekap')]

    for sheet in sheet_order:
        try:
            df = xl.parse(sheet)
        except Exception:
            continue
        if df.empty:
            continue

        # Normalisasi nama kolom
        df.columns = [str(c).strip() for c in df.columns]

        # Cari kolom unit, serial, komponen, tanggal
        def find_col(candidates):
            for c in candidates:
                if c in df.columns:
                    return c
            return None

        # Format PTBA cleaned (Bahasa Indonesia) — prioritaskan kode PTBA yang sudah ada
        ptba_code_col = find_col(['Kode Unit PTBA', 'Kode Unit'])
        unit_col   = ptba_code_col or find_col(['No. Lambung Unit', 'Unit No', 'Unit ID', 'UNIT_NO', 'No Unit'])
        serial_col = find_col(['Serial Chassis', 'Asset Serial Number', 'Serial No', 'Chassis No', 'SERIAL_NO'])
        comp_col   = find_col(['Komponen (Norm)', 'Komponen (Asli)', 'Component Type', 'Component', 'COMPONENT', 'Compartment'])
        date_col   = find_col(['Tgl Sampling', 'Sampel Date', 'Sample Date', 'Sampled Date', 'SAMPL_DT1', 'Tanggal'])
        smu_col    = find_col(['HM Unit (jam)', 'HM Oil', 'Equip Hours', 'Meter', 'HRS_KM_TOT', 'SMU', 'Equipment Hours'])
        sev_col    = find_col(['Status Vendor', 'Health', 'Severity', 'Eval', 'EVAL_CODE', 'Overall Eval', 'Status'])

        if not unit_col and not serial_col:
            continue  # bukan sheet data unit
        if not date_col:
            continue

        # Mapping kolom elemen kimia
        element_map = {
            'fe': find_col(['Fe (ppm)', 'Fe', 'Iron', 'IRON', 'Iron (Fe) mg/kg']),
            'cu': find_col(['Cu (ppm)', 'Cu', 'Copper', 'COPPER', 'Copper (Cu) mg/kg']),
            'al': find_col(['Al (ppm)', 'Al', 'Aluminum', 'Aluminium', 'ALUMINIUM']),
            'cr': find_col(['Cr (ppm)', 'Cr', 'Chromium', 'CHROMIUM']),
            'pb': find_col(['Pb (ppm)', 'Pb', 'Lead', 'LEAD']),
            'si': find_col(['Si (ppm)', 'Si', 'Silicon', 'SILICON']),
            'na': find_col(['Na (ppm)', 'Na', 'Sodium', 'SODIUM']),
            'mg': find_col(['Mg (ppm)', 'Mg', 'Magnesium', 'MAGNESIUM']),
            'v40': find_col(['Viskositas 40°C (cSt)', 'VIS-40', 'Viscosity 40', 'visc_40']),
            'v100': find_col(['Viskositas 100°C (cSt)', 'VIS-100', 'Viscosity 100', 'VISC_CST']),
            'tbn': find_col(['TBN (mgKOH/g)', 'TBN', 'T_B_N']),
            'tan': find_col(['TAN (mgKOH/g)', 'TAN', 'T_A_N']),
            'water': find_col(['Air (%)', 'Water', 'Water %', 'WATER']),
            'soot': find_col(['Jelaga / Soot', 'Soot', 'Soot ABS/cm']),
            'oxid': find_col(['Oksidasi', 'Oxidation', 'OXIDATION']),
            'nitr': find_col(['Nitrasi', 'Nitration', 'NITRATION']),
        }

        model_col = find_col(['Model ', 'Model', 'MODEL'])
        vendor_src_col = find_col(['Vendor Lab', 'Vendor', 'VENDOR'])
        method_col = find_col(['Metode Match'])  # sudah ada di cleaned file

        vendor_name = sheet.lower().replace(' ', '_') if sheet.lower() not in ('semua data', 'all unit') else 'ptba_compiled'

        # Kalau sheet ini sudah punya kode PTBA (cleaned format), skip matching
        already_matched = ptba_code_col is not None

        for _, row in df.iterrows():
            uid   = str(row.get(unit_col,   '') or '').strip() if unit_col   else ''
            ser   = str(row.get(serial_col, '') or '').strip() if serial_col else ''
            model = str(row.get(model_col,  '') or '').strip() if model_col  else ''
            comp_raw = str(row.get(comp_col, '') or '').strip() if comp_col else ''
            sampled = _date(row.get(date_col)) if date_col else None
            row_vendor = str(row.get(vendor_src_col, '') or '').strip() if vendor_src_col else vendor_name

            if not sampled:
                continue
            if not uid and not ser:
                continue

            if already_matched:
                # Kode PTBA sudah ada di kolom 'Kode Unit PTBA'
                ptba_code = uid if uid and uid.lower() not in ('nan', '') else None
                method = str(row.get(method_col, '') or 'direct').strip() if method_col else 'direct'
                if not method or method == 'nan':
                    method = 'direct'
            elif registry is not None:
                ptba_code, method = match_to_ptba(uid, ser, registry, model)
            else:
                ptba_code, method = (uid if uid else None), ('direct' if uid else 'unmatched')

            rec = OilSampleRecord(
                vendor=vendor_name, source_file=f"{filepath}::{sheet}",
                ptba_unit_code=ptba_code, match_method=method,
                unit_id_vendor=uid or None, unit_serial=ser or None,
                component_raw=comp_raw or None, component=_normalize_component(comp_raw),
                sampled_at=sampled,
                smu_hours=_float(row.get(smu_col)) if smu_col else None,
                iron_fe=_float(row.get(element_map['fe'])) if element_map['fe'] else None,
                copper_cu=_float(row.get(element_map['cu'])) if element_map['cu'] else None,
                aluminum_al=_float(row.get(element_map['al'])) if element_map['al'] else None,
                chromium_cr=_float(row.get(element_map['cr'])) if element_map['cr'] else None,
                lead_pb=_float(row.get(element_map['pb'])) if element_map['pb'] else None,
                silicon_si=_float(row.get(element_map['si'])) if element_map['si'] else None,
                sodium_na=_float(row.get(element_map['na'])) if element_map['na'] else None,
                magnesium_mg=_float(row.get(element_map['mg'])) if element_map['mg'] else None,
                viscosity_40=_float(row.get(element_map['v40'])) if element_map['v40'] else None,
                viscosity_100=_float(row.get(element_map['v100'])) if element_map['v100'] else None,
                tbn=_float(row.get(element_map['tbn'])) if element_map['tbn'] else None,
                tan=_float(row.get(element_map['tan'])) if element_map['tan'] else None,
                water_pct=_float(row.get(element_map['water'])) if element_map['water'] else None,
                soot=_float(row.get(element_map['soot'])) if element_map['soot'] else None,
                oxidation=_float(row.get(element_map['oxid'])) if element_map['oxid'] else None,
                nitration=_float(row.get(element_map['nitr'])) if element_map['nitr'] else None,
                vendor_severity=_map_severity(str(row.get(sev_col, '') or '')) if sev_col else None,
                raw_data={str(k): (None if (v is None or (isinstance(v, float) and __import__('math').isnan(v))) else str(v)) for k, v in row.items()},
            )
            all_records.append(rec)

    return all_records


PARSERS = {
    'tekenomiks': parse_tekenomiks,
    'indotruck': parse_indotruck,
    'trakindo': parse_trakindo,
    'united_tractors': parse_united_tractors,
    'ptba_compiled': parse_ptba_compiled,
}


def parse_oil_sample_file(filepath: str, asset_registry_path: Optional[str] = None) -> dict:
    """
    Auto-detect vendor dari konten file, parse, normalisasi,
    dan cocokkan ke kode unit PTBA.
    """
    warnings = []
    vendor = detect_vendor(filepath)

    if vendor.startswith('unknown'):
        return {
            'vendor': vendor, 'total_records': 0, 'records': [],
            'warnings': [], 'error': f'Format tidak dikenali. Vendor tidak terdeteksi dari konten file.'
        }

    registry = None
    if asset_registry_path:
        try:
            registry = load_asset_registry(asset_registry_path)
        except Exception as e:
            warnings.append(f'Asset registry tidak bisa dibaca: {e}')

    try:
        records = PARSERS[vendor](filepath, registry)

        unmatched = [r for r in records if r.match_method == 'unmatched']
        if unmatched:
            ids = list({r.unit_id_vendor for r in unmatched if r.unit_id_vendor})[:5]
            warnings.append(
                f'{len(unmatched)} baris tidak cocok ke unit PTBA '
                f'(contoh ID vendor: {", ".join(ids)}). Perlu konfirmasi manual.'
            )

        no_date = [r for r in records if not r.sampled_at]
        if no_date:
            warnings.append(f'{len(no_date)} baris tidak memiliki tanggal sampel — diabaikan.')
        records = [r for r in records if r.sampled_at]

        match_summary = {}
        for r in records:
            match_summary[r.match_method] = match_summary.get(r.match_method, 0) + 1

        return {
            'vendor': vendor,
            'total_records': len(records),
            'records': records,
            'match_summary': match_summary,
            'warnings': warnings,
            'error': None,
        }

    except Exception as e:
        return {'vendor': vendor, 'total_records': 0, 'records': [], 'warnings': [], 'error': f'{e}'}


# ---------------------------------------------------------------------------
# Test
# ---------------------------------------------------------------------------

if __name__ == '__main__':
    BASE = '/Users/macbookprom1/Documents/Data Analyst/reliability-pdm/'
    ASSET = BASE + '1. Asset Management.xlsx'

    files = [
        '/Users/macbookprom1/Downloads/Tekenomiks.csv',
        '/Users/macbookprom1/Downloads/Indotruck Utama.xlsx',
        '/Users/macbookprom1/Downloads/Trakindo Utama.xlsx',
        '/Users/macbookprom1/Downloads/UT_PAP_export_16-06-2026_09-43-30.xls',
    ]

    for f in files:
        result = parse_oil_sample_file(f, ASSET)
        recs = result['records']
        print(f"\n{'='*60}")
        print(f"File   : {Path(f).name}")
        print(f"Vendor : {result['vendor']}")
        print(f"Records: {result['total_records']}")
        print(f"Match  : {result.get('match_summary', {})}")
        for w in result.get('warnings', []):
            print(f"WARN   : {w}")
        if result.get('error'):
            print(f"ERROR  : {result['error']}")
        if recs:
            r = recs[0]
            print(f"Sample : ptba={r.ptba_unit_code} ({r.match_method}) | comp={r.component} | date={r.sampled_at} | HM={r.smu_hours}")
            print(f"  Fe={r.iron_fe} Cu={r.copper_cu} Al={r.aluminum_al} Si={r.silicon_si}")
            print(f"  Vis40={r.viscosity_40} TBN={r.tbn} Severity={r.vendor_severity}")
