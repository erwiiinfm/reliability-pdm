from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.sos import router as sos_router
from app.api.service_form import router as form_router
from app.api.assets import router as assets_router
from app.api.inspection import router as inspection_router
from app.api.workers import router as workers_router

app = FastAPI(
    title='PDM — Predictive Maintenance System',
    description='PT Bukit Asam (Persero) Tbk — Unit Pertambangan Tanjung Enim',
    version='0.1.0',
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=['*'],   # nanti restrict ke domain frontend
    allow_methods=['*'],
    allow_headers=['*'],
)

app.include_router(sos_router)
app.include_router(form_router)
app.include_router(assets_router)
app.include_router(inspection_router)
app.include_router(workers_router)


@app.get('/', tags=['Health'])
def root():
    return {'status': 'ok', 'system': 'PDM PTBA'}
