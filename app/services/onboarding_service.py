import json
import datetime
import logging
from decimal import Decimal
from typing import Dict, Any, Optional

from app.config import (
    BIGQUERY_PROJECT_ID,
    WESBYTES_DATASET_ID,
    PLANES_OFICIALES,
    DEMO_PLAN_CONFIG,
    ECUADOR_TZ,
    calcular_montos_periodo,
    calcular_plan_personalizado,
    normalizar_periodo,
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
    y lo registra en `wesbyte.administracion.planes` si es personalizado.
    """
    code_upper = (plan_codigo or "SUPER_GOLD").strip().upper()
    if code_upper == "DEMO":
        return DEMO_PLAN_CONFIG.copy()
    for p in PLANES_OFICIALES:
        if p["codigo"] == code_upper or str(p.get("slug", "")).upper() == code_upper or str(p["id"]) == code_upper:
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
        "precio_mensual": calc["precio_mensual"],
        "precio_anual": calc["precio_anual"],
        "descuento_anual_pct": calc["descuento_anual_pct"],
        "precio_anual_con_descuento": calc["precio_anual_con_descuento"],
        "ahorro_anual": calc["ahorro_anual"],
        "equivalente_mensual_anual": calc["equivalente_mensual_anual"],
        "periodos": {
            "1_year": calcular_montos_periodo(calc["precio_anual"], "1_year"),
            "2_years": calcular_montos_periodo(calc["precio_anual"], "2_years"),
            "3_years": calcular_montos_periodo(calc["precio_anual"], "3_years"),
        },
        "es_destacado": False,
        "es_personalizado": True,
    }


def register_tenant_and_user(payload: Dict[str, Any]) -> Dict[str, Any]:
    """
    Registra al cliente en `wesbyte.administracion.clientes` (en estado 'pendiente_pago')
    y registra su usuario administrador en `wesbyte.administracion.usuarios`.
    """
    nombre_empresa = str(payload.get("nombre_empresa", "")).strip()
    nombres_completos = str(payload.get("nombres_completos", "")).strip()
    correo = str(payload.get("correo", "")).strip().lower()
    celular = str(payload.get("celular", "")).strip()
    password = str(payload.get("password", "")).strip()
    username = correo
    ruc_cedula = str(payload.get("ruc_cedula", "")).strip()
    ciudad = str(payload.get("ciudad", "Quito, Ecuador")).strip()
    requiere_nda_personalizado = bool(payload.get("requiere_nda_personalizado", False))
    periodo = normalizar_periodo(payload.get("periodo"))
    modalidad_preferida = f"pago_unico_{periodo}"

    if not nombre_empresa or not nombres_completos or not correo or not celular or not password:
        raise ValueError("Todos los campos principales (Empresa, Nombres, Correo, Celular y Contraseña) son obligatorios.")

    dataset_id = normalize_dataset_name(nombre_empresa)
    # Proteger datasets reservados del sistema
    if dataset_id in ("administracion", "wesbytes", "broker_pmp", "prototipo"):
        dataset_id = f"{dataset_id}_empresa"

    plan_codigo = str(payload.get("plan_codigo") or payload.get("plan") or "SUPER_GOLD")
    es_demo = plan_codigo.strip().upper() == "DEMO" or bool(payload.get("es_demo", False))
    if es_demo:
        plan_codigo = "DEMO"
    custom_params = payload.get("custom_params")
    plan = get_plan_by_code_or_id(plan_codigo, custom_params)

    cliente_id = get_next_id("clientes", WESBYTES_DATASET_ID)
    usuario_admin_id = get_next_id("usuarios", WESBYTES_DATASET_ID)
    pw_hash = hash_password(password)
    now_ec = datetime.datetime.now(ECUADOR_TZ)
    now_ts = now_ec.astimezone(datetime.timezone.utc)
    fecha_fin_demo = (now_ec + datetime.timedelta(days=5)).astimezone(datetime.timezone.utc) if es_demo else None

    # 1. Insertar en wesbyte.administracion.clientes
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
        "estado": "activo" if es_demo else "pendiente_pago",
        "max_usuarios": 1 if es_demo else int(plan["max_usuarios"]),
        "limite_mensual_emails": int(plan["limite_mensual_emails"]),
        "limite_mensual_whatsapp": int(plan["limite_mensual_whatsapp"]),
        "limite_mensual_api_ecuador": int(plan["limite_mensual_api_ecuador"]),
        "acuerdo_confidencialidad_aceptado": True,
        "fecha_acuerdo_confidencialidad": now_ts,
        "requiere_nda_personalizado": requiere_nda_personalizado,
        "es_demo": es_demo,
        "fecha_fin_demo": fecha_fin_demo,
        "activo": True,
        "created_at": now_ts,
    }
    append_rows(WESBYTES_DATASET_ID, "clientes", [cliente_row])

    # 2. Insertar en wesbyte.administracion.usuarios (el usuario de ingreso es el correo electrónico)
    usuarios_rows = [
        {
            "id": usuario_admin_id,
            "cliente_id": cliente_id,
            "username": correo,
            "nombre": nombres_completos,
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
    append_rows(WESBYTES_DATASET_ID, "usuarios", usuarios_rows)

    if es_demo:
        try:
            clone_broker_pmp_dataset_empty(dataset_id)
        except Exception as exc:
            logger.warning(f"Error clonando dataset demo {dataset_id}: {exc}")

    return {
        "cliente_id": cliente_id,
        "es_demo": es_demo,
        "fecha_fin_demo": fecha_fin_demo.isoformat() if fecha_fin_demo else None,
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
        "periodo": periodo,
        "modalidad_preferida": modalidad_preferida,
        "requiere_nda_personalizado": requiere_nda_personalizado,
    }


def registrar_intento_pago_payphone(
    cliente_id: int,
    plan_id: int,
    modalidad_pago: str,
    client_transaction_id: str,
    monto_usd: float,
    metadata: Optional[Dict[str, Any]] = None,
) -> int:
    """Registra una intención de pago pendiente en wesbyte.administracion.pagos_payphone."""
    now_ts = datetime.datetime.now(datetime.timezone.utc)
    pago_id = get_next_id("pagos_payphone", WESBYTES_DATASET_ID)
    pago_row = {
        "id": pago_id,
        "cliente_id": int(cliente_id),
        "suscripcion_id": None,
        "plan_id": int(plan_id),
        "modalidad_pago": str(modalidad_pago),
        "client_transaction_id": str(client_transaction_id),
        "payphone_transaction_id": None,
        "authorization_code": None,
        "monto_centavos": int(round(float(monto_usd) * 100)),
        "monto_usd": Decimal(f"{float(monto_usd):.2f}"),
        "estado_pago": "pending",
        "metodo_pago": "PAYPHONE_BOX",
        "respuesta_raw_json": json.dumps(metadata or {}, ensure_ascii=False),
        "created_at": now_ts,
        "confirmed_at": None,
    }
    append_rows(WESBYTES_DATASET_ID, "pagos_payphone", [pago_row])
    return pago_id


def buscar_intento_pago_payphone(client_transaction_id: str) -> Optional[Dict[str, Any]]:
    """Busca un intento de pago en wesbyte.administracion.pagos_payphone por su client_transaction_id."""
    clean_tx = str(client_transaction_id or "").strip().replace("'", "''")
    if not clean_tx:
        return None
    df = get_query(f"""
        SELECT id, cliente_id, plan_id, modalidad_pago, monto_usd, estado_pago, respuesta_raw_json
        FROM `{BIGQUERY_PROJECT_ID}.{WESBYTES_DATASET_ID}.pagos_payphone`
        WHERE client_transaction_id = '{clean_tx}'
        ORDER BY id DESC
        LIMIT 1
    """)
    if df.empty:
        return None
    return df.iloc[0].to_dict()


def completar_upgrade_payphone(
    cliente_id: int,
    intento: Dict[str, Any],
    payment_info: Dict[str, Any],
) -> Dict[str, Any]:
    """Confirma un pago de upgrade de plan o usuarios adicionales y actualiza límites en BigQuery."""
    from app.config import PLANES_OFICIALES

    client_tx = str(payment_info.get("client_transaction_id") or "").replace("'", "''")
    pp_tx_id = str(payment_info.get("payphone_transaction_id") or "").replace("'", "''")
    auth_code = str(payment_info.get("authorization_code") or "").replace("'", "''")
    raw_meta = {}
    try:
        raw_meta = json.loads(intento.get("respuesta_raw_json") or "{}")
    except Exception:
        raw_meta = {}

    raw_full = json.dumps(
        {"intento": raw_meta, "payphone": payment_info.get("raw") or {}},
        ensure_ascii=False,
    ).replace("'", "''")

    execute_query(f"""
        UPDATE `{BIGQUERY_PROJECT_ID}.{WESBYTES_DATASET_ID}.pagos_payphone`
        SET estado_pago = 'APPROVED',
            payphone_transaction_id = '{pp_tx_id}',
            authorization_code = '{auth_code}',
            respuesta_raw_json = '{raw_full}',
            confirmed_at = CURRENT_TIMESTAMP()
        WHERE client_transaction_id = '{client_tx}'
    """)

    modalidad = str(intento.get("modalidad_pago") or "")
    monto_pagado = float(intento.get("monto_usd") or 0.0)

    if modalidad == "upgrade_plan":
        nuevo_codigo = str(raw_meta.get("nuevo_plan_codigo") or "").strip().upper()
        nuevo_plan = next((p for p in PLANES_OFICIALES if p["codigo"] == nuevo_codigo), None)
        if not nuevo_plan:
            pid = int(intento.get("plan_id") or 0)
            nuevo_plan = next((p for p in PLANES_OFICIALES if p["id"] == pid), None)
        if nuevo_plan:
            execute_query(f"""
                UPDATE `{BIGQUERY_PROJECT_ID}.{WESBYTES_DATASET_ID}.clientes`
                SET plan_id = {nuevo_plan['id']},
                    max_usuarios = {nuevo_plan['max_usuarios']},
                    limite_mensual_whatsapp = {nuevo_plan['limite_mensual_whatsapp']},
                    limite_mensual_emails = {nuevo_plan['limite_mensual_emails']},
                    limite_mensual_api_ecuador = {nuevo_plan['limite_mensual_api_ecuador']}
                WHERE id = {int(cliente_id)}
            """)
            execute_query(f"""
                UPDATE `{BIGQUERY_PROJECT_ID}.{WESBYTES_DATASET_ID}.suscripciones`
                SET plan_id = {nuevo_plan['id']},
                    monto_total_cobrado = IFNULL(monto_total_cobrado, 0) + NUMERIC '{monto_pagado:.2f}',
                    updated_at = CURRENT_TIMESTAMP()
                WHERE cliente_id = {int(cliente_id)} AND estado IN ('activa', 'por_vencer')
            """)
    elif modalidad == "mejora_usuarios":
        adicionales = max(1, int(raw_meta.get("usuarios_adicionales") or 1))
        execute_query(f"""
            UPDATE `{BIGQUERY_PROJECT_ID}.{WESBYTES_DATASET_ID}.clientes`
            SET max_usuarios = IFNULL(max_usuarios, 1) + {adicionales}
            WHERE id = {int(cliente_id)}
        """)
        execute_query(f"""
            UPDATE `{BIGQUERY_PROJECT_ID}.{WESBYTES_DATASET_ID}.suscripciones`
            SET monto_total_cobrado = IFNULL(monto_total_cobrado, 0) + NUMERIC '{monto_pagado:.2f}',
                updated_at = CURRENT_TIMESTAMP()
            WHERE cliente_id = {int(cliente_id)} AND estado IN ('activa', 'por_vencer')
        """)

    return {"success": True, "modalidad": modalidad, "monto_pagado": monto_pagado}


def marcar_intento_pago_rechazado(
    client_transaction_id: str,
    payphone_transaction_id: Optional[str] = None,
    raw: Optional[Dict[str, Any]] = None,
) -> None:
    """Marca un intento de pago como REJECTED en wesbyte.administracion.pagos_payphone."""
    clean_tx = str(client_transaction_id or "").strip().replace("'", "''")
    if not clean_tx:
        return
    clean_pp = str(payphone_transaction_id or "").replace("'", "''")
    clean_raw = json.dumps(raw or {}, ensure_ascii=False).replace("'", "''")
    execute_query(f"""
        UPDATE `{BIGQUERY_PROJECT_ID}.{WESBYTES_DATASET_ID}.pagos_payphone`
        SET estado_pago = 'REJECTED',
            payphone_transaction_id = '{clean_pp}',
            respuesta_raw_json = '{clean_raw}'
        WHERE client_transaction_id = '{clean_tx}'
    """)


def complete_payment_and_provision(
    registration_data: Dict[str, Any],
    modalidad_pago: str,
    payment_info: Dict[str, Any],
) -> Dict[str, Any]:
    """
    Una vez confirmado el pago único (1 Año, 2 Años -10% o 3 Años -15% con PayPhone):
    1. Registra la suscripción en `wesbyte.administracion.suscripciones`.
    2. Registra la transacción en `wesbyte.administracion.pagos_payphone`.
    3. Activa al cliente en `wesbyte.administracion.clientes`.
    4. Crea el dataset nuevo `wesbyte.<dataset_id>` y aprovisiona tablas, vistas y usuarios.
    5. Inicializa los contadores en `consumos_diarios` y `consumos_mensuales`.
    """
    cliente_id = int(registration_data["cliente_id"])
    plan = registration_data["plan"]
    plan_id = int(plan["id"])
    dataset_id = str(registration_data["dataset_id"])
    username = str(registration_data.get("username", "admin"))
    pw_hash = str(registration_data["password_hash"])

    raw_mod = str(modalidad_pago or registration_data.get("periodo") or "1_year").strip().lower()
    raw_period = raw_mod.replace("pago_unico_", "")
    if raw_period in ("anual_unico", ""):
        raw_period = "1_year"
    periodo = normalizar_periodo(raw_period)
    montos = calcular_montos_periodo(float(plan["precio_anual"]), periodo)

    monto_subtotal = float(montos["subtotal"])
    descuento_aplicado = float(montos["ahorro"])
    monto_total = float(montos["total"])
    modalidad_guardar = f"pago_unico_{periodo}"

    today = datetime.date.today()
    fecha_fin = today + datetime.timedelta(days=365 * int(montos["anios"]))
    fecha_proximo_cobro = fecha_fin
    now_ts = datetime.datetime.now(datetime.timezone.utc)

    # 1. Crear suscripción en wesbyte.administracion.suscripciones
    suscripcion_id = get_next_id("suscripciones", WESBYTES_DATASET_ID)
    suscripcion_row = {
        "id": suscripcion_id,
        "cliente_id": cliente_id,
        "plan_id": plan_id,
        "modalidad_pago": modalidad_guardar,
        "monto_subtotal": Decimal(f"{monto_subtotal:.2f}"),
        "descuento_aplicado": Decimal(f"{descuento_aplicado:.2f}"),
        "monto_total_cobrado": Decimal(f"{monto_total:.2f}"),
        "moneda": "USD",
        "fecha_inicio": today,
        "fecha_fin": fecha_fin,
        "fecha_proximo_cobro": fecha_proximo_cobro,
        "renovacion_automatica": False,
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

    # 2. Registrar o actualizar el pago en wesbyte.administracion.pagos_payphone
    tx_id = str(payment_info.get("client_transaction_id", "")).strip()
    pp_tx_id = str(payment_info.get("payphone_transaction_id", "")).strip()
    auth_code = str(payment_info.get("authorization_code", "AUTH-OK")).strip()
    metodo_pago = str(payment_info.get("metodo_pago", "PAYPHONE")).strip()
    raw_json = json.dumps(payment_info.get("raw", {}), ensure_ascii=False)

    existing_pago = None
    if tx_id:
        clean_tx = tx_id.replace("'", "''")
        existing_pago = get_query(f"""
            SELECT id FROM `{BIGQUERY_PROJECT_ID}.{WESBYTES_DATASET_ID}.pagos_payphone`
            WHERE client_transaction_id = '{clean_tx}'
            LIMIT 1
        """)

    if existing_pago is not None and not existing_pago.empty:
        pago_id = int(existing_pago.iloc[0]["id"])
        clean_pp = pp_tx_id.replace("'", "''")
        clean_auth = auth_code.replace("'", "''")
        clean_met = metodo_pago.replace("'", "''")
        clean_raw = raw_json.replace("'", "''")
        execute_query(f"""
            UPDATE `{BIGQUERY_PROJECT_ID}.{WESBYTES_DATASET_ID}.pagos_payphone`
            SET suscripcion_id = {suscripcion_id},
                plan_id = {plan_id},
                modalidad_pago = '{modalidad_pago.replace("'", "''")}',
                payphone_transaction_id = '{clean_pp}',
                authorization_code = '{clean_auth}',
                monto_centavos = {int(round(monto_total * 100))},
                monto_usd = NUMERIC '{monto_total:.2f}',
                estado_pago = 'APPROVED',
                metodo_pago = '{clean_met}',
                respuesta_raw_json = '{clean_raw}',
                confirmed_at = CURRENT_TIMESTAMP()
            WHERE id = {pago_id}
        """)
    else:
        pago_id = get_next_id("pagos_payphone", WESBYTES_DATASET_ID)
        pago_row = {
            "id": pago_id,
            "cliente_id": cliente_id,
            "suscripcion_id": suscripcion_id,
            "plan_id": plan_id,
            "modalidad_pago": modalidad_pago,
            "client_transaction_id": tx_id,
            "payphone_transaction_id": pp_tx_id,
            "authorization_code": auth_code,
            "monto_centavos": int(round(monto_total * 100)),
            "monto_usd": Decimal(f"{monto_total:.2f}"),
            "estado_pago": "APPROVED",
            "metodo_pago": metodo_pago,
            "respuesta_raw_json": raw_json,
            "created_at": now_ts,
            "confirmed_at": now_ts,
        }
        append_rows(WESBYTES_DATASET_ID, "pagos_payphone", [pago_row])

    # 3. Actualizar estado del cliente a 'activo' en wesbyte.administracion.clientes
    try:
        execute_query(f"""
            UPDATE `{BIGQUERY_PROJECT_ID}.{WESBYTES_DATASET_ID}.clientes`
            SET estado = 'activo', activo = TRUE, es_demo = FALSE
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

    # 4b. Registrar al cliente como administrador en wesbyte.administracion.usuarios_app (usuario = correo)
    permisos_all = "registros_medicos,registros_vehiculares,registros_vida_e_inversion,registros_ramos_generales"
    correo_admin = str(registration_data.get("correo") or username or "").strip().lower()
    correo_sql = correo_admin.replace("'", "''")
    existing_ua = get_query(
        f"""
        SELECT id
        FROM `{BIGQUERY_PROJECT_ID}.{WESBYTES_DATASET_ID}.usuarios_app`
        WHERE cliente_id = {cliente_id}
          AND (LOWER(TRIM(email)) = '{correo_sql}' OR LOWER(TRIM(username)) = '{correo_sql}')
        LIMIT 1
        """
    )
    if existing_ua.empty:
        usuario_app_id = get_next_id("usuarios_app", WESBYTES_DATASET_ID)
        usuarios_app_rows = [
            {
                "id": usuario_app_id,
                "cliente_id": cliente_id,
                "username": correo_admin,
                "nombre_completo": str(registration_data.get("nombres_completos") or correo_admin),
                "email": correo_admin,
                "celular": str(registration_data.get("celular") or ""),
                "password_hash": pw_hash,
                "permiso": "administrador",
                "dataset_id": dataset_id,
                "activo": True,
                "permisos_seguros": permisos_all,
                "permisos_paneles": "cargos,reportes,renovaciones,nuevos_negocios,estadisticas,administracion",
                "ultimo_login": None,
                "login_count": 0,
                "created_at": now_ts,
                "updated_at": now_ts,
            }
        ]
        append_rows(WESBYTES_DATASET_ID, "usuarios_app", usuarios_app_rows)

    # 5. Clonar dataset prototipo -> wesbyte.<dataset_id> con tablas vacías + usuarios en maestra_usuarios_app
    provisioning_result = clone_broker_pmp_dataset_empty(
        target_dataset_id=dataset_id,
        admin_username=correo_admin,
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
