# main.py
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from api_core import router as api_router, init_runtime as init_api_runtime
from webhook import router as webhook_router
from database import init_db

APP_BUILD = "build_10_fixed"  # conserva tu número de build

app = FastAPI()
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Inicializaciones (DB, clientes externos, etc.)
init_db()
init_api_runtime()  # crea clientes (OpenAI), carga .env, etc.

# Rutas
app.include_router(api_router)
app.include_router(webhook_router)

@app.get("/")
def root():
    return {"ok": True, "service": "cassany", "build": APP_BUILD, "docs": "/docs"}

@app.get("/__version")
def version():
    return {"build": APP_BUILD}
