import datetime
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

import jwt
from fastapi import APIRouter, Body, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from google.cloud import bigquery

from app.config import (
    ACCESS_TOKEN_EXPIRE_MINUTES,
    ALGORITHM,
    APP_VERSION,
    BIGQUERY_PROJECT_ID,
    SECRET_KEY,
    SOPORTE_EMAIL,
    WESBYTE_APP_URL,
    WESBYTES_DATASET_ID,
)
from app.database import get_bq_client, verify_password

logger = logging.getLogger("wesbyte_portal.cliente")
router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).resolve().parent.parent / "templates"))

COOKIE_NAME = "wb_cliente"
P = BIGQUERY_PROJECT_ID
W = WESBYTES_DATASET_ID


def get_sesion(request: Request) -> Optional[Dict[str, Any]]:
    """Devuelve la sesión del cliente (cookie JWT) o None."""
    token = request.cookies.get(COOKIE_NAME)
    if not token:
        return None
    try:
        return jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
    except Exception:
        return None


def _rows(sql: str, params: Optional[List[bigquery.ScalarQueryParameter]] = None) -> List[Dict[str, Any]]:
    cfg = bigquery.QueryJobConfig(query_parameters=params or [])
    return [dict(r) for r in get_bq_client().query(sql, job_config=cfg).result()]


@router.get("/ingresar", response_class=HTMLResponse)
async def ingresar_page(request: Request):
    if get_sesion(request):
        return RedirectResponse("/mi-portal", status_code=302)
    return templates.TemplateResponse(
        request=request, name="login.html",
        context={"soporte_email": SOPORTE_EMAIL, "app_version": APP_VERSION, "sesion": None},
    )


@router.post("/api/ingresar")
async def api_ingresar(payload: Dict[str, Any] = Body(...)):
    """Login de clientes que ya pagaron: usuario o correo + contraseña."""
    login = str(payload.get("usuario", "")).strip()
    password = str(payload.get("password", ""))
    if not login or not password:
        raise HTTPException(status_code=400, detail="Ingresa tu usuario y contraseña.")

    candidatos = _rows(
        f"""
        SELECT u.id, u.cliente_id, u.username, u.nombre, u.email, u.password_hash, u.rol,
               c.estado, c.nombre AS empresa, c.dataset_id
        FROM `{P}.{W}.usuarios` u
        JOIN `{P}.{W}.clientes` c ON c.id = u.cliente_id
        WHERE u.activo = TRUE
          AND (LOWER(u.email) = LOWER(@l) OR LOWER(u.username) = LOWER(@l))
        """,
        [bigquery.ScalarQueryParameter("l", "STRING", login)],
    )
    validos = [c for c in candidatos if verify_password(password, str(c.get("password_hash") or ""))]
    if not validos:
        raise HTTPException(status_code=401, detail="Usuario o contraseña incorrectos.")

    activos = [c for c in validos if c.get("estado") == "activo"]
    if not activos:
        raise HTTPException(
            status_code=403,
            detail="Tu cuenta aún no tiene un pago confirmado. Completa el pago para activar tu portal.",
        )

    u = activos[0]
    exp = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    token = jwt.encode(
        {"sub": u["username"], "uid": int(u["id"]), "cid": int(u["cliente_id"]),
         "nombre": u["nombre"], "empresa": u["empresa"], "exp": exp},
        SECRET_KEY, algorithm=ALGORITHM,
    )
    resp = JSONResponse({"success": True, "redirect_url": "/mi-portal"})
    resp.set_cookie(COOKIE_NAME, token, httponly=True, samesite="lax",
                    max_age=ACCESS_TOKEN_EXPIRE_MINUTES * 60)
    return resp


@router.get("/salir")
async def salir():
    resp = RedirectResponse("/", status_code=302)
    resp.delete_cookie(COOKIE_NAME)
    return resp


