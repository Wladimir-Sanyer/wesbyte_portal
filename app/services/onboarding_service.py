import json
import datetime
import logging
from typing import Dict, Any, Optional

from app.config import (
    BIGQUERY_PROJECT_ID,
    WESBYTES_DATASET_ID,
    PLANES_OFICIALES,
    calcular_plan_personalizado,
)
from app.database import (
    get_query,
    execute_query,
    append_rows,
    get_next_id,
    hash_password,
    normalize_dataset_name,
    clone_broker_pmp_dataset_empty,
)

logger = logging.getLogger("wesbyte_portal.onboarding")


def get_plan_by_code_or_id(
    plan_codigo: str,
    custom_params: Optional[Dict[str, int]] = None,
) -> Dict[str, Any]:
    """
    Obtiene el detalle del plan comercial seleccionado o calcula un Plan Personalizado
    y lo registra en `broker-pmp.wesbytes.planes` si es personalizado.
    """
    code_upper = (plan_codigo or "SUPER_GOLD").strip().upper()
    for p in PLANES_OFICIALES:
        if p["codigo"] == code_upper or str(p["id"]) == code_upper:
            return p.copy()

    # Plan Personalizado generado desde la calculadora
    params = custom_params or {}
    calc = calcular_plan_personalizado(
        usuarios=int(params.get("usuarios", 3)),
        whatsapp_mensual=int(params.get("whatsapp_mensual", 150)),
        emails_mensual=int(params.get("emails_mensual", 120)),
        api_ecuador_mensual=int(params.get("api_ecuador_mensual", 250)),
    )

    plan_id = get_next_id("planes", WESBYTES_DATASET_ID)
    if plan_id < 100:
        plan_id = 100 + plan_id

    now_str = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    nombre_custom = (
        f"Plan Personalizado ({calc['usuarios']} usr · {calc['limite_mensual_whatsapp']} WA · "
        f"{calc['limite_mensual_emails']} Email · {calc['limite_mensual_api_ecuador']} API)"
    )

    insert_sql = f"""
    INSERT INTO `{BIGQUERY_PROJECT_ID}.{WESBYTES_DATASET_ID}.planes` (
        id, codigo, nombre, subtitulo, rango_clientes_min, rango_clientes_max,
        rango_clientes_label, tipo_periodo, max_usuarios,
        limite_diario_emails, limite_diario_whatsapp, limite_diario_api_ecuador,
        limite_mensual_emails, limite_mensual_whatsapp, limite_mensual_api_ecuador,
        precio_mensual, precio_anual, descuento_anual_pct, precio_anual_con_descuento,
        es_destacado, es_personalizado, activo, created_at
    ) VALUES (
        {plan_id},
        'CUSTOM_{plan_id}',
        '{nombre_custom}',
        'Plan configurado a medida en la calculadora Wesbyte',
        20,
        500,
        'Configuración a medida',
        'anual',
        {calc['usuarios']},
        {calc['limite_diario_emails']},
        {calc['limite_diario_whatsapp']},
        {calc['limite_diario_api_ecuador']},
        {calc['limite_mensual_emails']},
        {calc['limite_mensual_whatsapp']},
        {calc['limite_mensual_api_ecuador']},
        NUMERIC '{calc['precio_mensual']:.2f}',
        NUMERIC '{calc['precio_anual']:.2f}',
        NUMERIC '{calc['descuento_anual_pct']:.2f}',
        NUMERIC '{calc['precio_anual_con_descuento']:.2f}',
        FALSE,
        TRUE,
        TRUE,
        TIMESTAMP('{now_str}')
    )
    """
    execute_query(insert_sql)

    return {
        "id": plan_id,
        "codigo": f"CUSTOM_{plan_id}",
        "nombre": "Plan Personalizado",
        "tier": "Custom",
        "badge": "A Medida",
        "color": "custom",
        "subtitulo": nombre_custom,
        "rango_clientes_label": "Personalizado a medida",
        "max_usuarios": calc["usuarios"],
        "limite_mensual_whatsapp": calc["limite_mensual_whatsapp"],
        "limite_mensual_emails": calc["limite_mensual_emails"],
        "limite_mensual_api_ecuador": calc["limite_mensual_api_ecuador"],
        "limite_diario_whatsapp": calc["limite_diario_whatsapp"],
        "limite_diario_emails": calc["limite_diario_emails"],
        "limite_diario_api_ecuador": calc["limite_diario_api_ecuador"],
        "precio_mensual": calc["precio_mensual"],
        "precio_anual": calc["precio_anual"],
        "descuento_anual_pct": calc["descuento_anual_pct"],
        "precio_anual_con_descuento": calc["precio_anual_con_descuento"],
        "ahorro_anual": calc["ahorro_anual"],
        "equivalente_mensual_anual": calc["equivalente_mensual_anual"],
        "es_destacado": False,
        "es_personalizado": True,
    }


