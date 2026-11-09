import logging
from contextlib import asynccontextmanager
from pathlib import Path
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app.config import APP_VERSION
from app.database import init_wesbytes_schema_and_seeds
from app.routers import portal, cliente

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("wesbyte_portal")


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Inicializando esquema y catálogo de planes en broker-pmp.wesbytes...")
    try:
        init_wesbytes_schema_and_seeds()
        logger.info("Esquema broker-pmp.wesbytes verificado correctamente.")
    except Exception as exc:
        logger.error(f"Advertencia al inicializar BigQuery en startup: {exc}")
    yield


app = FastAPI(
    title="Wesbyte Portal Comercial & Onboarding Multi-Tenant",
    version=APP_VERSION,
    lifespan=lifespan,
)

static_dir = Path(__file__).resolve().parent / "static"
app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

app.include_router(portal.router)
app.include_router(cliente.router)


@app.get("/health")
async def health():
    return {"status": "ok", "service": "wesbyte_portal", "version": APP_VERSION}
