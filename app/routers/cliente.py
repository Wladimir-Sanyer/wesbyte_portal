import asyncio
import datetime
import json
import logging
import re
import secrets
import string
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, List, Optional

import jwt
from fastapi import APIRouter, Body, File, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from google.cloud import bigquery
from pydantic import BaseModel, Field

from app.config import (
    ACCESS_TOKEN_EXPIRE_MINUTES,
    ALGORITHM,
    APP_VERSION,
    BIGQUERY_PROJECT_ID,
    DEMO_PLAN_CONFIG,
    ECUADOR_TZ,
    IVA_PORCENTAJE,
    PLANES_OFICIALES,
    SECRET_KEY,
    SOPORTE_EMAIL,
    TARIFAS_CALCULADORA,
    WESBYTE_APP_URL,
    WESBYTES_DATASET_ID,
)
from app.database import append_rows, execute_query, get_bq_client, get_next_id, hash_password, verify_password
from app.services.email_service import EmailService, EmailServiceError
from app.services.google_drive_service import GoogleDriveService, GoogleDriveServiceError, normalize_drive_logo_url
from app.routers.media import prime_media_cache
from app.services.onboarding_service import registrar_intento_pago_payphone
from app.services.payphone_service import build_payphone_box_config, generate_client_tx_id
from app.services.notificaciones_config_service import (
    get_cliente_notificaciones_y_plantillas,
    save_cliente_notificaciones_habilitadas,
    save_cliente_plantilla,
)
from app.services.test_notification_service import send_test_notification
from app.services.whatsapp_client import (
    WhatsAppClient,
    WhatsAppClientError,
    sync_client_whatsapp_from_admin,
)
from app.services.welcome_notification_service import WHATSAPP_WESBYTE_SENDER_CLIENT_ID

logger = logging.getLogger("wesbyte_portal.cliente")
router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).resolve().parent.parent / "templates"))

COOKIE_NAME = "wb_cliente"
P = BIGQUERY_PROJECT_ID
W = WESBYTES_DATASET_ID

_CACHE_LOCK = threading.Lock()
_DASHBOARD_CACHE: Dict[int, tuple[float, Dict[str, Any]]] = {}
_DATASET_STATS_CACHE: Dict[str, tuple[float, Dict[str, Any]]] = {}
_PLANES_MAP_CACHE: Optional[tuple[float, Dict[int, Dict[str, Any]]]] = None

_DASHBOARD_TTL_SEC = 60.0
_DATASET_STATS_TTL_SEC = 300.0
_PLANES_TTL_SEC = 600.0

_AUDIT_LOCK = threading.Lock()
_LAST_AUDIT_TS: float = 0.0
_AUDIT_INTERVAL_SEC = 21600.0  # 6 horas


def invalidate_cliente_cache(cliente_id: int) -> None:
    """Invalida el caché en memoria del dashboard de un cliente tras cualquier escritura."""
    with _CACHE_LOCK:
        _DASHBOARD_CACHE.pop(int(cliente_id), None)


def invalidate_planes_cache() -> None:
    """Invalida el caché en memoria del catálogo de planes."""
    global _PLANES_MAP_CACHE
    with _CACHE_LOCK:
        _PLANES_MAP_CACHE = None


def get_sesion(request: Request) -> Optional[Dict[str, Any]]:
    """Devuelve la sesión del cliente (cookie JWT) o None."""
    token = request.cookies.get(COOKIE_NAME)
    if not token:
        return None
    try:
        return jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
    except Exception:
        return None


def _is_superadmin(sesion: Optional[Dict[str, Any]]) -> bool:
    if not sesion:
        return False
    return (sesion.get("rol") == "superadmin") or (
        sesion.get("sub") in ("admin", "wasm") and int(sesion.get("cid") or sesion.get("cliente_id") or 0) == 0
    )


def _generar_password_temporal(length: int = 10) -> str:
    alphabet = string.ascii_letters + string.digits
    return "Wb" + "".join(secrets.choice(alphabet) for _ in range(max(6, length - 2)))


def _enviar_correo_credenciales(
    to_email: str,
    nombre: str,
    temp_password: str,
    empresa: str = "Wesbyte",
    es_reset: bool = False,
) -> tuple[bool, str]:
    """Envía por Resend el correo con el usuario (email) y la contraseña temporal."""
    titulo = "Recuperación de Contraseña · Wesbyte" if es_reset else "Bienvenido a Wesbyte · Tus Credenciales de Acceso"
    subtitulo = (
        "Se ha generado una nueva contraseña temporal para tu cuenta."
        if es_reset
        else f"Tu cuenta ha sido creada en <strong>{empresa}</strong>."
    )
    boton_url = (
        "https://wesbyte.app/actualizar-password"
        if es_reset
        else f"{WESBYTE_APP_URL.rstrip('/')}/actualizar-password"
    )
    html_body = f"""
    <div style="font-family: 'Segoe UI', Arial, sans-serif; max-width: 560px; margin: 0 auto; background: #ffffff; border: 1px solid #e2e8f0; border-radius: 16px; overflow: hidden;">
      <div style="background: linear-gradient(135deg, #12163a, #4f46e5); padding: 28px 24px; color: #ffffff;">
        <h2 style="margin: 0; font-size: 22px; font-weight: 800;">{titulo}</h2>
        <p style="margin: 6px 0 0; opacity: .88; font-size: 14px;">{subtitulo}</p>
      </div>
      <div style="padding: 24px; color: #1e293b; font-size: 15px; line-height: 1.6;">
        <p>Hola <strong>{nombre}</strong>,</p>
        <p>Ingresa utilizando tu correo electrónico y la siguiente contraseña temporal:</p>
        <div style="background: #f8fafc; border: 1px solid #cbd5e1; border-radius: 12px; padding: 16px; margin: 16px 0;">
          <div style="margin-bottom: 8px;"><strong>Usuario (Correo):</strong> {to_email}</div>
          <div><strong>Contraseña Temporal:</strong> <code style="background:#eef2ff;color:#4338ca;padding:4px 10px;border-radius:6px;font-size:16px;font-weight:700;">{temp_password}</code></div>
        </div>
        <p style="color: #b45309; background: #fffbeb; border: 1px solid #fde68a; border-radius: 10px; padding: 12px; font-size: 13px;">
          <strong>Importante:</strong> Por tu seguridad, al iniciar sesión por primera vez con esta contraseña temporal, el sistema te solicitará actualizarla por una contraseña personal.
        </p>
        <div style="margin-top: 22px; text-align: center;">
          <a href="{boton_url}" style="display:inline-block;background:#4f46e5;color:#ffffff;text-decoration:none;font-weight:700;padding:12px 24px;border-radius:10px;">Actualizar contraseña</a>
        </div>
        <div style="margin-top:24px;padding-top:12px;border-top:1px solid #e2e8f0;text-align:center;font-size:10px;color:#94a3b8;letter-spacing:0.03em;">
          Enviado por <a href="https://wesbyte.app" style="color:#64748b;text-decoration:none;font-weight:600;">wesbyte.app</a>
        </div>
      </div>
    </div>
    """
    text_body = (
        f"Hola {nombre},\n\n"
        f"{'Se generó una nueva contraseña temporal.' if es_reset else f'Tu cuenta en {empresa} ha sido creada.'}\n"
        f"Usuario (Correo): {to_email}\n"
        f"Contraseña Temporal: {temp_password}\n\n"
        f"En tu primer ingreso se te solicitará actualizar tu contraseña.\n"
        f"Actualizar contraseña: {boton_url}\n"
    )
    try:
        EmailService().send_email(
            to=to_email,
            subject=titulo,
            html=html_body,
            text=text_body,
        )
        return True, ""
    except Exception as exc:
        logger.warning(f"No se pudo enviar correo de credenciales a {to_email}: {exc}")
        return False, str(exc)


def _rows(sql: str, params: Optional[List[bigquery.ScalarQueryParameter]] = None) -> List[Dict[str, Any]]:
    cfg = bigquery.QueryJobConfig(query_parameters=params or [])
    return [dict(r) for r in get_bq_client().query(sql, job_config=cfg).result()]


RAMOS_VALIDOS = (
    "registros_medicos",
    "registros_vehiculares",
    "registros_vida_e_inversion",
    "registros_ramos_generales",
)
PANELES_VALIDOS = (
    "cargos",
    "reportes",
    "renovaciones",
    "nuevos_negocios",
    "estadisticas",
    "administracion",
)
ALL_PANELES_STR = ",".join(PANELES_VALIDOS)


def _parse_csv_or_list(raw: Any, valid_values: tuple[str, ...]) -> List[str]:
    if isinstance(raw, list):
        return [str(v).strip() for v in raw if str(v).strip() in valid_values]
    if isinstance(raw, str):
        return [v.strip() for v in raw.split(",") if v.strip() in valid_values]
    return []


def _derive_legacy_permiso(selected_paneles: List[str]) -> str:
    if "administracion" in selected_paneles:
        return "supervisor"
    if "cargos" in selected_paneles:
        return "actualizador"
    return "visualizador"


def _legacy_paneles_for_permiso(permiso: str) -> str:
    p = (permiso or "").strip().lower()
    if p in ("administrador", "admin", "superadmin", "supervisor"):
        return ALL_PANELES_STR
    if p == "actualizador":
        return "cargos,reportes,renovaciones,nuevos_negocios"
    return "reportes,renovaciones,nuevos_negocios,estadisticas"


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
    """Login único tanto para el superadmin (admin/wasm) como para los clientes."""
    login = str(payload.get("usuario", "")).strip()
    password = str(payload.get("password", ""))
    if not login or not password:
        raise HTTPException(status_code=400, detail="Ingresa tu usuario y contraseña.")

    # 1. Caso especial: Superadmin / Creador de la plataforma
    if (login.lower() in ("admin", "wasm", "admin@wesbyte.app")) and (password in ("wasm", "admin")):
        now = datetime.datetime.now(datetime.timezone.utc)
        exp = now + datetime.timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
        token = jwt.encode(
            {
                "sub": "admin",
                "uid": 1,
                "cid": 0,
                "rol": "superadmin",
                "nombre": "Administrador General",
                "empresa": "Wesbyte Global",
                "exp": exp,
            },
            SECRET_KEY,
            algorithm=ALGORITHM,
        )
        resp = JSONResponse({"success": True, "redirect_url": "/mi-portal"})
        resp.set_cookie(
            COOKIE_NAME, token, httponly=True, samesite="lax",
            max_age=ACCESS_TOKEN_EXPIRE_MINUTES * 60,
        )
        return resp

    # 2. Clientes administradores de los tenants (buscando primero en usuarios_app)
    def _lookup_login_candidates() -> List[Dict[str, Any]]:
        cands = _rows(
            f"""
            SELECT ua.id, ua.cliente_id, ua.username, ua.nombre_completo AS nombre, ua.email,
                   ua.password_hash, ua.permiso AS rol,
                   IFNULL(ua.debe_cambiar_password, FALSE) AS debe_cambiar_password,
                   c.estado, c.nombre AS empresa, c.dataset_id,
                   c.plan_id, IFNULL(c.es_demo, FALSE) AS es_demo, c.fecha_fin_demo, c.created_at AS cli_created_at
            FROM `{P}.{W}.usuarios_app` ua
            JOIN `{P}.{W}.clientes` c ON c.id = ua.cliente_id
            WHERE ua.activo = TRUE
              AND (LOWER(ua.email) = LOWER(@l) OR LOWER(ua.username) = LOWER(@l))
            """,
            [bigquery.ScalarQueryParameter("l", "STRING", login)],
        )
        if not cands:
            cands = _rows(
                f"""
                SELECT u.id, u.cliente_id, u.username, u.nombre, u.email, u.password_hash, u.rol,
                       IFNULL(u.debe_cambiar_password, FALSE) AS debe_cambiar_password,
                       c.estado, c.nombre AS empresa, c.dataset_id,
                       c.plan_id, IFNULL(c.es_demo, FALSE) AS es_demo, c.fecha_fin_demo, c.created_at AS cli_created_at
                FROM `{P}.{W}.usuarios` u
                JOIN `{P}.{W}.clientes` c ON c.id = u.cliente_id
                WHERE u.activo = TRUE
                  AND (LOWER(u.email) = LOWER(@l) OR LOWER(u.username) = LOWER(@l))
                """,
                [bigquery.ScalarQueryParameter("l", "STRING", login)],
            )
        return cands

    candidatos = await asyncio.to_thread(_lookup_login_candidates)

    validos = [c for c in candidatos if verify_password(password, str(c.get("password_hash") or ""))]
    if not validos:
        raise HTTPException(status_code=401, detail="Correo o contraseña incorrectos.")

    u = validos[0]
    rol_usuario = str(u.get("rol", "")).lower().strip()

    if u.get("estado") == "cancelado":
        raise HTTPException(
            status_code=403,
            detail="Tu cuenta ha sido cancelada (por solicitud o por superar 2 meses sin pago). Contacta a soporte para reactivar tu plan.",
        )
    if u.get("estado") == "pendiente_pago":
        if rol_usuario not in ("admin", "administrador", "superadmin"):
            raise HTTPException(
                status_code=403,
                detail="Tu cuenta aún tiene pendiente el pago del plan. Solicita al administrador de tu empresa completar el pago.",
            )
    elif u.get("estado") not in ("activo", "demo"):
        raise HTTPException(
            status_code=403,
            detail="Tu cuenta aún no tiene un pago confirmado. Completa el pago para activar tu portal.",
        )

    debe_cambiar = bool(u.get("debe_cambiar_password", False))
    if debe_cambiar:
        now = datetime.datetime.now(datetime.timezone.utc)
        exp = now + datetime.timedelta(minutes=60)
        token = jwt.encode(
            {
                "sub": u["username"],
                "uid": int(u["id"]),
                "cid": int(u["cliente_id"]),
                "rol": rol_usuario,
                "nombre": u["nombre"],
                "empresa": u["empresa"],
                "email": u.get("email") or "",
                "dataset_id": u.get("dataset_id") or "",
                "must_change_pw": True,
                "exp": exp,
            },
            SECRET_KEY,
            algorithm=ALGORITHM,
        )
        resp = JSONResponse({
            "success": True,
            "require_password_change": True,
            "redirect_url": "/actualizar-password",
        })
        resp.set_cookie(COOKIE_NAME, token, httponly=True, samesite="lax", max_age=3600)
        return resp

    # REGLA ESTRICTA: El portal administrativo y de facturación es EXCLUSIVO para el titular o administrador.
    # Los colaboradores (supervisores, actualizadores, visualizadores) solo deben acceder al aplicativo operativo web.
    if rol_usuario not in ("admin", "administrador", "superadmin"):
        raise HTTPException(
            status_code=403,
            detail=f"Acceso restringido: Este portal administrativo es exclusivo para el titular o administrador de la empresa. Para utilizar la plataforma operativa, por favor ingresa en {WESBYTE_APP_URL}",
        )

    # Verificar si es cuenta Demo expirada para enviarla directo a pagar (sin queries extra si no es demo)
    redirect_target = "/mi-portal"
    demo_expirada_login = False
    if bool(u.get("es_demo")) or str(u.get("estado") or "").lower() == "demo":
        try:
            cid_chk = int(u["cliente_id"])
            sus_chk = await asyncio.to_thread(
                lambda: (_rows(
                    f"SELECT estado, fecha_fin FROM `{P}.{W}.suscripciones` WHERE cliente_id=@cid ORDER BY id DESC LIMIT 1",
                    [bigquery.ScalarQueryParameter("cid", "INT64", cid_chk)],
                ) or [None])[0]
            )
            cli_chk = {
                "estado": u.get("estado"),
                "es_demo": u.get("es_demo"),
                "fecha_fin_demo": u.get("fecha_fin_demo"),
                "created_at": u.get("cli_created_at"),
                "plan_id": u.get("plan_id"),
            }
            demo_st = _compute_demo_status(cli_chk, None, sus_chk)
            if demo_st["es_demo"] and demo_st["demo_expirada"]:
                demo_expirada_login = True
                redirect_target = "/mi-portal?demo_expirada=1"
        except Exception:
            pass

    # Nota: No se incrementa login_count ni ultimo_login en el portal administrativo,
    # ya que estas métricas registran exclusivamente el uso del aplicativo operativo web.
    now = datetime.datetime.now(datetime.timezone.utc)
    exp = now + datetime.timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    token = jwt.encode(
        {"sub": u["username"], "uid": int(u["id"]), "cid": int(u["cliente_id"]),
         "rol": "admin", "nombre": u["nombre"], "empresa": u["empresa"], "exp": exp},
        SECRET_KEY, algorithm=ALGORITHM,
    )
    resp = JSONResponse({
        "success": True,
        "demo_expirada": demo_expirada_login,
        "redirect_url": redirect_target,
    })
    resp.set_cookie(COOKIE_NAME, token, httponly=True, samesite="lax",
                    max_age=ACCESS_TOKEN_EXPIRE_MINUTES * 60)
    return resp


