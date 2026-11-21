"""
Servicio para envío de pruebas de plantillas de notificaciones (Correo vía Resend
y WhatsApp vía Evolution API) con datos ficticios y PDF dummy en memoria.
"""
from __future__ import annotations

import base64
import html
import logging
import re
from typing import Any, Dict, Optional

from google.cloud import bigquery

from app.config import BIGQUERY_PROJECT_ID, WESBYTES_DATASET_ID
from app.database import get_bq_client
from app.services.email_service import EmailService, EmailServiceError
from app.services.notificaciones_config_service import (
    CATALOGO_POR_CODIGO,
    PLANTILLA_VARIABLES,
    get_cliente_notificaciones_y_plantillas,
)
from app.services.whatsapp_client import (
    WhatsAppClient,
    WhatsAppClientError,
    sync_client_whatsapp_from_admin,
)

logger = logging.getLogger("wesbyte_portal.test_notification")

NOTIFICACIONES_CON_PDF = frozenset({"envio_poliza", "condiciones_renovacion"})

NOTIF_TO_WA_CATEGORY = {
    "proxima_renovacion": "renewal",
    "regularizaciones": "pending_fee",
    "envio_poliza": "report",
    "cumpleanios": "birthday",
    "condiciones_renovacion": "renewal",
}


def build_dummy_pdf_bytes(
    text_line: str = "Documento PDF de prueba - Vista previa de adjunto",
) -> bytes:
    """
    Genera un PDF 1.4 válido y ligero completamente en memoria (sin escritura en disco)
    con el texto de vista previa solicitado.
    """
    safe_text = (
        text_line.replace("\\", "\\\\")
        .replace("(", "\\(")
        .replace(")", "\\)")
        .encode("latin-1", errors="replace")
        .decode("latin-1")
    )
    stream_content = (
        "BT\n"
        "/F1 14 Tf\n"
        "50 750 Td\n"
        f"({safe_text}) Tj\n"
        "0 -24 Td\n"
        "/F1 10 Tf\n"
        "(Wesbyte - Notificacion de prueba con datos de ejemplo) Tj\n"
        "ET\n"
    ).encode("latin-1")

    objects = [
        b"1 0 obj\n<< /Type /Catalog /Pages 2 0 R >>\nendobj\n",
        b"2 0 obj\n<< /Type /Pages /Kids [3 0 R] /Count 1 >>\nendobj\n",
        (
            b"3 0 obj\n"
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] "
            b"/Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>\n"
            b"endobj\n"
        ),
        (
            f"4 0 obj\n<< /Length {len(stream_content)} >>\nstream\n".encode("latin-1")
            + stream_content
            + b"endstream\nendobj\n"
        ),
        (
            b"5 0 obj\n"
            b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>\n"
            b"endobj\n"
        ),
    ]

    pdf = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for obj in objects:
        offsets.append(len(pdf))
        pdf.extend(obj)

    xref_pos = len(pdf)
    pdf.extend(f"xref\n0 {len(offsets)}\n".encode("latin-1"))
    pdf.extend(b"0000000000 65535 f \n")
    for off in offsets[1:]:
        pdf.extend(f"{off:010d} 00000 n \n".encode("latin-1"))

    pdf.extend(
        (
            f"trailer\n<< /Size {len(offsets)} /Root 1 0 R >>\n"
            f"startxref\n{xref_pos}\n%%EOF\n"
        ).encode("latin-1")
    )
    return bytes(pdf)


def get_dummy_variables(broker_nombre: str = "Seguros Alianza") -> Dict[str, str]:
    """Construye el mapa completo de variables ficticias y sus alias comunes."""
    vars_map: Dict[str, str] = {
        v["key"]: v["ejemplo"] for v in PLANTILLA_VARIABLES
    }
    if broker_nombre:
        vars_map["broker"] = broker_nombre

    # Alias comunes que los usuarios pueden escribir en plantillas
    vars_map.update(
        {
            "nombre_cliente": vars_map["nombres_completos_razon_social"],
            "nombres_completos": vars_map["nombres_completos_razon_social"],
            "poliza": vars_map["numero_contrato"],
            "fecha_vencimiento": vars_map["fecha_vigencia"],
            "ramo": vars_map["ramos"],
            "regularizaciones_faltantes": "• Pago inicial pendiente\n• Firma de solicitud",
        }
    )
    return vars_map


