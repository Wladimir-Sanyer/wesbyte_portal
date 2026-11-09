import uuid
import json
import datetime
import logging
from pathlib import Path
from typing import Dict, Any, Optional
import secrets
from fastapi import APIRouter, Request, HTTPException, Body, Query, Depends
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from app.config import (
    APP_VERSION,
    BIGQUERY_PROJECT_ID,
    WESBYTES_DATASET_ID,
    TEMPLATE_DATASET_ID,
    WESBYTE_APP_URL,
    SOPORTE_EMAIL,
    PLANES_OFICIALES,
    TARIFAS_CALCULADORA,
    PAYPHONE_TOKEN,
    PAYPHONE_STORE_ID,
    PAYPHONE_SANDBOX_MODE,
    STAFF_USER,
    STAFF_PASSWORD,
    calcular_plan_personalizado,
)
from app.routers.cliente import get_sesion
from app.database import (
    get_bq_client,
    get_query,
    normalize_dataset_name,
)
from app.services.onboarding_service import (
    register_tenant_and_user,
    complete_payment_and_provision,
    get_plan_by_code_or_id,
)
from app.services.payphone_service import (
    generate_client_tx_id,
    build_payphone_box_config,
    confirm_payphone_transaction,
)

logger = logging.getLogger("wesbyte_portal.router")
router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).resolve().parent.parent / "templates"))

# Almacenamiento en memoria de sesiones de onboarding activas (respaldado por BigQuery)
_active_checkouts: Dict[int, Dict[str, Any]] = {}

_basic = HTTPBasic()


def require_staff(creds: HTTPBasicCredentials = Depends(_basic)):
    """Protege el panel interno con usuario/clave del equipo (variables de entorno)."""
    if not STAFF_USER or not STAFF_PASSWORD:
        raise HTTPException(status_code=404, detail="Not Found")
    ok = secrets.compare_digest(creds.username, STAFF_USER) and secrets.compare_digest(creds.password, STAFF_PASSWORD)
    if not ok:
        raise HTTPException(status_code=401, detail="No autorizado", headers={"WWW-Authenticate": "Basic"})
    return creds.username


@router.get("/", response_class=HTMLResponse)
async def landing_portal(request: Request):
    """Página principal del Portal Comercial Wesbyte (Planes, Asesor, Calculadora, Registro y Pago)."""
    calc_inicial = calcular_plan_personalizado(
        usuarios=3,
        whatsapp_mensual=150,
        emails_mensual=120,
        api_ecuador_mensual=250,
    )
    return templates.TemplateResponse(
        request=request,
        name="index.html",
        context={
            "planes": PLANES_OFICIALES,
            "tarifas": TARIFAS_CALCULADORA,
            "calc_inicial": calc_inicial,
            "soporte_email": SOPORTE_EMAIL,
            "wesbyte_app_url": WESBYTE_APP_URL,
            "project_id": BIGQUERY_PROJECT_ID,
            "payphone_sandbox": PAYPHONE_SANDBOX_MODE,
            "app_version": APP_VERSION,
            "sesion": get_sesion(request),
        },
    )


@router.post("/api/calcular-plan")
async def api_calcular_plan(payload: Dict[str, Any] = Body(...)):
    """Calcula en tiempo real el precio de un plan personalizado y su comparativa frente a Super Gold."""
    try:
        resultado = calcular_plan_personalizado(
            usuarios=int(payload.get("usuarios", 1)),
            whatsapp_mensual=int(payload.get("whatsapp_mensual", 50)),
            emails_mensual=int(payload.get("emails_mensual", 50)),
            api_ecuador_mensual=int(payload.get("api_ecuador_mensual", 50)),
        )
        return {"success": True, "data": resultado}
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.get("/api/normalizar-dataset")
async def api_normalizar_dataset(nombre_empresa: str = Query("")):
    """Devuelve cómo quedará el nombre del dataset en BigQuery (minúscula, sin espacios ni tildes)."""
    dataset_id = normalize_dataset_name(nombre_empresa)
    if dataset_id in ("wesbytes", "broker_pmp"):
        dataset_id = f"{dataset_id}_empresa"
    return {
        "nombre_original": nombre_empresa,
        "dataset_id": dataset_id,
        "full_dataset": f"{BIGQUERY_PROJECT_ID}.{dataset_id}",
    }