def _dashboard(cid: int) -> Optional[Dict[str, Any]]:
    cp = [bigquery.ScalarQueryParameter("cid", "INT64", cid)]
    cli = _rows(f"SELECT * FROM `{P}.{W}.clientes` WHERE id=@cid LIMIT 1", cp)
    if not cli:
        return None
    cliente = cli[0]

    plan = (_rows(f"SELECT * FROM `{P}.{W}.planes` WHERE id=@plan",
                  [bigquery.ScalarQueryParameter("plan", "INT64", int(cliente.get("plan_id") or 0))]) or [{}])[0]
    sus = (_rows(f"SELECT * FROM `{P}.{W}.suscripciones` WHERE cliente_id=@cid ORDER BY id DESC LIMIT 1", cp) or [None])[0]
    pagos = _rows(f"SELECT * FROM `{P}.{W}.pagos_payphone` WHERE cliente_id=@cid ORDER BY id DESC LIMIT 10", cp)
    usuarios = _rows(f"SELECT username, nombre, rol, activo, ultimo_login FROM `{P}.{W}.usuarios` WHERE cliente_id=@cid ORDER BY id", cp)

    periodo = datetime.date.today().strftime("%Y-%m")
    cons = (_rows(
        f"SELECT * FROM `{P}.{W}.consumos_mensuales` WHERE cliente_id=@cid AND periodo=@p ORDER BY id DESC LIMIT 1",
        cp + [bigquery.ScalarQueryParameter("p", "STRING", periodo)]) or [{}])[0]

    def uso(total_key: str, limite_key: str) -> Dict[str, Any]:
        usado = int(cons.get(total_key) or 0)
        limite = int(cliente.get(limite_key) or 0)
        pct = min(100, round(usado * 100 / limite)) if limite else 0
        return {"usado": usado, "limite": limite, "pct": pct}

    consumos = {
        "whatsapp": uso("total_whatsapp", "limite_mensual_whatsapp"),
        "emails": uso("total_emails", "limite_mensual_emails"),
        "api": uso("total_api_ecuador", "limite_mensual_api_ecuador"),
    }

    # Datos cargados en su base de datos
    registros: Dict[str, int] = {}
    total_clientes = 0
    ds = cliente.get("dataset_id")
    if ds:
        client = get_bq_client()
        for name in ("maestra_clientes", "registros_medicos", "registros_vehiculares",
                     "registros_vida_e_inversion", "registros_ramos_generales"):
            try:
                n = int(client.get_table(f"{P}.{ds}.{name}").num_rows or 0)
            except Exception:
                n = 0
            if name == "maestra_clientes":
                total_clientes = n
            else:
                registros[name] = n

    dias_restantes = None
    if sus and sus.get("fecha_fin"):
        dias_restantes = (sus["fecha_fin"] - datetime.date.today()).days

    return {
        "cliente": cliente, "plan": plan, "sus": sus, "pagos": pagos, "usuarios": usuarios,
        "consumos": consumos, "registros": registros, "total_clientes": total_clientes,
        "total_registros": sum(registros.values()), "dias_restantes": dias_restantes,
        "periodo": periodo,
    }


@router.get("/mi-portal", response_class=HTMLResponse)
async def mi_portal(request: Request):
    sesion = get_sesion(request)
    if not sesion:
        return RedirectResponse("/ingresar", status_code=302)
    try:
        data = _dashboard(int(sesion["cid"]))
    except Exception:
        logger.exception("Error cargando dashboard")
        raise HTTPException(status_code=500, detail="No pudimos cargar tu portal. Intenta de nuevo.")
    if not data or data["cliente"].get("estado") != "activo":
        return RedirectResponse("/salir", status_code=302)
    return templates.TemplateResponse(
        request=request, name="mi_portal.html",
        context={"sesion": sesion, "d": data, "soporte_email": SOPORTE_EMAIL,
                 "wesbyte_app_url": WESBYTE_APP_URL, "app_version": APP_VERSION},
    )
