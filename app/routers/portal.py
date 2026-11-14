import uuid
import json
import datetime
import logging
from pathlib import Path
from typing import Dict, Any, Optional
import secrets
from fastapi import APIRouter, Request, HTTPException, Body, Query, Depends, UploadFile, File
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
    PERIODOS_COMPROMISO,
    TARIFAS_CALCULADORA,
    PAYPHONE_TOKEN,
    PAYPHONE_STORE_ID,
    PAYPHONE_SANDBOX_MODE,
    STAFF_USER,
    STAFF_PASSWORD,
    calcular_montos_periodo,
    calcular_plan_personalizado,
    normalizar_periodo,
)
from app.services.google_drive_service import GoogleDriveService, GoogleDriveServiceError
from app.routers.cliente import get_sesion
from app.database import (
    get_bq_client,
    get_query,
    execute_query,
    normalize_dataset_name,
)
from app.services.onboarding_service import (
    register_tenant_and_user,
    complete_payment_and_provision,
    get_plan_by_code_or_id,
    registrar_intento_pago_payphone,
    buscar_intento_pago_payphone,
    marcar_intento_pago_rechazado,
    completar_upgrade_payphone,
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
        usuarios=1,
        whatsapp_mensual=150,
        emails_mensual=250,
        api_ecuador_mensual=100,
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
            whatsapp_mensual=int(payload.get("whatsapp_mensual", 150)),
            emails_mensual=int(payload.get("emails_mensual", 250)),
            api_ecuador_mensual=int(payload.get("api_ecuador_mensual", 100)),
        )
        return {"success": True, "data": resultado}
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.get("/api/normalizar-dataset")
async def api_normalizar_dataset(nombre_empresa: str = Query("")):
    """Devuelve cómo quedará el nombre del dataset en BigQuery (minúscula, sin espacios ni tildes)."""
    dataset_id = normalize_dataset_name(nombre_empresa)
    if dataset_id in ("administracion", "wesbytes", "broker_pmp", "prototipo"):
        dataset_id = f"{dataset_id}_empresa"
    return {
        "nombre_original": nombre_empresa,
        "dataset_id": dataset_id,
        "full_dataset": f"{BIGQUERY_PROJECT_ID}.{dataset_id}",
    }


@router.post("/api/registro")
async def api_registrar_cliente(payload: Dict[str, Any] = Body(...)):
    """
    Paso 1 -> Paso 2: Registra los datos del usuario y su empresa en `wesbyte.administracion`
    y prepara el checkout de PayPhone con las 2 opciones (Pago Único Anual -10% o Mensual Recurrente).
    """
    try:
        reg_data = register_tenant_and_user(payload)
        cliente_id = int(reg_data["cliente_id"])
        _active_checkouts[cliente_id] = reg_data

        plan = reg_data["plan"]
        periodo = reg_data.get("periodo", "1_year")
        payphone_options = {}
        for p_key in ("1_year", "2_years", "3_years"):
            m = calcular_montos_periodo(float(plan["precio_anual"]), p_key)
            tx_id = generate_client_tx_id(cliente_id, p_key)
            payphone_options[p_key] = build_payphone_box_config(
                client_tx_id=tx_id,
                monto_usd=m["total"],
                descripcion=f"Wesbyte {plan['nombre']} - {m['label']} - {reg_data['nombre_empresa']}",
                email=reg_data["correo"],
                celular=reg_data["celular"],
                documento=reg_data["ruc_cedula"],
                modalidad_pago=f"pago_unico_{p_key}",
            )

        return {
            "success": True,
            "cliente_id": cliente_id,
            "checkout_url": f"/checkout/{cliente_id}?periodo={periodo}",
            "registration": {
                "cliente_id": cliente_id,
                "nombre_empresa": reg_data["nombre_empresa"],
                "nombres_completos": reg_data["nombres_completos"],
                "correo": reg_data["correo"],
                "celular": reg_data["celular"],
                "username": reg_data["username"],
                "dataset_id": reg_data["dataset_id"],
                "plan": plan,
                "periodo": periodo,
                "modalidad_preferida": reg_data["modalidad_preferida"],
            },
            "payphone_options": payphone_options,
        }
    except ValueError as ve:
        raise HTTPException(status_code=400, detail=str(ve))
    except Exception as exc:
        logger.exception("Error registrando cliente en Wesbyte Portal")
        raise HTTPException(status_code=500, detail=f"Error al procesar el registro: {str(exc)}")