def register_tenant_and_user(payload: Dict[str, Any]) -> Dict[str, Any]:
    """
    Registra al cliente en `broker-pmp.wesbytes.clientes` (en estado 'pendiente_pago')
    y registra su usuario administrador en `broker-pmp.wesbytes.usuarios`.
    """
    nombre_empresa = str(payload.get("nombre_empresa", "")).strip()
    nombres_completos = str(payload.get("nombres_completos", "")).strip()
    correo = str(payload.get("correo", "")).strip().lower()
    celular = str(payload.get("celular", "")).strip()
    password = str(payload.get("password", "")).strip()
    username = str(payload.get("username", "")).strip() or correo.split("@")[0]
    ruc_cedula = str(payload.get("ruc_cedula", "")).strip()
    ciudad = str(payload.get("ciudad", "Quito, Ecuador")).strip()
    requiere_nda_personalizado = bool(payload.get("requiere_nda_personalizado", False))
    modalidad_preferida = str(payload.get("modalidad_pago", "anual_unico")).strip()

    if not nombre_empresa or not nombres_completos or not correo or not celular or not password:
        raise ValueError("Todos los campos principales (Empresa, Nombres, Correo, Celular y Contraseña) son obligatorios.")

    dataset_id = normalize_dataset_name(nombre_empresa)
    # Proteger datasets reservados del sistema
    if dataset_id in ("wesbytes", "broker_pmp"):
        dataset_id = f"{dataset_id}_empresa"

    plan_codigo = str(payload.get("plan_codigo", "SUPER_GOLD"))
    custom_params = payload.get("custom_params")
    plan = get_plan_by_code_or_id(plan_codigo, custom_params)

    cliente_id = get_next_id("clientes", WESBYTES_DATASET_ID)
    usuario_admin_id = get_next_id("usuarios", WESBYTES_DATASET_ID)
    pw_hash = hash_password(password)
    now_ts = datetime.datetime.now(datetime.timezone.utc)

    # 1. Insertar en broker-pmp.wesbytes.clientes
    cliente_row = {
        "id": cliente_id,
        "plan_id": int(plan["id"]),
        "nombre": nombre_empresa,
        "nombres_contacto": nombres_completos,
        "ruc_cedula": ruc_cedula,
        "email": correo,
        "celular": celular,
        "ciudad": ciudad,
        "dataset_id": dataset_id,
        "proyecto": BIGQUERY_PROJECT_ID,
        "estado": "pendiente_pago",
        "max_usuarios": int(plan["max_usuarios"]),
        "limite_diario_emails": int(plan["limite_diario_emails"]),
        "limite_diario_whatsapp": int(plan["limite_diario_whatsapp"]),
        "limite_diario_api_ecuador": int(plan["limite_diario_api_ecuador"]),
        "limite_mensual_emails": int(plan["limite_mensual_emails"]),
        "limite_mensual_whatsapp": int(plan["limite_mensual_whatsapp"]),
        "limite_mensual_api_ecuador": int(plan["limite_mensual_api_ecuador"]),
        "acuerdo_confidencialidad_aceptado": True,
        "fecha_acuerdo_confidencialidad": now_ts,
        "requiere_nda_personalizado": requiere_nda_personalizado,
        "activo": True,
        "created_at": now_ts,
    }
    append_rows(WESBYTES_DATASET_ID, "clientes", [cliente_row])

    # 2. Insertar en broker-pmp.wesbytes.usuarios:
    # Siempre creamos el usuario 'admin' con la misma contraseña y además el usuario registrado si es distinto
    usuarios_rows = [
        {
            "id": usuario_admin_id,
            "cliente_id": cliente_id,
            "username": "admin",
            "nombre": f"{nombres_completos} (Admin)",
            "email": correo,
            "celular": celular,
            "password_hash": pw_hash,
            "rol": "admin",
            "dataset_id": dataset_id,
            "activo": True,
            "ultimo_login": None,
            "created_at": now_ts,
        }
    ]
    if username.lower() != "admin":
        usuarios_rows.append({
            "id": usuario_admin_id + 1,
            "cliente_id": cliente_id,
            "username": username,
            "nombre": nombres_completos,
            "email": correo,
            "celular": celular,
            "password_hash": pw_hash,
            "rol": "admin",
            "dataset_id": dataset_id,
            "activo": True,
            "ultimo_login": None,
            "created_at": now_ts,
        })
    append_rows(WESBYTES_DATASET_ID, "usuarios", usuarios_rows)

    return {
        "cliente_id": cliente_id,
        "usuario_id": usuario_admin_id,
        "nombre_empresa": nombre_empresa,
        "nombres_completos": nombres_completos,
        "correo": correo,
        "celular": celular,
        "ruc_cedula": ruc_cedula,
        "ciudad": ciudad,
        "username": username,
        "password_hash": pw_hash,
        "dataset_id": dataset_id,
        "plan": plan,
        "modalidad_preferida": modalidad_preferida,
        "requiere_nda_personalizado": requiere_nda_personalizado,
    }