def render_template_with_dummy_data(text: str, broker_nombre: str = "Seguros Alianza") -> str:
    """Reemplaza todas las variables {clave} en el texto usando datos ficticios realistas."""
    if not text:
        return ""
    vars_map = get_dummy_variables(broker_nombre)

    def _replace(match: re.Match[str]) -> str:
        key = match.group(1).strip()
        return vars_map.get(key, match.group(0))

    return re.sub(r"\{([a-zA-Z0-9_]+)\}", _replace, text)


def _wrap_test_email_html(body: str, broker_nombre: str, es_html: bool = False) -> str:
    if es_html:
        content_html = body
    else:
        paragraphs = [
            f"<p style='margin:0 0 12px 0;line-height:1.6;'>{html.escape(p).replace(chr(10), '<br>')}</p>"
            for p in body.split("\n\n")
            if p.strip()
        ]
        content_html = "".join(paragraphs) or f"<p>{html.escape(body)}</p>"

    return (
        "<div style='font-family:Inter,Arial,sans-serif;max-width:620px;margin:0 auto;"
        "background:#ffffff;border:1px solid #e2e8f0;border-radius:12px;overflow:hidden;'>"
        "<div style='background:#0f172a;color:#ffffff;padding:14px 20px;font-size:13px;"
        "font-weight:700;'>"
        f"{html.escape(broker_nombre)} · Vista previa de notificación (Prueba)"
        "</div>"
        f"<div style='padding:22px 24px;color:#1e293b;font-size:14px;'>{content_html}</div>"
        "<div style='background:#f8fafc;color:#64748b;padding:12px 20px;font-size:11px;"
        "border-top:1px solid #e2e8f0;'>"
        "Este es un envío de prueba generado con datos de ejemplo desde Wesbyte."
        "</div>"
        "</div>"
    )


def _get_cliente_info(cliente_id: int) -> Dict[str, Any]:
    sql = f"""
    SELECT c.id, c.nombre, c.email, c.plan_id, IFNULL(p.codigo, CAST(c.plan_id AS STRING)) AS plan_codigo
    FROM `{BIGQUERY_PROJECT_ID}.{WESBYTES_DATASET_ID}.clientes` c
    LEFT JOIN `{BIGQUERY_PROJECT_ID}.{WESBYTES_DATASET_ID}.planes` p ON c.plan_id = p.id
    WHERE c.id = @cid
    LIMIT 1
    """
    cfg = bigquery.QueryJobConfig(
        query_parameters=[bigquery.ScalarQueryParameter("cid", "INT64", int(cliente_id))]
    )
    rows = [dict(r) for r in get_bq_client().query(sql, job_config=cfg).result()]
    return rows[0] if rows else {"id": cliente_id, "nombre": "Wesbyte Demo", "email": "", "plan_id": 0, "plan_codigo": ""}