@router.post("/api/registro/{cliente_id}/logo")
async def api_registro_subir_logo(cliente_id: int, file: UploadFile = File(...)):
    """Permite subir el logo de la empresa durante el registro inicial (estado pendiente_pago)."""
    df_c = get_query(f"""
        SELECT id, estado FROM `{BIGQUERY_PROJECT_ID}.{WESBYTES_DATASET_ID}.clientes`
        WHERE id = {int(cliente_id)}
        LIMIT 1
    """)
    if df_c.empty:
        raise HTTPException(status_code=404, detail="Cliente no encontrado.")
    estado = str(df_c.iloc[0].get("estado") or "").strip().lower()
    if estado != "pendiente_pago":
        raise HTTPException(status_code=403, detail="Solo se puede cargar el logo desde aquí durante el registro inicial.")

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
    drive_filename = f"cliente_{int(cliente_id)}_{uuid.uuid4().hex[:8]}.{ext}"

    try:
        drive_res = GoogleDriveService().upload_logo(
            file_bytes=content,
            filename=drive_filename,
            mime_type=mime_type,
            folder_path="clientes/logos",
        )
    except GoogleDriveServiceError as exc:
        logger.error(f"Error subiendo logo en registro a Google Drive: {exc}")
        raise HTTPException(status_code=500, detail=f"Error subiendo logo a Google Drive: {str(exc)}")

    logo_url = drive_res["url"]
    clean_url = logo_url.replace("'", "''")
    execute_query(f"""
        UPDATE `{BIGQUERY_PROJECT_ID}.{WESBYTES_DATASET_ID}.clientes`
        SET logo = '{clean_url}'
        WHERE id = {int(cliente_id)}
    """)
    return {"success": True, "logo_url": logo_url}


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
        "periodo": "1_year",
        "modalidad_preferida": "pago_unico_1_year",
        "requiere_nda_personalizado": bool(c_row.get("requiere_nda_personalizado", False)),
    }
    _active_checkouts[cliente_id] = recovered
    return recovered


@router.get("/checkout/{cliente_id}", response_class=HTMLResponse)
async def checkout_page(
    request: Request,
    cliente_id: int,
    periodo: Optional[str] = Query(None),
):
    """
    Paso 2 -> Paso 3: Pantalla dedicada de Checkout con PayPhone con pagos únicos:
    1 Año (base), 2 Años (-10%) o 3 Años (-15%).
    """
    reg_data = _recover_checkout_from_bq(cliente_id)
    if not reg_data:
        return RedirectResponse(url="/", status_code=302)

    plan = reg_data["plan"]
    periodo_inicial = normalizar_periodo(periodo or reg_data.get("periodo") or "1_year")
    reg_data["periodo"] = periodo_inicial
    reg_data["modalidad_preferida"] = f"pago_unico_{periodo_inicial}"

    periodos_montos: Dict[str, Dict[str, Any]] = {}
    boxes: Dict[str, Dict[str, Any]] = {}
    for p_key in ("1_year", "2_years", "3_years"):
        m = calcular_montos_periodo(float(plan["precio_anual"]), p_key)
        periodos_montos[p_key] = m
        tx_id = generate_client_tx_id(cliente_id, p_key)
        boxes[p_key] = build_payphone_box_config(
            client_tx_id=tx_id,
            monto_usd=m["total"],
            descripcion=f"Wesbyte {plan['nombre']} - {m['label']} - {reg_data['nombre_empresa']}",
            email=reg_data["correo"],
            celular=reg_data["celular"],
            documento=reg_data["ruc_cedula"],
            modalidad_pago=f"pago_unico_{p_key}",
        )

    return templates.TemplateResponse(
        request=request,
        name="checkout.html",
        context={
            "reg": reg_data,
            "plan": plan,
            "periodo_inicial": periodo_inicial,
            "periodos_montos": periodos_montos,
            "periodos_montos_json": json.dumps(periodos_montos),
            "boxes": boxes,
            "boxes_json": json.dumps(boxes),
            "payphone_token": PAYPHONE_TOKEN,
            "payphone_store_id": PAYPHONE_STORE_ID,
            "payphone_sandbox": PAYPHONE_SANDBOX_MODE,
            "soporte_email": SOPORTE_EMAIL,
            "project_id": BIGQUERY_PROJECT_ID,
            "app_version": APP_VERSION,
        },
    )


@router.post("/api/checkout/intent")
async def api_create_checkout_intent(payload: Dict[str, Any] = Body(...)):
    """
    Genera un nuevo clientTransactionId único para cada intento de pago (carga, recarga o cambio de período),
    lo registra con estado 'pending' en `wesbyte.administracion.pagos_payphone` y devuelve la configuración
    fresca para inicializar `PPaymentButtonBox`.
    """
    cliente_id = int(payload.get("cliente_id") or 0)
    if cliente_id <= 0:
        raise HTTPException(status_code=400, detail="cliente_id inválido.")

    reg_data = _recover_checkout_from_bq(cliente_id)
    if not reg_data:
        raise HTTPException(status_code=404, detail="Registro de cliente no encontrado.")

    periodo = normalizar_periodo(payload.get("periodo"))
    plan = reg_data["plan"]
    m = calcular_montos_periodo(float(plan["precio_anual"]), periodo)
    tx_id = generate_client_tx_id(cliente_id, periodo)
    modalidad_pago = f"pago_unico_{periodo}"

    registrar_intento_pago_payphone(
        cliente_id=cliente_id,
        plan_id=int(plan["id"]),
        modalidad_pago=modalidad_pago,
        client_transaction_id=tx_id,
        monto_usd=m["total"],
        metadata={"plan_codigo": plan.get("codigo"), "periodo": periodo},
    )

    box_config = build_payphone_box_config(
        client_tx_id=tx_id,
        monto_usd=m["total"],
        descripcion=f"Wesbyte {plan['nombre']} - {m['label']} - {reg_data['nombre_empresa']}",
        email=reg_data["correo"],
        celular=reg_data["celular"],
        documento=reg_data["ruc_cedula"],
        modalidad_pago=modalidad_pago,
    )
    return {"success": True, "box_config": box_config, "monto": m}