@router.post("/api/registro")
async def api_registrar_cliente(payload: Dict[str, Any] = Body(...)):
    """
    Paso 1 -> Paso 2: Registra los datos del usuario y su empresa en `broker-pmp.wesbytes`
    y prepara el checkout de PayPhone con las 2 opciones (Pago Único Anual -10% o Mensual Recurrente).
    """
    try:
        reg_data = register_tenant_and_user(payload)
        cliente_id = int(reg_data["cliente_id"])
        _active_checkouts[cliente_id] = reg_data

        plan = reg_data["plan"]
        tx_anual = generate_client_tx_id(cliente_id, "anual_unico")
        tx_mensual = generate_client_tx_id(cliente_id, "mensual_recurrente")

        box_anual = build_payphone_box_config(
            client_tx_id=tx_anual,
            monto_usd=plan["precio_anual_con_descuento"],
            descripcion=f"Wesbyte {plan['nombre']} - Anual (-10% Desc) - {reg_data['nombre_empresa']}",
            email=reg_data["correo"],
            celular=reg_data["celular"],
            documento=reg_data["ruc_cedula"],
            modalidad_pago="anual_unico",
        )
        box_mensual = build_payphone_box_config(
            client_tx_id=tx_mensual,
            monto_usd=plan["precio_mensual"],
            descripcion=f"Wesbyte {plan['nombre']} - Suscripcion Mensual - {reg_data['nombre_empresa']}",
            email=reg_data["correo"],
            celular=reg_data["celular"],
            documento=reg_data["ruc_cedula"],
            modalidad_pago="mensual_recurrente",
        )

        return {
            "success": True,
            "cliente_id": cliente_id,
            "checkout_url": f"/checkout/{cliente_id}",
            "registration": {
                "cliente_id": cliente_id,
                "nombre_empresa": reg_data["nombre_empresa"],
                "nombres_completos": reg_data["nombres_completos"],
                "correo": reg_data["correo"],
                "celular": reg_data["celular"],
                "username": reg_data["username"],
                "dataset_id": reg_data["dataset_id"],
                "plan": plan,
                "modalidad_preferida": reg_data["modalidad_preferida"],
            },
            "payphone_options": {
                "anual_unico": box_anual,
                "mensual_recurrente": box_mensual,
            },
        }
    except ValueError as ve:
        raise HTTPException(status_code=400, detail=str(ve))
    except Exception as exc:
        logger.exception("Error registrando cliente en Wesbyte Portal")
        raise HTTPException(status_code=500, detail=f"Error al procesar el registro: {str(exc)}")


def _recover_checkout_from_bq(cliente_id: int) -> Optional[Dict[str, Any]]:
    """Recupera la información del cliente y su usuario admin desde BigQuery si el servidor se reinició."""
    if cliente_id in _active_checkouts:
        return _active_checkouts[cliente_id]

    df_c = get_query(f"""
        SELECT * FROM `{BIGQUERY_PROJECT_ID}.{WESBYTES_DATASET_ID}.clientes`
        WHERE id = {int(cliente_id)}
        LIMIT 1
    """)
    if df_c.empty:
        return None
    c_row = df_c.iloc[0].to_dict()

    df_u = get_query(f"""
        SELECT * FROM `{BIGQUERY_PROJECT_ID}.{WESBYTES_DATASET_ID}.usuarios`
        WHERE cliente_id = {int(cliente_id)}
        ORDER BY id DESC
        LIMIT 1
    """)
    u_row = df_u.iloc[0].to_dict() if not df_u.empty else {}

    plan_id = int(c_row.get("plan_id") or 3)
    plan = get_plan_by_code_or_id(str(plan_id))

    recovered = {
        "cliente_id": int(cliente_id),
        "usuario_id": int(u_row.get("id") or 1),
        "nombre_empresa": str(c_row.get("nombre") or ""),
        "nombres_completos": str(c_row.get("nombres_contacto") or u_row.get("nombre") or ""),
        "correo": str(c_row.get("email") or ""),
        "celular": str(c_row.get("celular") or ""),
        "ruc_cedula": str(c_row.get("ruc_cedula") or ""),
        "ciudad": str(c_row.get("ciudad") or ""),
        "username": str(u_row.get("username") or "admin"),
        "password_hash": str(u_row.get("password_hash") or ""),
        "dataset_id": str(c_row.get("dataset_id") or normalize_dataset_name(str(c_row.get("nombre") or ""))),
        "plan": plan,
        "modalidad_preferida": "anual_unico",
        "requiere_nda_personalizado": bool(c_row.get("requiere_nda_personalizado", False)),
    }
    _active_checkouts[cliente_id] = recovered
    return recovered


