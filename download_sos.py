"""
download_sos.py — Unduh file PAP SOS dari SharePoint PTBA secara otomatis.

Cara pakai:
  python3 download_sos.py            # download sekali
  python3 download_sos.py --watch    # download lalu ulangi tiap 2 jam
"""

import sys, shutil, tempfile, time, signal, os, asyncio
from pathlib import Path
from datetime import datetime

SHARE_URL   = (
    'https://bukitasamcoid-my.sharepoint.com/:x:/g/personal/snasri_bukitasam_co_id/'
    'IQBq6xn5H66PQLk_n3672yIiAaXPwYjjIQVcvDHQzq_eB7U?e=TcuVFf'
)
DOWNLOADS   = Path.home() / 'Downloads'
CHROME_PROF = Path.home() / 'Library/Application Support/Google/Chrome'
INTERVAL_SEC = 2 * 60 * 60

_running = True


def ts():
    return datetime.now().strftime('%H:%M:%S')


def copy_profile():
    tmp = tempfile.mkdtemp(prefix='pdm_chrome_')
    src = CHROME_PROF / 'Default'
    dst = Path(tmp) / 'Default'
    print(f'[{ts()}] Menyalin profil Chrome...')
    shutil.copytree(src, dst, ignore=shutil.ignore_patterns(
        'lock', 'SingletonLock', 'SingletonCookie', '*.log',
        'GPUCache', 'ShaderCache', 'Code Cache', 'Cache',
    ))
    return tmp


async def download_once():
    from playwright.async_api import async_playwright

    tmp = copy_profile()
    try:
        async with async_playwright() as p:
            ctx = await p.chromium.launch_persistent_context(
                tmp,
                headless=True,
                downloads_path=str(DOWNLOADS),
                args=['--no-sandbox', '--disable-gpu',
                      '--disable-dev-shm-usage'],
            )
            page = await ctx.new_page()
            print(f'[{ts()}] Membuka SharePoint...')

            # expect_download dulu SEBELUM navigate — karena URL langsung trigger download
            async with page.expect_download(timeout=45_000) as dl_info:
                try:
                    await page.goto(SHARE_URL + '&download=1',
                                    wait_until='commit', timeout=30_000)
                except Exception:
                    pass  # "Download is starting" error normal, download tetap berjalan

            dl    = await dl_info.value
            fname = dl.suggested_filename or 'SOS_PAP_latest.xlsx'
            dest  = DOWNLOADS / fname
            await dl.save_as(str(dest))
            print(f'[{ts()}] Berhasil: {dest} ({dest.stat().st_size // 1024} KB)')

            await ctx.close()
            return True

    except Exception as e:
        print(f'[{ts()}] Gagal: {e}')
        return False
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def signal_handler(sig, frame):
    global _running
    print('\nBerhenti.')
    _running = False


if __name__ == '__main__':
    signal.signal(signal.SIGINT, signal_handler)
    watch = '--watch' in sys.argv

    print('=== PDM — Download SOS dari SharePoint ===')
    asyncio.run(download_once())

    if watch:
        print(f'\nMode watch aktif — mengunduh ulang tiap {INTERVAL_SEC // 3600} jam.')
        print('Tekan Ctrl+C untuk berhenti.\n')
        while _running:
            for _ in range(INTERVAL_SEC):
                if not _running:
                    break
                time.sleep(1)
            if _running:
                asyncio.run(download_once())
