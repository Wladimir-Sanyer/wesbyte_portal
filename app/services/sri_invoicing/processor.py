"""
Orquestador idempotente de emisión, autorización SRI, almacenamiento en Drive
y despacho por correo (Resend) de facturas electrónicas de Wesbyte.
"""
from __future__ import annotations

import asyncio
import base64
import datetime
import fcntl
import html
import logging
from typing import Any, Dict, List, Optional

from app.config import (
    BIGQUERY_PROJECT_ID,
    SOPORTE_EMAIL,
    SRI_AMBIENTE,
    SRI_CONTRIBUYENTE_RIMPE,
    SRI_DIR_ESTABLECIMIENTO,
    SRI_DIR_MATRIZ,
    SRI_ESTABLECIMIENTO,
    SRI_NOMBRE_COMERCIAL,
    SRI_OBLIGADO_CONTABILIDAD,
    SRI_P12_PASSWORD,
    SRI_P12_PATH,
    SRI_PUNTO_EMISION,
    SRI_RAZON_SOCIAL,
    SRI_RUC_EMISOR,
    WESBYTE_APP_URL,
    WESBYTES_DATASET_ID,
)
from app.database import append_rows, execute_query, get_query
from app.services.email_service import EmailService
from app.services.google_drive_service import GoogleDriveService
from app.services.sri_invoicing.access_key import TZ_ECUADOR, generar_clave_acceso_sri
from app.services.sri_invoicing.ride_generator import generar_ride_pdf_bytes
from app.services.sri_invoicing.sri_client import SriSoapClient
from app.services.sri_invoicing.validators import (
    SriValidationError,
    clasificar_y_validar_identificacion_sri,
)
from app.services.sri_invoicing.xml_builder import construir_xml_factura_sri
from app.services.sri_invoicing.xml_signer import firmar_xml_xades_bes

logger = logging.getLogger("wesbyte_portal.sri_processor")


def _esc_sql(val: Optional[str]) -> str:
    return (
        str(val or "")
        .replace("\\", "\\\\")
        .replace("'", "\\'")
        .replace("\r", " ")
        .replace("\n", " ")
    )


def _safe_str(val: Any, default: str = "") -> str:
    if val is None:
        return default
    s = str(val).strip()
    if s.lower() in ("", "nan", "none", "null", "<na>", "nat"):
        return default
    return s