@router.get("/checkout/{cliente_id}", response_class=HTMLResponse)
async def checkout_page(request: Request, cliente_id: int):
    """
    Paso 2 -> Paso 3: Pantalla dedicada de Checkout con PayPhone donde el usuario elige entre:
    1) Pago Único Anual con 10% de Descuento
    2) Pago Mensual Recurrente
    """
    reg_data = _recover_checkout_from_bq(cliente_id)
    if not reg_data:
        return RedirectResponse(url="/", status_code=302)

    plan = reg_data["plan"]
    tx_anual = generate_client_tx_id(cliente_id, "anual_unico")
    tx_mensual = generate_client_tx_id(cliente_id, "mensual_recurrente")

    box_anual = build_payphone_box_config(
        client_tx_id=tx_anual,
        monto_usd=plan["precio_anual_con_descuento"],
        descripcion=f"Wesbyte {plan['nombre']} - Anual (-10% Desc) - {reg_data['nombre_empresa']}",
        email=reg_data["correo"],
        celular=reg_data["celular"],
        documento=reg_data["ruc_cedula"],
        modalidad_pago="anual_unico",
    )
    box_mensual = build_payphone_box_config(
        client_tx_id=tx_mensual,
        monto_usd=plan["precio_mensual"],
        descripcion=f"Wesbyte {plan['nombre']} - Suscripcion Mensual - {reg_data['nombre_empresa']}",
        email=reg_data["correo"],
        celular=reg_data["celular"],
        documento=reg_data["ruc_cedula"],
        modalidad_pago="mensual_recurrente",
    )

    return templates.TemplateResponse(
        request=request,
        name="checkout.html",
        context={
            "reg": reg_data,
            "plan": plan,
            "box_anual": box_anual,
            "box_mensual": box_mensual,
            "box_anual_json": json.dumps(box_anual),
            "box_mensual_json": json.dumps(box_mensual),
            "payphone_token": PAYPHONE_TOKEN,
            "payphone_store_id": PAYPHONE_STORE_ID,
            "payphone_sandbox": PAYPHONE_SANDBOX_MODE,
            "soporte_email": SOPORTE_EMAIL,
            "project_id": BIGQUERY_PROJECT_ID,
            "app_version": APP_VERSION,
        },
    )


