#!/usr/bin/env python3
"""
Selenium Sync Server — berjalan di host Mac, port 8001.
Dashboard memanggil POST http://localhost:8001/sync untuk trigger download OneDrive.
"""

import json, time, threading, subprocess, sys, datetime
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path

STATUS_FILE  = Path('/tmp/pdm-selenium-status.json')
_lock        = threading.Lock()
_running     = False
SYNC_HOUR    = 15   # Jam sync otomatis (GMT+7 / WIB)
SYNC_MINUTE  = 0


def _read_status() -> dict:
    if STATUS_FILE.exists():
        try:
            return json.loads(STATUS_FILE.read_text())
        except Exception:
            pass
    return {'state': 'idle', 'last_sync': None, 'result': None}


def _write_status(state: str, result: dict = None):
    STATUS_FILE.write_text(json.dumps({
        'state': state,
        'last_sync': time.strftime('%Y-%m-%dT%H:%M:%S'),
        'result': result,
    }))


def _do_sync():
    global _running
    _write_status('running')
    script = Path(__file__).parent / 'sync_onedrive.py'
    try:
        proc = subprocess.run(
            [sys.executable, str(script)],
            capture_output=True, text=True, timeout=300
        )
        # Parse result dari stdout terakhir (baris JSON)
        out = proc.stdout.strip()
        result = None
        for line in reversed(out.splitlines()):
            line = line.strip()
            if line.startswith('{'):
                try:
                    result = json.loads(line)
                    break
                except Exception:
                    pass
        if result is None:
            result = {'error': proc.stderr[-500:] if proc.stderr else 'Tidak ada output'}
        _write_status('done', result)
    except subprocess.TimeoutExpired:
        _write_status('done', {'error': 'Timeout (5 menit)'})
    except Exception as e:
        _write_status('done', {'error': str(e)})
    finally:
        _running = False


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass  # Suppress request logs

    def _cors(self):
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type')

    def do_OPTIONS(self):
        self.send_response(200)
        self._cors()
        self.end_headers()

    def do_GET(self):
        if self.path == '/status':
            self._json(_read_status())
        else:
            self._json({'error': 'Not found'}, 404)

    def do_POST(self):
        global _running
        if self.path == '/sync':
            with _lock:
                if _running:
                    self._json({'state': 'running', 'message': 'Sync sedang berjalan...'})
                    return
                _running = True
                t = threading.Thread(target=_do_sync, daemon=True)
                t.start()
            self._json({'state': 'started', 'message': 'Chrome dibuka, menunggu download...'})
        else:
            self._json({'error': 'Not found'}, 404)

    def _json(self, data, code=200):
        body = json.dumps(data).encode()
        self.send_response(code)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', len(body))
        self._cors()
        self.end_headers()
        self.wfile.write(body)


def _scheduler():
    """Jalankan sync otomatis setiap hari jam SYNC_HOUR:SYNC_MINUTE WIB."""
    print(f'[scheduler] Auto-sync dijadwalkan tiap hari jam {SYNC_HOUR:02d}:{SYNC_MINUTE:02d} WIB', flush=True)
    last_run_date = None
    while True:
        now = datetime.datetime.now()  # waktu lokal Mac (sudah WIB kalau timezone Mac = WIB)
        if now.hour == SYNC_HOUR and now.minute == SYNC_MINUTE and now.date() != last_run_date:
            print(f'[scheduler] Memulai auto-sync jam {SYNC_HOUR:02d}:{SYNC_MINUTE:02d}', flush=True)
            last_run_date = now.date()
            global _running
            with _lock:
                if not _running:
                    _running = True
                    threading.Thread(target=_do_sync, daemon=True).start()
        time.sleep(30)  # cek tiap 30 detik


if __name__ == '__main__':
    threading.Thread(target=_scheduler, daemon=True).start()
    server = HTTPServer(('localhost', 8001), Handler)
    print(f'[selenium_server] Berjalan di http://localhost:8001', flush=True)
    server.serve_forever()