@router.get("/salir")
async def salir():
    resp = RedirectResponse("/", status_code=302)
    resp.delete_cookie(COOKIE_NAME)
    return resp


@router.get("/actualizar-password", response_class=HTMLResponse)
async def actualizar_password_page(request: Request):
    sesion = get_sesion(request)
    if not sesion:
        return RedirectResponse("/ingresar", status_code=302)
    return templates.TemplateResponse(
        request=request,
        name="actualizar_password.html",
        context={
            "sesion": sesion,
            "soporte_email": SOPORTE_EMAIL,
            "wesbyte_app_url": WESBYTE_APP_URL,
            "app_version": APP_VERSION,
        },
    )


@router.post("/api/actualizar-password")
async def api_actualizar_password(request: Request, payload: Dict[str, Any] = Body(...)):
    sesion = get_sesion(request)
    if not sesion:
        raise HTTPException(status_code=401, detail="Sesión expirada. Inicia sesión nuevamente.")

    nueva_pw = str(payload.get("nueva_password", "")).strip()
    confirmar_pw = str(payload.get("confirmar_password", "")).strip()
    if len(nueva_pw) < 6:
        raise HTTPException(status_code=400, detail="La nueva contraseña debe tener al menos 6 caracteres.")
    if nueva_pw != confirmar_pw:
        raise HTTPException(status_code=400, detail="Las contraseñas no coinciden.")

    uid = int(sesion.get("uid") or 0)
    cid = int(sesion.get("cid") or 0)
    username = str(sesion.get("sub") or "").strip()
    email = str(sesion.get("email") or "").strip()
    rol = str(sesion.get("rol") or "").lower().strip()
    ds = str(sesion.get("dataset_id") or "").strip()

    pw_hash = hash_password(nueva_pw)

    try:
        execute_query(f"""
            UPDATE `{P}.{W}.usuarios_app`
            SET password_hash = '{pw_hash}',
                debe_cambiar_password = FALSE,
                updated_at = CURRENT_TIMESTAMP()
            WHERE id = {uid} AND cliente_id = {cid}
        """)
    except Exception as exc:
        logger.warning(f"Error actualizando password en usuarios_app: {exc}")

    try:
        clean_user = username.replace("'", "''")
        clean_email = email.replace("'", "''")
        where_u = f"LOWER(TRIM(username)) = LOWER(TRIM('{clean_user}'))"
        if clean_email:
            where_u = f"({where_u} OR LOWER(TRIM(email)) = LOWER(TRIM('{clean_email}')))"
        execute_query(f"""
            UPDATE `{P}.{W}.usuarios`
            SET password_hash = '{pw_hash}',
                debe_cambiar_password = FALSE
            WHERE cliente_id = {cid} AND {where_u}
        """)
    except Exception as exc:
        logger.warning(f"Error actualizando password en usuarios: {exc}")

    if ds:
        try:
            clean_user = username.replace("'", "''")
            execute_query(f"""
                UPDATE `{P}.{ds}.maestra_usuarios_app`
                SET password = '{pw_hash}',
                    fecha_modificacion = CURRENT_TIMESTAMP()
                WHERE LOWER(TRIM(username)) = LOWER(TRIM('{clean_user}'))
            """)
        except Exception:
            pass

    if rol in ("admin", "administrador", "superadmin"):
        now = datetime.datetime.now(datetime.timezone.utc)
        exp = now + datetime.timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
        token = jwt.encode(
            {
                "sub": username,
                "uid": uid,
                "cid": cid,
                "rol": "admin",
                "nombre": sesion.get("nombre", ""),
                "empresa": sesion.get("empresa", ""),
                "exp": exp,
            },
            SECRET_KEY,
            algorithm=ALGORITHM,
        )
        resp = JSONResponse({
            "success": True,
            "message": "Contraseña actualizada correctamente.",
            "redirect_url": "/mi-portal",
        })
        resp.set_cookie(COOKIE_NAME, token, httponly=True, samesite="lax", max_age=ACCESS_TOKEN_EXPIRE_MINUTES * 60)
        return resp

    resp = JSONResponse({
        "success": True,
        "message": f"Contraseña actualizada correctamente. Ya puedes ingresar a la plataforma operativa en {WESBYTE_APP_URL}.",
        "redirect_url": WESBYTE_APP_URL,
    })
    resp.delete_cookie(COOKIE_NAME)
    return resp


@router.post("/api/olvide-password")
async def api_olvide_password(payload: Dict[str, Any] = Body(...)):
    email = str(payload.get("email", "")).strip().lower()
    if not email or "@" not in email:
        raise HTTPException(status_code=400, detail="Ingresa un correo electrónico válido.")

    candidatos = _rows(
        f"""
        SELECT ua.id, ua.cliente_id, ua.username, ua.nombre_completo AS nombre, ua.email,
               c.nombre AS empresa, c.dataset_id
        FROM `{P}.{W}.usuarios_app` ua
        JOIN `{P}.{W}.clientes` c ON c.id = ua.cliente_id
        WHERE ua.activo = TRUE AND LOWER(TRIM(ua.email)) = LOWER(@e)
        LIMIT 1
        """,
        [bigquery.ScalarQueryParameter("e", "STRING", email)],
    )
    tabla_origen = "usuarios_app"
    if not candidatos:
        candidatos = _rows(
            f"""
            SELECT u.id, u.cliente_id, u.username, u.nombre, u.email,
                   c.nombre AS empresa, c.dataset_id
            FROM `{P}.{W}.usuarios` u
            JOIN `{P}.{W}.clientes` c ON c.id = u.cliente_id
            WHERE u.activo = TRUE AND LOWER(TRIM(u.email)) = LOWER(@e)
            LIMIT 1
            """,
            [bigquery.ScalarQueryParameter("e", "STRING", email)],
        )
        tabla_origen = "usuarios"

    if not candidatos:
        return {
            "success": True,
            "message": "Si tu correo está registrado y activo, recibirás una contraseña temporal en tu bandeja de entrada.",
        }

    u = candidatos[0]
    temp_pw = _generar_password_temporal()
    pw_hash = hash_password(temp_pw)
    uid = int(u["id"])
    cid = int(u["cliente_id"])
    username = str(u.get("username") or "").replace("'", "''")
    ds = str(u.get("dataset_id") or "").strip()

    try:
        if tabla_origen == "usuarios_app":
            execute_query(f"""
                UPDATE `{P}.{W}.usuarios_app`
                SET password_hash = '{pw_hash}',
                    debe_cambiar_password = TRUE,
                    updated_at = CURRENT_TIMESTAMP()
                WHERE id = {uid} AND cliente_id = {cid}
            """)
        execute_query(f"""
            UPDATE `{P}.{W}.usuarios`
            SET password_hash = '{pw_hash}',
                debe_cambiar_password = TRUE
            WHERE cliente_id = {cid}
              AND (LOWER(TRIM(email)) = LOWER('{email.replace("'", "''")}') OR LOWER(TRIM(username)) = LOWER('{username}'))
        """)
    except Exception as exc:
        logger.warning(f"Error actualizando contraseña temporal en BigQuery: {exc}")

    if ds and username:
        try:
            execute_query(f"""
                UPDATE `{P}.{ds}.maestra_usuarios_app`
                SET password = '{pw_hash}',
                    fecha_modificacion = CURRENT_TIMESTAMP()
                WHERE LOWER(TRIM(username)) = LOWER('{username}')
            """)
        except Exception:
            pass

    enviado, err_msg = _enviar_correo_credenciales(
        to_email=email,
        nombre=str(u.get("nombre") or email),
        temp_password=temp_pw,
        empresa=str(u.get("empresa") or "Wesbyte"),
        es_reset=True,
    )
    if not enviado:
        raise HTTPException(
            status_code=500,
            detail=f"No se pudo enviar el correo de recuperación: {err_msg}",
        )

    return {
        "success": True,
        "message": f"Se ha enviado una contraseña temporal a {email}. Al ingresar con ella, el sistema te pedirá actualizarla.",
    }


def _get_planes_map_cached() -> Dict[int, Dict[str, Any]]:
    """Retorna el catálogo de planes indexado por ID con caché TTL de 10 minutos."""
    global _PLANES_MAP_CACHE
    now = time.time()
    with _CACHE_LOCK:
        if _PLANES_MAP_CACHE and (now - _PLANES_MAP_CACHE[0]) < _PLANES_TTL_SEC:
            return _PLANES_MAP_CACHE[1]
    rows = _rows(f"SELECT * FROM `{P}.{W}.planes`")
    pmap = {int(r["id"]): r for r in rows if r.get("id") is not None}
    with _CACHE_LOCK:
        _PLANES_MAP_CACHE = (now, pmap)
    return pmap


def _get_dataset_stats_cached(ds: str) -> tuple[int, Dict[str, int], int]:
    """Obtiene conteo de filas y tamaño en bytes del dataset del cliente en paralelo, con caché TTL de 5 minutos."""
    if not ds:
        return 0, {}, 0
    now = time.time()
    with _CACHE_LOCK:
        cached = _DATASET_STATS_CACHE.get(ds)
        if cached and (now - cached[0]) < _DATASET_STATS_TTL_SEC:
            st = cached[1]
            return st["total_clientes"], dict(st["registros"]), st["ds_bytes"]

    table_names = (
        "maestra_clientes",
        "registros_medicos",
        "registros_vehiculares",
        "registros_vida_e_inversion",
        "registros_ramos_generales",
    )
    client = get_bq_client()

    def _inspect_table(name: str) -> tuple[str, int, int]:
        try:
            tbl = client.get_table(f"{P}.{ds}.{name}")
            return name, int(tbl.num_rows or 0), int(tbl.num_bytes or 0)
        except Exception:
            return name, 0, 0

    registros: Dict[str, int] = {}
    total_clientes = 0
    ds_bytes = 0
    with ThreadPoolExecutor(max_workers=5) as pool:
        for name, n_rows, n_bytes in pool.map(_inspect_table, table_names):
            ds_bytes += n_bytes
            if name == "maestra_clientes":
                total_clientes = n_rows
            else:
                registros[name] = n_rows

    with _CACHE_LOCK:
        _DATASET_STATS_CACHE[ds] = (
            now,
            {"total_clientes": total_clientes, "registros": dict(registros), "ds_bytes": ds_bytes},
        )
    return total_clientes, registros, ds_bytes