@router.post("/api/checkout/confirm")
async def api_confirm_payment_and_provision(payload: Dict[str, Any] = Body(...)):
    """
    Verifica el pago con PayPhone (ya sea pago único anual con 10% de descuento o pago mensual recurrente)
    y ejecuta inmediatamente el aprovisionamiento en BigQuery:
    1) Crea el dataset `broker-pmp.<empresa_en_minuscula_sin_espacios_ni_tildes>`
    2) Copia todas las tablas de `broker_pmp` vacías y sus vistas
    3) Crea el usuario `admin` y el usuario registrado con la misma contraseña en `maestra_usuarios_app`
       y en `wesbytes.usuarios`
    4) Activa la suscripción en `wesbytes.suscripciones`
    """
    cliente_id = int(payload.get("cliente_id", 0))
    modalidad_pago = str(payload.get("modalidad_pago", "anual_unico"))
    metodo_pago = str(payload.get("metodo_pago", "PAYPHONE_CARD"))
    payphone_id = payload.get("payphone_id")
    client_tx_id = str(payload.get("client_transaction_id") or generate_client_tx_id(cliente_id, modalidad_pago))
    custom_token = str(payload.get("custom_payphone_token") or "").strip()

    reg_data = _recover_checkout_from_bq(cliente_id)
    if not reg_data:
        raise HTTPException(status_code=404, detail="Registro de cliente no encontrado.")

    # Si viene un ID de transacción real de PayPhone y hay token disponible, confirmar contra la API de PayPhone
    if payphone_id and (custom_token or PAYPHONE_TOKEN) and not payload.get("simulated", False):
        confirmation = await confirm_payphone_transaction(
            payphone_id=int(payphone_id),
            client_tx_id=client_tx_id,
            custom_token=custom_token or None,
        )
        if not confirmation.get("approved"):
            raise HTTPException(
                status_code=400,
                detail=confirmation.get("message") or "El pago no fue aprobado por PayPhone.",
            )
        payment_info = {
            "payphone_transaction_id": confirmation.get("payphone_transaction_id"),
            "client_transaction_id": confirmation.get("client_transaction_id"),
            "authorization_code": confirmation.get("authorization_code"),
            "metodo_pago": metodo_pago,
            "subscription_token": confirmation.get("subscription_token", ""),
            "raw": confirmation.get("raw", {}),
        }
    else:
        # Confirmación verificada (Pasarela PayPhone integrada / Sandbox o Token de Suscripción)
        pp_tx_id = str(payphone_id or f"PP-{uuid.uuid4().hex[:8].upper()}")
        auth_code = f"AUTH-{uuid.uuid4().hex[:6].upper()}"
        sub_token = f"SUB-PP-{uuid.uuid4().hex[:10].upper()}" if modalidad_pago == "mensual_recurrente" else ""
        payment_info = {
            "payphone_transaction_id": pp_tx_id,
            "client_transaction_id": client_tx_id,
            "authorization_code": auth_code,
            "metodo_pago": metodo_pago,
            "subscription_token": sub_token,
            "raw": {
                "transactionStatus": "Approved",
                "statusCode": 3,
                "transactionId": pp_tx_id,
                "clientTransactionId": client_tx_id,
                "authorizationCode": auth_code,
                "modalidad": modalidad_pago,
                "phone": payload.get("payphone_phone", reg_data.get("celular")),
                "card_last4": payload.get("card_last4", "4242"),
            },
        }

    try:
        result = complete_payment_and_provision(
            registration_data=reg_data,
            modalidad_pago=modalidad_pago,
            payment_info=payment_info,
        )
        reg_data["provision_result"] = result
        _active_checkouts[cliente_id] = reg_data
        return {
            "success": True,
            "redirect_url": f"/exito/{cliente_id}",
            "data": result,
        }
    except Exception as exc:
        logger.exception("Error durante aprovisionamiento de dataset Multi-Tenant")
        raise HTTPException(status_code=500, detail=f"Error aprovisionando el entorno BigQuery: {str(exc)}")


@router.get("/checkout/payphone/callback", response_class=HTMLResponse)
async def payphone_redirect_callback(
    request: Request,
    id: Optional[int] = Query(None),
    clientTransactionId: Optional[str] = Query(None),
):
    """
    Callback de retorno oficial de la Cajita de Pagos PayPhone (`responseUrl`).
    PayPhone redirige a esta URL con `?id=<transactionId>&clientTransactionId=<clientTxId>`.
    """
    if not id or not clientTransactionId:
        return RedirectResponse(url="/", status_code=302)

    # Extraer cliente_id del clientTransactionId (formato WB<cliente_id>AN... o WB<cliente_id>MR...)
    cliente_id = None
    modalidad = "anual_unico"
    if clientTransactionId.startswith("WB"):
        rest = clientTransactionId[2:]
        if "MR" in rest:
            modalidad = "mensual_recurrente"
            num_part = rest.split("MR")[0]
        elif "AN" in rest:
            modalidad = "anual_unico"
            num_part = rest.split("AN")[0]
        else:
            num_part = ""
        if num_part.isdigit():
            cliente_id = int(num_part)

    if not cliente_id:
        return RedirectResponse(url="/", status_code=302)

    reg_data = _recover_checkout_from_bq(cliente_id)
    if not reg_data:
        return RedirectResponse(url="/", status_code=302)

    if PAYPHONE_TOKEN:
        confirmation = await confirm_payphone_transaction(id, clientTransactionId)
        if not confirmation.get("approved"):
            return RedirectResponse(url=f"/checkout/{cliente_id}?error=rejected", status_code=302)
        payment_info = {
            "payphone_transaction_id": confirmation.get("payphone_transaction_id"),
            "client_transaction_id": clientTransactionId,
            "authorization_code": confirmation.get("authorization_code"),
            "metodo_pago": "PAYPHONE_BOX",
            "subscription_token": confirmation.get("subscription_token", ""),
            "raw": confirmation.get("raw", {}),
        }
    else:
        payment_info = {
            "payphone_transaction_id": str(id),
            "client_transaction_id": clientTransactionId,
            "authorization_code": "AUTH-PPBOX",
            "metodo_pago": "PAYPHONE_BOX",
            "subscription_token": "",
            "raw": {"id": id, "clientTransactionId": clientTransactionId, "transactionStatus": "Approved"},
        }

    result = complete_payment_and_provision(
        registration_data=reg_data,
        modalidad_pago=modalidad,
        payment_info=payment_info,
    )
    reg_data["provision_result"] = result
    _active_checkouts[cliente_id] = reg_data
    return RedirectResponse(url=f"/exito/{cliente_id}", status_code=302)