async def send_test_notification(
    cliente_id: int,
    notificacion_codigo: str,
    canal: str,
    destino: str,
    asunto: Optional[str] = None,
    mensaje: Optional[str] = None,
    es_html: Optional[bool] = None,
) -> Dict[str, Any]:
    """
    Envía una notificación de prueba al destino indicado por correo (Resend)
    o WhatsApp (Evolution API), reemplazando variables con datos ficticios y
    adjuntando un PDF dummy en memoria solo para envio_poliza y condiciones_renovacion.
    """
    code = (notificacion_codigo or "").strip()
    meta = CATALOGO_POR_CODIGO.get(code)
    if not meta:
        raise ValueError(f"Tipo de notificación no válido: '{code}'.")

    canal_norm = (canal or "").strip().lower()
    if canal_norm not in ("email", "whatsapp"):
        raise ValueError("El canal debe ser 'email' o 'whatsapp'.")

    clean_dest = (destino or "").strip()
    if not clean_dest:
        raise ValueError("Debes indicar un destino válido.")

    cliente_info = _get_cliente_info(cliente_id)
    broker_nombre = str(cliente_info.get("nombre") or "Wesbyte").strip()

    # Si no se pasó mensaje en pantalla, leer de BD / catálogo
    resolved_msg = (mensaje or "").strip()
    resolved_subject = (asunto or "").strip()
    resolved_html = bool(es_html) if es_html is not None else False

    if not resolved_msg or (canal_norm == "email" and not resolved_subject):
        cfg_data = get_cliente_notificaciones_y_plantillas(
            client_id=cliente_id,
            plan_id=str(cliente_info.get("plan_id") or ""),
            plan_codigo=str(cliente_info.get("plan_codigo") or ""),
        )
        item_cfg = next(
            (n for n in cfg_data.get("notificaciones", []) if n.get("codigo") == code),
            {},
        )
        if not resolved_msg:
            resolved_msg = str(
                item_cfg.get(f"{canal_norm}_mensaje")
                or meta.get(f"default_mensaje_{canal_norm}")
                or ""
            )
        if canal_norm == "email" and not resolved_subject:
            resolved_subject = str(
                item_cfg.get("email_asunto")
                or meta.get("default_asunto_email")
                or f"Prueba de {meta.get('nombre', code)}"
            )
        if es_html is None and canal_norm == "email":
            resolved_html = bool(item_cfg.get("email_es_html", False))

    rendered_msg = render_template_with_dummy_data(resolved_msg, broker_nombre)
    rendered_subject = render_template_with_dummy_data(resolved_subject, broker_nombre)

    incluye_pdf = code in NOTIFICACIONES_CON_PDF
    pdf_bytes: Optional[bytes] = None
    pdf_b64: Optional[str] = None
    pdf_filename = f"prueba_{code}.pdf"
    if incluye_pdf:
        pdf_bytes = build_dummy_pdf_bytes()
        pdf_b64 = base64.b64encode(pdf_bytes).decode("ascii")

    if canal_norm == "email":
        if "@" not in clean_dest:
            raise ValueError("El correo electrónico de destino no es válido.")
        html_body = _wrap_test_email_html(rendered_msg, broker_nombre, es_html=resolved_html)
        text_body = re.sub(r"<[^>]+>", "", rendered_msg) if resolved_html else rendered_msg
        attachments = None
        if incluye_pdf and pdf_b64:
            attachments = [
                {
                    "filename": pdf_filename,
                    "content_base64": pdf_b64,
                    "content_type": "application/pdf",
                }
            ]
        try:
            res = EmailService().send_email(
                to=[clean_dest],
                subject=f"[PRUEBA] {rendered_subject}",
                html=html_body,
                text=text_body,
                from_sender=f"{broker_nombre} <notifications@wesbyte.app>",
                reply_to=str(cliente_info.get("email") or "").strip() or None,
                attachments=attachments,
            )
        except EmailServiceError as exc:
            raise RuntimeError(str(exc)) from exc

        return {
            "success": True,
            "canal": "email",
            "destino": clean_dest,
            "incluye_pdf": incluye_pdf,
            "email_id": res.get("id"),
            "message": (
                f"Correo de prueba enviado a {clean_dest}"
                + (" con PDF adjunto." if incluye_pdf else ".")
            ),
        }

    # canal_norm == "whatsapp"
    digits = re.sub(r"\D", "", clean_dest)
    if len(digits) < 8:
        raise ValueError("El número de WhatsApp debe incluir código de país (ej. 593991234567).")

    try:
        await sync_client_whatsapp_from_admin(cliente_id)
    except WhatsAppClientError as exc:
        logger.warning(f"No se pudo verificar aprovisionamiento WA para prueba cliente {cliente_id}: {exc.detail}")

    wa_category = NOTIF_TO_WA_CATEGORY.get(code, "renewal")
    try:
        wa_res = await WhatsAppClient().send_message(
            client_id=cliente_id,
            category=wa_category,
            recipient_phone=digits,
            message_text=rendered_msg,
            pdf_base64=pdf_b64 if incluye_pdf else None,
            pdf_filename=pdf_filename if incluye_pdf else None,
        )
    except WhatsAppClientError as exc:
        raise RuntimeError(f"Error en WhatsApp (HTTP {exc.status_code}): {exc.detail}") from exc

    return {
        "success": True,
        "canal": "whatsapp",
        "destino": digits,
        "incluye_pdf": incluye_pdf,
        "whatsapp_response": wa_res,
        "message": (
            f"Mensaje de prueba encolado/enviado por WhatsApp a +{digits}"
            + (" con PDF adjunto." if incluye_pdf else ".")
        ),
    }