def complete_payment_and_provision(
    registration_data: Dict[str, Any],
    modalidad_pago: str,
    payment_info: Dict[str, Any],
) -> Dict[str, Any]:
    """
    Una vez confirmado el pago (con PayPhone en modalidad 'anual_unico' con 10% descuento
    o 'mensual_recurrente'):
    1. Registra la suscripción en `broker-pmp.wesbytes.suscripciones`.
    2. Registra la transacción en `broker-pmp.wesbytes.pagos_payphone`.
    3. Activa al cliente en `broker-pmp.wesbytes.clientes`.
    4. Crea el dataset nuevo `broker-pmp.<dataset_id>` (minúscula, sin espacios ni tildes),
       copia todas las tablas de `broker_pmp` vacías, recrea las vistas y guarda en
       `maestra_usuarios_app` tanto el usuario `admin` como el usuario registrado con la misma contraseña.
    5. Inicializa los contadores en `consumos_diarios` y `consumos_mensuales`.
    """
    cliente_id = int(registration_data["cliente_id"])
    plan = registration_data["plan"]
    plan_id = int(plan["id"])
    dataset_id = str(registration_data["dataset_id"])
    username = str(registration_data.get("username", "admin"))
    pw_hash = str(registration_data["password_hash"])

    es_anual_unico = modalidad_pago == "anual_unico"
    if es_anual_unico:
        monto_subtotal = float(plan["precio_anual"])
        descuento_aplicado = float(plan["ahorro_anual"])
        monto_total = float(plan["precio_anual_con_descuento"])
    else:
        monto_subtotal = float(plan["precio_mensual"])
        descuento_aplicado = 0.00
        monto_total = float(plan["precio_mensual"])

    today = datetime.date.today()
    fecha_fin = today + datetime.timedelta(days=365)
    fecha_proximo_cobro = (today + datetime.timedelta(days=30)) if not es_anual_unico else fecha_fin
    now_ts = datetime.datetime.now(datetime.timezone.utc)

    # 1. Crear suscripción en broker-pmp.wesbytes.suscripciones
    suscripcion_id = get_next_id("suscripciones", WESBYTES_DATASET_ID)
    suscripcion_row = {
        "id": suscripcion_id,
        "cliente_id": cliente_id,
        "plan_id": plan_id,
        "modalidad_pago": modalidad_pago,
        "monto_subtotal": round(monto_subtotal, 2),
        "descuento_aplicado": round(descuento_aplicado, 2),
        "monto_total_cobrado": round(monto_total, 2),
        "moneda": "USD",
        "fecha_inicio": today,
        "fecha_fin": fecha_fin,
        "fecha_proximo_cobro": fecha_proximo_cobro,
        "renovacion_automatica": (not es_anual_unico),
        "desea_renovar": True,
        "motivo_no_renovacion": None,
        "estado": "activa",
        "payphone_transaction_id": str(payment_info.get("payphone_transaction_id", "")),
        "payphone_client_tx_id": str(payment_info.get("client_transaction_id", "")),
        "payphone_subscription_token": str(payment_info.get("subscription_token", "")),
        "notificacion_enviada_30d": False,
        "notificacion_enviada_15d": False,
        "notificacion_enviada_7d": False,
        "created_at": now_ts,
        "updated_at": now_ts,
    }
    append_rows(WESBYTES_DATASET_ID, "suscripciones", [suscripcion_row])

    # 2. Registrar el pago en broker-pmp.wesbytes.pagos_payphone
    pago_id = get_next_id("pagos_payphone", WESBYTES_DATASET_ID)
    pago_row = {
        "id": pago_id,
        "cliente_id": cliente_id,
        "suscripcion_id": suscripcion_id,
        "plan_id": plan_id,
        "modalidad_pago": modalidad_pago,
        "client_transaction_id": str(payment_info.get("client_transaction_id", "")),
        "payphone_transaction_id": str(payment_info.get("payphone_transaction_id", "")),
        "authorization_code": str(payment_info.get("authorization_code", "AUTH-OK")),
        "monto_centavos": int(round(monto_total * 100)),
        "monto_usd": round(monto_total, 2),
        "estado_pago": "APPROVED",
        "metodo_pago": str(payment_info.get("metodo_pago", "PAYPHONE")),
        "respuesta_raw_json": json.dumps(payment_info.get("raw", {}), ensure_ascii=False),
        "created_at": now_ts,
        "confirmed_at": now_ts,
    }
    append_rows(WESBYTES_DATASET_ID, "pagos_payphone", [pago_row])

    # 3. Actualizar estado del cliente a 'activo' en broker-pmp.wesbytes.clientes
    try:
        execute_query(f"""
            UPDATE `{BIGQUERY_PROJECT_ID}.{WESBYTES_DATASET_ID}.clientes`
            SET estado = 'activo', activo = TRUE
            WHERE id = {cliente_id}
        """)
    except Exception as exc:
        logger.warning(f"No se pudo ejecutar UPDATE en clientes (LoadJob reciente): {exc}")

    # 4. Inicializar consumos_diarios y consumos_mensuales para el nuevo cliente
    consumo_diario_id = get_next_id("consumos_diarios", WESBYTES_DATASET_ID)
    append_rows(
        WESBYTES_DATASET_ID,
        "consumos_diarios",
        [
            {
                "id": consumo_diario_id,
                "cliente_id": cliente_id,
                "fecha": today,
                "total_emails": 0,
                "total_whatsapp": 0,
                "total_api_ecuador": 0,
                "limite_alcanzado_email": False,
                "limite_alcanzado_whatsapp": False,
                "limite_alcanzado_api": False,
                "updated_at": now_ts,
            }
        ],
    )

    consumo_mensual_id = get_next_id("consumos_mensuales", WESBYTES_DATASET_ID)
    append_rows(
        WESBYTES_DATASET_ID,
        "consumos_mensuales",
        [
            {
                "id": consumo_mensual_id,
                "cliente_id": cliente_id,
                "periodo": today.strftime("%Y-%m"),
                "total_emails": 0,
                "total_whatsapp": 0,
                "total_api_ecuador": 0,
                "bloqueado_mensual_email": False,
                "bloqueado_mensual_whatsapp": False,
                "bloqueado_mensual_api": False,
                "updated_at": now_ts,
            }
        ],
    )

    # 5. Clonar dataset broker_pmp -> broker-pmp.<dataset_id> con tablas vacías + usuarios en maestra_usuarios_app
    provisioning_result = clone_broker_pmp_dataset_empty(
        target_dataset_id=dataset_id,
        admin_username=username,
        admin_password_plain="",
        admin_password_hashed=pw_hash,
    )

    return {
        "cliente_id": cliente_id,
        "suscripcion_id": suscripcion_id,
        "pago_id": pago_id,
        "modalidad_pago": modalidad_pago,
        "monto_subtotal": round(monto_subtotal, 2),
        "descuento_aplicado": round(descuento_aplicado, 2),
        "monto_total_cobrado": round(monto_total, 2),
        "fecha_inicio": today.isoformat(),
        "fecha_fin": fecha_fin.isoformat(),
        "fecha_proximo_cobro": fecha_proximo_cobro.isoformat(),
        "provisioning": provisioning_result,
        "payment_info": payment_info,
    }
