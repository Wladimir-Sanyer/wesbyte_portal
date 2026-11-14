"""
Servicio de configuración de notificaciones habilitadas y plantillas personalizadas
(WhatsApp y Correo Electrónico) por cliente en wesbyte.administracion.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from google.cloud import bigquery

from app.config import BIGQUERY_PROJECT_ID, WESBYTES_DATASET_ID
from app.database import get_bq_client

logger = logging.getLogger("wesbyte_portal.notificaciones_config")

PLANTILLA_VARIABLES: List[Dict[str, str]] = [
    {"key": "nombres_completos_razon_social", "label": "Nombres / Razón Social", "ejemplo": "María Fernanda López"},
    {"key": "cedula", "label": "Cédula", "ejemplo": "0912345678"},
    {"key": "numero_contrato", "label": "Número de contrato", "ejemplo": "POL-2026-00451"},
    {"key": "fecha_vigencia", "label": "Fecha de vigencia", "ejemplo": "15/12/2026"},
    {"key": "nombre_agente", "label": "Nombre del agente", "ejemplo": "Carlos Mendoza"},
    {"key": "broker", "label": "Broker", "ejemplo": "Seguros Alianza"},
    {"key": "aseguradora", "label": "Aseguradora", "ejemplo": "Equinoccial"},
    {"key": "ramos", "label": "Ramos", "ejemplo": "Vehículos / Vida"},
    {"key": "plan", "label": "Plan", "ejemplo": "Plan Super Gold"},
]

NOTIFICACIONES_CATALOGO: List[Dict[str, Any]] = [
    {
        "codigo": "proxima_renovacion",
        "orden": 1,
        "nombre": "Envío de Próxima Renovación",
        "modo": "1 mes antes automático",
        "es_automatico": True,
        "editable": True,
        "icono": "ti-calendar-time",
        "color": "indigo",
        "descripcion": (
            "Se envía automáticamente 1 mes antes de la fecha de vigencia para "
            "avisar al cliente que su póliza está próxima a renovar."
        ),
        "default_whatsapp": True,
        "default_email": True,
        "default_asunto_email": "Aviso de próxima renovación · Contrato {numero_contrato}",
        "default_mensaje_email": (
            "Estimado/a cliente (CI: {cedula}),\n\n"
            "Le informamos que su póliza {numero_contrato} del ramo {ramos} con la "
            "aseguradora {aseguradora} (Plan {plan}) tiene fecha de vigencia hasta el {fecha_vigencia}.\n\n"
            "Su agente {nombre_agente} de {broker} se pondrá en contacto para gestionar su renovación sin interrupciones."
        ),
        "default_mensaje_whatsapp": (
            "Hola, te saludamos de *{broker}*. Te recordamos que tu póliza *{numero_contrato}* "
            "({ramos} - {aseguradora}) vence el *{fecha_vigencia}*. "
            "Tu agente *{nombre_agente}* te ayudará con tu próxima renovación."
        ),
    },
    {
        "codigo": "regularizaciones",
        "orden": 2,
        "nombre": "Envío de Regularizaciones",
        "modo": "Acuerdo y pasos de regularización",
        "es_automatico": False,
        "editable": True,
        "icono": "ti-file-check",
        "color": "amber",
        "descripcion": (
            "Envía el acuerdo de renovación y los pasos de regularización para "
            "completar o normalizar la póliza del cliente."
        ),
        "default_whatsapp": True,
        "default_email": True,
        "default_asunto_email": "Acuerdo de renovación y pasos de regularización · {numero_contrato}",
        "default_mensaje_email": (
            "Estimado/a cliente (CI: {cedula}),\n\n"
            "Compartimos el acuerdo de renovación y los pasos de regularización para su contrato "
            "{numero_contrato} ({ramos} - {aseguradora}, vigencia {fecha_vigencia}).\n\n"
            "Para cualquier consulta, comuníquese con su agente {nombre_agente} ({broker})."
        ),
        "default_mensaje_whatsapp": (
            "Hola, te compartimos de *{broker}* el acuerdo de renovación y los pasos de regularización "
            "para tu contrato *{numero_contrato}* ({aseguradora} - {ramos}). "
            "Vigencia: *{fecha_vigencia}*. Agente: *{nombre_agente}*."
        ),
    },
    {
        "codigo": "envio_poliza",
        "orden": 3,
        "nombre": "Envío de Póliza",
        "modo": "Manual (requiere presionar push)",
        "es_automatico": False,
        "editable": True,
        "icono": "ti-file-certificate",
        "color": "teal",
        "descripcion": (
            "Envía la póliza emitida al cliente o agente cuando presionas el botón "
            "de envío manual en el portal."
        ),
        "default_whatsapp": True,
        "default_email": True,
        "default_asunto_email": "Entrega de Póliza {numero_contrato} · {aseguradora}",
        "default_mensaje_email": (
            "Estimado/a cliente (CI: {cedula}),\n\n"
            "Le hacemos entrega de su póliza {numero_contrato} correspondiente al ramo {ramos} "
            "con {aseguradora} (Plan {plan}), vigente hasta el {fecha_vigencia}.\n\n"
            "Atentamente,\n{nombre_agente} · {broker}"
        ),
        "default_mensaje_whatsapp": (
            "Hola, *{broker}* te hace entrega de tu póliza *{numero_contrato}* "
            "({aseguradora} - {ramos}, Plan {plan}) con vigencia *{fecha_vigencia}*. "
            "Atentamente, *{nombre_agente}*."
        ),
    },
    {
        "codigo": "cumpleanios",
        "orden": 4,
        "nombre": "Envío de Cumpleaños Automático",
        "modo": "Automático · No editable",
        "es_automatico": True,
        "editable": False,
        "icono": "ti-cake",
        "color": "pink",
        "descripcion": (
            "Felicitación automática que se envía a tus clientes el día de su cumpleaños "
            "con la plantilla oficial del sistema. No se puede editar."
        ),
        "default_whatsapp": False,
        "default_email": True,
        "default_asunto_email": "¡Feliz Cumpleaños de parte de {broker}!",
        "default_mensaje_email": (
            "Plantilla automática oficial del sistema: se envía con el diseño corporativo "
            "y el logo de tu empresa el día del cumpleaños de cada cliente."
        ),
        "default_mensaje_whatsapp": (
            "Mensaje automático oficial del sistema enviado el día del cumpleaños de cada cliente."
        ),
    },
    {
        "codigo": "condiciones_renovacion",
        "orden": 5,
        "nombre": "Enviar Condiciones de Renovación",
        "modo": "Manual (requiere presionar push)",
        "es_automatico": False,
        "editable": True,
        "icono": "ti-list-check",
        "color": "violet",
        "descripcion": (
            "Envía las condiciones propuestas para la renovación de la póliza "
            "cuando presionas el botón de envío manual."
        ),
        "default_whatsapp": True,
        "default_email": True,
        "default_asunto_email": "Condiciones de renovación · Póliza {numero_contrato}",
        "default_mensaje_email": (
            "Estimado/a cliente (CI: {cedula}),\n\n"
            "Le compartimos las condiciones de renovación para su póliza {numero_contrato} "
            "({ramos} - {aseguradora}, Plan {plan}) con fecha de vigencia {fecha_vigencia}.\n\n"
            "Quedamos atentos a su confirmación.\n{nombre_agente} · {broker}"
        ),
        "default_mensaje_whatsapp": (
            "Hola, te enviamos desde *{broker}* las condiciones de renovación de tu póliza "
            "*{numero_contrato}* ({aseguradora} - {ramos}, vigencia {fecha_vigencia}). "
            "Escríbenos si deseas confirmar con tu agente *{nombre_agente}*."
        ),
    },
]

CATALOGO_POR_CODIGO: Dict[str, Dict[str, Any]] = {
    item["codigo"]: item for item in NOTIFICACIONES_CATALOGO
}


def _bq_rows(sql: str, params: Optional[List[bigquery.ScalarQueryParameter]] = None) -> List[Dict[str, Any]]:
    cfg = bigquery.QueryJobConfig(query_parameters=params) if params else None
    return [dict(r) for r in get_bq_client().query(sql, job_config=cfg).result()]


def _bq_exec(sql: str, params: Optional[List[bigquery.ScalarQueryParameter]] = None) -> None:
    cfg = bigquery.QueryJobConfig(query_parameters=params) if params else None
    get_bq_client().query(sql, job_config=cfg).result()


def get_cliente_notificaciones_y_plantillas(
    client_id: int,
    plan_id: str = "",
    plan_codigo: str = "",
) -> Dict[str, Any]:
    """
    Retorna la configuración unificada de las 5 notificaciones para el cliente:
    - habilitación por canal (whatsapp_habilitado / email_habilitado)
    - plantillas personalizadas (asunto y mensaje para email, mensaje para whatsapp)
    - catálogo de variables disponibles
    """
    P = BIGQUERY_PROJECT_ID
    W = WESBYTES_DATASET_ID
    cp = [bigquery.ScalarQueryParameter("cid", "INT64", int(client_id))]

    # Fallback de legacy configuracion_planes_notificaciones (para clientes existentes)
    legacy_flags: Dict[str, bool] = {}
    try:
        rows_leg = _bq_rows(
            f"""
            SELECT correo_cumpleanios_activo, correo_renovaciones_activo,
                   whatsapp_cumpleanios_activo, whatsapp_renovaciones_activo
            FROM `{P}.{W}.configuracion_planes_notificaciones`
            WHERE client_id = @cid
            LIMIT 1
            """,
            cp,
        )
        if not rows_leg and (plan_id or plan_codigo):
            rows_leg = _bq_rows(
                f"""
                SELECT correo_cumpleanios_activo, correo_renovaciones_activo,
                       whatsapp_cumpleanios_activo, whatsapp_renovaciones_activo
                FROM `{P}.{W}.configuracion_planes_notificaciones`
                WHERE client_id IS NULL AND (id_plan = @pid OR id_plan = @pcod)
                LIMIT 1
                """,
                [
                    bigquery.ScalarQueryParameter("pid", "STRING", str(plan_id or "")),
                    bigquery.ScalarQueryParameter("pcod", "STRING", str(plan_codigo or "")),
                ],
            )
        if rows_leg:
            legacy_flags = rows_leg[0]
    except Exception as exc:
        logger.debug(f"Legacy notificaciones fallback skipped for client {client_id}: {exc}")

    habilitadas_map: Dict[str, Dict[str, bool]] = {}
    try:
        rows_hab = _bq_rows(
            f"""
            SELECT notificacion_codigo, whatsapp_habilitado, email_habilitado
            FROM `{P}.{W}.notificaciones_habilitadas`
            WHERE client_id = @cid
            """,
            cp,
        )
        for r in rows_hab:
            habilitadas_map[str(r["notificacion_codigo"])] = {
                "whatsapp": bool(r.get("whatsapp_habilitado", False)),
                "email": bool(r.get("email_habilitado", False)),
            }
    except Exception as exc:
        logger.debug(f"No se pudo leer notificaciones_habilitadas para cliente {client_id}: {exc}")

    plantillas_map: Dict[str, Dict[str, Dict[str, str]]] = {}
    try:
        rows_tpl = _bq_rows(
            f"""
            SELECT notificacion_codigo, canal, asunto, mensaje
            FROM `{P}.{W}.plantillas_notificaciones`
            WHERE client_id = @cid
            """,
            cp,
        )
        for r in rows_tpl:
            cod = str(r["notificacion_codigo"])
            canal = str(r["canal"]).lower()
            plantillas_map.setdefault(cod, {})[canal] = {
                "asunto": str(r.get("asunto") or ""),
                "mensaje": str(r.get("mensaje") or ""),
            }
    except Exception as exc:
        logger.debug(f"No se pudo leer plantillas_notificaciones para cliente {client_id}: {exc}")

    items: List[Dict[str, Any]] = []
    for base in NOTIFICACIONES_CATALOGO:
        cod = base["codigo"]
        if cod in habilitadas_map:
            wa_on = habilitadas_map[cod]["whatsapp"]
            em_on = habilitadas_map[cod]["email"]
        elif cod == "cumpleanios" and legacy_flags:
            wa_on = bool(legacy_flags.get("whatsapp_cumpleanios_activo", base["default_whatsapp"]))
            em_on = bool(legacy_flags.get("correo_cumpleanios_activo", base["default_email"]))
        elif cod == "proxima_renovacion" and legacy_flags:
            wa_on = bool(legacy_flags.get("whatsapp_renovaciones_activo", base["default_whatsapp"]))
            em_on = bool(legacy_flags.get("correo_renovaciones_activo", base["default_email"]))
        else:
            wa_on = bool(base["default_whatsapp"])
            em_on = bool(base["default_email"])

        tpl_wa = plantillas_map.get(cod, {}).get("whatsapp", {})
        tpl_em = plantillas_map.get(cod, {}).get("email", {})

        items.append({
            "codigo": cod,
            "orden": base["orden"],
            "nombre": base["nombre"],
            "modo": base["modo"],
            "es_automatico": base["es_automatico"],
            "editable": base["editable"],
            "icono": base["icono"],
            "color": base["color"],
            "descripcion": base["descripcion"],
            "whatsapp_habilitado": wa_on,
            "email_habilitado": em_on,
            "whatsapp_mensaje": (
                tpl_wa.get("mensaje") if base["editable"] and tpl_wa.get("mensaje") else base["default_mensaje_whatsapp"]
            ),
            "email_asunto": (
                tpl_em.get("asunto") if base["editable"] and tpl_em.get("asunto") else base["default_asunto_email"]
            ),
            "email_mensaje": (
                tpl_em.get("mensaje") if base["editable"] and tpl_em.get("mensaje") else base["default_mensaje_email"]
            ),
            "whatsapp_personalizado": bool(base["editable"] and tpl_wa.get("mensaje")),
            "email_personalizado": bool(base["editable"] and (tpl_em.get("mensaje") or tpl_em.get("asunto"))),
        })

    return {
        "notificaciones": items,
        "variables": PLANTILLA_VARIABLES,
    }


def save_cliente_notificaciones_habilitadas(
    client_id: int,
    id_plan: str,
    nombre_plan: str,
    preferencias: Dict[str, Dict[str, Any]],
) -> None:
    """Guarda en notificaciones_habilitadas (y sincroniza cumpleaños/renovaciones en configuracion_planes_notificaciones)."""
    P = BIGQUERY_PROJECT_ID
    W = WESBYTES_DATASET_ID

    for base in NOTIFICACIONES_CATALOGO:
        cod = base["codigo"]
        cfg_item = preferencias.get(cod, {})
        wa = bool(cfg_item.get("whatsapp_habilitado", base["default_whatsapp"]))
        em = bool(cfg_item.get("email_habilitado", base["default_email"]))

        _bq_exec(
            f"""
            MERGE `{P}.{W}.notificaciones_habilitadas` T
            USING (SELECT @cid AS client_id, @cod AS notificacion_codigo) S
            ON T.client_id = S.client_id AND T.notificacion_codigo = S.notificacion_codigo
            WHEN MATCHED THEN
                UPDATE SET
                    whatsapp_habilitado = @wa,
                    email_habilitado = @em,
                    fecha_actualizacion = CURRENT_TIMESTAMP()
            WHEN NOT MATCHED THEN
                INSERT (
                    client_id, notificacion_codigo, whatsapp_habilitado, email_habilitado,
                    fecha_creacion, fecha_actualizacion
                )
                VALUES (
                    @cid, @cod, @wa, @em, CURRENT_TIMESTAMP(), CURRENT_TIMESTAMP()
                )
            """,
            [
                bigquery.ScalarQueryParameter("cid", "INT64", int(client_id)),
                bigquery.ScalarQueryParameter("cod", "STRING", cod),
                bigquery.ScalarQueryParameter("wa", "BOOL", wa),
                bigquery.ScalarQueryParameter("em", "BOOL", em),
            ],
        )

    # Mantener compatibilidad con configuracion_planes_notificaciones (usado por birthday_notification_service)
    cumple_cfg = preferencias.get("cumpleanios", {})
    renov_cfg = preferencias.get("proxima_renovacion", {})
    cc = bool(cumple_cfg.get("email_habilitado", True))
    wc = bool(cumple_cfg.get("whatsapp_habilitado", False))
    cr = bool(renov_cfg.get("email_habilitado", True))
    wr = bool(renov_cfg.get("whatsapp_habilitado", True))

    _bq_exec(
        f"""
        MERGE `{P}.{W}.configuracion_planes_notificaciones` T
        USING (SELECT @cid AS client_id) S
        ON T.client_id = S.client_id
        WHEN MATCHED THEN
            UPDATE SET
                id_plan = @id_plan,
                nombre_plan = @nombre_plan,
                correo_cumpleanios_activo = @cc,
                correo_renovaciones_activo = @cr,
                correo_cobros_activo = TRUE,
                whatsapp_cumpleanios_activo = @wc,
                whatsapp_renovaciones_activo = @wr,
                whatsapp_cobros_activo = TRUE,
                fecha_actualizacion = CURRENT_TIMESTAMP()
        WHEN NOT MATCHED THEN
            INSERT (
                client_id, id_plan, nombre_plan,
                correo_cumpleanios_activo, correo_renovaciones_activo, correo_cobros_activo,
                whatsapp_cumpleanios_activo, whatsapp_renovaciones_activo, whatsapp_cobros_activo,
                fecha_creacion, fecha_actualizacion
            )
            VALUES (
                @cid, @id_plan, @nombre_plan,
                @cc, @cr, TRUE,
                @wc, @wr, TRUE,
                CURRENT_TIMESTAMP(), CURRENT_TIMESTAMP()
            )
        """,
        [
            bigquery.ScalarQueryParameter("cid", "INT64", int(client_id)),
            bigquery.ScalarQueryParameter("id_plan", "STRING", str(id_plan or "1")),
            bigquery.ScalarQueryParameter("nombre_plan", "STRING", str(nombre_plan or "Plan Wesbyte")),
            bigquery.ScalarQueryParameter("cc", "BOOL", cc),
            bigquery.ScalarQueryParameter("cr", "BOOL", cr),
            bigquery.ScalarQueryParameter("wc", "BOOL", wc),
            bigquery.ScalarQueryParameter("wr", "BOOL", wr),
        ],
    )


def save_cliente_plantilla(
    client_id: int,
    notificacion_codigo: str,
    canal: str,
    asunto: Optional[str],
    mensaje: str,
) -> None:
    """Guarda o actualiza la plantilla personalizada de un cliente en plantillas_notificaciones."""
    meta = CATALOGO_POR_CODIGO.get(notificacion_codigo)
    if not meta:
        raise ValueError("Código de notificación no válido.")
    if not meta["editable"]:
        raise ValueError("Esta notificación es automática y no se puede editar.")

    canal_norm = str(canal or "").strip().lower()
    if canal_norm not in ("whatsapp", "email"):
        raise ValueError("Canal no válido. Debe ser 'whatsapp' o 'email'.")

    msg_clean = str(mensaje or "").strip()
    if not msg_clean:
        raise ValueError("El mensaje de la plantilla no puede estar vacío.")

    asunto_clean = str(asunto or "").strip() if canal_norm == "email" else ""
    if canal_norm == "email" and not asunto_clean:
        asunto_clean = meta["default_asunto_email"]

    P = BIGQUERY_PROJECT_ID
    W = WESBYTES_DATASET_ID

    _bq_exec(
        f"""
        MERGE `{P}.{W}.plantillas_notificaciones` T
        USING (
            SELECT @cid AS client_id, @cod AS notificacion_codigo, @canal AS canal
        ) S
        ON T.client_id = S.client_id
           AND T.notificacion_codigo = S.notificacion_codigo
           AND T.canal = S.canal
        WHEN MATCHED THEN
            UPDATE SET
                asunto = @asunto,
                mensaje = @mensaje,
                fecha_actualizacion = CURRENT_TIMESTAMP()
        WHEN NOT MATCHED THEN
            INSERT (
                client_id, notificacion_codigo, canal, asunto, mensaje,
                fecha_creacion, fecha_actualizacion
            )
            VALUES (
                @cid, @cod, @canal, @asunto, @mensaje,
                CURRENT_TIMESTAMP(), CURRENT_TIMESTAMP()
            )
        """,
        [
            bigquery.ScalarQueryParameter("cid", "INT64", int(client_id)),
            bigquery.ScalarQueryParameter("cod", "STRING", notificacion_codigo),
            bigquery.ScalarQueryParameter("canal", "STRING", canal_norm),
            bigquery.ScalarQueryParameter("asunto", "STRING", asunto_clean),
            bigquery.ScalarQueryParameter("mensaje", "STRING", msg_clean),
        ],
    )