def _superadmin_dashboard() -> Dict[str, Any]:
    """Recopila la información global de TODOS los clientes, consumos, usuarios, logs y tamaños de BD en paralelo."""
    client = get_bq_client()
    periodo = datetime.date.today().strftime("%Y-%m")

    def _q_clientes():
        return _rows(f"""
            SELECT 
                c.id, c.nombre, c.email, c.celular, c.dataset_id, c.estado, c.created_at,
                c.max_usuarios, c.limite_mensual_whatsapp, c.limite_mensual_emails, c.limite_mensual_api_ecuador,
                IFNULL(p.nombre, 'Sin Plan') AS plan_nombre,
                s.modalidad_pago, s.monto_total_cobrado, s.fecha_inicio, s.fecha_fin, s.estado AS sus_estado
            FROM `{P}.{W}.clientes` c
            LEFT JOIN `{P}.{W}.planes` p ON c.plan_id = p.id
            LEFT JOIN `{P}.{W}.suscripciones` s ON c.id = s.cliente_id
            ORDER BY c.id DESC
        """)

    def _q_consumos():
        return _rows(f"""
            SELECT cliente_id, total_emails, total_whatsapp, total_api_ecuador,
                   bloqueado_mensual_email, bloqueado_mensual_whatsapp, bloqueado_mensual_api
            FROM `{P}.{W}.consumos_mensuales`
            WHERE periodo = '{periodo}'
        """)

    def _q_usuarios():
        try:
            u_raw = _rows(f"""
                SELECT u.id, u.cliente_id, u.username, u.nombre_completo AS nombre, u.email, u.permiso AS rol,
                       u.permisos_seguros, IFNULL(u.permisos_paneles, '') AS permisos_paneles, u.activo, u.ultimo_login,
                       IFNULL(u.login_count, 0) AS login_count,
                       IFNULL(c.nombre, 'Wesbyte Global') AS empresa
                FROM `{P}.{W}.usuarios_app` u
                LEFT JOIN `{P}.{W}.clientes` c ON u.cliente_id = c.id
                ORDER BY u.ultimo_login DESC NULLS LAST
            """)
            if u_raw:
                return u_raw
        except Exception:
            pass
        return _rows(f"""
            SELECT u.id, u.cliente_id, u.username, u.nombre, u.email, u.rol, u.activo, u.ultimo_login,
                   IFNULL(u.login_count, 0) AS login_count,
                   IFNULL(c.nombre, 'Wesbyte Global') AS empresa
            FROM `{P}.{W}.usuarios` u
            LEFT JOIN `{P}.{W}.clientes` c ON u.cliente_id = c.id
            ORDER BY u.ultimo_login DESC NULLS LAST
        """)

    def _q_logs_wa():
        return _rows(f"""
            SELECT l.cliente_id, c.nombre AS empresa, l.telefono_destino, l.estado, l.error_detalle, l.fecha_envio AS created_at
            FROM `{P}.{W}.logs_whatsapp` l
            LEFT JOIN `{P}.{W}.clientes` c ON l.cliente_id = c.id
            ORDER BY l.fecha_envio DESC
            LIMIT 20
        """)

    def _q_logs_em():
        return _rows(f"""
            SELECT l.cliente_id, c.nombre AS empresa, l.destinatario, l.motivo, l.estado, l.error_detalle, l.created_at
            FROM `{P}.{W}.logs_emails` l
            LEFT JOIN `{P}.{W}.clientes` c ON l.cliente_id = c.id
            ORDER BY l.created_at DESC
            LIMIT 20
        """)

    def _q_logs_api():
        return _rows(f"""
            SELECT l.cliente_id, c.nombre AS empresa, l.endpoint, l.metodo, l.status_code, l.tiempo_respuesta_ms, l.bloqueado_por_limite, l.created_at
            FROM `{P}.{W}.logs_api_ecuador` l
            LEFT JOIN `{P}.{W}.clientes` c ON l.cliente_id = c.id
            ORDER BY l.created_at DESC
            LIMIT 20
        """)

    def _q_planes():
        try:
            return _rows(f"""
                SELECT id, codigo, nombre, subtitulo, tipo_periodo, max_usuarios,
                       limite_mensual_emails, limite_mensual_whatsapp, limite_mensual_api_ecuador,
                       CAST(IFNULL(precio_mensual, 0) AS FLOAT64) AS precio_mensual,
                       CAST(IFNULL(precio_anual, 0) AS FLOAT64) AS precio_anual,
                       CAST(IFNULL(descuento_anual_pct, 0) AS FLOAT64) AS descuento_anual_pct,
                       CAST(IFNULL(precio_anual_con_descuento, 0) AS FLOAT64) AS precio_anual_con_descuento,
                       es_destacado, es_personalizado, activo
                FROM `{P}.{W}.planes`
                ORDER BY id ASC
            """)
        except Exception:
            return []

    def _q_datasets():
        return {d.dataset_id: d for d in client.list_datasets()}

    with ThreadPoolExecutor(max_workers=8) as pool:
        f_cli = pool.submit(_q_clientes)
        f_con = pool.submit(_q_consumos)
        f_usr = pool.submit(_q_usuarios)
        f_wa = pool.submit(_q_logs_wa)
        f_em = pool.submit(_q_logs_em)
        f_api = pool.submit(_q_logs_api)
        f_pl = pool.submit(_q_planes)
        f_ds = pool.submit(_q_datasets)

        clientes_raw = f_cli.result()
        consumos_raw = f_con.result()
        usuarios_raw = f_usr.result()
        logs_wa = f_wa.result()
        logs_em = f_em.result()
        logs_api = f_api.result()
        planes_raw = f_pl.result()
        all_datasets = f_ds.result()

    consumos_map = {r["cliente_id"]: r for r in consumos_raw}

    def _calc_ds_size(ds_id: Optional[str]) -> tuple[int, int]:
        if not ds_id or ds_id not in all_datasets:
            return 0, 0
        ds_b = 0
        cnt = 0
        try:
            for t in client.list_tables(ds_id):
                tbl = client.get_table(f"{P}.{ds_id}.{t.table_id}")
                if tbl.num_bytes:
                    ds_b += tbl.num_bytes
                cnt += 1
        except Exception:
            pass
        return ds_b, cnt

    with ThreadPoolExecutor(max_workers=8) as pool:
        ds_sizes = list(pool.map(_calc_ds_size, [c.get("dataset_id") for c in clientes_raw]))

    total_bytes_global = 0
    for c, (ds_bytes, ds_tables_count) in zip(clientes_raw, ds_sizes):
        total_bytes_global += ds_bytes
        c["bytes"] = ds_bytes
        c["gb"] = round(ds_bytes / (1024**3), 4)
        c["mb"] = round(ds_bytes / (1024**2), 2)
        c["tables_count"] = ds_tables_count
        c["consumo"] = consumos_map.get(c["id"], {
            "total_emails": 0, "total_whatsapp": 0, "total_api_ecuador": 0
        })

    # Totales globales
    total_wa = sum(r.get("total_whatsapp", 0) for r in consumos_raw)
    total_em = sum(r.get("total_emails", 0) for r in consumos_raw)
    total_api = sum(r.get("total_api_ecuador", 0) for r in consumos_raw)
    total_logins = sum(int(u.get("login_count", 0)) for u in usuarios_raw)

    return {
        "is_superadmin": True,
        "periodo": periodo,
        "clientes": clientes_raw,
        "usuarios": usuarios_raw,
        "planes": planes_raw,
        "logs_whatsapp": logs_wa,
        "logs_emails": logs_em,
        "logs_api": logs_api,
        "totales": {
            "clientes_activos": len([c for c in clientes_raw if c.get("estado") == "activo"]),
            "total_clientes": len(clientes_raw),
            "total_usuarios": len(usuarios_raw),
            "total_gb": round(total_bytes_global / (1024**3), 4),
            "total_mb": round(total_bytes_global / (1024**2), 2),
            "total_whatsapp": total_wa,
            "total_emails": total_em,
            "total_api": total_api,
            "total_logins": total_logins,
        }
    }


