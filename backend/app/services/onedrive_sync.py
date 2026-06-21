"""
OneDrive Sync Service (stub)
Sync sekarang dijalankan via Selenium dari host — selenium_server.py port 8001.
File ini hanya menyimpan status sync terakhir.
"""

import json
from pathlib import Path
from datetime import datetime, timezone

SYNC_STATUS_PATH = Path('/tmp/pdm_sync_status.json')


def get_sync_status() -> dict:
    if not SYNC_STATUS_PATH.exists():
        return {'state': 'never', 'last_sync': None}
    return json.loads(SYNC_STATUS_PATH.read_text())


def set_sync_status(state: str, result: dict = None):
    SYNC_STATUS_PATH.write_text(json.dumps({
        'state': state,
        'last_sync': datetime.now(timezone.utc).isoformat(),
        'result': result,
    }))
