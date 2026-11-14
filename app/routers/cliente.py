import datetime
import json
import logging
import re
import secrets
import string
import uuid
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, List, Optional

import jwt
from fastapi import APIRouter, Body, File, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from google.cloud import bigquery

from app.config import (
    ACCESS_TOKEN_EXPIRE_MINUTES,
    ALGORITHM,
    APP_VERSION,
    BIGQUERY_PROJECT_ID,
    PLANES_OFICIALES,
    SECRET_KEY,
    SOPORTE_EMAIL,
    WESBYTE_APP_URL,
    WESBYTES_DATASET_ID,
)
from app.database import append_rows, execute_query, get_bq_client, get_next_id, hash_password, verify_password
from app.services.email_service import EmailService, EmailServiceError
from app.services.google_drive_service import GoogleDriveService, GoogleDriveServiceError
from app.services.notificaciones_config_service import (
    get_cliente_notificaciones_y_plantillas,
    save_cliente_notificaciones_habilitadas,
    save_cliente_plantilla,
)
from app.services.whatsapp_client import (
    WhatsAppClient,
    WhatsAppClientError,
    sync_client_whatsapp_from_admin,
)

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
          <a href="{WESBYTE_APP_URL}" style="display:inline-block;background:#4f46e5;color:#ffffff;text-decoration:none;font-weight:700;padding:12px 24px;border-radius:10px;">Ir a Wesbyte</a>
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
        f"Acceso: {WESBYTE_APP_URL}\n"
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
    candidatos = _rows(
        f"""
        SELECT ua.id, ua.cliente_id, ua.username, ua.nombre_completo AS nombre, ua.email,
               ua.password_hash, ua.permiso AS rol,
               IFNULL(ua.debe_cambiar_password, FALSE) AS debe_cambiar_password,
               c.estado, c.nombre AS empresa, c.dataset_id
        FROM `{P}.{W}.usuarios_app` ua
        JOIN `{P}.{W}.clientes` c ON c.id = ua.cliente_id
        WHERE ua.activo = TRUE
          AND (LOWER(ua.email) = LOWER(@l) OR LOWER(ua.username) = LOWER(@l))
        """,
        [bigquery.ScalarQueryParameter("l", "STRING", login)],
    )

    if not candidatos:
        candidatos = _rows(
            f"""
            SELECT u.id, u.cliente_id, u.username, u.nombre, u.email, u.password_hash, u.rol,
                   IFNULL(u.debe_cambiar_password, FALSE) AS debe_cambiar_password,
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
        raise HTTPException(status_code=401, detail="Correo o contraseña incorrectos.")

    u = validos[0]
    rol_usuario = str(u.get("rol", "")).lower().strip()

    if u.get("estado") == "cancelado":
        raise HTTPException(
            status_code=403,
            detail="Tu cuenta ha sido cancelada (por solicitud o por superar 2 meses sin pago). Contacta a soporte para reactivar tu plan.",
        )
    if u.get("estado") != "activo":
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

    # Nota: No se incrementa login_count ni ultimo_login en el portal administrativo,
    # ya que estas métricas registran exclusivamente el uso del aplicativo operativo web.
    now = datetime.datetime.now(datetime.timezone.utc)
    exp = now + datetime.timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    token = jwt.encode(
        {"sub": u["username"], "uid": int(u["id"]), "cid": int(u["cliente_id"]),
         "rol": "admin", "nombre": u["nombre"], "empresa": u["empresa"], "exp": exp},
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


def _superadmin_dashboard() -> Dict[str, Any]:
    """Recopila la información global de TODOS los clientes, consumos, usuarios, logs y tamaños de BD."""
    client = get_bq_client()
    periodo = datetime.date.today().strftime("%Y-%m")

    # 1. Todos los clientes con sus planes y suscripciones
    clientes_raw = _rows(f"""
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

    # 2. Consumos mensuales por cliente
    consumos_raw = _rows(f"""
        SELECT cliente_id, total_emails, total_whatsapp, total_api_ecuador,
               bloqueado_mensual_email, bloqueado_mensual_whatsapp, bloqueado_mensual_api
        FROM `{P}.{W}.consumos_mensuales`
        WHERE periodo = '{periodo}'
    """)
    consumos_map = {r["cliente_id"]: r for r in consumos_raw}

    # 3. Todos los usuarios con conteo de logins y último acceso (desde usuarios_app)
    try:
        usuarios_raw = _rows(f"""
            SELECT u.id, u.cliente_id, u.username, u.nombre_completo AS nombre, u.email, u.permiso AS rol,
                   u.permisos_seguros, u.activo, u.ultimo_login,
                   IFNULL(u.login_count, 0) AS login_count,
                   IFNULL(c.nombre, 'Wesbyte Global') AS empresa
            FROM `{P}.{W}.usuarios_app` u
            LEFT JOIN `{P}.{W}.clientes` c ON u.cliente_id = c.id
            ORDER BY u.ultimo_login DESC NULLS LAST
        """)
    except Exception:
        usuarios_raw = []

    if not usuarios_raw:
        usuarios_raw = _rows(f"""
            SELECT u.id, u.cliente_id, u.username, u.nombre, u.email, u.rol, u.activo, u.ultimo_login,
                   IFNULL(u.login_count, 0) AS login_count,
                   IFNULL(c.nombre, 'Wesbyte Global') AS empresa
            FROM `{P}.{W}.usuarios` u
            LEFT JOIN `{P}.{W}.clientes` c ON u.cliente_id = c.id
            ORDER BY u.ultimo_login DESC NULLS LAST
        """)

    # 4. Cálculo de GB y tablas por cada dataset de cliente
    datasets_info = []
    total_bytes_global = 0
    all_datasets = {d.dataset_id: d for d in client.list_datasets()}

    for c in clientes_raw:
        ds_id = c.get("dataset_id")
        ds_bytes = 0
        ds_tables_count = 0
        if ds_id and ds_id in all_datasets:
            try:
                for t in client.list_tables(ds_id):
                    tbl = client.get_table(f"{P}.{ds_id}.{t.table_id}")
                    if tbl.num_bytes:
                        ds_bytes += tbl.num_bytes
                    ds_tables_count += 1
            except Exception:
                pass
        total_bytes_global += ds_bytes
        c["bytes"] = ds_bytes
        c["gb"] = round(ds_bytes / (1024**3), 4)
        c["mb"] = round(ds_bytes / (1024**2), 2)
        c["tables_count"] = ds_tables_count
        c["consumo"] = consumos_map.get(c["id"], {
            "total_emails": 0, "total_whatsapp": 0, "total_api_ecuador": 0
        })

    # 5. Logs recientes de WhatsApp, Emails y APIs
    logs_wa = _rows(f"""
        SELECT l.cliente_id, c.nombre AS empresa, l.telefono_destino, l.estado, l.error_detalle, l.fecha_envio AS created_at
        FROM `{P}.{W}.logs_whatsapp` l
        LEFT JOIN `{P}.{W}.clientes` c ON l.cliente_id = c.id
        ORDER BY l.fecha_envio DESC
        LIMIT 20
    """)
    logs_em = _rows(f"""
        SELECT l.cliente_id, c.nombre AS empresa, l.destinatario, l.motivo, l.estado, l.error_detalle, l.created_at
        FROM `{P}.{W}.logs_emails` l
        LEFT JOIN `{P}.{W}.clientes` c ON l.cliente_id = c.id
        ORDER BY l.created_at DESC
        LIMIT 20
    """)
    logs_api = _rows(f"""
        SELECT l.cliente_id, c.nombre AS empresa, l.endpoint, l.metodo, l.status_code, l.tiempo_respuesta_ms, l.bloqueado_por_limite, l.created_at
        FROM `{P}.{W}.logs_api_ecuador` l
        LEFT JOIN `{P}.{W}.clientes` c ON l.cliente_id = c.id
        ORDER BY l.created_at DESC
        LIMIT 20
    """)

    # Totales globales
    total_wa = sum(r.get("total_whatsapp", 0) for r in consumos_raw)
    total_em = sum(r.get("total_emails", 0) for r in consumos_raw)
    total_api = sum(r.get("total_api_ecuador", 0) for r in consumos_raw)
    total_logins = sum(int(u.get("login_count", 0)) for u in usuarios_raw)

    # 6. Todos los planes en wesbyte.administracion.planes
    try:
        planes_raw = _rows(f"""
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
        planes_raw = []

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
    usuarios = _rows(f"SELECT email, username, nombre, rol, activo, ultimo_login, IFNULL(login_count, 0) AS login_count FROM `{P}.{W}.usuarios` WHERE cliente_id=@cid ORDER BY id", cp)

    # Usuarios en usuarios_app (con roles de supervisor, actualizador, visualizador y permisos de seguros)
    try:
        usuarios_app = _rows(f"""
            SELECT id, cliente_id, username, nombre_completo, email, celular, permiso, dataset_id, activo,
                   permisos_seguros, ultimo_login, IFNULL(login_count, 0) AS login_count, created_at
            FROM `{P}.{W}.usuarios_app`
            WHERE cliente_id=@cid
            ORDER BY id ASC
        """, cp)
    except Exception:
        usuarios_app = []

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

    # Datos cargados y tamaño en GB de su base de datos
    registros: Dict[str, int] = {}
    total_clientes = 0
    ds_bytes = 0
    ds = cliente.get("dataset_id")
    if ds:
        client = get_bq_client()
        for name in ("maestra_clientes", "registros_medicos", "registros_vehiculares",
                     "registros_vida_e_inversion", "registros_ramos_generales"):
            try:
                tbl = client.get_table(f"{P}.{ds}.{name}")
                n = int(tbl.num_rows or 0)
                if tbl.num_bytes:
                    ds_bytes += tbl.num_bytes
            except Exception:
                n = 0
            if name == "maestra_clientes":
                total_clientes = n
            else:
                registros[name] = n

    dias_restantes = None
    if sus and sus.get("fecha_fin"):
        dias_restantes = (sus["fecha_fin"] - datetime.date.today()).days

    # Logs del cliente
    logs_wa = _rows(f"SELECT telefono_destino, estado, error_detalle, fecha_envio AS created_at FROM `{P}.{W}.logs_whatsapp` WHERE cliente_id=@cid ORDER BY fecha_envio DESC LIMIT 10", cp)
    logs_em = _rows(f"SELECT destinatario, motivo, estado, error_detalle, created_at FROM `{P}.{W}.logs_emails` WHERE cliente_id=@cid ORDER BY created_at DESC LIMIT 10", cp)
    logs_api = _rows(f"SELECT endpoint, metodo, status_code, tiempo_respuesta_ms, created_at FROM `{P}.{W}.logs_api_ecuador` WHERE cliente_id=@cid ORDER BY created_at DESC LIMIT 10", cp)

    # Configuración de notificaciones del cliente (por client_id o fallback a su plan)
    notif_cfg = {
        "correo_cumpleanios_activo": True,
        "correo_renovaciones_activo": True,
        "correo_cobros_activo": True,
        "whatsapp_cumpleanios_activo": False,
        "whatsapp_renovaciones_activo": True,
        "whatsapp_cobros_activo": True,
    }
    try:
        rows_notif = _rows(
            f"""
            SELECT correo_cumpleanios_activo, correo_renovaciones_activo, correo_cobros_activo,
                   whatsapp_cumpleanios_activo, whatsapp_renovaciones_activo, whatsapp_cobros_activo
            FROM `{P}.{W}.configuracion_planes_notificaciones`
            WHERE client_id = @cid
            LIMIT 1
            """,
            cp,
        )
        if not rows_notif:
            plan_id_str = str(cliente.get("plan_id") or "")
            plan_cod = str(plan.get("codigo") or "")
            rows_notif = _rows(
                f"""
                SELECT correo_cumpleanios_activo, correo_renovaciones_activo, correo_cobros_activo,
                       whatsapp_cumpleanios_activo, whatsapp_renovaciones_activo, whatsapp_cobros_activo
                FROM `{P}.{W}.configuracion_planes_notificaciones`
                WHERE client_id IS NULL AND (id_plan = @pid OR id_plan = @pcod)
                LIMIT 1
                """,
                [
                    bigquery.ScalarQueryParameter("pid", "STRING", plan_id_str),
                    bigquery.ScalarQueryParameter("pcod", "STRING", plan_cod),
                ],
            )
        if rows_notif:
            rn = rows_notif[0]
            notif_cfg = {k: bool(rn.get(k, False)) for k in notif_cfg.keys()}
    except Exception as exc:
        logger.debug(f"No se pudo cargar configuracion_planes_notificaciones para cliente {cid}: {exc}")

    notif_detalle_data = get_cliente_notificaciones_y_plantillas(
        cid,
        str(cliente.get("plan_id") or ""),
        str(plan.get("codigo") or ""),
    )

    return {
        "is_superadmin": False,
        "cliente": cliente, "plan": plan, "sus": sus, "pagos": pagos, "usuarios": usuarios,
        "notificaciones_config": notif_cfg,
        "notificaciones_detalle": notif_detalle_data["notificaciones"],
        "plantilla_variables": notif_detalle_data["variables"],
        "usuarios_app": usuarios_app,
        "usuarios_activos_count": usuarios_activos_count,
        "max_usuarios": max_usr,
        "es_plan_prime": es_plan_prime,
        "limite_alcanzado": limite_alcanzado,
        "puede_crear_usuarios": puede_crear_usuarios,
        "consumos": consumos, "registros": registros, "total_clientes": total_clientes,
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


def auditar_vencimientos_suscripciones() -> None:
    """
    Regla de negocio automática:
    Si un cliente tiene más de 60 días (2 meses) sin pagar respecto a su fecha de vencimiento:
    Se cancela automáticamente la suscripción y se desactiva al cliente.
    """
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
    # Ejecutar auditoría de vencimientos (regla de 2 meses)
    auditar_vencimientos_suscripciones()

    sesion = get_sesion(request)
    if not sesion:
        return RedirectResponse("/ingresar", status_code=302)
    if sesion.get("must_change_pw"):
        return RedirectResponse("/actualizar-password", status_code=302)

    # Si es superadmin
    is_sa = (sesion.get("rol") == "superadmin") or (sesion.get("sub") in ("admin", "wasm") and int(sesion.get("cid") or sesion.get("cliente_id") or 0) == 0)
    if is_sa:
        try:
            data = _superadmin_dashboard()
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
        data = _dashboard(cid)
    except Exception:
        logger.exception("Error cargando dashboard")
        raise HTTPException(status_code=500, detail="No pudimos cargar tu portal. Intenta de nuevo.")
    if not data or data["cliente"].get("estado") != "activo":
        return RedirectResponse("/salir", status_code=302)
    return templates.TemplateResponse(
        request=request, name="mi_portal.html",
        context={"sesion": sesion, "d": data, "is_superadmin": False,
                 "soporte_email": SOPORTE_EMAIL, "wesbyte_app_url": WESBYTE_APP_URL,
                 "app_version": APP_VERSION},
    )


@router.post("/api/suscripcion/cancelar")
async def api_cancelar_suscripcion(request: Request, payload: Dict[str, Any] = Body(...)):
    """Permite al cliente cancelar voluntariamente su plan."""
    sesion = get_sesion(request)
    if not sesion or not sesion.get("cid"):
        raise HTTPException(status_code=401, detail="No autorizado.")

    cid = int(sesion["cid"])
    motivo = str(payload.get("motivo", "Cancelación solicitada por el usuario")).strip()
    client = get_bq_client()
    now_ts = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

    try:
        # Cancelar en suscripciones
        client.query(f"""
            UPDATE `{P}.{W}.suscripciones`
            SET estado = 'cancelada',
                renovacion_automatica = FALSE,
                desea_renovar = FALSE,
                motivo_no_renovacion = '{motivo.replace("'", "''")}',
                updated_at = TIMESTAMP('{now_ts}')
            WHERE cliente_id = {cid} AND estado IN ('activa', 'por_vencer', 'vencida')
        """).result()

        # Marcar cliente cancelado
        client.query(f"""
            UPDATE `{P}.{W}.clientes`
            SET estado = 'cancelado',
                activo = FALSE
            WHERE id = {cid}
        """).result()

        return {"success": True, "message": "Tu suscripción ha sido cancelada correctamente."}
    except Exception as exc:
        logger.exception("Error cancelando suscripción")
        raise HTTPException(status_code=500, detail=f"No se pudo procesar la cancelación: {str(exc)}")


@router.post("/api/suscripcion/mejorar")
async def api_mejorar_suscripcion(request: Request, payload: Dict[str, Any] = Body(...)):
    """Permite al cliente subir de plan o agregar usuarios adicionales a su cuenta."""
    sesion = get_sesion(request)
    if not sesion or not sesion.get("cid"):
        raise HTTPException(status_code=401, detail="No autorizado.")

    cid = int(sesion["cid"])
    tipo = str(payload.get("tipo", "")).strip()  # 'cambio_plan' o 'agregar_usuarios'
    client = get_bq_client()
    now_ts = datetime.datetime.now(datetime.timezone.utc)

    cli_rows = _rows(f"SELECT * FROM `{P}.{W}.clientes` WHERE id=@cid LIMIT 1",
                     [bigquery.ScalarQueryParameter("cid", "INT64", cid)])
    if not cli_rows:
        raise HTTPException(status_code=404, detail="Cliente no encontrado.")
    cli = cli_rows[0]

    if tipo == "agregar_usuarios":
        adicionales = int(payload.get("usuarios_adicionales", 1))
        if adicionales < 1:
            raise HTTPException(status_code=400, detail="Cantidad de usuarios inválida.")
        nuevo_max = int(cli.get("max_usuarios", 1)) + adicionales
        costo_mensual = adicionales * 6.00
        costo_anual = round(costo_mensual * 12 * 0.90, 2)

        client.query(f"""
            UPDATE `{P}.{W}.clientes`
            SET max_usuarios = {nuevo_max}
            WHERE id = {cid}
        """).result()

        pago_id = get_next_id("pagos_payphone", WESBYTES_DATASET_ID)
        pago_row = {
            "id": pago_id,
            "cliente_id": cid,
            "suscripcion_id": None,
            "plan_id": int(cli.get("plan_id", 1)),
            "modalidad_pago": "mejora_usuarios",
            "client_transaction_id": f"UPG-USR-{cid}-{uuid.uuid4().hex[:6].upper()}",
            "payphone_transaction_id": f"PP-UPG-{uuid.uuid4().hex[:6].upper()}",
            "authorization_code": "AUTH-UPG-OK",
            "monto_centavos": int(costo_anual * 100),
            "monto_usd": Decimal(f"{costo_anual:.2f}"),
            "estado_pago": "APPROVED",
            "metodo_pago": "PAYPHONE",
            "respuesta_raw_json": json.dumps({"tipo": "usuarios_adicionales", "cantidad": adicionales, "nuevo_max": nuevo_max}),
            "created_at": now_ts,
            "confirmed_at": now_ts,
        }
        append_rows(WESBYTES_DATASET_ID, "pagos_payphone", [pago_row])

        return {"success": True, "message": f"Se han agregado {adicionales} usuarios adicionales a tu cuenta. Total permitido: {nuevo_max} usuarios."}

    elif tipo == "cambio_plan":
        nuevo_codigo = str(payload.get("nuevo_plan_codigo", "")).strip().upper()
        nuevo_plan = next((p for p in PLANES_OFICIALES if p["codigo"] == nuevo_codigo), None)
        if not nuevo_plan:
            raise HTTPException(status_code=400, detail="Plan no válido.")

        client.query(f"""
            UPDATE `{P}.{W}.clientes`
            SET plan_id = {nuevo_plan['id']},
                max_usuarios = {nuevo_plan['max_usuarios']},
                limite_mensual_whatsapp = {nuevo_plan['limite_mensual_whatsapp']},
                limite_mensual_emails = {nuevo_plan['limite_mensual_emails']},
                limite_mensual_api_ecuador = {nuevo_plan['limite_mensual_api_ecuador']}
            WHERE id = {cid}
        """).result()

        client.query(f"""
            UPDATE `{P}.{W}.suscripciones`
            SET plan_id = {nuevo_plan['id']},
                monto_subtotal = {Decimal(str(nuevo_plan['precio_anual']))},
                descuento_aplicado = {Decimal(str(nuevo_plan['ahorro_anual']))},
                monto_total_cobrado = {Decimal(str(nuevo_plan['precio_anual_con_descuento']))},
                updated_at = CURRENT_TIMESTAMP()
            WHERE cliente_id = {cid} AND estado IN ('activa', 'por_vencer')
        """).result()

        pago_id = get_next_id("pagos_payphone", WESBYTES_DATASET_ID)
        pago_row = {
            "id": pago_id,
            "cliente_id": cid,
            "suscripcion_id": None,
            "plan_id": nuevo_plan["id"],
            "modalidad_pago": "upgrade_plan",
            "client_transaction_id": f"UPG-PLAN-{cid}-{uuid.uuid4().hex[:6].upper()}",
            "payphone_transaction_id": f"PP-UPG-{uuid.uuid4().hex[:6].upper()}",
            "authorization_code": "AUTH-UPG-OK",
            "monto_centavos": int(nuevo_plan["precio_anual_con_descuento"] * 100),
            "monto_usd": Decimal(f"{nuevo_plan['precio_anual_con_descuento']:.2f}"),
            "estado_pago": "APPROVED",
            "metodo_pago": "PAYPHONE",
            "respuesta_raw_json": json.dumps({"tipo": "upgrade_plan", "nuevo_plan": nuevo_plan["nombre"]}),
            "created_at": now_ts,
            "confirmed_at": now_ts,
        }
        append_rows(WESBYTES_DATASET_ID, "pagos_payphone", [pago_row])

        return {"success": True, "message": f"¡Tu plan ha sido actualizado a {nuevo_plan['nombre']} exitosamente!"}

    raise HTTPException(status_code=400, detail="Tipo de mejora no soportado.")


@router.post("/api/usuarios-app/crear")
async def api_crear_usuario_app(request: Request, payload: Dict[str, Any] = Body(...)):
    """Permite al cliente crear colaboradores para su empresa según su plan y asignar ramos de seguros."""
    sesion = get_sesion(request)
    if not sesion:
        raise HTTPException(status_code=401, detail="Sesión expirada. Inicia sesión nuevamente.")

    cliente_id = int(sesion.get("cid") or sesion.get("cliente_id") or 0)
    if cliente_id <= 0:
        raise HTTPException(status_code=403, detail="Solo los clientes autenticados pueden crear usuarios para su empresa.")

    # 1. Obtener datos del cliente y su plan
    cp = [bigquery.ScalarQueryParameter("cid", "INT64", cliente_id)]
    df_cli = _rows(f"SELECT id, nombre, dataset_id, max_usuarios, estado, plan_id FROM `{P}.{W}.clientes` WHERE id=@cid", cp)
    if not df_cli:
        raise HTTPException(status_code=404, detail="Empresa no encontrada.")
    cli_row = df_cli[0]
    if cli_row.get("estado") != "activo":
        raise HTTPException(status_code=403, detail="Tu cuenta se encuentra inactiva.")

    max_usr = int(cli_row.get("max_usuarios") or 1)
    dataset_id = str(cli_row.get("dataset_id")).strip()

    # Validación estricta según el plan del usuario
    if max_usr <= 1:
        raise HTTPException(
            status_code=400,
            detail="Tu Plan Prime solo incluye 1 usuario administrador. Para añadir supervisores, actualizadores o visualizadores para tu equipo, debes mejorar tu plan a Medium o Super Gold, o contratar usuarios adicionales."
        )

    # Contar usuarios activos en usuarios_app
    df_count = _rows(f"SELECT COUNT(1) AS c FROM `{P}.{W}.usuarios_app` WHERE cliente_id=@cid AND activo=TRUE", cp)
    actuales = int(df_count[0]["c"]) if df_count else 0
    if actuales >= max_usr:
        raise HTTPException(
            status_code=400,
            detail=f"Has completado el límite de {max_usr} usuarios autorizados en tu plan. Si necesitas sumar más colaboradores, contrata usuarios adicionales ($6/mes)."
        )

    # Validar campos requeridos (el usuario es el correo registrado y la contraseña se genera aleatoriamente)
    nombre_completo = str(payload.get("nombre_completo", "")).strip()
    email = str(payload.get("email", "")).strip().lower()
    raw_username = email
    permiso = str(payload.get("permiso", "")).strip().lower()
    permisos_seguros = payload.get("permisos_seguros")

    if not nombre_completo:
        raise HTTPException(status_code=400, detail="El nombre completo es obligatorio.")
    if not email or "@" not in email:
        raise HTTPException(status_code=400, detail="El correo electrónico es obligatorio y será el usuario de ingreso.")
    if " " in raw_username or len(raw_username) < 3:
        raise HTTPException(status_code=400, detail="El nombre de usuario no debe contener espacios y debe tener al menos 3 caracteres.")

    roles_permitidos = ["supervisor", "actualizador", "visualizador"]
    if permiso not in roles_permitidos:
        raise HTTPException(status_code=400, detail=f"Rol no válido. Debe ser uno de: {', '.join(roles_permitidos)}.")

    # Validar selector de ramos de seguros
    ramos_validos = ["registros_medicos", "registros_vehiculares", "registros_vida_e_inversion", "registros_ramos_generales"]
    if isinstance(permisos_seguros, list):
        selected_ramos = [r for r in permisos_seguros if r in ramos_validos]
    elif isinstance(permisos_seguros, str):
        selected_ramos = [r.strip() for r in permisos_seguros.split(",") if r.strip() in ramos_validos]
    else:
        selected_ramos = []

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

    if not nombre_completo:
        raise HTTPException(status_code=400, detail="El nombre completo es obligatorio.")

    # Validar rol
    if es_admin:
        nuevo_permiso = "administrador"
    else:
        roles_permitidos = ["supervisor", "actualizador", "visualizador"]
        if permiso not in roles_permitidos:
            raise HTTPException(status_code=400, detail=f"Rol no válido. Debe ser uno de: {', '.join(roles_permitidos)}.")
        nuevo_permiso = permiso

    # Validar selector de ramos
    ramos_validos = ["registros_medicos", "registros_vehiculares", "registros_vida_e_inversion", "registros_ramos_generales"]
    if isinstance(permisos_seguros, list):
        selected_ramos = [r for r in permisos_seguros if r in ramos_validos]
    elif isinstance(permisos_seguros, str):
        selected_ramos = [r.strip() for r in permisos_seguros.split(",") if r.strip() in ramos_validos]
    else:
        selected_ramos = []

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
            permisos_seguros = '{permisos_str}'
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
        pw_part_target = f", password = '{pw_hash}'" if pw_hash else ""
        execute_query(f"""
            UPDATE `{P}.{ds}.maestra_usuarios_app`
            SET access = '{nuevo_permiso}',
                permisos_seguros = '{permisos_str}',
                fecha_modificacion = CURRENT_TIMESTAMP()
                {pw_part_target}
            WHERE LOWER(TRIM(username)) = LOWER(TRIM('{username}'))
        """)
    except Exception:
        pass

    return {
        "success": True,
        "message": f"Usuario '{username}' actualizado correctamente.",
        "usuario": {
            "id": usuario_id,
            "username": username,
            "nombre_completo": nombre_completo,
            "permiso": nuevo_permiso,
            "permisos_seguros": permisos_str,
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

    # Sincronizar toggles de WhatsApp con wesbyte_api
    try:
        await sync_client_whatsapp_from_admin(cliente_id)
    except Exception as exc:
        logger.warning(f"No se pudo sincronizar preferencias WhatsApp con wesbyte_api para cliente {cliente_id}: {exc}")

    return {
        "success": True,
        "message": "Preferencias de notificaciones guardadas correctamente.",
    }


@router.get("/api/whatsapp/estado")
async def api_whatsapp_estado(request: Request):
    """Consulta y sincroniza el estado de conexión de WhatsApp del cliente con wesbyte_api."""
    sesion = get_sesion(request)
    if not sesion:
        raise HTTPException(status_code=401, detail="Sesión expirada.")
    cliente_id = int(sesion.get("cid") or sesion.get("cliente_id") or 0)
    if cliente_id <= 0:
        raise HTTPException(status_code=403, detail="Solo clientes autenticados pueden consultar WhatsApp.")

    try:
        await sync_client_whatsapp_from_admin(cliente_id)
        wa = WhatsAppClient()
        estado = await wa.get_status(cliente_id)
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
    """Genera el código QR de vinculación de WhatsApp desde wesbyte_api para el cliente autenticado."""
    sesion = get_sesion(request)
    if not sesion:
        raise HTTPException(status_code=401, detail="Sesión expirada.")
    cliente_id = int(sesion.get("cid") or sesion.get("cliente_id") or 0)
    if cliente_id <= 0:
        raise HTTPException(status_code=403, detail="Solo clientes autenticados pueden vincular WhatsApp.")

    try:
        provision = await sync_client_whatsapp_from_admin(cliente_id)
        wa = WhatsAppClient()
        res = await wa.connect_qr(cliente_id)
        qr = res.get("qr_base64") or ""
        if qr and not qr.startswith("data:image"):
            qr = f"data:image/png;base64,{qr}"
        return {
            "success": True,
            "requires_qr": True,
            "client_id": cliente_id,
            "plan_tier": provision.get("plan_tier"),
            "instance_name": res.get("instance_name"),
            "qr_base64": qr,
            "connection_status": "qr_ready",
            "message": "Escanea el código QR desde WhatsApp > Dispositivos vinculados.",
        }
    except WhatsAppClientError as exc:
        if exc.status_code == 409:
            wa = WhatsAppClient()
            estado = await wa.get_status(cliente_id)
            return {
                "success": True,
                "requires_qr": True,
                "already_connected": True,
                "connection_status": "connected",
                "phone_connected": estado.get("phone_connected"),
                "instance_name": estado.get("instance_name"),
                "message": "Tu WhatsApp ya se encuentra conectado.",
            }
        raise HTTPException(status_code=exc.status_code, detail=exc.detail)
    except Exception as exc:
        logger.error(f"Error generando QR WhatsApp en wesbyte_api para cliente {cliente_id}: {exc}")
        raise HTTPException(status_code=502, detail="No se pudo generar el código QR con el servicio de WhatsApp.")


@router.post("/api/notificaciones/plantillas/guardar")
async def api_guardar_plantilla_notificacion(request: Request, payload: Dict[str, Any] = Body(...)):
    """Guarda el formato personalizado de correo o WhatsApp de una notificación en plantillas_notificaciones."""
    sesion = get_sesion(request)
    if not sesion:
        raise HTTPException(status_code=401, detail="Sesión expirada.")
    cliente_id = int(sesion.get("cid") or sesion.get("cliente_id") or 0)
    if cliente_id <= 0:
        raise HTTPException(status_code=403, detail="Solo clientes autenticados pueden configurar plantillas.")

    try:
        save_cliente_plantilla(
            client_id=cliente_id,
            notificacion_codigo=str(payload.get("notificacion_codigo") or ""),
            canal=str(payload.get("canal") or ""),
            asunto=payload.get("asunto"),
            mensaje=str(payload.get("mensaje") or ""),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    return {
        "success": True,
        "message": "Plantilla guardada correctamente.",
    }


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
        drive_res = GoogleDriveService().upload_logo(
            file_bytes=content,
            filename=drive_filename,
            mime_type=mime_type,
            folder_path="clientes/logos",
        )
    except GoogleDriveServiceError as exc:
        logger.error(f"Error subiendo logo a Google Drive: {exc}")
        raise HTTPException(status_code=500, detail=f"Error subiendo logo a Google Drive: {str(exc)}")

    logo_url = drive_res["url"]
    clean_url = logo_url.replace("'", "''")
    execute_query(f"""
        UPDATE `{P}.{W}.clientes`
        SET logo = '{clean_url}'
        WHERE id = {cliente_id}
    """)

    return {
        "success": True,
        "logo_url": logo_url,
        "file_id": drive_res["file_id"],
        "message": "Logo guardado en Google Drive (clientes/logos) correctamente.",
    }
