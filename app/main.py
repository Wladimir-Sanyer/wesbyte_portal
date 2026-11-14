import asyncio
import datetime
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from zoneinfo import ZoneInfo
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app.config import APP_VERSION
from app.database import init_wesbytes_schema_and_seeds
from app.routers import portal, cliente, internal_notifications

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("wesbyte_portal")


async def _daily_birthday_scheduler():
    """Ejecuta todos los días a las 08:00 AM hora Ecuador el proceso de cumpleaños."""
    from app.services.birthday_notification_service import BirthdayNotificationService
    tz_ec = ZoneInfo("America/Guayaquil")

    while True:
        now_ec = datetime.datetime.now(tz_ec)
        next_run = now_ec.replace(hour=8, minute=0, second=0, microsecond=0)
        if now_ec >= next_run:
            next_run += datetime.timedelta(days=1)
        wait_seconds = max(1.0, (next_run - now_ec).total_seconds())
        await asyncio.sleep(wait_seconds)
        try:
            logger.info("[Job 8AM Ecuador] Ejecutando BirthdayNotificationService...")
            res = await asyncio.to_thread(BirthdayNotificationService().run_birthday_process)
            logger.info(f"[Job 8AM Ecuador] Cumpleaños procesados: {res.get('total_despachados', 0)}")
        except Exception as exc:
            logger.error(f"[Job 8AM Ecuador] Error en cumpleaños: {exc}", exc_info=True)


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Inicializando esquema y catálogo de planes en wesbyte.administracion...")
    try:
        init_wesbytes_schema_and_seeds()
        logger.info("Esquema wesbyte.administracion verificado correctamente.")
    except Exception as exc:
        logger.error(f"Advertencia al inicializar BigQuery en startup: {exc}")

    # Arrancar scheduler diario de cumpleaños (8:00 AM Ecuador) en un único worker
    try:
        import fcntl
        lock_file = open("/tmp/wesbyte_portal_birthday.lock", "w")
        fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        app.state._birthday_lock_file = lock_file
        asyncio.create_task(_daily_birthday_scheduler())
        logger.info("Scheduler diario de cumpleaños (08:00 AM Ecuador) iniciado.")
    except BlockingIOError:
        pass
    except Exception:
        asyncio.create_task(_daily_birthday_scheduler())

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
app.include_router(internal_notifications.router)


@app.get("/health")
async def health():
    return {"status": "ok", "service": "wesbyte_portal", "version": APP_VERSION}
