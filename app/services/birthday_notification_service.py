import logging
import datetime
from typing import List, Optional, Dict, Any
from google.cloud import bigquery
from google.api_core.exceptions import NotFound

from app.config import (
    GCP_PROJECT_ID,
    WESBYTE_CENTRAL_DATASET,
)
from app.database import get_bq_client, get_next_id, append_rows
from app.services.email_service import EmailService, EmailServiceError
from app.services.google_drive_service import normalize_drive_logo_url

logger = logging.getLogger("wesbyte_portal.services.birthday")


class BirthdayNotificationService:
    """
    Servicio multi-tenant para la detección y despacho automatizado
    de felicitaciones de cumpleaños a clientes de aseguradoras / brokers activos.
    """

    def __init__(self, email_service: Optional[EmailService] = None):
        self.bq_client: bigquery.Client = get_bq_client()
        self.email_service: EmailService = email_service or EmailService()

    def get_active_tenants(self) -> List[Dict[str, Any]]:
        """
        Paso 1 y 2:
        Consulta la tabla central `clientes` filtrando `activo = TRUE`,
        valida que su plan/configuración tenga cumpleaños habilitado (Email y/o WhatsApp)
        y que no haya superado su límite mensual en `consumos_mensuales`.
        """
        from zoneinfo import ZoneInfo
        periodo_actual = datetime.datetime.now(ZoneInfo("America/Guayaquil")).strftime("%Y-%m")
        sql = f"""
        SELECT 
            c.id AS id_cliente,
            ANY_VALUE(c.nombre) AS nombre_broker,
            ANY_VALUE(c.email) AS email_broker,
            ANY_VALUE(c.logo) AS logo_url,
            ANY_VALUE(c.plan_id) AS id_plan,
            ANY_VALUE(c.dataset_id) AS dataset_id,
            ANY_VALUE(p.codigo) AS codigo_plan,
            ANY_VALUE(p.nombre) AS nombre_plan,
            ANY_VALUE(COALESCE(c.limite_mensual_emails, p.limite_mensual_emails, 0)) AS limite_mensual_emails,
            ANY_VALUE(COALESCE(c.limite_mensual_whatsapp, p.limite_mensual_whatsapp, 0)) AS limite_mensual_whatsapp,
            ANY_VALUE(COALESCE(cm.total_emails, 0)) AS total_emails,
            ANY_VALUE(COALESCE(cm.total_whatsapp, 0)) AS total_whatsapp,
            ANY_VALUE(COALESCE(cm.bloqueado_mensual_email, FALSE)) AS bloqueado_mensual_email,
            ANY_VALUE(COALESCE(cm.bloqueado_mensual_whatsapp, FALSE)) AS bloqueado_mensual_whatsapp,
            LOGICAL_OR(cpn.correo_cumpleanios_activo) AS cpn_correo_cumple,
            LOGICAL_OR(cpn.whatsapp_cumpleanios_activo) AS cpn_wa_cumple,
            LOGICAL_OR(nh.email_habilitado) AS nh_email_cumple,
            LOGICAL_OR(nh.whatsapp_habilitado) AS nh_wa_cumple
        FROM `{GCP_PROJECT_ID}.{WESBYTE_CENTRAL_DATASET}.clientes` c
        LEFT JOIN `{GCP_PROJECT_ID}.{WESBYTE_CENTRAL_DATASET}.planes` p 
            ON c.plan_id = p.id
        LEFT JOIN `{GCP_PROJECT_ID}.{WESBYTE_CENTRAL_DATASET}.consumos_mensuales` cm
            ON c.id = cm.cliente_id AND cm.periodo = '{periodo_actual}'
        LEFT JOIN `{GCP_PROJECT_ID}.{WESBYTE_CENTRAL_DATASET}.configuracion_planes_notificaciones` cpn 
            ON (cpn.client_id = c.id OR (cpn.client_id IS NULL AND (cpn.id_plan = CAST(c.plan_id AS STRING) OR cpn.id_plan = p.codigo)))
        LEFT JOIN `{GCP_PROJECT_ID}.{WESBYTE_CENTRAL_DATASET}.notificaciones_habilitadas` nh
            ON nh.client_id = c.id AND nh.notificacion_codigo = 'cumpleanios'
        WHERE c.activo = TRUE 
          AND (c.estado = 'activo' OR c.estado IS NULL)
        GROUP BY c.id
        """
        try:
            query_job = self.bq_client.query(sql)
            results = list(query_job.result())
            tenants = []
            for r in results:
                if r["nh_email_cumple"] is not None:
                    em_flag = bool(r["nh_email_cumple"])
                elif r["cpn_correo_cumple"] is not None:
                    em_flag = bool(r["cpn_correo_cumple"])
                else:
                    em_flag = True

                if r["nh_wa_cumple"] is not None:
                    wa_flag = bool(r["nh_wa_cumple"])
                elif r["cpn_wa_cumple"] is not None:
                    wa_flag = bool(r["cpn_wa_cumple"])
                else:
                    wa_flag = False
                lim_em = int(r["limite_mensual_emails"] or 0)
                lim_wa = int(r["limite_mensual_whatsapp"] or 0)
                email_ok = bool(em_flag) and not bool(r["bloqueado_mensual_email"]) and (lim_em == 0 or int(r["total_emails"] or 0) < lim_em)
                wa_ok = bool(wa_flag) and not bool(r["bloqueado_mensual_whatsapp"]) and (lim_wa == 0 or int(r["total_whatsapp"] or 0) < lim_wa)
                if not email_ok and not wa_ok:
                    continue
                tenants.append({
                    "id_cliente": int(r["id_cliente"]),
                    "nombre_broker": str(r["nombre_broker"] or "Wesbyte Broker"),
                    "email_broker": str(r["email_broker"] or "").strip(),
                    "logo_url": normalize_drive_logo_url(r.get("logo_url")),
                    "id_plan": r["id_plan"],
                    "codigo_plan": str(r["codigo_plan"] or ""),
                    "nombre_plan": str(r["nombre_plan"] or ""),
                    "dataset_id": str(r["dataset_id"] or "").strip(),
                    "email_ok": email_ok,
                    "wa_ok": wa_ok,
                    "limite_mensual_whatsapp": lim_wa,
                })
            logger.info(f"Tenants activos con felicitación de cumpleaños habilitada: {len(tenants)}")
            return tenants
        except Exception as exc:
            logger.error(f"Error consultando tenants activos en BigQuery: {exc}", exc_info=True)
            raise

    def get_birthdays_for_tenant(
        self,
        dataset_id: str,
        dia: int,
        mes: int,
    ) -> List[Dict[str, Any]]:
        """
        Paso 3:
        Consulta dinámicamente la tabla maestra de clientes del tenant (`{dataset_id}.maestra_clientes`),
        aplica DISTINCT por cédula / identificación y filtra aquellos cuya fecha de nacimiento
        coincida con el día y mes indicados.
        """
        table_ref = f"{GCP_PROJECT_ID}.{dataset_id}.maestra_clientes"
        try:
            table = self.bq_client.get_table(table_ref)
        except NotFound:
            logger.warning(f"La tabla maestra no existe para el tenant en {table_ref}. Omitiendo tenant.")
            return []
        except Exception as exc:
            logger.warning(f"No se pudo acceder a la tabla {table_ref}: {exc}. Omitiendo tenant.")
            return []

        col_names = {f.name.lower() for f in table.schema}

        # Identificar columna de cédula / identificación
        if "cedula" in col_names:
            id_col = "cedula"
        elif "identificacion" in col_names:
            id_col = "identificacion"
        elif "id" in col_names:
            id_col = "id"
        else:
            logger.warning(f"No se encontró columna identificadora en {table_ref}")
            return []

        # Identificar columna de fecha de nacimiento
        if "fecha_nacimiento" in col_names:
            dob_col = "fecha_nacimiento"
        else:
            logger.warning(f"No se encontró columna 'fecha_nacimiento' en {table_ref}")
            return []

        # Identificar columna de correo / email
        if "correo" in col_names:
            email_col = "correo"
        elif "email" in col_names:
            email_col = "email"
        else:
            logger.warning(f"No se encontró columna 'correo' o 'email' en {table_ref}")
            return []

        # Identificar columna de teléfono / celular
        if "celular" in col_names:
            phone_select = "celular AS celular"
        elif "telefono" in col_names:
            phone_select = "telefono AS celular"
        else:
            phone_select = "'' AS celular"

        # Identificar columnas de nombres
        has_separate_names = ("nombre" in col_names and "apellido" in col_names)
        has_full_name = "nombres_completos_razon_social" in col_names or "nombre_completo" in col_names
        full_name_col = "nombres_completos_razon_social" if "nombres_completos_razon_social" in col_names else "nombre_completo"

        if has_separate_names:
            name_select = f"{id_col} AS cedula, nombre, apellido, {email_col} AS email, {phone_select}, {dob_col} AS fecha_nacimiento"
        elif has_full_name:
            name_select = f"{id_col} AS cedula, {full_name_col} AS nombre_completo, {email_col} AS email, {phone_select}, {dob_col} AS fecha_nacimiento"
        else:
            name_select = f"{id_col} AS cedula, 'Cliente' AS nombre_completo, {email_col} AS email, {phone_select}, {dob_col} AS fecha_nacimiento"

        activo_clause = "AND activo = TRUE" if "activo" in col_names else ""

        sql = f"""
        SELECT DISTINCT
            {name_select}
        FROM `{table_ref}`
        WHERE {dob_col} IS NOT NULL
          AND EXTRACT(DAY FROM {dob_col}) = {dia}
          AND EXTRACT(MONTH FROM {dob_col}) = {mes}
          {activo_clause}
        """

        try:
            query_job = self.bq_client.query(sql)
            rows = list(query_job.result())
            cumpleanieros = []

            for r in rows:
                raw_id = str(r["cedula"]) if r.get("cedula") is not None else ""
                raw_email = str(r["email"]).strip() if r.get("email") is not None else ""
                raw_dob = str(r["fecha_nacimiento"]) if r.get("fecha_nacimiento") is not None else ""

                if has_separate_names:
                    nombre = str(r.get("nombre") or "").strip()
                    apellido = str(r.get("apellido") or "").strip()
                else:
                    nombre_completo = str(r.get("nombre_completo") or "Estimado/a Cliente").strip()
                    parts = nombre_completo.split()
                    if len(parts) >= 3:
                        nombre = " ".join(parts[:2])
                        apellido = " ".join(parts[2:])
                    elif len(parts) == 2:
                        nombre = parts[0]
                        apellido = parts[1]
                    elif len(parts) == 1:
                        nombre = parts[0]
                        apellido = ""
                    else:
                        nombre = "Estimado/a Cliente"
                        apellido = ""

                cumpleanieros.append({
                    "cedula": raw_id,
                    "nombre": nombre,
                    "apellido": apellido,
                    "nombre_completo": f"{nombre} {apellido}".strip(),
                    "email": raw_email,
                    "celular": str(r.get("celular") or "").strip(),
                    "fecha_nacimiento": raw_dob,
                })

            return cumpleanieros
        except Exception as exc:
            logger.error(f"Error consultando cumpleañeros en {table_ref}: {exc}", exc_info=True)
            return []

    def log_email_dispatch(
        self,
        cliente_id: int,
        destinatario: str,
        motivo: str,
        estado: str,
        error_detalle: Optional[str] = None,
    ) -> None:
        """Registra el evento de envío de correo en `wesbyte.administracion.logs_emails` y actualiza el consumo."""
        from app.services.consumo_service import log_email_event

        log_email_event(
            cliente_id=cliente_id,
            destinatario=destinatario,
            motivo=motivo,
            estado=estado,
            error_detalle=error_detalle,
            increment_quota=(str(estado).strip().upper() == "ENVIADO"),
        )

    def run_birthday_process(
        self,
        forzar_fecha: Optional[str] = None,
        email_prueba: Optional[str] = None,
        solo_lectura: bool = False,
    ) -> Dict[str, Any]:
        """
        Ejecuta el flujo completo de extracción multi-tenant y despacho:
        - Si solo_lectura=True: extrae y valida todos los datos sin enviar correos.
        - Si email_prueba está especificado: envía un único correo de muestra a esa dirección con el primer cumpleañero detectado.
        - Si solo_lectura=False y email_prueba es None: despacha a todos los clientes reales detectados con copia (cc) al broker.
        """
        # Determinar fecha objetivo (día y mes)
        if forzar_fecha:
            try:
                dt_target = datetime.datetime.strptime(forzar_fecha.strip(), "%Y-%m-%d").date()
            except ValueError:
                raise ValueError(f"Formato de fecha inválido para 'forzar_fecha': {forzar_fecha}. Debe ser YYYY-MM-DD.")
        else:
            # Fecha actual UTC / Ecuador
            dt_target = datetime.date.today()

        dia_actual = dt_target.day
        mes_actual = dt_target.month

        # Paso 1 y 2: Extraer brokers activos con el feature habilitado en su plan
        tenants = self.get_active_tenants()

        total_cumpleanieros = 0
        total_despachados = 0
        errores: List[Dict[str, Any]] = []
        detalles_brokers: List[Dict[str, Any]] = []

        todos_los_cumpleanieros: List[Dict[str, Any]] = []

        # Paso 3: Consultar cumpleañeros en cada tenant
        for t in tenants:
            dataset_id = t["dataset_id"]
            if not dataset_id:
                logger.info(f"Broker {t['nombre_broker']} (ID {t['id_cliente']}) no tiene dataset_id asignado. Se omite.")
                continue

            cumpleanieros = self.get_birthdays_for_tenant(dataset_id, dia=dia_actual, mes=mes_actual)
            total_cumpleanieros += len(cumpleanieros)

            for c in cumpleanieros:
                todos_los_cumpleanieros.append({
                    "tenant_id": t["id_cliente"],
                    "nombre_broker": t["nombre_broker"],
                    "email_broker": t["email_broker"],
                    "logo_url": t.get("logo_url", ""),
                    "email_ok": t.get("email_ok", True),
                    "wa_ok": t.get("wa_ok", False),
                    "codigo_plan": t.get("codigo_plan", "MEDIUM"),
                    "limite_mensual_whatsapp": t.get("limite_mensual_whatsapp", 50),
                    **c,
                })

            detalles_brokers.append({
                "id_cliente": t["id_cliente"],
                "nombre_broker": t["nombre_broker"],
                "email_broker": t["email_broker"],
                "dataset_id": dataset_id,
                "plan": t["codigo_plan"] or t["nombre_plan"],
                "conteo_cumpleanieros": len(cumpleanieros),
                "cumpleanieros": cumpleanieros,
            })

        # Paso 4: Despacho de correos
        despachos_realizados: List[Dict[str, Any]] = []

        if solo_lectura:
            logger.info("Modo 'solo_lectura' activo: No se despachó ningún correo con Resend.")
            estado_ejecucion = "simulacion_solo_lectura"

        elif email_prueba:
            # Modo prueba dirigida: despachar un único correo a la dirección de prueba
            destinatario_test = email_prueba.strip()
            logger.info(f"Modo 'email_prueba' activo: Enviando 1 correo de muestra a {destinatario_test}")

            if todos_los_cumpleanieros:
                muestra = todos_los_cumpleanieros[0]
                nombre_dest = muestra["nombre_completo"]
                broker_dest = muestra["nombre_broker"]
                email_broker_cc = muestra["email_broker"]
                cliente_id_log = muestra["tenant_id"]
            else:
                nombre_dest = "Carlos Mendoza (Cliente Muestra)"
                broker_dest = tenants[0]["nombre_broker"] if tenants else "Wesbyte Seguros"
                email_broker_cc = tenants[0]["email_broker"] if tenants else "soporte@wesbyte.app"
                cliente_id_log = tenants[0]["id_cliente"] if tenants else 1

            html_body = self.email_service.generate_birthday_html(
                nombre_cliente=nombre_dest,
                nombre_broker=broker_dest,
                email_broker=email_broker_cc,
            )
            text_body = self.email_service.generate_birthday_text(
                nombre_cliente=nombre_dest,
                nombre_broker=broker_dest,
                email_broker=email_broker_cc,
            )
            asunto = f"🎂 [TEST WESBYTE] ¡Feliz Cumpleaños, {nombre_dest}! — {broker_dest}"
            from_sender = f"{broker_dest} <notifications@wesbyte.app>"
            reply_to = email_broker_cc if email_broker_cc and "@" in email_broker_cc else None

            try:
                res = self.email_service.send_email(
                    to=[destinatario_test],
                    subject=asunto,
                    html=html_body,
                    text=text_body,
                    from_sender=from_sender,
                    reply_to=reply_to,
                    cc=None,  # No se añade cc en prueba para evitar spam al broker
                )
                total_despachados += 1
                despachos_realizados.append({
                    "destinatario": destinatario_test,
                    "modo": "prueba_muestra",
                    "from": from_sender,
                    "reply_to": reply_to,
                    "resend_id": res.get("id"),
                    "cliente_muestra": nombre_dest,
                    "broker": broker_dest,
                })
                self.log_email_dispatch(
                    cliente_id=cliente_id_log,
                    destinatario=destinatario_test,
                    motivo=f"prueba_cumpleanios: {nombre_dest}",
                    estado="enviado",
                )
            except EmailServiceError as exc:
                errores.append({"destinatario": destinatario_test, "error": str(exc)})
                self.log_email_dispatch(
                    cliente_id=cliente_id_log,
                    destinatario=destinatario_test,
                    motivo=f"prueba_cumpleanios: {nombre_dest}",
                    estado="error",
                    error_detalle=str(exc),
                )

            estado_ejecucion = "prueba_despachada"

        else:
            # Modo producción: Despachar a todos los cumpleañeros reales (Email y/o WhatsApp)
            import httpx
            from app.config import WHATSAPP_API_URL, WHATSAPP_SERVICE_API_KEY
            estado_ejecucion = "despacho_produccion"
            for item in todos_los_cumpleanieros:
                broker_name = item["nombre_broker"]
                broker_email = item["email_broker"]
                cliente_nombre = item["nombre_completo"]
                cliente_id_log = item["tenant_id"]

                # 1) Correo electrónico
                destinatario_email = item["email"]
                if item.get("email_ok", True) and destinatario_email and "@" in destinatario_email:
                    custom_tpl_rows = []
                    try:
                        custom_tpl_rows = list(
                            self.bq_client.query(
                                f"""
                                SELECT asunto, mensaje, IFNULL(es_html, FALSE) AS es_html
                                FROM `{GCP_PROJECT_ID}.{WESBYTE_CENTRAL_DATASET}.plantillas_notificaciones`
                                WHERE client_id = {int(cliente_id_log)}
                                  AND notificacion_codigo = 'cumpleanios'
                                  AND LOWER(canal) = 'email'
                                LIMIT 1
                                """
                            ).result()
                        )
                    except Exception:
                        custom_tpl_rows = []

                    if custom_tpl_rows and str(custom_tpl_rows[0].get("mensaje") or "").strip():
                        import re as _re
                        raw_msg = str(custom_tpl_rows[0]["mensaje"])
                        raw_asu = str(custom_tpl_rows[0].get("asunto") or f"🎉 ¡Feliz Cumpleaños, {item['nombre']}! — {broker_name}")
                        vars_bday = {
                            "nombres_completos_razon_social": cliente_nombre,
                            "cedula": str(item.get("cedula") or ""),
                            "broker": broker_name,
                            "nombre_agente": broker_name,
                            "logo": (
                                f'<img src="{item.get("logo_url")}" alt="{broker_name}" style="max-height:70px;max-width:220px;object-fit:contain;" />'
                                if item.get("logo_url")
                                else broker_name
                            ),
                        }
                        for k_v, v_v in vars_bday.items():
                            raw_msg = raw_msg.replace(f"{{{{{k_v}}}}}", v_v).replace(f"{{{k_v}}}", v_v)
                            raw_asu = raw_asu.replace(f"{{{{{k_v}}}}}", v_v).replace(f"{{{k_v}}}", v_v)
                        html_body = raw_msg
                        text_body = _re.sub(r"<[^>]+>", "", raw_msg)
                        asunto = raw_asu
                    else:
                        html_body = self.email_service.generate_birthday_html(
                            nombre_cliente=cliente_nombre,
                            nombre_broker=broker_name,
                            email_broker=broker_email,
                            logo_cliente_url=item.get("logo_url"),
                        )
                        text_body = self.email_service.generate_birthday_text(
                            nombre_cliente=cliente_nombre,
                            nombre_broker=broker_name,
                            email_broker=broker_email,
                        )
                        asunto = f"🎉 ¡Feliz Cumpleaños, {item['nombre']}! — {broker_name}"

                    from_sender = f"{broker_name} <notifications@wesbyte.app>" if broker_name else self.email_service.sender_default
                    reply_to = broker_email if broker_email and "@" in broker_email else None
                    cc_recipients = [broker_email] if broker_email and "@" in broker_email else None

                    try:
                        res = self.email_service.send_email(
                            to=[destinatario_email],
                            subject=asunto,
                            html=html_body,
                            text=text_body,
                            from_sender=from_sender,
                            reply_to=reply_to,
                            cc=cc_recipients,
                        )
                        total_despachados += 1
                        despachos_realizados.append({
                            "canal": "email",
                            "destinatario": destinatario_email,
                            "from": from_sender,
                            "reply_to": reply_to,
                            "cc": cc_recipients or [],
                            "cliente": cliente_nombre,
                            "broker": broker_name,
                            "resend_id": res.get("id"),
                        })
                        self.log_email_dispatch(
                            cliente_id=cliente_id_log,
                            destinatario=destinatario_email,
                            motivo=f"cumpleanios: {cliente_nombre}",
                            estado="enviado",
                        )
                    except EmailServiceError as exc:
                        errores.append({"canal": "email", "destinatario": destinatario_email, "error": str(exc)})
                        self.log_email_dispatch(
                            cliente_id=cliente_id_log,
                            destinatario=destinatario_email,
                            motivo=f"cumpleanios: {cliente_nombre}",
                            estado="error",
                            error_detalle=str(exc),
                        )

                # 2) WhatsApp vía wesbyte_api
                dest_phone = str(item.get("celular") or "").strip()
                if item.get("wa_ok") and dest_phone:
                    wa_msg = (
                        f"🎉 *¡Feliz Cumpleaños, {cliente_nombre}!*\n\n"
                        f"En *{broker_name}* te deseamos un año lleno de salud, alegría y grandes éxitos.\n\n"
                        f"— Enviado por wesbyte.app"
                    )
                    wa_headers = {"X-API-Key": WHATSAPP_SERVICE_API_KEY, "Content-Type": "application/json"}
                    try:
                        with httpx.Client(timeout=15.0) as hclient:
                            hclient.post(
                                f"{WHATSAPP_API_URL}/api/v1/whatsapp/{cliente_id_log}/provision",
                                headers=wa_headers,
                                json={
                                    "plan_code": item.get("codigo_plan") or "MEDIUM",
                                    "monthly_limit": item.get("limite_mensual_whatsapp") or 50,
                                    "notify_birthdays": True,
                                },
                            )
                            resp = hclient.post(
                                f"{WHATSAPP_API_URL}/api/v1/whatsapp/send",
                                headers=wa_headers,
                                json={
                                    "client_id": cliente_id_log,
                                    "category": "birthday",
                                    "recipient_phone": dest_phone,
                                    "message_text": wa_msg,
                                },
                            )
                            if resp.status_code < 400:
                                total_despachados += 1
                                despachos_realizados.append({
                                    "canal": "whatsapp",
                                    "destinatario": dest_phone,
                                    "cliente": cliente_nombre,
                                    "broker": broker_name,
                                })
                            else:
                                errores.append({"canal": "whatsapp", "destinatario": dest_phone, "error": resp.text[:200]})
                    except Exception as exc:
                        errores.append({"canal": "whatsapp", "destinatario": dest_phone, "error": str(exc)})

        return {
            "estado": estado_ejecucion,
            "fecha_evaluada": str(dt_target),
            "dia": dia_actual,
            "mes": mes_actual,
            "solo_lectura": solo_lectura,
            "email_prueba_solicitado": email_prueba,
            "tenants_consultados": len(tenants),
            "total_cumpleanieros_detectados": total_cumpleanieros,
            "total_correos_despachados": total_despachados,
            "despachos_realizados": despachos_realizados,
            "errores": errores,
            "resumen_por_broker": detalles_brokers,
            "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        }
