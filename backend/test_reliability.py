"""Test reliability engine dengan data nyata."""
import sys, os
sys.path.insert(0, os.path.dirname(__file__))

from app.services.oil_sample_parser import parse_oil_sample_file
from app.services.sos_reliability import analyze_all, summarize

BASE = '/Users/macbookprom1/Documents/Data Analyst/reliability-pdm/'
ASSET = BASE + '1. Asset Management.xlsx'

VENDOR_FILES = [
    '/Users/macbookprom1/Downloads/Tekenomiks.csv',
    '/Users/macbookprom1/Downloads/Indotruck Utama.xlsx',
    '/Users/macbookprom1/Downloads/Trakindo Utama.xlsx',
    '/Users/macbookprom1/Downloads/UT_PAP_export_16-06-2026_09-43-30.xls',
]

all_records = []
for f in VENDOR_FILES:
    r = parse_oil_sample_file(f, ASSET)
    all_records.extend(r['records'])

print(f'Total records: {len(all_records):,}')

results = analyze_all(all_records)
summary = summarize(results)

print('\n' + '='*60)
print('RINGKASAN RELIABILITY ANALYSIS')
print('='*60)
print(f"Total chain dianalisa  : {summary['total_chains']}")
print(f"Severity distribution  : {summary['severity_distribution']}")
print(f"Vendor disagree        : {summary['vendor_disagree_count']} chain")
print(f"Average confidence     : {summary['avg_confidence']}")
print(f"Alert flags terbanyak  : {list(summary['alert_flags'].items())[:5]}")

# ── Tampilkan 5 teratas extreme/critical ──────────────────────────────────
print('\n' + '─'*60)
print('TOP 10 — UNIT YANG PERLU PERHATIAN')
print('─'*60)

for r in results[:10]:
    agree_str = ''
    if r.vendor_agree is False:
        agree_str = f'  ⚠  BEDA DENGAN VENDOR ({r.vendor_severity})'
    elif r.vendor_agree is True:
        agree_str = f'  ✓ vendor agree'

    print(f'\n[{r.our_severity.upper():8}] {r.ptba_unit_code} — {r.component}'
          f'  (conf={r.confidence:.0%}, n={r.n_samples_used}){agree_str}')

    for f in r.findings[:4]:
        print(f'  • {f.element:15} [{f.finding_type:13}]  {f.message[:90]}')

    if r.alert_flags:
        print(f'  → Perlu konfirmasi: {", ".join(r.alert_flags)}')

# ── Chain yang kita TIDAK setuju dengan vendor ────────────────────────────
print('\n' + '─'*60)
print('DISAGREEMENT DENGAN VENDOR (kami lebih serius)')
print('─'*60)

disagreements = [r for r in results if r.vendor_agree is False
                 and (r.our_severity in ('critical', 'extreme'))
                 and r.vendor_severity in ('good', 'normal')]

for r in disagreements[:8]:
    print(f'\n  {r.ptba_unit_code} — {r.component}')
    print(f'  Vendor: {r.vendor_severity} | Kami: {r.our_severity}')
    print(f'  {r.vendor_disagree_reason}')
    for f in r.findings[:2]:
        print(f'    ↳ {f.message[:100]}')