def _compute_demo_status(
    cliente: Dict[str, Any],
    plan: Optional[Dict[str, Any]] = None,
    sus: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Calcula el estado del Plan Demo (5 días) usando la zona horaria America/Guayaquil."""
    now_ec = datetime.datetime.now(ECUADOR_TZ)
    today_ec = now_ec.date()

    plan_cod = str((plan or {}).get("codigo") or "").strip().upper()
    estado_cli = str(cliente.get("estado") or "").strip().lower()
    raw_es_demo = bool(cliente.get("es_demo")) or plan_cod == "DEMO" or estado_cli == "demo"

    # Si tiene una suscripción paga activa vigente, ya no está en demo
    tiene_sus_paga = False
    if sus:
        sus_estado = str(sus.get("estado") or "").lower()
        sus_fin = sus.get("fecha_fin")
        if sus_estado in ("activa", "por_vencer") and sus_fin and sus_fin >= today_ec:
            tiene_sus_paga = True

    es_demo = raw_es_demo and not tiene_sus_paga
    if not es_demo:
        return {
            "es_demo": False,
            "demo_expirada": False,
            "fecha_fin_demo": None,
            "dias_restantes": None,
            "limite_clientes": None,
        }

    raw_fin = cliente.get("fecha_fin_demo")
    if isinstance(raw_fin, datetime.datetime):
        fin_ec = raw_fin.replace(tzinfo=datetime.timezone.utc).astimezone(ECUADOR_TZ) if raw_fin.tzinfo is None else raw_fin.astimezone(ECUADOR_TZ)
    else:
        created = cliente.get("created_at")
        if isinstance(created, datetime.datetime):
            c_ec = created.replace(tzinfo=datetime.timezone.utc).astimezone(ECUADOR_TZ) if created.tzinfo is None else created.astimezone(ECUADOR_TZ)
            fin_ec = c_ec + datetime.timedelta(days=5)
        else:
            fin_ec = now_ec + datetime.timedelta(days=5)

    demo_expirada = now_ec > fin_ec
    dias_restantes = 0 if demo_expirada else max(0, (fin_ec.date() - today_ec).days)

    return {
        "es_demo": True,
        "demo_expirada": demo_expirada,
        "fecha_fin_demo": fin_ec.isoformat(),
        "dias_restantes": dias_restantes,
        "limite_clientes": int(DEMO_PLAN_CONFIG["limite_clientes"]),
    }


def _assert_demo_not_expired_by_cid(cid: int) -> Dict[str, Any]:
    cp = [bigquery.ScalarQueryParameter("cid", "INT64", cid)]
    cli = _rows(f"SELECT * FROM `{P}.{W}.clientes` WHERE id=@cid LIMIT 1", cp)
    if not cli:
        raise HTTPException(status_code=404, detail="Empresa no encontrada.")
    cliente = cli[0]
    plan = (_rows(f"SELECT * FROM `{P}.{W}.planes` WHERE id=@plan",
                  [bigquery.ScalarQueryParameter("plan", "INT64", int(cliente.get("plan_id") or 0))]) or [{}])[0]
    sus = (_rows(f"SELECT * FROM `{P}.{W}.suscripciones` WHERE cliente_id=@cid ORDER BY id DESC LIMIT 1", cp) or [None])[0]
    demo = _compute_demo_status(cliente, plan, sus)
    if demo["es_demo"] and demo["demo_expirada"]:
        raise HTTPException(
            status_code=403,
            detail="Tu plan Demo de 5 días ha expirado. Activa un plan para continuar realizando cambios.",
        )
    return {"cliente": cliente, "plan": plan, "sus": sus, "demo": demo}


def _dashboard(cid: int) -> Optional[Dict[str, Any]]:
    now = time.time()
    with _CACHE_LOCK:
        cached = _DASHBOARD_CACHE.get(int(cid))
        if cached and (now - cached[0]) < _DASHBOARD_TTL_SEC:
            return cached[1]

    cp = [bigquery.ScalarQueryParameter("cid", "INT64", cid)]
    periodo = datetime.date.today().strftime("%Y-%m")

    def _q_cliente():
        return _rows(f"SELECT * FROM `{P}.{W}.clientes` WHERE id=@cid LIMIT 1", cp)

    def _q_sus():
        return (_rows(f"SELECT * FROM `{P}.{W}.suscripciones` WHERE cliente_id=@cid ORDER BY id DESC LIMIT 1", cp) or [None])[0]

    def _q_pagos():
        return _rows(f"SELECT * FROM `{P}.{W}.pagos_payphone` WHERE cliente_id=@cid ORDER BY id DESC LIMIT 10", cp)

    def _q_usuarios():
        return _rows(f"SELECT email, username, nombre, rol, activo, ultimo_login, IFNULL(login_count, 0) AS login_count FROM `{P}.{W}.usuarios` WHERE cliente_id=@cid ORDER BY id", cp)

    def _q_usuarios_app():
        try:
            return _rows(f"""
                SELECT id, cliente_id, username, nombre_completo, email, celular, permiso, dataset_id, activo,
                       permisos_seguros, IFNULL(permisos_paneles, '') AS permisos_paneles,
                       ultimo_login, IFNULL(login_count, 0) AS login_count, created_at
                FROM `{P}.{W}.usuarios_app`
                WHERE cliente_id=@cid
                ORDER BY id ASC
            """, cp)
        except Exception:
            return []

    def _q_consumos():
        return (_rows(
            f"SELECT * FROM `{P}.{W}.consumos_mensuales` WHERE cliente_id=@cid AND periodo=@p ORDER BY id DESC LIMIT 1",
            cp + [bigquery.ScalarQueryParameter("p", "STRING", periodo)],
        ) or [{}])[0]

    def _q_logs_wa():
        return _rows(f"SELECT telefono_destino, estado, error_detalle, fecha_envio AS created_at FROM `{P}.{W}.logs_whatsapp` WHERE cliente_id=@cid ORDER BY fecha_envio DESC LIMIT 10", cp)

    def _q_logs_em():
        return _rows(f"SELECT destinatario, motivo, estado, error_detalle, created_at FROM `{P}.{W}.logs_emails` WHERE cliente_id=@cid ORDER BY created_at DESC LIMIT 10", cp)

    def _q_logs_api():
        return _rows(f"SELECT endpoint, metodo, status_code, tiempo_respuesta_ms, created_at FROM `{P}.{W}.logs_api_ecuador` WHERE cliente_id=@cid ORDER BY created_at DESC LIMIT 10", cp)

    with ThreadPoolExecutor(max_workers=10) as pool:
        f_cli = pool.submit(_q_cliente)
        f_planes = pool.submit(_get_planes_map_cached)
        f_sus = pool.submit(_q_sus)
        f_pagos = pool.submit(_q_pagos)
        f_usuarios = pool.submit(_q_usuarios)
        f_usuarios_app = pool.submit(_q_usuarios_app)
        f_cons = pool.submit(_q_consumos)
        f_logs_wa = pool.submit(_q_logs_wa)
        f_logs_em = pool.submit(_q_logs_em)
        f_logs_api = pool.submit(_q_logs_api)

        cli = f_cli.result()
        if not cli:
            return None
        cliente = cli[0]
        planes_map = f_planes.result()
        plan = planes_map.get(int(cliente.get("plan_id") or 0), {})
        sus = f_sus.result()
        pagos = f_pagos.result()
        usuarios = f_usuarios.result()
        usuarios_app_raw = f_usuarios_app.result()
        cons = f_cons.result()
        logs_wa = f_logs_wa.result()
        logs_em = f_logs_em.result()
        logs_api = f_logs_api.result()

    # Procesar usuarios_app
    seen_users = set()
    usuarios_app = []
    for u_row in usuarios_app_raw:
        key = (u_row.get("email") or u_row.get("username") or "").strip().lower()
        if key and key in seen_users:
            continue
        if key:
            seen_users.add(key)
        if not str(u_row.get("permisos_paneles") or "").strip():
            u_row["permisos_paneles"] = _legacy_paneles_for_permiso(str(u_row.get("permiso") or ""))
        usuarios_app.append(u_row)

    if not usuarios_app:
        usuarios_app = [
            {
                "id": u.get("id", 1),
                "cliente_id": cid,
                "username": u.get("username", "admin"),
                "nombre_completo": u.get("nombre", "Administrador"),
                "email": u.get("email", ""),
                "celular": "",
                "permiso": "administrador",
                "dataset_id": cliente.get("dataset_id"),
                "activo": bool(u.get("activo", True)),
                "permisos_seguros": "registros_medicos,registros_vehiculares,registros_vida_e_inversion,registros_ramos_generales",
                "permisos_paneles": ALL_PANELES_STR,
                "ultimo_login": u.get("ultimo_login"),
                "login_count": int(u.get("login_count", 0)),
                "created_at": None,
            }
            for u in usuarios
        ]

    usuarios_activos_count = sum(1 for u in usuarios_app if u.get("activo"))
    max_usr = int(cliente.get("max_usuarios") or plan.get("max_usuarios") or 1)
    es_plan_prime = (max_usr <= 1)
    limite_alcanzado = (usuarios_activos_count >= max_usr)
    puede_crear_usuarios = (not es_plan_prime) and (not limite_alcanzado)

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

    dias_restantes = None
    if sus and sus.get("fecha_fin"):
        dias_restantes = (sus["fecha_fin"] - datetime.datetime.now(ECUADOR_TZ).date()).days

    demo_info = _compute_demo_status(cliente, plan, sus)
    if demo_info["es_demo"]:
        dias_restantes = demo_info["dias_restantes"]
        max_usr = 1
        es_plan_prime = True
        puede_crear_usuarios = False
        if not plan:
            plan = DEMO_PLAN_CONFIG.copy()

    from app.services.email_template_designer_service import (
        get_ai_quota_for_plan,
        get_ai_usage_this_month,
    )
    ia_habilitada, ia_cuota_total = get_ai_quota_for_plan(
        int(cliente.get("plan_id") or 0),
        str(plan.get("codigo") or ""),
        bool(demo_info["es_demo"]),
    )

    # Ejecutar en paralelo: stats del dataset, plantillas/notificaciones y uso IA
    ds = cliente.get("dataset_id") or ""
    with ThreadPoolExecutor(max_workers=3) as pool:
        f_stats = pool.submit(_get_dataset_stats_cached, ds)
        f_notif = pool.submit(
            get_cliente_notificaciones_y_plantillas,
            cid,
            str(cliente.get("plan_id") or ""),
            str(plan.get("codigo") or ""),
        )
        f_ia = pool.submit(get_ai_usage_this_month, cid) if ia_habilitada else None

        total_clientes, registros, ds_bytes = f_stats.result()
        notif_detalle_data = f_notif.result()
        ia_usadas_mes = f_ia.result() if f_ia else 0

    ia_disponibles_mes = max(0, ia_cuota_total - ia_usadas_mes)

    # Reutilizar legacy_flags de notif_detalle_data sin repetir query
    notif_cfg = {
        "correo_cumpleanios_activo": True,
        "correo_renovaciones_activo": True,
        "correo_cobros_activo": True,
        "whatsapp_cumpleanios_activo": False,
        "whatsapp_renovaciones_activo": True,
        "whatsapp_cobros_activo": True,
    }
    legacy_flags = notif_detalle_data.get("legacy_flags") or {}
    if legacy_flags:
        notif_cfg = {k: bool(legacy_flags.get(k, notif_cfg[k])) for k in notif_cfg.keys()}

    cliente["logo"] = normalize_drive_logo_url(cliente.get("logo"))

    result = {
        "is_superadmin": False,
        "cliente": cliente, "plan": plan, "sus": sus, "pagos": pagos, "usuarios": usuarios,
        "notificaciones_config": notif_cfg,
        "notificaciones_detalle": notif_detalle_data["notificaciones"],
        "plantilla_variables": notif_detalle_data["variables"],
        "ultima_plantilla": notif_detalle_data.get("ultima_plantilla"),
        "ia_email_config": {
            "habilitada": ia_habilitada,
            "cuota_total": ia_cuota_total,
            "usadas_mes": ia_usadas_mes,
            "disponibles_mes": ia_disponibles_mes,
        },
        "usuarios_app": usuarios_app,
        "usuarios_activos_count": usuarios_activos_count,
        "max_usuarios": max_usr,
        "es_plan_prime": es_plan_prime,
        "limite_alcanzado": limite_alcanzado,
        "puede_crear_usuarios": puede_crear_usuarios,
        "consumos": consumos, "registros": registros, "total_clientes": total_clientes,
        "clientes_usados": total_clientes,
        "limite_clientes": demo_info["limite_clientes"],
        "es_demo": demo_info["es_demo"],
        "demo_expirada": demo_info["demo_expirada"],
        "fecha_fin_demo": demo_info["fecha_fin_demo"],
        "total_registros": sum(registros.values()), "dias_restantes": dias_restantes,
        "periodo": periodo,
        "ds_bytes": ds_bytes,
        "ds_gb": round(ds_bytes / (1024**3), 4),
        "ds_mb": round(ds_bytes / (1024**2), 2),
        "logs_whatsapp": logs_wa,
        "logs_emails": logs_em,
        "logs_api": logs_api,
        "planes_catalogo": PLANES_OFICIALES,
    }
    with _CACHE_LOCK:
        _DASHBOARD_CACHE[int(cid)] = (time.time(), result)
    return result


def auditar_vencimientos_suscripciones() -> None:
    """
    Regla de negocio automática (con throttling de 6 horas):
    Si un cliente tiene más de 60 días (2 meses) sin pagar respecto a su fecha de vencimiento:
    Se cancela automáticamente la suscripción y se desactiva al cliente.
    """
    global _LAST_AUDIT_TS
    now = time.time()
    with _AUDIT_LOCK:
        if (now - _LAST_AUDIT_TS) < _AUDIT_INTERVAL_SEC:
            return
        _LAST_AUDIT_TS = now

    try:
        client = get_bq_client()
        # 1. Cancelar suscripciones con más de 60 días (2 meses) de impago
        client.query(f"""
            UPDATE `{P}.{W}.suscripciones`
            SET estado = 'cancelada',
                renovacion_automatica = FALSE,
                desea_renovar = FALSE,
                motivo_no_renovacion = 'Cancelación automática por impago superior a 60 días (2 meses)',
                updated_at = CURRENT_TIMESTAMP()
            WHERE estado IN ('activa', 'por_vencer', 'vencida')
              AND DATE_DIFF(CURRENT_DATE(), IFNULL(fecha_proximo_cobro, fecha_fin), DAY) >= 60
        """).result()

        # 2. Desactivar y marcar clientes como cancelados
        client.query(f"""
            UPDATE `{P}.{W}.clientes` c
            SET c.estado = 'cancelado',
                c.activo = FALSE
            WHERE c.id IN (
                SELECT cliente_id FROM `{P}.{W}.suscripciones`
                WHERE estado = 'cancelada'
                  AND motivo_no_renovacion = 'Cancelación automática por impago superior a 60 días (2 meses)'
            )
            AND c.estado != 'cancelado'
        """).result()
    except Exception as exc:
        logger.debug(f"Aviso auditando vencimientos de suscripciones: {exc}")


@router.get("/mi-portal", response_class=HTMLResponse)
async def mi_portal(request: Request):
    # Ejecutar auditoría de vencimientos en segundo plano (sin bloquear el TTFB)
    if (time.time() - _LAST_AUDIT_TS) >= _AUDIT_INTERVAL_SEC:
        asyncio.create_task(asyncio.to_thread(auditar_vencimientos_suscripciones))

    sesion = get_sesion(request)
    if not sesion:
        return RedirectResponse("/ingresar", status_code=302)
    if sesion.get("must_change_pw"):
        return RedirectResponse("/actualizar-password", status_code=302)

    # Si es superadmin
    is_sa = (sesion.get("rol") == "superadmin") or (sesion.get("sub") in ("admin", "wasm") and int(sesion.get("cid") or sesion.get("cliente_id") or 0) == 0)
    if is_sa:
        try:
            data = await asyncio.to_thread(_superadmin_dashboard)
            return templates.TemplateResponse(
                request=request, name="mi_portal.html",
                context={"sesion": sesion, "d": data, "is_superadmin": True,
                         "soporte_email": SOPORTE_EMAIL, "wesbyte_app_url": WESBYTE_APP_URL,
                         "app_version": APP_VERSION},
            )
        except Exception:
            logger.exception("Error cargando superadmin dashboard")
            raise HTTPException(status_code=500, detail="Error cargando panel de control superadmin.")

    # Si es cliente normal
    if str(sesion.get("rol", "")).lower().strip() not in ("admin", "administrador"):
        return RedirectResponse("/salir", status_code=302)

    try:
        cid = int(sesion.get("cid") or sesion.get("cliente_id") or 0)
        data = await asyncio.to_thread(_dashboard, cid)
    except Exception:
        logger.exception("Error cargando dashboard")
        raise HTTPException(status_code=500, detail="No pudimos cargar tu portal. Intenta de nuevo.")
    if not data or data["cliente"].get("estado") not in ("activo", "pendiente_pago", "demo"):
        return RedirectResponse("/salir", status_code=302)
    demo_bloqueada = bool(data.get("es_demo") and data.get("demo_expirada"))
    if demo_bloqueada or data["cliente"].get("estado") == "pendiente_pago":
        return RedirectResponse(f"/checkout/{cid}", status_code=302)
    pago_pendiente = (data["cliente"].get("estado") == "pendiente_pago") or demo_bloqueada
    return templates.TemplateResponse(
        request=request, name="mi_portal.html",
        context={"sesion": sesion, "d": data, "is_superadmin": False,
                 "pago_pendiente": pago_pendiente,
                 "demo_bloqueada": demo_bloqueada,
                 "soporte_email": SOPORTE_EMAIL, "wesbyte_app_url": WESBYTE_APP_URL,
                 "app_version": APP_VERSION},
    )


@router.post("/api/suscripcion/cancelar")
async def api_cancelar_suscripcion(request: Request, payload: Dict[str, Any] = Body(...)):
    """Los planes anuales/multianuales de pago único corren hasta su fecha de vencimiento sin cancelación anticipada."""
    sesion = get_sesion(request)
    if not sesion or not sesion.get("cid"):
        raise HTTPException(status_code=401, detail="No autorizado.")
    raise HTTPException(
        status_code=400,
        detail="Tu suscripción es de pago único por el período contratado y no requiere ni admite cancelación anticipada. Tu servicio seguirá activo hasta la fecha de vencimiento.",
    )


def _calcular_meses_prorrateo(f_ini: Optional[datetime.date], f_fin: Optional[datetime.date], today: datetime.date) -> Dict[str, int]:
    """
    Calcula meses totales del contrato, meses usados y meses no usados.
    Regla: si ya pasó el primer día del mes de ciclo actual, ese mes se cobra completo
    en el plan anterior y la diferencia se cobra por los meses no usados restantes.
    """
    if f_ini and f_fin and f_fin > f_ini:
        meses_totales = max(1, (f_fin.year - f_ini.year) * 12 + (f_fin.month - f_ini.month))
        if f_fin.day > f_ini.day and (f_fin.day - f_ini.day) >= 15:
            meses_totales += 1
        meses_transcurridos = max(0, (today.year - f_ini.year) * 12 + (today.month - f_ini.month))
        # Si ya pasó el primer día del mes de servicio (today.day > f_ini.day), se cobra completo ese mes
        if today >= f_ini:
            meses_usados = meses_transcurridos + (1 if today.day > f_ini.day else 0)
        else:
            meses_usados = 0
        meses_usados = max(0, min(meses_totales - 1, meses_usados))
        meses_no_usados = max(1, meses_totales - meses_usados)
        dias_totales = max(1, (f_fin - f_ini).days)
        dias_restantes = max(1, min(dias_totales, (f_fin - today).days))
    else:
        meses_totales = 12
        meses_usados = 0
        meses_no_usados = 12
        dias_totales = 365
        dias_restantes = 365
    return {
        "meses_totales": meses_totales,
        "meses_usados": meses_usados,
        "meses_no_usados": meses_no_usados,
        "dias_totales": dias_totales,
        "dias_restantes": dias_restantes,
    }


def _calcular_upgrade_diferencia(
    cid: int,
    cli: Dict[str, Any],
    tipo: str,
    nuevo_plan_codigo: str = "",
    usuarios_adicionales: int = 0,
    emails_adicionales: int = 0,
    whatsapp_adicionales: int = 0,
) -> Dict[str, Any]:
    """Calcula el crédito por meses no usados del plan actual y la diferencia neta a pagar por upgrade o recursos."""
    cp = [bigquery.ScalarQueryParameter("cid", "INT64", cid)]
    sus_rows = _rows(
        f"""
        SELECT id, plan_id, modalidad_pago, monto_total_cobrado, fecha_inicio, fecha_fin, estado
        FROM `{P}.{W}.suscripciones`
        WHERE cliente_id=@cid AND estado IN ('activa', 'por_vencer')
        ORDER BY id DESC LIMIT 1
        """,
        cp,
    )
    sus = sus_rows[0] if sus_rows else {}

    plan_actual_id = int(cli.get("plan_id") or sus.get("plan_id") or 0)
    plan_actual = next((p for p in PLANES_OFICIALES if p["id"] == plan_actual_id), DEMO_PLAN_CONFIG)
    es_demo_cli = bool(cli.get("es_demo")) or plan_actual_id == 0

    today = datetime.datetime.now(ECUADOR_TZ).date()
    f_ini = sus.get("fecha_inicio")
    f_fin = sus.get("fecha_fin")
    if isinstance(f_ini, datetime.datetime):
        f_ini = f_ini.date()
    if isinstance(f_fin, datetime.datetime):
        f_fin = f_fin.date()

    prorrateo = _calcular_meses_prorrateo(f_ini, f_fin, today)
    meses_totales = prorrateo["meses_totales"]
    meses_usados = prorrateo["meses_usados"]
    meses_no_usados = prorrateo["meses_no_usados"]
    dias_totales = prorrateo["dias_totales"]
    dias_restantes = prorrateo["dias_restantes"]

    nota_prorrateo = (
        f"Si ya pasó el primer día del mes en curso, ese mes se cobra completo en tu plan actual "
        f"({meses_usados} {'mes usado' if meses_usados == 1 else 'meses usados'}) y la diferencia se calcula "
        f"sobre los {meses_no_usados} {'mes no usado' if meses_no_usados == 1 else 'meses no usados'} restantes."
    )

    monto_pagado_actual = 0.0 if es_demo_cli else float(sus.get("monto_total_cobrado") or plan_actual.get("precio_anual_con_descuento") or 0)
    valor_mensual_actual = 0.0 if es_demo_cli else (monto_pagado_actual / meses_totales)
    credito_actual = 0.0 if es_demo_cli else round(valor_mensual_actual * meses_no_usados, 2)

    if tipo == "cambio_plan":
        codigo = str(nuevo_plan_codigo or "").strip().upper()
        nuevo_plan = next((p for p in PLANES_OFICIALES if p["codigo"] == codigo), None)
        if not nuevo_plan:
            raise HTTPException(status_code=400, detail="Plan no válido.")
        if not es_demo_cli and nuevo_plan["id"] <= plan_actual_id:
            raise HTTPException(status_code=400, detail="Solo puedes subir a un plan superior al actual.")

        if es_demo_cli:
            costo_nuevo_periodo = round(float(nuevo_plan["precio_anual_con_descuento"]), 2)
            diferencia = costo_nuevo_periodo
            meses_no_usados = 12
            meses_usados = 0
            meses_totales = 12
            dias_restantes = 365
            nota_prorrateo = "Activación de plan anual completo (12 meses)."
        else:
            valor_mensual_nuevo = float(nuevo_plan["precio_anual_con_descuento"]) / 12.0
            costo_nuevo_periodo = round(valor_mensual_nuevo * meses_no_usados, 2)
            diferencia = round(costo_nuevo_periodo - credito_actual, 2)
            if diferencia < 1.00:
                diferencia = 1.00

        base_imponible = diferencia
        iva_monto = round(base_imponible * (IVA_PORCENTAJE / 100.0), 2)
        total_con_iva = round(base_imponible + iva_monto, 2)
        msi_meses = 6 if total_con_iva > 250 else 3
        return {
            "tipo": "cambio_plan",
            "modalidad_pago": "upgrade_plan",
            "plan_id": int(nuevo_plan["id"]),
            "plan_actual_nombre": plan_actual["nombre"],
            "nuevo_plan_codigo": nuevo_plan["codigo"],
            "nuevo_plan_nombre": nuevo_plan["nombre"],
            "meses_totales": meses_totales,
            "meses_usados": meses_usados,
            "meses_no_usados": meses_no_usados,
            "dias_totales": dias_totales,
            "dias_restantes": dias_restantes,
            "monto_pagado_actual": round(monto_pagado_actual, 2),
            "credito_actual": credito_actual,
            "costo_nuevo_periodo": costo_nuevo_periodo,
            "base_imponible": base_imponible,
            "iva_pct": IVA_PORCENTAJE,
            "iva": iva_monto,
            "diferencia_usd": total_con_iva,
            "msi_meses": msi_meses,
            "cuota_msi": round(total_con_iva / msi_meses, 2),
            "nota_prorrateo": nota_prorrateo,
            "descripcion": f"Upgrade a {nuevo_plan['nombre']} ({meses_no_usados} meses restantes)",
        }

    elif tipo in ("agregar_usuarios", "agregar_recursos"):
        u_extra = max(0, int(usuarios_adicionales or 0))
        e_extra = max(0, int(emails_adicionales or 0))
        w_extra = max(0, int(whatsapp_adicionales or 0))
        if tipo == "agregar_usuarios" and u_extra == 0 and e_extra == 0 and w_extra == 0:
            u_extra = 1
        if u_extra == 0 and e_extra == 0 and w_extra == 0:
            raise HTTPException(status_code=400, detail="Selecciona al menos un usuario, correo o mensaje de WhatsApp adicional.")

        # Tarifa escalonada por volumen para proteger la escalera de planes y mejorar percepción en paquetes altos
        if e_extra >= 5000:
            tarifa_email = 0.004
        elif e_extra >= 2500:
            tarifa_email = 0.005
        else:
            tarifa_email = float(TARIFAS_CALCULADORA["costo_email_unitario"])

        if w_extra >= 2500:
            tarifa_wa = 0.018
        elif w_extra >= 1000:
            tarifa_wa = 0.020
        else:
            tarifa_wa = float(TARIFAS_CALCULADORA["costo_whatsapp_unitario"])

        costo_u_mes = round(u_extra * float(TARIFAS_CALCULADORA["costo_usuario_extra_mensual"]), 2)
        costo_e_mes = round(e_extra * tarifa_email, 2)
        costo_w_mes = round(w_extra * tarifa_wa, 2)
        costo_mensual_total = round(costo_u_mes + costo_e_mes + costo_w_mes, 2)

        costo_nuevo_periodo = round(costo_mensual_total * meses_no_usados, 2)
        base_imponible = max(1.00, costo_nuevo_periodo)
        iva_monto = round(base_imponible * (IVA_PORCENTAJE / 100.0), 2)
        total_con_iva = round(base_imponible + iva_monto, 2)
        msi_meses = 6 if total_con_iva > 250 else 3

        partes_desc = []
        if u_extra > 0:
            partes_desc.append(f"+{u_extra} usr")
        if e_extra > 0:
            partes_desc.append(f"+{e_extra} correos/mes")
        if w_extra > 0:
            partes_desc.append(f"+{w_extra} WA/mes")
        desc_str = f"{', '.join(partes_desc)} ({meses_no_usados} meses restantes)"

        return {
            "tipo": "agregar_recursos",
            "modalidad_pago": "mejora_recursos" if (e_extra > 0 or w_extra > 0) else "mejora_usuarios",
            "plan_id": plan_actual_id,
            "plan_actual_nombre": plan_actual["nombre"],
            "usuarios_adicionales": u_extra,
            "emails_adicionales": e_extra,
            "whatsapp_adicionales": w_extra,
            "costo_usuarios_mes": costo_u_mes,
            "costo_emails_mes": costo_e_mes,
            "costo_whatsapp_mes": costo_w_mes,
            "costo_mensual_adicional": costo_mensual_total,
            "meses_totales": meses_totales,
            "meses_usados": meses_usados,
            "meses_no_usados": meses_no_usados,
            "dias_totales": dias_totales,
            "dias_restantes": dias_restantes,
            "monto_pagado_actual": round(monto_pagado_actual, 2),
            "credito_actual": 0.0,
            "costo_nuevo_periodo": costo_nuevo_periodo,
            "base_imponible": base_imponible,
            "iva_pct": IVA_PORCENTAJE,
            "iva": iva_monto,
            "diferencia_usd": total_con_iva,
            "msi_meses": msi_meses,
            "cuota_msi": round(total_con_iva / msi_meses, 2),
            "nota_prorrateo": nota_prorrateo,
            "descripcion": desc_str,
        }

    raise HTTPException(status_code=400, detail="Tipo de mejora no soportado.")


@router.post("/api/suscripcion/upgrade/preview")
async def api_upgrade_preview(request: Request, payload: Dict[str, Any] = Body(...)):
    """Calcula el desglose prorrateado para subir de plan o agregar usuarios."""
    sesion = get_sesion(request)
    if not sesion or not sesion.get("cid"):
        raise HTTPException(status_code=401, detail="No autorizado.")

    cid = int(sesion["cid"])
    cli_rows = _rows(f"SELECT * FROM `{P}.{W}.clientes` WHERE id=@cid LIMIT 1",
                     [bigquery.ScalarQueryParameter("cid", "INT64", cid)])
    if not cli_rows:
        raise HTTPException(status_code=404, detail="Cliente no encontrado.")

    resumen = _calcular_upgrade_diferencia(
        cid=cid,
        cli=cli_rows[0],
        tipo=str(payload.get("tipo", "cambio_plan")).strip(),
        nuevo_plan_codigo=str(payload.get("nuevo_plan_codigo", "")),
        usuarios_adicionales=int(payload.get("usuarios_adicionales", 0) or 0),
        emails_adicionales=int(payload.get("emails_adicionales", 0) or 0),
        whatsapp_adicionales=int(payload.get("whatsapp_adicionales", 0) or 0),
    )
    return {"success": True, "resumen": resumen}


@router.post("/api/suscripcion/upgrade/intent")
async def api_upgrade_intent(request: Request, payload: Dict[str, Any] = Body(...)):
    """Genera una intención de pago real en PayPhone para pagar la diferencia del upgrade."""
    sesion = get_sesion(request)
    if not sesion or not sesion.get("cid"):
        raise HTTPException(status_code=401, detail="No autorizado.")

    cid = int(sesion["cid"])
    cli_rows = _rows(f"SELECT * FROM `{P}.{W}.clientes` WHERE id=@cid LIMIT 1",
                     [bigquery.ScalarQueryParameter("cid", "INT64", cid)])
    if not cli_rows:
        raise HTTPException(status_code=404, detail="Cliente no encontrado.")
    cli = cli_rows[0]

    resumen = _calcular_upgrade_diferencia(
        cid=cid,
        cli=cli,
        tipo=str(payload.get("tipo", "cambio_plan")).strip(),
        nuevo_plan_codigo=str(payload.get("nuevo_plan_codigo", "")),
        usuarios_adicionales=int(payload.get("usuarios_adicionales", 0) or 0),
        emails_adicionales=int(payload.get("emails_adicionales", 0) or 0),
        whatsapp_adicionales=int(payload.get("whatsapp_adicionales", 0) or 0),
    )

    client_tx_id = generate_client_tx_id(cid, "UPG")
    registrar_intento_pago_payphone(
        cliente_id=cid,
        plan_id=resumen["plan_id"],
        modalidad_pago=resumen["modalidad_pago"],
        client_transaction_id=client_tx_id,
        monto_usd=resumen["diferencia_usd"],
        metadata={
            "tipo": resumen["tipo"],
            "nuevo_plan_codigo": resumen.get("nuevo_plan_codigo", ""),
            "usuarios_adicionales": resumen.get("usuarios_adicionales", 0),
            "emails_adicionales": resumen.get("emails_adicionales", 0),
            "whatsapp_adicionales": resumen.get("whatsapp_adicionales", 0),
            "credito_actual": resumen["credito_actual"],
            "costo_nuevo_periodo": resumen["costo_nuevo_periodo"],
            "diferencia_usd": resumen["diferencia_usd"],
            "meses_no_usados": resumen.get("meses_no_usados", 0),
            "dias_restantes": resumen["dias_restantes"],
        },
    )

    box_config = build_payphone_box_config(
        client_tx_id=client_tx_id,
        monto_usd=resumen["diferencia_usd"],
        descripcion=resumen["descripcion"],
        email=str(cli.get("email") or sesion.get("sub") or "cliente@wesbyte.app"),
        celular=str(cli.get("celular") or "0999999999"),
        documento=str(cli.get("ruc_cedula") or "9999999999"),
        modalidad_pago=resumen["modalidad_pago"],
        base_imponible_usd=resumen.get("base_imponible"),
    )
    return {"success": True, "resumen": resumen, "box_config": box_config}


@router.post("/api/suscripcion/mejorar")
async def api_mejorar_suscripcion(request: Request, payload: Dict[str, Any] = Body(...)):
    """Redirige al flujo obligatorio de pago real con PayPhone."""
    raise HTTPException(
        status_code=400,
        detail="Las mejoras de plan o usuarios deben pagarse a través de PayPhone (/api/suscripcion/upgrade/intent).",
    )


@router.post("/api/usuarios-app/crear")
async def api_crear_usuario_app(request: Request, payload: Dict[str, Any] = Body(...)):
    """Permite al cliente crear colaboradores para su empresa según su plan y asignar ramos de seguros."""
    sesion = get_sesion(request)
    if not sesion:
        raise HTTPException(status_code=401, detail="Sesión expirada. Inicia sesión nuevamente.")

    cliente_id = int(sesion.get("cid") or sesion.get("cliente_id") or 0)
    if cliente_id <= 0:
        raise HTTPException(status_code=403, detail="Solo los clientes autenticados pueden crear usuarios para su empresa.")

    # 1. Obtener datos del cliente y su plan (validando Demo)
    demo_ctx = _assert_demo_not_expired_by_cid(cliente_id)
    cli_row = demo_ctx["cliente"]
    if cli_row.get("estado") not in ("activo", "demo"):
        raise HTTPException(status_code=403, detail="Tu cuenta se encuentra inactiva.")

    max_usr = 1 if demo_ctx["demo"]["es_demo"] else int(cli_row.get("max_usuarios") or 1)
    dataset_id = str(cli_row.get("dataset_id")).strip()

    # Validación estricta según el plan del usuario
    if demo_ctx["demo"]["es_demo"]:
        df_count = _rows(f"SELECT COUNT(1) AS c FROM `{P}.{W}.usuarios_app` WHERE cliente_id=@cid AND activo=TRUE", [bigquery.ScalarQueryParameter("cid", "INT64", cliente_id)])
        df_count_leg = _rows(f"SELECT COUNT(1) AS c FROM `{P}.{W}.usuarios` WHERE cliente_id=@cid AND activo=TRUE", [bigquery.ScalarQueryParameter("cid", "INT64", cliente_id)])
        actuales_demo = max(
            int(df_count[0]["c"]) if df_count else 0,
            int(df_count_leg[0]["c"]) if df_count_leg else 0,
        )
        if actuales_demo >= 1:
            raise HTTPException(status_code=403, detail="El plan Demo solo permite 1 usuario")

    if max_usr <= 1:
        raise HTTPException(
            status_code=403 if demo_ctx["demo"]["es_demo"] else 400,
            detail="El plan Demo solo permite 1 usuario" if demo_ctx["demo"]["es_demo"] else "Tu Plan Prime solo incluye 1 usuario administrador. Para añadir supervisores, actualizadores o visualizadores para tu equipo, debes mejorar tu plan a Medium o Super Gold, o contratar usuarios adicionales."
        )

    # Contar usuarios activos en usuarios_app
    cp = [bigquery.ScalarQueryParameter("cid", "INT64", cliente_id)]
    df_count = _rows(f"SELECT COUNT(1) AS c FROM `{P}.{W}.usuarios_app` WHERE cliente_id=@cid AND activo=TRUE", cp)
    actuales = int(df_count[0]["c"]) if df_count else 0
    if actuales >= max_usr:
        raise HTTPException(
            status_code=400,
            detail=f"Has completado el límite de {max_usr} usuarios autorizados en tu plan. Si necesitas sumar más colaboradores, contrata usuarios adicionales ($4/mes)."
        )

    # Validar campos requeridos (el usuario es el correo registrado y la contraseña se genera aleatoriamente)
    nombre_completo = str(payload.get("nombre_completo", "")).strip()
    email = str(payload.get("email", "")).strip().lower()
    raw_username = email
    permiso = str(payload.get("permiso", "")).strip().lower()
    permisos_seguros = payload.get("permisos_seguros")
    permisos_paneles = payload.get("permisos_paneles")

    if not nombre_completo:
        raise HTTPException(status_code=400, detail="El nombre completo es obligatorio.")
    if not email or "@" not in email:
        raise HTTPException(status_code=400, detail="El correo electrónico es obligatorio y será el usuario de ingreso.")
    if " " in raw_username or len(raw_username) < 3:
        raise HTTPException(status_code=400, detail="El nombre de usuario no debe contener espacios y debe tener al menos 3 caracteres.")

    selected_paneles = _parse_csv_or_list(permisos_paneles, PANELES_VALIDOS)
    if not selected_paneles:
        raise HTTPException(status_code=400, detail="Debes seleccionar al menos un panel al que tendrá acceso el colaborador.")
    permisos_paneles_str = ",".join(selected_paneles)

    roles_permitidos = ["supervisor", "actualizador", "visualizador"]
    if permiso not in roles_permitidos:
        permiso = _derive_legacy_permiso(selected_paneles)

    # Validar selector de ramos de seguros
    selected_ramos = _parse_csv_or_list(permisos_seguros, RAMOS_VALIDOS)
    if not selected_ramos:
        raise HTTPException(status_code=400, detail="Debes seleccionar al menos un tipo de seguro al que tendrá acceso el colaborador.")

    permisos_seguros_str = ",".join(selected_ramos)

    # Verificar que el correo o username no exista ya en usuarios_app
    up = [
        bigquery.ScalarQueryParameter("u", "STRING", raw_username),
        bigquery.ScalarQueryParameter("e", "STRING", email),
    ]
    exist_usr = _rows(
        f"SELECT username, email FROM `{P}.{W}.usuarios_app` WHERE LOWER(TRIM(username))=@u OR LOWER(TRIM(email))=@e",
        up,
    )
    if exist_usr:
        raise HTTPException(status_code=400, detail=f"Ya existe un usuario registrado con el correo '{email}' o usuario '{raw_username}'.")

    temp_password = _generar_password_temporal()
    now_ts = datetime.datetime.now(datetime.timezone.utc)
    pw_hash = hash_password(temp_password)
    new_id = get_next_id("usuarios_app", W)

    # 1. Guardar en wesbyte.administracion.usuarios_app
    nuevo_row = {
        "id": new_id,
        "cliente_id": cliente_id,
        "username": raw_username,
        "nombre_completo": nombre_completo,
        "email": email,
        "celular": "",
        "password_hash": pw_hash,
        "permiso": permiso,
        "dataset_id": dataset_id,
        "activo": True,
        "debe_cambiar_password": True,
        "permisos_seguros": permisos_seguros_str,
        "permisos_paneles": permisos_paneles_str,
        "ultimo_login": None,
        "login_count": 0,
        "created_at": now_ts,
        "updated_at": now_ts,
    }
    append_rows(W, "usuarios_app", [nuevo_row])

    # 2. Sincronizar en wesbyte.administracion.usuarios para compatibilidad
    try:
        u_id_legacy = get_next_id("usuarios", W)
        append_rows(W, "usuarios", [{
            "id": u_id_legacy,
            "cliente_id": cliente_id,
            "username": raw_username,
            "nombre": nombre_completo,
            "email": email,
            "celular": "",
            "password_hash": pw_hash,
            "rol": permiso,
            "dataset_id": dataset_id,
            "activo": True,
            "debe_cambiar_password": True,
            "ultimo_login": None,
            "login_count": 0,
            "created_at": now_ts,
        }])
    except Exception as exc:
        logger.warning(f"Error sincronizando con usuarios: {exc}")

    # 3. Sincronizar en target_dataset.maestra_usuarios_app si existe
    try:
        execute_query(f"ALTER TABLE `{P}.{dataset_id}.maestra_usuarios_app` ADD COLUMN IF NOT EXISTS permisos_paneles STRING")
        append_rows(dataset_id, "maestra_usuarios_app", [{
            "id": str(uuid.uuid4()),
            "username": raw_username,
            "password": pw_hash,
            "access": permiso,
            "is_hashed_password": True,
            "admin": False,
            "activo": True,
            "fecha_creacion": now_ts,
            "fecha_modificacion": now_ts,
            "usuario_creador": sesion.get("sub", "cliente"),
            "usuario_modificador": sesion.get("sub", "cliente"),
            "permisos_seguros": permisos_seguros_str,
            "permisos_paneles": permisos_paneles_str,
        }])
    except Exception as exc:
        logger.warning(f"No se pudo insertar en {dataset_id}.maestra_usuarios_app: {exc}")

    # 4. Enviar correo con contraseña temporal al usuario registrado
    enviado, err_envio = _enviar_correo_credenciales(
        to_email=email,
        nombre=nombre_completo,
        temp_password=temp_password,
        empresa=str(cli_row.get("nombre") or "Wesbyte"),
        es_reset=False,
    )
    msg_correo = (
        f"Se enviaron las credenciales temporales a {email}."
        if enviado
        else f"Usuario creado, pero no se pudo enviar el correo ({err_envio}). Contraseña temporal: {temp_password}"
    )

    invalidate_cliente_cache(cliente_id)
    return {
        "success": True,
        "email_sent": enviado,
        "message": f"Usuario '{email}' creado exitosamente con rol {permiso.capitalize()}. {msg_correo}",
        "usuario": {
            "id": new_id,
            "username": raw_username,
            "email": email,
            "nombre_completo": nombre_completo,
            "permiso": permiso,
            "permisos_seguros": permisos_seguros_str,
            "permisos_paneles": permisos_paneles_str,
        }
    }


@router.post("/api/usuarios-app/toggle-activo")
async def api_toggle_usuario_app(request: Request, payload: Dict[str, Any] = Body(...)):
    """Permite al cliente activar o desactivar un usuario de su equipo."""
    sesion = get_sesion(request)
    if not sesion:
        raise HTTPException(status_code=401, detail="Sesión expirada.")
    cliente_id = int(sesion.get("cid") or sesion.get("cliente_id") or 0)
    usuario_id = int(payload.get("usuario_id", 0))
    nuevo_estado = bool(payload.get("activo", False))

    cp = [
        bigquery.ScalarQueryParameter("uid", "INT64", usuario_id),
        bigquery.ScalarQueryParameter("cid", "INT64", cliente_id),
    ]
    df_u = _rows(f"SELECT id, username, permiso, dataset_id FROM `{P}.{W}.usuarios_app` WHERE id=@uid AND cliente_id=@cid", cp)
    if not df_u:
        raise HTTPException(status_code=404, detail="Usuario no encontrado.")

    u_row = df_u[0]
    if u_row["permiso"] == "administrador" and not nuevo_estado:
        raise HTTPException(status_code=400, detail="No puedes desactivar la cuenta del administrador principal.")

    username = str(u_row["username"])
    ds = str(u_row["dataset_id"])

    # Actualizar estado en usuarios_app
    execute_query(f"""
        UPDATE `{P}.{W}.usuarios_app`
        SET activo = {str(nuevo_estado).upper()}, updated_at = CURRENT_TIMESTAMP()
        WHERE id = {usuario_id} AND cliente_id = {cliente_id}
    """)
    # Actualizar en usuarios
    try:
        execute_query(f"""
            UPDATE `{P}.{W}.usuarios`
            SET activo = {str(nuevo_estado).upper()}
            WHERE LOWER(TRIM(username)) = LOWER(TRIM('{username}')) AND cliente_id = {cliente_id}
        """)
    except Exception:
        pass
    # Actualizar en maestra_usuarios_app
    try:
        execute_query(f"""
            UPDATE `{P}.{ds}.maestra_usuarios_app`
            SET activo = {str(nuevo_estado).upper()}, fecha_modificacion = CURRENT_TIMESTAMP()
            WHERE LOWER(TRIM(username)) = LOWER(TRIM('{username}'))
        """)
    except Exception:
        pass

    invalidate_cliente_cache(cliente_id)
    return {
        "success": True,
        "message": f"Usuario '{username}' {'activado' if nuevo_estado else 'desactivado'} correctamente."
    }


@router.post("/api/usuarios-app/editar")
async def api_editar_usuario_app(request: Request, payload: Dict[str, Any] = Body(...)):
    """Permite al cliente editar los permisos, rol, contraseña o ramos de seguros de un colaborador."""
    sesion = get_sesion(request)
    if not sesion:
        raise HTTPException(status_code=401, detail="Sesión expirada.")
    cliente_id = int(sesion.get("cid") or sesion.get("cliente_id") or 0)
    usuario_id = int(payload.get("usuario_id", 0))

    cp = [
        bigquery.ScalarQueryParameter("uid", "INT64", usuario_id),
        bigquery.ScalarQueryParameter("cid", "INT64", cliente_id),
    ]
    df_u = _rows(f"SELECT id, username, permiso, dataset_id FROM `{P}.{W}.usuarios_app` WHERE id=@uid AND cliente_id=@cid", cp)
    if not df_u:
        raise HTTPException(status_code=404, detail="Usuario no encontrado.")

    u_row = df_u[0]
    username = str(u_row["username"])
    ds = str(u_row["dataset_id"])
    es_admin = (u_row["permiso"] == "administrador")

    nombre_completo = str(payload.get("nombre_completo", "")).strip()
    email = str(payload.get("email", "")).strip().lower()
    password = str(payload.get("password", "")).strip()
    permiso = str(payload.get("permiso", "")).strip().lower()
    permisos_seguros = payload.get("permisos_seguros")
    permisos_paneles = payload.get("permisos_paneles")

    if not nombre_completo:
        raise HTTPException(status_code=400, detail="El nombre completo es obligatorio.")

    # Validar paneles y rol legacy compatible
    if es_admin:
        nuevo_permiso = "administrador"
        permisos_paneles_str = ALL_PANELES_STR
    else:
        selected_paneles = _parse_csv_or_list(permisos_paneles, PANELES_VALIDOS)
        if not selected_paneles:
            raise HTTPException(status_code=400, detail="Debes seleccionar al menos un panel al que tendrá acceso el colaborador.")
        permisos_paneles_str = ",".join(selected_paneles)
        roles_permitidos = ["supervisor", "actualizador", "visualizador"]
        nuevo_permiso = permiso if permiso in roles_permitidos else _derive_legacy_permiso(selected_paneles)

    # Validar selector de ramos
    selected_ramos = _parse_csv_or_list(permisos_seguros, RAMOS_VALIDOS)
    if not selected_ramos:
        raise HTTPException(status_code=400, detail="Debes seleccionar al menos un tipo de seguro al que tendrá acceso el colaborador.")

    permisos_str = ",".join(selected_ramos)

    # Actualizar campos en usuarios_app
    update_pw_clause = ""
    pw_hash = None
    if password:
        if len(password) < 6:
            raise HTTPException(status_code=400, detail="La nueva contraseña debe tener al menos 6 caracteres.")
        pw_hash = hash_password(password)
        update_pw_clause = f", password_hash = '{pw_hash}'"

    clean_nombre = nombre_completo.replace("'", "''")
    clean_email = email.replace("'", "''")

    execute_query(f"""
        UPDATE `{P}.{W}.usuarios_app`
        SET nombre_completo = '{clean_nombre}',
            email = '{clean_email}',
            permiso = '{nuevo_permiso}',
            permisos_seguros = '{permisos_str}',
            permisos_paneles = '{permisos_paneles_str}'
            {update_pw_clause},
            updated_at = CURRENT_TIMESTAMP()
        WHERE id = {usuario_id} AND cliente_id = {cliente_id}
    """)

    # Actualizar en usuarios
    try:
        pw_part = f", password_hash = '{pw_hash}'" if pw_hash else ""
        execute_query(f"""
            UPDATE `{P}.{W}.usuarios`
            SET nombre = '{clean_nombre}',
                email = '{clean_email}',
                rol = '{nuevo_permiso}'
                {pw_part}
            WHERE LOWER(TRIM(username)) = LOWER(TRIM('{username}')) AND cliente_id = {cliente_id}
        """)
    except Exception:
        pass

    # Actualizar en target dataset maestra_usuarios_app
    try:
        execute_query(f"ALTER TABLE `{P}.{ds}.maestra_usuarios_app` ADD COLUMN IF NOT EXISTS permisos_paneles STRING")
        pw_part_target = f", password = '{pw_hash}'" if pw_hash else ""
        execute_query(f"""
            UPDATE `{P}.{ds}.maestra_usuarios_app`
            SET access = '{nuevo_permiso}',
                permisos_seguros = '{permisos_str}',
                permisos_paneles = '{permisos_paneles_str}',
                fecha_modificacion = CURRENT_TIMESTAMP()
                {pw_part_target}
            WHERE LOWER(TRIM(username)) = LOWER(TRIM('{username}'))
        """)
    except Exception:
        pass

    invalidate_cliente_cache(cliente_id)
    return {
        "success": True,
        "message": f"Usuario '{username}' actualizado correctamente.",
        "usuario": {
            "id": usuario_id,
            "username": username,
            "nombre_completo": nombre_completo,
            "permiso": nuevo_permiso,
            "permisos_seguros": permisos_str,
            "permisos_paneles": permisos_paneles_str,
        }
    }


@router.post("/api/notificaciones/guardar")
async def api_guardar_notificaciones(request: Request, payload: Dict[str, Any] = Body(...)):
    """Guarda las preferencias de notificaciones (Email y WhatsApp) para el cliente en configuracion_planes_notificaciones."""
    sesion = get_sesion(request)
    if not sesion:
        raise HTTPException(status_code=401, detail="Sesión expirada.")
    cliente_id = int(sesion.get("cid") or sesion.get("cliente_id") or 0)
    if cliente_id <= 0:
        raise HTTPException(status_code=403, detail="Solo clientes autenticados pueden configurar notificaciones.")

    cp = [bigquery.ScalarQueryParameter("cid", "INT64", cliente_id)]
    cli_rows = _rows(
        f"""
        SELECT c.plan_id, IFNULL(p.codigo, CAST(c.plan_id AS STRING)) AS codigo_plan,
               IFNULL(p.nombre, 'Plan Personalizado') AS nombre_plan
        FROM `{P}.{W}.clientes` c
        LEFT JOIN `{P}.{W}.planes` p ON c.plan_id = p.id
        WHERE c.id = @cid
        LIMIT 1
        """,
        cp,
    )
    if not cli_rows:
        raise HTTPException(status_code=404, detail="Cliente no encontrado.")

    id_plan = str(cli_rows[0].get("codigo_plan") or cli_rows[0].get("plan_id") or "1")
    nombre_plan = str(cli_rows[0].get("nombre_plan") or "Plan Wesbyte")

    preferencias = payload.get("preferencias")
    if not isinstance(preferencias, dict):
        # Compatibilidad con formato anterior plano
        preferencias = {
            "cumpleanios": {
                "whatsapp_habilitado": bool(payload.get("whatsapp_cumpleanios_activo", False)),
                "email_habilitado": bool(payload.get("correo_cumpleanios_activo", True)),
            },
            "proxima_renovacion": {
                "whatsapp_habilitado": bool(payload.get("whatsapp_renovaciones_activo", True)),
                "email_habilitado": bool(payload.get("correo_renovaciones_activo", True)),
            },
        }

    save_cliente_notificaciones_habilitadas(cliente_id, id_plan, nombre_plan, preferencias)
    invalidate_cliente_cache(cliente_id)

    # Sincronizar toggles de WhatsApp con wesbyte_api
    try:
        await sync_client_whatsapp_from_admin(cliente_id)
    except Exception as exc:
        logger.warning(f"No se pudo sincronizar preferencias WhatsApp con wesbyte_api para cliente {cliente_id}: {exc}")

    return {
        "success": True,
        "message": "Preferencias de notificaciones guardadas correctamente.",
    }


def _resolve_whatsapp_target(sesion: Optional[Dict[str, Any]]) -> tuple[int, bool]:
    """Devuelve (cliente_id, es_superadmin) para la gestión de WhatsApp."""
    if not sesion:
        raise HTTPException(status_code=401, detail="Sesión expirada.")
    if _is_superadmin(sesion):
        return WHATSAPP_WESBYTE_SENDER_CLIENT_ID, True
    cliente_id = int(sesion.get("cid") or sesion.get("cliente_id") or 0)
    if cliente_id <= 0:
        raise HTTPException(status_code=403, detail="Solo clientes autenticados pueden gestionar WhatsApp.")
    return cliente_id, False


@router.get("/api/whatsapp/estado")
async def api_whatsapp_estado(request: Request):
    """Consulta y sincroniza el estado de conexión de WhatsApp del cliente o del emisor oficial (superadmin)."""
    cliente_id, es_superadmin = _resolve_whatsapp_target(get_sesion(request))

    try:
        wa = WhatsAppClient()
        if not es_superadmin:
            await sync_client_whatsapp_from_admin(cliente_id)
            estado = await wa.get_status(cliente_id)
        else:
            try:
                estado = await wa.get_status(cliente_id)
            except WhatsAppClientError as exc:
                if exc.status_code == 404:
                    await sync_client_whatsapp_from_admin(cliente_id)
                    estado = await wa.get_status(cliente_id)
                else:
                    raise
        return {
            "success": True,
            "requires_qr": True,
            **estado,
        }
    except WhatsAppClientError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail)
    except Exception as exc:
        logger.error(f"Error consultando estado WhatsApp en wesbyte_api para cliente {cliente_id}: {exc}")
        raise HTTPException(status_code=502, detail="No se pudo conectar con el servicio de WhatsApp.")


@router.post("/api/whatsapp/conectar")
async def api_whatsapp_conectar(request: Request):
    """Genera el código QR de vinculación de WhatsApp desde wesbyte_api para el cliente o superadmin."""
    cliente_id, es_superadmin = _resolve_whatsapp_target(get_sesion(request))

    try:
        wa = WhatsAppClient()
        provision: Dict[str, Any] = {}
        if not es_superadmin:
            provision = await sync_client_whatsapp_from_admin(cliente_id)
            res = await wa.connect_qr(cliente_id)
        else:
            try:
                res = await wa.connect_qr(cliente_id)
            except WhatsAppClientError as exc:
                if exc.status_code == 404:
                    provision = await sync_client_whatsapp_from_admin(cliente_id)
                    res = await wa.connect_qr(cliente_id)
                else:
                    raise
        qr = res.get("qr_base64") or ""
        if qr and not qr.startswith("data:image"):
            qr = f"data:image/png;base64,{qr}"
        return {
            "success": True,
            "requires_qr": True,
            "client_id": cliente_id,
            "plan_tier": provision.get("plan_tier") or "WESBYTE_OFICIAL",
            "instance_name": res.get("instance_name"),
            "qr_base64": qr,
            "connection_status": "qr_ready",
            "message": "Escanea el código QR desde WhatsApp > Dispositivos vinculados.",
        }
    except WhatsAppClientError as exc:
        if exc.status_code == 409:
            wa = WhatsAppClient()
            estado = await wa.get_status(cliente_id)
            phone = estado.get("phone_connected")
            tel_txt = f" (+{phone})" if phone else ""
            return {
                "success": True,
                "requires_qr": True,
                "already_connected": True,
                "connection_status": "connected",
                "phone_connected": phone,
                "instance_name": estado.get("instance_name"),
                "message": f"Tu WhatsApp ya se encuentra conectado{tel_txt}. Primero cierra la conexión actual en tu dispositivo (Dispositivos vinculados) si deseas generar otro código QR.",
            }
        raise HTTPException(status_code=exc.status_code, detail=exc.detail)
    except Exception as exc:
        logger.error(f"Error generando QR WhatsApp en wesbyte_api para cliente {cliente_id}: {exc}")
        raise HTTPException(status_code=502, detail="No se pudo generar el código QR con el servicio de WhatsApp.")


@router.get("/api/portal/plan-estado")
async def api_portal_plan_estado(request: Request):
    """Retorna el estado actual del plan (incluyendo modo DEMO, días restantes y clientes usados)."""
    sesion = get_sesion(request)
    if not sesion:
        raise HTTPException(status_code=401, detail="Sesión expirada.")
    cliente_id = int(sesion.get("cid") or sesion.get("cliente_id") or 0)
    if cliente_id <= 0:
        return {"es_demo": False, "demo_expirada": False, "dias_restantes": None, "clientes_usados": 0, "limite_clientes": None}
    dash = _dashboard(cliente_id)
    if not dash:
        raise HTTPException(status_code=404, detail="Empresa no encontrada.")
    return {
        "es_demo": dash["es_demo"],
        "demo_expirada": dash["demo_expirada"],
        "fecha_fin_demo": dash["fecha_fin_demo"],
        "dias_restantes": dash["dias_restantes"],
        "clientes_usados": dash["clientes_usados"],
        "limite_clientes": dash["limite_clientes"],
        "plan_nombre": dash["plan"].get("nombre") or "Plan Wesbyte",
    }


class TestNotificationRequest(BaseModel):
    notificacion_codigo: str = Field(..., min_length=1)
    canal: str = Field(..., min_length=1)
    destino: str = Field(..., min_length=1)
    asunto: Optional[str] = None
    mensaje: Optional[str] = None
    es_html: Optional[bool] = None


@router.post("/api/notificaciones/plantillas/probar")
async def api_probar_plantilla_notificacion(request: Request, payload: TestNotificationRequest):
    """Envía una notificación de prueba por correo (Resend) o WhatsApp (Evolution API) con datos ficticios."""
    sesion = get_sesion(request)
    if not sesion:
        raise HTTPException(status_code=401, detail="Sesión expirada.")
    cliente_id = int(sesion.get("cid") or sesion.get("cliente_id") or 0)
    if cliente_id <= 0:
        raise HTTPException(status_code=403, detail="Solo clientes autenticados pueden probar plantillas.")

    demo_ctx = _assert_demo_not_expired_by_cid(cliente_id)
    es_demo = bool(demo_ctx["demo"].get("es_demo"))
    notif_cod = str(payload.notificacion_codigo or "").strip()

    DEMO_MAX_PRUEBAS_POR_NOTIF = 3
    pruebas_usadas = 0
    if es_demo:
        try:
            cnt_rows = _rows(
                f"""
                SELECT COUNT(1) AS total
                FROM `{P}.{W}.logs_pruebas_notificaciones`
                WHERE client_id = @cid AND notificacion_codigo = @cod
                """,
                [
                    bigquery.ScalarQueryParameter("cid", "INT64", cliente_id),
                    bigquery.ScalarQueryParameter("cod", "STRING", notif_cod),
                ],
            )
            pruebas_usadas = int(cnt_rows[0]["total"]) if cnt_rows else 0
        except Exception:
            pruebas_usadas = 0

        if pruebas_usadas >= DEMO_MAX_PRUEBAS_POR_NOTIF:
            raise HTTPException(
                status_code=403,
                detail=(
                    f"Has alcanzado el límite de {DEMO_MAX_PRUEBAS_POR_NOTIF} pruebas para esta notificación en tu Plan Demo. "
                    "Activa un plan comercial para realizar envíos de prueba ilimitados."
                ),
            )

    try:
        result = await send_test_notification(
            cliente_id=cliente_id,
            notificacion_codigo=notif_cod,
            canal=payload.canal,
            destino=payload.destino,
            asunto=payload.asunto,
            mensaje=payload.mensaje,
            es_html=payload.es_html,
        )
        if es_demo:
            try:
                nid = get_next_id("logs_pruebas_notificaciones", W)
                append_rows(
                    W,
                    "logs_pruebas_notificaciones",
                    [
                        {
                            "id": nid,
                            "client_id": cliente_id,
                            "notificacion_codigo": notif_cod,
                            "canal": str(payload.canal or "").strip().lower(),
                            "destino": str(payload.destino or "").strip(),
                            "created_at": datetime.datetime.now(datetime.timezone.utc),
                        }
                    ],
                )
                pruebas_usadas += 1
            except Exception as exc:
                logger.warning(f"No se pudo registrar log de prueba Demo para cliente {cliente_id}: {exc}")
            result["pruebas_restantes_demo"] = max(0, DEMO_MAX_PRUEBAS_POR_NOTIF - pruebas_usadas)
            result["message"] = (
                f"{result.get('message', 'Prueba enviada')} "
                f"(Te quedan {result['pruebas_restantes_demo']} de {DEMO_MAX_PRUEBAS_POR_NOTIF} pruebas en Plan Demo)."
            )
        return result
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc))


@router.post("/api/notificaciones/plantillas/guardar")
async def api_guardar_plantilla_notificacion(request: Request, payload: Dict[str, Any] = Body(...)):
    """Guarda el formato personalizado de correo o WhatsApp de una notificación en plantillas_notificaciones."""
    sesion = get_sesion(request)
    if not sesion:
        raise HTTPException(status_code=401, detail="Sesión expirada.")
    cliente_id = int(sesion.get("cid") or sesion.get("cliente_id") or 0)
    if cliente_id <= 0:
        raise HTTPException(status_code=403, detail="Solo clientes autenticados pueden configurar plantillas.")
    _assert_demo_not_expired_by_cid(cliente_id)

    try:
        save_cliente_plantilla(
            client_id=cliente_id,
            notificacion_codigo=str(payload.get("notificacion_codigo") or ""),
            canal=str(payload.get("canal") or ""),
            asunto=payload.get("asunto"),
            mensaje=str(payload.get("mensaje") or ""),
            es_html=bool(payload.get("es_html", False)),
            config_json=payload.get("config_json"),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    invalidate_cliente_cache(cliente_id)
    return {
        "success": True,
        "message": "Plantilla guardada correctamente.",
    }


@router.post("/api/notificaciones/plantillas/render-preset")
async def api_render_preset_plantilla_email(request: Request, payload: Dict[str, Any] = Body(...)):
    """Genera el HTML email-safe de una de las 3 plantillas preestablecidas (clasica, moderna, minimalista)."""
    sesion = get_sesion(request)
    if not sesion:
        raise HTTPException(status_code=401, detail="Sesión expirada.")
    cliente_id = int(sesion.get("cid") or sesion.get("cliente_id") or 0)
    if cliente_id <= 0:
        raise HTTPException(status_code=403, detail="Solo clientes autenticados pueden diseñar plantillas.")

    from app.services.email_template_designer_service import render_preset_email_html

    try:
        html_out = render_preset_email_html(
            preset_id=str(payload.get("preset_id") or "clasica"),
            notificacion_codigo=str(payload.get("notificacion_codigo") or "envio_poliza"),
            primary_color=str(payload.get("primary_color") or "#1e3a8a"),
            logo_url=str(payload.get("logo_url") or ""),
            broker_nombre=str(payload.get("broker_nombre") or "{{broker}}"),
            titulo=payload.get("titulo"),
            saludo=payload.get("saludo"),
            intro=payload.get("intro"),
            cierre=payload.get("cierre"),
            mostrar_tabla_resumen=bool(payload.get("mostrar_tabla_resumen", True)),
        )
        return {"success": True, "html": html_out}
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.post("/api/notificaciones/plantillas/ia-generar")
async def api_generar_plantilla_email_ia(request: Request, payload: Dict[str, Any] = Body(...)):
    """
    Endpoint de Adaptación/Generación de plantillas de correo con IA (Gemini gemini-2.5-flash-lite):
    1. Valida que el plan del tenant sea Plan 2 (Medium) o Plan 3 (Super Gold).
    2. Valida que no haya excedido su cuota mensual de generaciones IA.
    3. Inyecta únicamente el diccionario de variables reales de la notificación solicitada.
    4. Retorna { html_final, variables_usadas, variables_descartadas, resumen_cambios } y cuota actualizada.
    """
    sesion = get_sesion(request)
    if not sesion:
        raise HTTPException(status_code=401, detail="Sesión expirada.")
    cliente_id = int(sesion.get("cid") or sesion.get("cliente_id") or 0)
    if cliente_id <= 0:
        raise HTTPException(status_code=403, detail="Solo clientes autenticados pueden usar el Editor con IA.")

    ctx = _assert_demo_not_expired_by_cid(cliente_id)
    cliente = ctx["cliente"]
    plan = ctx["plan"]
    demo = ctx["demo"]

    from app.services.email_template_designer_service import (
        generate_or_adapt_email_template_with_ai,
        get_ai_quota_for_plan,
        get_ai_usage_this_month,
        record_ai_usage,
    )

    ia_ok, cuota_total = get_ai_quota_for_plan(
        int(cliente.get("plan_id") or 0),
        str(plan.get("codigo") or ""),
        bool(demo.get("es_demo")),
    )
    if not ia_ok:
        raise HTTPException(
            status_code=403,
            detail="El Editor Inteligente con IA e Importación HTML está disponible en los planes Medium y Super Gold. Mejora tu plan para desbloquearlo.",
        )

    usadas = get_ai_usage_this_month(cliente_id)
    if usadas >= cuota_total:
        raise HTTPException(
            status_code=429,
            detail=f"Has alcanzado tu límite mensual de {cuota_total} generaciones con IA en tu plan actual.",
        )

    notificacion_codigo = str(payload.get("notificacion_codigo") or "").strip()
    instrucciones = str(payload.get("prompt_o_html") or "").strip()
    current_html = str(payload.get("current_html") or "").strip()
    primary_color = str(payload.get("primary_color") or "#1e3a8a").strip()
    logo_url = str(payload.get("logo_url") or cliente.get("logo") or "").strip()

    if not instrucciones:
        raise HTTPException(
            status_code=400,
            detail="Escribe tus instrucciones de diseño o pega el código HTML que deseas adaptar.",
        )

    try:
        resultado = await generate_or_adapt_email_template_with_ai(
            notificacion_codigo=notificacion_codigo,
            user_prompt_or_html=instrucciones,
            current_html=current_html,
            primary_color=primary_color,
            logo_url=logo_url,
            broker_nombre=str(cliente.get("nombre") or "Seguros Alianza"),
        )
        record_ai_usage(
            client_id=cliente_id,
            notificacion_codigo=notificacion_codigo,
            prompt_resumen=instrucciones[:250],
            modelo=resultado.get("modelo_usado", "gemini-2.5-flash-lite"),
        )
        nuevas_usadas = usadas + 1
        disponibles = max(0, cuota_total - nuevas_usadas)
        return {
            "success": True,
            **resultado,
            "cuota": {
                "cuota_total": cuota_total,
                "usadas_mes": nuevas_usadas,
                "disponibles_mes": disponibles,
            },
        }
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc))


@router.post("/api/superadmin/planes/guardar")
async def api_superadmin_guardar_plan(request: Request, payload: Dict[str, Any] = Body(...)):
    """Permite al superadmin (wasm/admin) crear o editar planes en wesbyte.administracion.planes."""
    sesion = get_sesion(request)
    if not _is_superadmin(sesion):
        raise HTTPException(status_code=403, detail="Acceso exclusivo para superadministrador.")

    plan_id = int(payload.get("id") or 0)
    codigo = str(payload.get("codigo", "")).strip().upper().replace(" ", "_")
    nombre = str(payload.get("nombre", "")).strip()
    subtitulo = str(payload.get("subtitulo", "")).strip()
    tipo_periodo = str(payload.get("tipo_periodo", "anual")).strip() or "anual"
    max_usuarios = max(1, int(payload.get("max_usuarios") or 1))
    limite_mensual_emails = max(0, int(payload.get("limite_mensual_emails") or 0))
    limite_mensual_whatsapp = max(0, int(payload.get("limite_mensual_whatsapp") or 0))
    limite_mensual_api_ecuador = max(0, int(payload.get("limite_mensual_api_ecuador") or 0))
    precio_mensual = float(payload.get("precio_mensual") or 0)
    precio_anual = float(payload.get("precio_anual") or 0)
    descuento_anual_pct = float(payload.get("descuento_anual_pct") or 0)
    precio_anual_con_descuento = float(payload.get("precio_anual_con_descuento") or 0)
    es_destacado = "TRUE" if payload.get("es_destacado") else "FALSE"
    es_personalizado = "TRUE" if payload.get("es_personalizado") else "FALSE"
    activo = "TRUE" if payload.get("activo", True) else "FALSE"

    if not codigo or not nombre:
        raise HTTPException(status_code=400, detail="El código y nombre del plan son obligatorios.")

    c_cod = codigo.replace("'", "''")
    c_nom = nombre.replace("'", "''")
    c_sub = subtitulo.replace("'", "''")
    c_per = tipo_periodo.replace("'", "''")

    if plan_id > 0:
        execute_query(f"""
            UPDATE `{P}.{W}.planes`
            SET codigo = '{c_cod}',
                nombre = '{c_nom}',
                subtitulo = '{c_sub}',
                tipo_periodo = '{c_per}',
                max_usuarios = {max_usuarios},
                limite_mensual_emails = {limite_mensual_emails},
                limite_mensual_whatsapp = {limite_mensual_whatsapp},
                limite_mensual_api_ecuador = {limite_mensual_api_ecuador},
                precio_mensual = NUMERIC '{precio_mensual:.2f}',
                precio_anual = NUMERIC '{precio_anual:.2f}',
                descuento_anual_pct = NUMERIC '{descuento_anual_pct:.2f}',
                precio_anual_con_descuento = NUMERIC '{precio_anual_con_descuento:.2f}',
                es_destacado = {es_destacado},
                es_personalizado = {es_personalizado},
                activo = {activo}
            WHERE id = {plan_id}
        """)
        invalidate_planes_cache()
        return {"success": True, "message": f"Plan '{nombre}' actualizado correctamente.", "id": plan_id}

    nuevo_id = get_next_id("planes", W)
    execute_query(f"""
        INSERT INTO `{P}.{W}.planes` (
            id, codigo, nombre, subtitulo, tipo_periodo, max_usuarios,
            limite_mensual_emails, limite_mensual_whatsapp, limite_mensual_api_ecuador,
            precio_mensual, precio_anual, descuento_anual_pct, precio_anual_con_descuento,
            es_destacado, es_personalizado, activo, created_at
        ) VALUES (
            {nuevo_id}, '{c_cod}', '{c_nom}', '{c_sub}', '{c_per}', {max_usuarios},
            {limite_mensual_emails}, {limite_mensual_whatsapp}, {limite_mensual_api_ecuador},
            NUMERIC '{precio_mensual:.2f}', NUMERIC '{precio_anual:.2f}',
            NUMERIC '{descuento_anual_pct:.2f}', NUMERIC '{precio_anual_con_descuento:.2f}',
            {es_destacado}, {es_personalizado}, {activo}, CURRENT_TIMESTAMP()
        )
    """)
    invalidate_planes_cache()
    return {"success": True, "message": f"Plan '{nombre}' creado correctamente.", "id": nuevo_id}


@router.post("/api/cliente/logo")
async def api_subir_logo_cliente(request: Request, file: UploadFile = File(...)):
    """Sube el logo de la empresa a Google Drive (clientes/logos) y guarda la URL en wesbyte.administracion.clientes.logo."""
    sesion = get_sesion(request)
    if not sesion:
        raise HTTPException(status_code=401, detail="Sesión expirada.")
    cliente_id = int(sesion.get("cid") or sesion.get("cliente_id") or 0)
    if cliente_id <= 0:
        raise HTTPException(status_code=403, detail="Solo clientes autenticados pueden subir su logo.")

    orig_name = (file.filename or "logo.png").strip()
    ext = orig_name.rsplit(".", 1)[-1].lower() if "." in orig_name else "png"
    if ext not in ("png", "jpg", "jpeg", "webp", "svg"):
        raise HTTPException(status_code=400, detail="Formato no permitido. Usa PNG, JPG, WEBP o SVG.")

    content = await file.read()
    if not content:
        raise HTTPException(status_code=400, detail="El archivo está vacío.")
    if len(content) > 5 * 1024 * 1024:
        raise HTTPException(status_code=400, detail="El logo no debe superar los 5 MB.")

    mime_map = {
        "png": "image/png",
        "jpg": "image/jpeg",
        "jpeg": "image/jpeg",
        "webp": "image/webp",
        "svg": "image/svg+xml",
    }
    mime_type = file.content_type or mime_map.get(ext, "image/png")
    drive_filename = f"cliente_{cliente_id}_{uuid.uuid4().hex[:8]}.{ext}"

    try:
        drive_res = await asyncio.to_thread(
            GoogleDriveService().upload_logo,
            file_bytes=content,
            filename=drive_filename,
            mime_type=mime_type,
            folder_path="clientes/logos",
        )
    except GoogleDriveServiceError as exc:
        logger.error(f"Error subiendo logo a Google Drive: {exc}")
        raise HTTPException(status_code=500, detail=f"Error subiendo logo a Google Drive: {str(exc)}")

    prime_media_cache(drive_res.get("file_id", ""), mime_type, content)
    logo_url = drive_res["url"]
    clean_url = logo_url.replace("'", "''")
    await asyncio.to_thread(
        execute_query,
        f"""
        UPDATE `{P}.{W}.clientes`
        SET logo = '{clean_url}'
        WHERE id = {cliente_id}
        """,
    )
    invalidate_cliente_cache(cliente_id)

    return {
        "success": True,
        "logo_url": logo_url,
        "file_id": drive_res["file_id"],
        "message": "Logo guardado en Google Drive (clientes/logos) correctamente.",
    }


@router.post("/api/cliente/empresa")
async def api_guardar_datos_empresa(request: Request, payload: Dict[str, Any] = Body(...)):
    """Actualiza nombre (nombre_empresa) y ruc_cedula del cliente en wesbyte.administracion.clientes."""
    sesion = get_sesion(request)
    if not sesion or not sesion.get("cid"):
        raise HTTPException(status_code=401, detail="Sesión expirada. Inicia sesión nuevamente.")

    cliente_id = int(sesion["cid"])
    nombre_empresa = str(payload.get("nombre_empresa") or "").strip()
    ruc_cedula = str(payload.get("ruc_cedula") or "").strip()
    if len(nombre_empresa) < 2:
        raise HTTPException(status_code=400, detail="El nombre de la empresa debe tener al menos 2 caracteres.")
    if len(ruc_cedula) < 5:
        raise HTTPException(status_code=400, detail="Ingresa un RUC o cédula válido.")

    clean_nombre = nombre_empresa.replace("'", "''")
    clean_ruc = ruc_cedula.replace("'", "''")
    await asyncio.to_thread(
        execute_query,
        f"""
        UPDATE `{P}.{W}.clientes`
        SET nombre = '{clean_nombre}',
            ruc_cedula = '{clean_ruc}'
        WHERE id = {cliente_id}
        """,
    )
    invalidate_cliente_cache(cliente_id)
    return {
        "success": True,
        "nombre_empresa": nombre_empresa,
        "ruc_cedula": ruc_cedula,
        "message": "Datos de tu empresa actualizados correctamente.",
    }
