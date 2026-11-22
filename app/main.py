import asyncio
import datetime
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from zoneinfo import ZoneInfo
from fastapi import FastAPI, Request
from fastapi.responses import PlainTextResponse, Response
from fastapi.staticfiles import StaticFiles

from app.config import APP_VERSION, SRI_INVOICE_HOUR_ECUADOR
from app.database import init_wesbytes_schema_and_seeds
from app.routers import portal, cliente, internal_notifications, media

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


async def _daily_sri_invoicing_scheduler():
    """Ejecuta todos los días a la hora configurada (01:00 AM hora Ecuador por defecto) la facturación electrónica SRI."""
    from app.services.sri_invoicing import procesar_lote_facturas_pendientes
    tz_ec = ZoneInfo("America/Guayaquil")

    parts = (SRI_INVOICE_HOUR_ECUADOR or "01:00").split(":")
    try:
        target_hour = max(0, min(23, int(parts[0])))
        target_min = max(0, min(59, int(parts[1]) if len(parts) > 1 else 0))
    except Exception:
        target_hour, target_min = 1, 0

    while True:
        now_ec = datetime.datetime.now(tz_ec)
        next_run = now_ec.replace(hour=target_hour, minute=target_min, second=0, microsecond=0)
        if now_ec >= next_run:
            next_run += datetime.timedelta(days=1)
        wait_seconds = max(1.0, (next_run - now_ec).total_seconds())
        await asyncio.sleep(wait_seconds)
        try:
            logger.info("[Job SRI %02d:%02d Ecuador] Procesando lote de facturas pendientes...", target_hour, target_min)
            res = await procesar_lote_facturas_pendientes(limit=30)
            logger.info(
                "[Job SRI Ecuador] Evaluadas: %s | Autorizadas: %s | Errores: %s",
                res.get("total_evaluadas", 0),
                res.get("autorizadas", 0),
                res.get("errores", 0),
            )
        except Exception as exc:
            logger.error("[Job SRI Ecuador] Error en facturación nocturna: %s", exc, exc_info=True)


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

    # Arrancar scheduler nocturno de facturación electrónica SRI (01:00 AM Ecuador) en un único worker
    try:
        import fcntl
        sri_lock_file = open("/tmp/wesbyte_portal_sri_invoicing.lock", "w")
        fcntl.flock(sri_lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        app.state._sri_invoicing_lock_file = sri_lock_file
        asyncio.create_task(_daily_sri_invoicing_scheduler())
        logger.info("Scheduler nocturno de facturación electrónica SRI (%s Ecuador) iniciado.", SRI_INVOICE_HOUR_ECUADOR)
    except BlockingIOError:
        pass
    except Exception:
        asyncio.create_task(_daily_sri_invoicing_scheduler())

    yield


app = FastAPI(
    title="Wesbyte Portal Comercial & Onboarding Multi-Tenant",
    version=APP_VERSION,
    lifespan=lifespan,
)


@app.middleware("http")
async def add_referrer_policy_header(request: Request, call_next):
    response = await call_next(request)
    response.headers.setdefault("Referrer-Policy", "origin-when-cross-origin")
    return response

static_dir = Path(__file__).resolve().parent / "static"
app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

app.include_router(portal.router)
app.include_router(cliente.router)
app.include_router(internal_notifications.router)
app.include_router(internal_notifications.sri_internal_router)
app.include_router(media.router)


@app.get("/robots.txt", response_class=PlainTextResponse, include_in_schema=False)
async def robots_txt(request: Request):
    scheme = request.headers.get("x-forwarded-proto", request.url.scheme)
    base_url = f"{scheme}://{request.url.netloc}"
    content = (
        "User-agent: *\n"
        "Allow: /\n"
        "Disallow: /mi-portal\n"
        "Disallow: /ingresar\n"
        "Disallow: /salir\n"
        "Disallow: /checkout\n"
        "Disallow: /exito\n"
        "Disallow: /panel-clientes\n"
        "Disallow: /api/\n"
        "Disallow: /internal/\n"
        f"Sitemap: {base_url}/sitemap.xml\n"
    )
    return PlainTextResponse(content=content, media_type="text/plain")


@app.get("/sitemap.xml", include_in_schema=False)
async def sitemap_xml(request: Request):
    scheme = request.headers.get("x-forwarded-proto", request.url.scheme)
    base_url = f"{scheme}://{request.url.netloc}"
    today = datetime.date.today().isoformat()
    xml = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
        "  <url>\n"
        f"    <loc>{base_url}/</loc>\n"
        f"    <lastmod>{today}</lastmod>\n"
        "    <changefreq>weekly</changefreq>\n"
        "    <priority>1.0</priority>\n"
        "  </url>\n"
        "</urlset>\n"
    )
    return Response(content=xml, media_type="application/xml")


@app.get("/health")
async def health():
    return {"status": "ok", "service": "wesbyte_portal", "version": APP_VERSION}