def _asignar_siguiente_secuencial_atomico(
    establecimiento: str,
    punto_emision: str,
    ambiente: str,
) -> str:
    """Obtiene e incrementa atómicamente el secuencial de 9 dígitos para (establecimiento, punto_emision, ambiente)."""
    estab = establecimiento.strip().zfill(3)[:3]
    pto = punto_emision.strip().zfill(3)[:3]
    amb = "2" if str(ambiente).strip() == "2" else "1"

    with open("/tmp/wesbyte_sri_secuencial.lock", "w") as lock_f:
        fcntl.flock(lock_f, fcntl.LOCK_EX)
        try:
            df_sec = get_query(f"""
                SELECT ultimo_secuencial
                FROM `{BIGQUERY_PROJECT_ID}.{WESBYTES_DATASET_ID}.sri_secuenciales`
                WHERE establecimiento = '{_esc_sql(estab)}'
                  AND punto_emision = '{_esc_sql(pto)}'
                  AND ambiente = '{_esc_sql(amb)}'
                ORDER BY ultimo_secuencial DESC
                LIMIT 1
            """)
            df_max_fac = get_query(f"""
                SELECT COALESCE(MAX(SAFE_CAST(secuencial AS INT64)), 0) AS max_sec
                FROM `{BIGQUERY_PROJECT_ID}.{WESBYTES_DATASET_ID}.datos_facturacion`
                WHERE establecimiento = '{_esc_sql(estab)}'
                  AND punto_emision = '{_esc_sql(pto)}'
                  AND ambiente = '{_esc_sql(amb)}'
            """)
            raw_table = _safe_str(df_sec.iloc[0]["ultimo_secuencial"], "0") if not df_sec.empty else "0"
            raw_fac = _safe_str(df_max_fac.iloc[0]["max_sec"], "0") if not df_max_fac.empty else "0"
            curr_table = int(float(raw_table))
            curr_fac = int(float(raw_fac))
            siguiente = max(curr_table, curr_fac) + 1

            if df_sec.empty:
                append_rows(
                    WESBYTES_DATASET_ID,
                    "sri_secuenciales",
                    [
                        {
                            "establecimiento": estab,
                            "punto_emision": pto,
                            "ambiente": amb,
                            "ultimo_secuencial": siguiente,
                            "updated_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                        }
                    ],
                )
            else:
                execute_query(f"""
                    UPDATE `{BIGQUERY_PROJECT_ID}.{WESBYTES_DATASET_ID}.sri_secuenciales`
                    SET ultimo_secuencial = {siguiente},
                        updated_at = CURRENT_TIMESTAMP()
                    WHERE establecimiento = '{_esc_sql(estab)}'
                      AND punto_emision = '{_esc_sql(pto)}'
                      AND ambiente = '{_esc_sql(amb)}'
                """)
            return f"{siguiente:09d}"
        finally:
            fcntl.flock(lock_f, fcntl.LOCK_UN)


def _build_invoice_email_html(
    *,
    razon_social: str,
    numero_factura: str,
    clave_acceso: str,
    fecha_emision: str,
    monto_total: float,
) -> tuple[str, str]:
    portal_url = (WESBYTE_APP_URL or "https://portal.wesbyte.app").strip().rstrip("/")
    html_body = f"""
    <div style="font-family: Inter, Arial, sans-serif; max-width: 600px; margin: 0 auto; color: #1e293b; background: #ffffff; border: 1px solid #e2e8f0; border-radius: 16px; overflow: hidden;">
      <div style="background: linear-gradient(135deg, #12163a, #4f46e5); padding: 26px 24px; color: #ffffff; text-align: center;">
        <h1 style="margin: 0; font-size: 21px; font-weight: 800;">Factura Electrónica Autorizada · Wesbyte</h1>
        <p style="margin: 6px 0 0 0; opacity: 0.9; font-size: 14px;">Comprobante No. {html.escape(numero_factura)}</p>
      </div>
      <div style="padding: 26px 24px; line-height: 1.6; font-size: 15px;">
        <p style="margin-top: 0;">Estimado(a) <strong>{html.escape(razon_social)}</strong>,</p>
        <p>Adjunto a este correo encontrarás tu <strong>Factura Electrónica autorizada por el SRI</strong> en formatos <strong>PDF (RIDE)</strong> y <strong>XML</strong> correspondiente a tu suscripción en Wesbyte.</p>
        <div style="background: #f8fafc; border: 1px solid #cbd5e1; border-radius: 12px; padding: 16px 18px; margin: 18px 0; font-size: 14px;">
          <p style="margin: 0 0 6px 0;"><strong>Factura No.:</strong> {html.escape(numero_factura)}</p>
          <p style="margin: 0 0 6px 0;"><strong>Fecha de Emisión:</strong> {html.escape(fecha_emision)}</p>
          <p style="margin: 0 0 6px 0;"><strong>Total Pagado:</strong> ${monto_total:,.2f} USD</p>
          <p style="margin: 0; word-break: break-all;"><strong>Clave de Acceso SRI:</strong> <code style="font-size: 12px;">{html.escape(clave_acceso)}</code></p>
        </div>
        <div style="text-align: center; margin: 24px 0;">
          <a href="{html.escape(portal_url)}" style="display: inline-block; background: #4f46e5; color: #ffffff; text-decoration: none; font-weight: 700; padding: 12px 24px; border-radius: 10px;">
            Ir a {html.escape(portal_url)}
          </a>
        </div>
        <p style="font-size: 13px; color: #64748b; margin-bottom: 0;">
          ¿Tienes alguna consulta? Escríbenos a <a href="mailto:{html.escape(SOPORTE_EMAIL)}" style="color: #4f46e5;">{html.escape(SOPORTE_EMAIL)}</a>.
        </p>
      </div>
    </div>
    """
    text_body = (
        f"Estimado(a) {razon_social},\n\n"
        f"Adjunto encontrarás tu Factura Electrónica No. {numero_factura} autorizada por el SRI (PDF RIDE y XML).\n"
        f"- Fecha de Emisión: {fecha_emision}\n"
        f"- Total Pagado: ${monto_total:,.2f} USD\n"
        f"- Clave de Acceso: {clave_acceso}\n\n"
        f"Portal Wesbyte: {portal_url}\n"
    )
    return html_body, text_body


def emitir_y_enviar_factura_sync(datos_facturacion_id: int) -> Dict[str, Any]:
    """
    Orquestador síncrono e idempotente para una fila de `datos_facturacion`:
      1. Verifica que el pago en `pagos_payphone` sea APPROVED y `estado_sri != 'AUTORIZADO'`.
      2. Valida identificación SRI (RUC/Cédula/Pasaporte y regla Consumidor Final <= $50).
      3. Si aún no tiene `secuencial` ni `clave_acceso`, los asigna y persiste en BigQuery ANTES de llamar al SRI.
      4. Construye XML -> Firma XAdES-BES (.p12) -> Recepción y Autorización SOAP SRI.
      5. Si AUTORIZADO: genera RIDE PDF, respalda XML y PDF en Google Drive (`Facturas_SRI/{YYYY}/{MM}`),
         envía correo con Resend adjuntando PDF + XML y marca `AUTORIZADO` / `emitida`.
    """
    df = get_query(f"""
        SELECT *
        FROM `{BIGQUERY_PROJECT_ID}.{WESBYTES_DATASET_ID}.datos_facturacion`
        WHERE id = {int(datos_facturacion_id)}
        LIMIT 1
    """)
    if df.empty:
        return {"success": False, "error": f"No existe datos_facturacion.id={datos_facturacion_id}"}

    row = df.iloc[0].to_dict()
    estado_actual = str(row.get("estado_sri") or "").upper()
    if estado_actual == "AUTORIZADO" and str(row.get("estado_factura") or "").lower() == "emitida":
        return {
            "success": True,
            "already_authorized": True,
            "id": int(datos_facturacion_id),
            "clave_acceso": row.get("clave_acceso"),
            "numero_autorizacion": row.get("numero_autorizacion"),
        }

    if not SRI_RUC_EMISOR or len("".join(c for c in SRI_RUC_EMISOR if c.isdigit())) != 13:
        return {
            "success": False,
            "error": "Falta configurar SRI_RUC_EMISOR (13 dígitos) en las variables de entorno.",
        }

    cliente_id = int(row.get("cliente_id") or 0)
    client_tx = str(row.get("client_transaction_id") or "").strip()

    # 1. Verificar pago APPROVED en pagos_payphone
    cond_tx = f"client_transaction_id = '{_esc_sql(client_tx)}'" if client_tx else f"cliente_id = {cliente_id}"
    df_pago = get_query(f"""
        SELECT client_transaction_id, payphone_transaction_id, modalidad_pago, estado_pago
        FROM `{BIGQUERY_PROJECT_ID}.{WESBYTES_DATASET_ID}.pagos_payphone`
        WHERE {cond_tx} AND UPPER(estado_pago) = 'APPROVED'
        ORDER BY confirmed_at DESC, created_at DESC
        LIMIT 1
    """)
    if df_pago.empty:
        msg_no_pago = "El pago asociado en pagos_payphone aún no registra estado APPROVED."
        execute_query(f"""
            UPDATE `{BIGQUERY_PROJECT_ID}.{WESBYTES_DATASET_ID}.datos_facturacion`
            SET ultimo_error_sri = '{_esc_sql(msg_no_pago)}',
                updated_at = CURRENT_TIMESTAMP()
            WHERE id = {int(datos_facturacion_id)}
        """)
        return {"success": False, "error": msg_no_pago}

    pago_row = df_pago.iloc[0].to_dict()
    payphone_ref = str(pago_row.get("payphone_transaction_id") or client_tx or "")

    monto_total = round(float(row.get("monto_total") or 0.0), 2)
    iva_pct = round(float(row.get("iva_pct") if row.get("iva_pct") is not None else 15.0), 2)
    monto_base = round(float(row.get("monto_base") or round(monto_total / (1.0 + iva_pct / 100.0), 2)), 2)
    iva_monto = round(float(row.get("iva_monto") or round(monto_total - monto_base, 2)), 2)

    # 2. Validar identificación antes de quemar secuencial
    try:
        tipo_id_sri, id_comprador_norm = clasificar_y_validar_identificacion_sri(
            str(row.get("ruc_cedula") or ""),
            monto_total=monto_total,
        )
    except SriValidationError as val_err:
        err_txt = str(val_err)
        execute_query(f"""
            UPDATE `{BIGQUERY_PROJECT_ID}.{WESBYTES_DATASET_ID}.datos_facturacion`
            SET estado_sri = 'ERROR',
                intentos_reintento = IFNULL(intentos_reintento, 0) + 1,
                ultimo_error_sri = '{_esc_sql(err_txt)}',
                updated_at = CURRENT_TIMESTAMP()
            WHERE id = {int(datos_facturacion_id)}
        """)
        return {"success": False, "error": err_txt, "estado_sri": "ERROR"}

    # 3. Asignar o reutilizar secuencial + clave de acceso (idempotencia ante timeouts)
    ambiente = _safe_str(row.get("ambiente"), SRI_AMBIENTE or "1")
    amb_norm = "2" if ambiente == "2" else "1"
    estab = _safe_str(row.get("establecimiento"), SRI_ESTABLECIMIENTO or "001").zfill(3)[:3]
    pto = _safe_str(row.get("punto_emision"), SRI_PUNTO_EMISION or "001").zfill(3)[:3]
    secuencial = _safe_str(row.get("secuencial"), "")
    clave_acceso = _safe_str(row.get("clave_acceso"), "")

    now_ec = datetime.datetime.now(TZ_ECUADOR)
    fecha_emision_val = row.get("fecha_emision")
    if isinstance(fecha_emision_val, datetime.date) and not isinstance(fecha_emision_val, datetime.datetime):
        fecha_emision_date = fecha_emision_val
    else:
        fecha_emision_date = now_ec.date()

    if not secuencial or not clave_acceso or len(clave_acceso) != 49:
        if not secuencial:
            secuencial = _asignar_siguiente_secuencial_atomico(estab, pto, amb_norm)
        fecha_emision_date = now_ec.date()
        clave_acceso = generar_clave_acceso_sri(
            ruc_emisor=SRI_RUC_EMISOR,
            ambiente=amb_norm,
            establecimiento=estab,
            punto_emision=pto,
            secuencial=secuencial,
            fecha_emision=fecha_emision_date,
        )
        # Guardar ANTES de contactar al SRI para que cualquier reintento futuro reutilice la misma clave
        execute_query(f"""
            UPDATE `{BIGQUERY_PROJECT_ID}.{WESBYTES_DATASET_ID}.datos_facturacion`
            SET tipo_identificacion = '{_esc_sql(tipo_id_sri)}',
                ruc_cedula = '{_esc_sql(id_comprador_norm)}',
                ambiente = '{_esc_sql(amb_norm)}',
                establecimiento = '{_esc_sql(estab)}',
                punto_emision = '{_esc_sql(pto)}',
                secuencial = '{_esc_sql(secuencial)}',
                clave_acceso = '{_esc_sql(clave_acceso)}',
                fecha_emision = DATE '{fecha_emision_date.isoformat()}',
                codigo_forma_pago_sri = '19',
                estado_sri = 'PENDIENTE',
                updated_at = CURRENT_TIMESTAMP()
            WHERE id = {int(datos_facturacion_id)}
        """)

    razon_social_comp = str(row.get("razon_social") or "CLIENTE WESBYTE").strip()
    correo_comp = str(row.get("correo_facturacion") or "").strip()
    direccion_comp = str(row.get("direccion") or "Ecuador").strip()
    telefono_comp = str(row.get("telefono") or "").strip()
    modalidad_pago = str(pago_row.get("modalidad_pago") or "pago_unico_1_year").strip().lower()
    modalidad_labels = {
        "pago_unico_1_year": "Plan Anual",
        "pago_unico_2_years": "Plan 2 Años",
        "pago_unico_3_years": "Plan 3 Años",
        "upgrade_plan": "Mejora de Plan",
        "mejora_usuarios": "Usuarios Adicionales",
    }
    periodo_label = modalidad_labels.get(modalidad_pago, modalidad_pago.replace("_", " ").title())
    descripcion_item = f"SUSCRIPCION SAAS/CRM PARA ASESORES DE SEGUROS WESBYTE ({periodo_label})"

    # 4. Construir XML -> Firmar XAdES-BES -> Enviar al SRI
    try:
        xml_sin_firma = construir_xml_factura_sri(
            ambiente=amb_norm,
            razon_social_emisor=SRI_RAZON_SOCIAL,
            nombre_comercial_emisor=SRI_NOMBRE_COMERCIAL,
            ruc_emisor=SRI_RUC_EMISOR,
            clave_acceso=clave_acceso,
            establecimiento=estab,
            punto_emision=pto,
            secuencial=secuencial,
            dir_matriz=SRI_DIR_MATRIZ,
            dir_establecimiento=SRI_DIR_ESTABLECIMIENTO,
            obligado_contabilidad=SRI_OBLIGADO_CONTABILIDAD,
            contribuyente_rimpe=SRI_CONTRIBUYENTE_RIMPE,
            fecha_emision=fecha_emision_date,
            tipo_identificacion_comprador=tipo_id_sri,
            razon_social_comprador=razon_social_comp,
            identificacion_comprador=id_comprador_norm,
            direccion_comprador=direccion_comp,
            correo_comprador=correo_comp,
            telefono_comprador=telefono_comp,
            descripcion_item=descripcion_item,
            monto_base=monto_base,
            iva_pct=iva_pct,
            iva_monto=iva_monto,
            monto_total=monto_total,
            forma_pago_sri="19",
            referencia_pago=payphone_ref,
        )

        xml_firmado = firmar_xml_xades_bes(
            xml_bytes=xml_sin_firma,
            p12_path=SRI_P12_PATH,
            p12_password=SRI_P12_PASSWORD,
        )

        sri_res = SriSoapClient(ambiente=amb_norm).emitir_comprobante_completo(
            xml_firmado_bytes=xml_firmado,
            clave_acceso=clave_acceso,
        )
    except Exception as exc:
        err_msg = f"Error firmando o comunicando con el SRI: {exc}"
        logger.error("[SRI Factura id=%s] %s", datos_facturacion_id, err_msg)
        execute_query(f"""
            UPDATE `{BIGQUERY_PROJECT_ID}.{WESBYTES_DATASET_ID}.datos_facturacion`
            SET estado_sri = 'ERROR',
                intentos_reintento = IFNULL(intentos_reintento, 0) + 1,
                ultimo_error_sri = '{_esc_sql(err_msg[:900])}',
                updated_at = CURRENT_TIMESTAMP()
            WHERE id = {int(datos_facturacion_id)}
        """)
        return {
            "success": False,
            "id": int(datos_facturacion_id),
            "clave_acceso": clave_acceso,
            "secuencial": secuencial,
            "estado_sri": "ERROR",
            "error": err_msg,
        }

    if not sri_res.get("autorizado"):
        est_sri = str(sri_res.get("estado_sri") or "ERROR")
        det_err = str(sri_res.get("detalle_error") or "No autorizado por el SRI.")
        execute_query(f"""
            UPDATE `{BIGQUERY_PROJECT_ID}.{WESBYTES_DATASET_ID}.datos_facturacion`
            SET estado_sri = '{_esc_sql(est_sri)}',
                intentos_reintento = IFNULL(intentos_reintento, 0) + 1,
                ultimo_error_sri = '{_esc_sql(det_err[:900])}',
                updated_at = CURRENT_TIMESTAMP()
            WHERE id = {int(datos_facturacion_id)}
        """)
        return {
            "success": False,
            "id": int(datos_facturacion_id),
            "clave_acceso": clave_acceso,
            "secuencial": secuencial,
            "estado_sri": est_sri,
            "error": det_err,
        }

    # 5. AUTORIZADO -> Generar RIDE PDF, subir a Google Drive y enviar por correo Resend
    numero_factura = f"{estab}-{pto}-{secuencial}"
    num_aut = str(sri_res.get("numero_autorizacion") or clave_acceso)
    fecha_aut_str = str(sri_res.get("fecha_autorizacion") or datetime.datetime.now(TZ_ECUADOR).isoformat())
    xml_aut_bytes: bytes = sri_res["xml_autorizado_bytes"]

    ride_pdf_bytes = generar_ride_pdf_bytes(
        ruc_emisor=SRI_RUC_EMISOR,
        razon_social_emisor=SRI_RAZON_SOCIAL,
        nombre_comercial_emisor=SRI_NOMBRE_COMERCIAL,
        dir_matriz=SRI_DIR_MATRIZ,
        dir_establecimiento=SRI_DIR_ESTABLECIMIENTO,
        obligado_contabilidad=SRI_OBLIGADO_CONTABILIDAD,
        contribuyente_rimpe=SRI_CONTRIBUYENTE_RIMPE,
        establecimiento=estab,
        punto_emision=pto,
        secuencial=secuencial,
        numero_autorizacion=num_aut,
        fecha_autorizacion=fecha_aut_str,
        ambiente=amb_norm,
        clave_acceso=clave_acceso,
        fecha_emision=fecha_emision_date.strftime("%d/%m/%Y"),
        razon_social_comprador=razon_social_comp,
        identificacion_comprador=id_comprador_norm,
        direccion_comprador=direccion_comp,
        correo_comprador=correo_comp,
        telefono_comprador=telefono_comp,
        descripcion_item=descripcion_item,
        monto_base=monto_base,
        iva_pct=iva_pct,
        iva_monto=iva_monto,
        monto_total=monto_total,
        referencia_pago=payphone_ref,
    )

    folder_drive = f"Facturas_SRI/{fecha_emision_date.strftime('%Y')}/{fecha_emision_date.strftime('%m')}"
    xml_filename = f"Factura_{numero_factura}.xml"
    pdf_filename = f"Factura_{numero_factura}.pdf"
    xml_drive_id = ""
    pdf_drive_id = ""

    try:
        gdrive = GoogleDriveService()
        up_xml = gdrive.upload_private_file(
            file_bytes=xml_aut_bytes,
            filename=xml_filename,
            mime_type="application/xml",
            folder_path=folder_drive,
        )
        up_pdf = gdrive.upload_private_file(
            file_bytes=ride_pdf_bytes,
            filename=pdf_filename,
            mime_type="application/pdf",
            folder_path=folder_drive,
        )
        xml_drive_id = str(up_xml.get("file_id") or "")
        pdf_drive_id = str(up_pdf.get("file_id") or "")
    except Exception as drive_exc:
        logger.warning("[SRI Factura %s] No se pudo respaldar en Google Drive: %s", numero_factura, drive_exc)

    enviado_email = False
    resend_id = ""
    if correo_comp and "@" in correo_comp:
        try:
            html_body, text_body = _build_invoice_email_html(
                razon_social=razon_social_comp,
                numero_factura=numero_factura,
                clave_acceso=clave_acceso,
                fecha_emision=fecha_emision_date.strftime("%d/%m/%Y"),
                monto_total=monto_total,
            )
            email_res = EmailService().send_email(
                to=correo_comp,
                subject=f"Factura Electrónica No. {numero_factura} · Wesbyte",
                html=html_body,
                text=text_body,
                attachments=[
                    {
                        "filename": pdf_filename,
                        "content_type": "application/pdf",
                        "content_base64": base64.b64encode(ride_pdf_bytes).decode("ascii"),
                    },
                    {
                        "filename": xml_filename,
                        "content_type": "application/xml",
                        "content_base64": base64.b64encode(xml_aut_bytes).decode("ascii"),
                    },
                ],
            )
            enviado_email = True
            resend_id = str(email_res.get("id") or "")
        except Exception as email_exc:
            logger.error("[SRI Factura %s] Error enviando correo a %s: %s", numero_factura, correo_comp, email_exc)

    execute_query(f"""
        UPDATE `{BIGQUERY_PROJECT_ID}.{WESBYTES_DATASET_ID}.datos_facturacion`
        SET estado_factura = 'emitida',
            estado_sri = 'AUTORIZADO',
            numero_autorizacion = '{_esc_sql(num_aut)}',
            fecha_autorizacion = CURRENT_TIMESTAMP(),
            ultimo_error_sri = '',
            xml_drive_id = '{_esc_sql(xml_drive_id)}',
            ride_pdf_drive_id = '{_esc_sql(pdf_drive_id)}',
            enviado_email = {'TRUE' if enviado_email else 'FALSE'},
            fecha_envio_email = {'CURRENT_TIMESTAMP()' if enviado_email else 'NULL'},
            resend_email_id = '{_esc_sql(resend_id)}',
            updated_at = CURRENT_TIMESTAMP()
        WHERE id = {int(datos_facturacion_id)}
    """)

    return {
        "success": True,
        "id": int(datos_facturacion_id),
        "numero_factura": numero_factura,
        "clave_acceso": clave_acceso,
        "numero_autorizacion": num_aut,
        "estado_sri": "AUTORIZADO",
        "xml_drive_id": xml_drive_id,
        "ride_pdf_drive_id": pdf_drive_id,
        "enviado_email": enviado_email,
        "resend_email_id": resend_id,
    }


async def emitir_y_enviar_factura(datos_facturacion_id: int) -> Dict[str, Any]:
    """Wrapper asíncrono que ejecuta `emitir_y_enviar_factura_sync` en un hilo de trabajo."""
    return await asyncio.to_thread(emitir_y_enviar_factura_sync, int(datos_facturacion_id))


async def procesar_lote_facturas_pendientes(limit: int = 30) -> Dict[str, Any]:
    """
    Busca hasta `limit` registros en `datos_facturacion` pendientes de autorización SRI
    con pago APPROVED en `pagos_payphone` e `intentos_reintento < 5`, y los procesa secuencialmente.
    """
    lim = max(1, min(int(limit or 30), 100))
    df_pend = await asyncio.to_thread(
        get_query,
        f"""
        SELECT f.id
        FROM `{BIGQUERY_PROJECT_ID}.{WESBYTES_DATASET_ID}.datos_facturacion` f
        WHERE IFNULL(UPPER(f.estado_sri), 'PENDIENTE') IN ('PENDIENTE', 'RECIBIDO', 'ERROR', '')
          AND IFNULL(f.intentos_reintento, 0) < 5
          AND IFNULL(LOWER(f.estado_factura), 'pendiente') != 'emitida'
          AND EXISTS (
              SELECT 1
              FROM `{BIGQUERY_PROJECT_ID}.{WESBYTES_DATASET_ID}.pagos_payphone` p
              WHERE UPPER(p.estado_pago) = 'APPROVED'
                AND (
                    (f.client_transaction_id IS NOT NULL AND f.client_transaction_id != '' AND p.client_transaction_id = f.client_transaction_id)
                    OR ((f.client_transaction_id IS NULL OR f.client_transaction_id = '') AND p.cliente_id = f.cliente_id)
                )
          )
        ORDER BY f.id ASC
        LIMIT {lim}
        """,
    )

    if df_pend.empty:
        return {"total_evaluadas": 0, "autorizadas": 0, "errores": 0, "resultados": []}

    ids = [int(r["id"]) for _, r in df_pend.iterrows()]
    resultados: List[Dict[str, Any]] = []
    autorizadas = 0
    errores = 0

    for fac_id in ids:
        res = await emitir_y_enviar_factura(fac_id)
        resultados.append(res)
        if res.get("success"):
            autorizadas += 1
        else:
            errores += 1
        await asyncio.sleep(1.0)

    return {
        "total_evaluadas": len(ids),
        "autorizadas": autorizadas,
        "errores": errores,
        "resultados": resultados,
    }
