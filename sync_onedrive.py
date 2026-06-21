#!/usr/bin/env python3
"""
Selenium OneDrive Sync — dijalankan dari selenium_server.py
Buka Chrome visible, navigasi ke SharePoint, download file Excel, import ke DB.
"""

import sys, time, os, shutil, requests
from pathlib import Path
from typing import Optional

from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from webdriver_manager.chrome import ChromeDriverManager

SHARE_URL    = os.getenv('ONEDRIVE_SHARE_URL',
    'https://bukitasamcoid-my.sharepoint.com/:x:/g/personal/snasri_bukitasam_co_id/'
    'IQBq6xn5H66PQLk_n3672yIiAaXPwYjjIQVcvDHQzq_eB7U?e=squYxh'
)
DOWNLOAD_DIR = Path('/tmp/pdm_selenium_dl')
BACKEND_URL  = 'http://localhost:8000'
LOG_FILE     = Path('/tmp/pdm-onedrive-sync.log')


def log(msg):
    ts = time.strftime('%Y-%m-%d %H:%M:%S')
    line = f'[{ts}] {msg}'
    print(line, flush=True)
    with open(LOG_FILE, 'a') as f:
        f.write(line + '\n')


def make_driver():
    DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
    opts = Options()
    opts.add_argument('--no-sandbox')
    opts.add_argument('--disable-dev-shm-usage')
    opts.add_argument('--disable-blink-features=AutomationControlled')
    opts.add_experimental_option('excludeSwitches', ['enable-automation'])
    opts.add_experimental_option('useAutomationExtension', False)
    opts.add_experimental_option('prefs', {
        'download.default_directory': str(DOWNLOAD_DIR),
        'download.prompt_for_download': False,
        'download.directory_upgrade': True,
        'safebrowsing.enabled': True,
    })
    service = Service(ChromeDriverManager().install())
    driver = webdriver.Chrome(service=service, options=opts)
    driver.execute_script("Object.defineProperty(navigator,'webdriver',{get:()=>undefined})")
    return driver


def wait_for_download(timeout=120) -> Optional[Path]:
    deadline = time.time() + timeout
    while time.time() < deadline:
        files = [f for f in DOWNLOAD_DIR.iterdir()
                 if f.suffix.lower() in ('.xlsx', '.xls')
                 and not f.name.endswith('.crdownload')]
        if files:
            return max(files, key=lambda f: f.stat().st_mtime)
        time.sleep(1)
    return None


def trigger_download(driver) -> bool:
    # Navigasi ke URL download langsung — lebih reliable dari klik tombol SharePoint
    cur = driver.current_url
    sep = '&' if '?' in cur else '?'
    dl_url = cur + sep + 'download=1'
    log(f'Navigasi ke URL download...')
    driver.get(dl_url)
    return True


def import_to_backend(filepath: Path) -> dict:
    log(f'Upload ke backend: {filepath.name}')
    with open(filepath, 'rb') as f:
        r = requests.post(
            f'{BACKEND_URL}/api/sos/upload',
            files={'file': (filepath.name, f,
                   'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')},
            timeout=120,
        )
    if r.status_code != 200:
        return {'error': f'Upload HTTP {r.status_code}: {r.text[:300]}'}

    data = r.json()
    session_id = data.get('session_id')
    if not session_id:
        return {'error': f'Tidak ada session_id: {data}'}

    r2 = requests.post(
        f'{BACKEND_URL}/api/sos/confirm',
        json={'session_id': session_id, 'selected_rows': None},
        headers={'Content-Type': 'application/json'},
        timeout=120,
    )
    if r2.status_code == 200:
        return r2.json()
    return {'error': f'Confirm HTTP {r2.status_code}: {r2.text[:300]}'}


def run_sync() -> dict:
    if DOWNLOAD_DIR.exists():
        shutil.rmtree(DOWNLOAD_DIR)
    DOWNLOAD_DIR.mkdir(parents=True)

    log('=== Mulai sync OneDrive via Selenium ===')
    driver = make_driver()
    try:
        log(f'Buka Chrome → {SHARE_URL[:60]}...')
        driver.get(SHARE_URL)

        # Tunggu halaman load — mungkin perlu login dulu
        log('Menunggu halaman SharePoint...')
        time.sleep(6)

        title = driver.title
        log(f'Halaman: {title[:80]}')

        # Klik tombol download
        trigger_download(driver)
        time.sleep(3)

        log('Menunggu file selesai didownload...')
        filepath = wait_for_download(timeout=120)
        if not filepath:
            return {'error': 'File tidak terdownload dalam 120 detik'}

        size_kb = filepath.stat().st_size // 1024
        log(f'File: {filepath.name} ({size_kb} KB)')

        result = import_to_backend(filepath)

        if 'error' in result:
            log(f'Import gagal: {result["error"]}')
            return result

        inserted = result.get('inserted', 0)
        dupes    = result.get('skipped_duplicate', 0)
        log(f'✓ Selesai: +{inserted} baru, {dupes} duplikat')
        return {
            'inserted': inserted,
            'skipped_duplicate': dupes,
            'size_kb': size_kb,
            'file': filepath.name,
        }

    except Exception as e:
        import traceback
        log(f'ERROR: {e}\n{traceback.format_exc()}')
        return {'error': str(e)}
    finally:
        driver.quit()


if __name__ == '__main__':
    import json as _json
    result = run_sync()
    print(_json.dumps(result))
    sys.exit(0 if 'error' not in result else 1)