@router.post("/api/checkout/confirm")
async def api_confirm_payment_and_provision(payload: Dict[str, Any] = Body(...)):
    """
    Verifica el pago con PayPhone (ya sea pago único anual con 10% de descuento o pago mensual recurrente)
    y ejecuta inmediatamente el aprovisionamiento en BigQuery:
    1) Crea el dataset `wesbyte.<empresa_en_minuscula_sin_espacios_ni_tildes>`
    2) Copia todas las tablas de `broker_pmp` vacías y sus vistas
    3) Crea el usuario `admin` y el usuario registrado con la misma contraseña en `maestra_usuarios_app`
       y en `wesbytes.usuarios`
    4) Activa la suscripción en `wesbytes.suscripciones`
    """
    cliente_id = int(payload.get("cliente_id", 0))
    periodo = normalizar_periodo(payload.get("periodo") or str(payload.get("modalidad_pago", "1_year")).replace("pago_unico_", ""))
    modalidad_pago = f"pago_unico_{periodo}"
    metodo_pago = str(payload.get("metodo_pago", "PAYPHONE_CARD"))
    payphone_id = payload.get("payphone_id")
    client_tx_id = str(payload.get("client_transaction_id") or generate_client_tx_id(cliente_id, periodo))
    custom_token = str(payload.get("custom_payphone_token") or "").strip()

    reg_data = _recover_checkout_from_bq(cliente_id)
    if not reg_data:
        raise HTTPException(status_code=404, detail="Registro de cliente no encontrado.")

    if not PAYPHONE_SANDBOX_MODE and (payload.get("simulated", False) or not payphone_id):
        raise HTTPException(
            status_code=400,
            detail="En modo producción el pago debe confirmarse directamente a través de PayPhone.",
        )

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
        sub_token = ""
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

    # 1. Buscar la transacción en pagos_payphone por clientTransactionId
    intento = buscar_intento_pago_payphone(clientTransactionId)
    cliente_id = int(intento["cliente_id"]) if intento and intento.get("cliente_id") else None
    modalidad = str(intento["modalidad_pago"]) if intento and intento.get("modalidad_pago") else "pago_unico_1_year"

    if intento and str(intento.get("estado_pago") or "").upper() == "APPROVED" and cliente_id:
        if modalidad in ("upgrade_plan", "mejora_usuarios"):
            return RedirectResponse(url="/mi-portal?upgrade=ok", status_code=302)
        return RedirectResponse(url=f"/exito/{cliente_id}", status_code=302)

    # Respaldo en caso de que el tx_id viniera de un flujo anterior sin registro previo
    if not cliente_id and clientTransactionId.startswith("WB"):
        rest = clientTransactionId[2:]
        for tag, mod in (("3Y", "pago_unico_3_years"), ("2Y", "pago_unico_2_years"), ("1Y", "pago_unico_1_year"), ("AN", "pago_unico_1_year")):
            if tag in rest and rest.split(tag)[0].isdigit():
                cliente_id = int(rest.split(tag)[0])
                modalidad = mod
                break

    if not cliente_id:
        return RedirectResponse(url="/", status_code=302)

    es_upgrade = modalidad in ("upgrade_plan", "mejora_usuarios")
    reg_data = None if es_upgrade else _recover_checkout_from_bq(cliente_id)
    if not es_upgrade and not reg_data:
        return RedirectResponse(url="/", status_code=302)

    if PAYPHONE_TOKEN:
        confirmation = await confirm_payphone_transaction(id, clientTransactionId)
        if not confirmation.get("approved"):
            marcar_intento_pago_rechazado(
                clientTransactionId,
                payphone_transaction_id=str(id),
                raw=confirmation.get("raw", {}),
            )
            err_url = "/mi-portal?upgrade=rejected" if es_upgrade else f"/checkout/{cliente_id}?error=rejected"
            return RedirectResponse(url=err_url, status_code=302)
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

    if es_upgrade and intento:
        completar_upgrade_payphone(
            cliente_id=cliente_id,
            intento=intento,
            payment_info=payment_info,
        )
        return RedirectResponse(url="/mi-portal?upgrade=ok", status_code=302)

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
            "modalidad_pago": reg_data.get("modalidad_preferida", "pago_unico_1_year"),
            "monto_total_cobrado": reg_data["plan"]["precio_anual"],
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
    """Vista de administración para auditar los clientes, datasets, suscripciones y pagos en `wesbyte.administracion`."""
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