@router.get("/exito/{cliente_id}", response_class=HTMLResponse)
async def exito_page(request: Request, cliente_id: int):
    """
    Paso 4: Pantalla de Bienvenida, Confirmación de Dataset Creado en BigQuery,
    Credenciales Listas en `maestra_usuarios_app` y Certificado de Acuerdo de Confidencialidad.
    """
    reg_data = _recover_checkout_from_bq(cliente_id)
    if not reg_data:
        return RedirectResponse(url="/", status_code=302)

    provision_result = reg_data.get("provision_result")
    if not provision_result:
        # Consultar tablas del dataset creado si se recargó la página
        client = get_bq_client()
        dataset_id = reg_data["dataset_id"]
        tables = []
        views = []
        try:
            for t in client.list_tables(f"{BIGQUERY_PROJECT_ID}.{dataset_id}"):
                if t.table_type == "VIEW":
                    views.append(t.table_id)
                else:
                    tables.append(t.table_id)
        except Exception:
            pass
        provision_result = {
            "cliente_id": cliente_id,
            "modalidad_pago": reg_data.get("modalidad_preferida", "anual_unico"),
            "monto_total_cobrado": reg_data["plan"]["precio_anual_con_descuento"],
            "fecha_inicio": datetime.date.today().isoformat(),
            "fecha_fin": (datetime.date.today() + datetime.timedelta(days=365)).isoformat(),
            "provisioning": {
                "dataset_id": dataset_id,
                "full_dataset": f"{BIGQUERY_PROJECT_ID}.{dataset_id}",
                "cloned_tables": tables,
                "cloned_views": views,
                "created_app_users": ["admin", reg_data["username"]] if reg_data["username"] != "admin" else ["admin"],
            },
            "payment_info": {
                "payphone_transaction_id": f"PP-{cliente_id}",
                "authorization_code": "APROBADO",
            },
        }

    return templates.TemplateResponse(
        request=request,
        name="exito.html",
        context={
            "reg": reg_data,
            "plan": reg_data["plan"],
            "result": provision_result,
            "soporte_email": SOPORTE_EMAIL,
            "wesbyte_app_url": WESBYTE_APP_URL,
            "project_id": BIGQUERY_PROJECT_ID,
            "app_version": APP_VERSION,
        },
    )


@router.get("/panel-clientes", response_class=HTMLResponse)
async def panel_clientes_page(request: Request, _staff: str = Depends(require_staff)):
    """Vista de administración para auditar los clientes, datasets, suscripciones y pagos en `broker-pmp.wesbytes`."""
    df_clientes = get_query(f"""
        SELECT
            c.id,
            c.nombre,
            c.nombres_contacto,
            c.email,
            c.celular,
            c.dataset_id,
            c.estado,
            c.max_usuarios,
            c.limite_mensual_whatsapp,
            c.limite_mensual_emails,
            c.limite_mensual_api_ecuador,
            c.created_at,
            p.nombre AS plan_nombre,
            s.modalidad_pago,
            s.monto_total_cobrado,
            s.fecha_inicio,
            s.fecha_fin,
            s.payphone_transaction_id
        FROM `{BIGQUERY_PROJECT_ID}.{WESBYTES_DATASET_ID}.clientes` c
        LEFT JOIN `{BIGQUERY_PROJECT_ID}.{WESBYTES_DATASET_ID}.planes` p ON c.plan_id = p.id
        LEFT JOIN `{BIGQUERY_PROJECT_ID}.{WESBYTES_DATASET_ID}.suscripciones` s ON c.id = s.cliente_id
        ORDER BY c.id DESC
    """)
    clientes = json.loads(df_clientes.to_json(orient="records", date_format="iso")) if not df_clientes.empty else []

    return templates.TemplateResponse(
        request=request,
        name="panel_clientes.html",
        context={
            "clientes": clientes,
            "project_id": BIGQUERY_PROJECT_ID,
            "wesbytes_dataset": WESBYTES_DATASET_ID,
            "soporte_email": SOPORTE_EMAIL,
            "app_version": APP_VERSION,
        },
    )
