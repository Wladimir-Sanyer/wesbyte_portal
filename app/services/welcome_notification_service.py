"""
Servicio de notificación de bienvenida / confirmación de pago (Correo + WhatsApp)
cuando un cliente contrata un plan nuevo o renueva/mejora su suscripción en Wesbyte.
"""
from __future__ import annotations

import asyncio
import html
import logging
import os
import re
from typing import Optional

from app.config import BIGQUERY_PROJECT_ID, SOPORTE_EMAIL, WESBYTE_APP_URL, WESBYTES_DATASET_ID
from app.database import get_query
from app.services.email_service import EmailService
from app.services.whatsapp_client import WhatsAppClient

logger = logging.getLogger("wesbyte_portal.welcome_notification")

WHATSAPP_WESBYTE_SENDER_CLIENT_ID = int(os.getenv("WHATSAPP_WESBYTE_SENDER_CLIENT_ID", "1"))


def _normalizar_telefono_wa(celular: str) -> str:
    digits = re.sub(r"\D", "", str(celular or ""))
    if len(digits) == 10 and digits.startswith("09"):
        return "593" + digits[1:]
    return digits


def _build_welcome_content(
    *,
    nombre_empresa: str,
    nombres_completos: str,
    correo: str,
    plan_nombre: str,
    monto_pagado: float,
    es_renovacion: bool,
    tx_id: str = "",
) -> tuple[str, str, str, str]:
    """Genera (asunto_email, html_email, texto_email, mensaje_whatsapp)."""
    portal_url = (WESBYTE_APP_URL or "https://portal.wesbyte.app").strip().rstrip("/")
    saludo = nombres_completos.strip() or nombre_empresa.strip() or "Cliente"
    plan_txt = plan_nombre.strip() or "Wesbyte"
    monto_txt = f"${monto_pagado:,.2f} USD" if monto_pagado else ""
    ref_txt = f" (Ref. PayPhone: {tx_id})" if tx_id else ""

    if es_renovacion:
        subject = f"¡Gracias por tu pago! Plan {plan_txt} actualizado en Wesbyte"
        titulo_accion = f"¡Gracias por renovar/actualizar tu plan {plan_txt}!"
        intro = (
            f"Hemos recibido correctamente tu pago"
            + (f" por <strong>{html.escape(monto_txt)}</strong>" if monto_txt else "")
            + f"{html.escape(ref_txt)}. Tu suscripción para <strong>{html.escape(nombre_empresa)}</strong> ya se encuentra activa."
        )
        wa_intro = (
            f"¡Hola {saludo}! 🙌\n\n"
            f"Gracias por tu pago"
            + (f" de *{monto_txt}*" if monto_txt else "")
            + f"{ref_txt}. Tu plan *{plan_txt}* para *{nombre_empresa}* ya quedó activo y renovado."
        )
    else:
        subject = f"¡Bienvenido a Wesbyte! Tu plan {plan_txt} está activo"
        titulo_accion = f"¡Bienvenido a tu plan {plan_txt}!"
        intro = (
            f"Muchas gracias por tu pago"
            + (f" por <strong>{html.escape(monto_txt)}</strong>" if monto_txt else "")
            + f"{html.escape(ref_txt)}. Tu entorno privado para <strong>{html.escape(nombre_empresa)}</strong> ya está activo y listo para operar."
        )
        wa_intro = (
            f"¡Hola {saludo}! 🎉\n\n"
            f"¡Bienvenido a tu plan *{plan_txt}* en *Wesbyte*! Muchas gracias por tu pago"
            + (f" de *{monto_txt}*" if monto_txt else "")
            + f"{ref_txt}. Tu entorno para *{nombre_empresa}* ya está activo."
        )

    html_body = f"""
    <div style="font-family: Inter, Arial, sans-serif; max-width: 600px; margin: 0 auto; color: #1e293b; background: #ffffff; border: 1px solid #e2e8f0; border-radius: 16px; overflow: hidden;">
      <div style="background: linear-gradient(135deg, #12163a, #4f46e5); padding: 28px 24px; color: #ffffff; text-align: center;">
        <h1 style="margin: 0; font-size: 22px; font-weight: 800;">{html.escape(titulo_accion)}</h1>
      </div>
      <div style="padding: 28px 24px; line-height: 1.6; font-size: 15px;">
        <p style="margin-top: 0;">Hola <strong>{html.escape(saludo)}</strong>,</p>
        <p>{intro}</p>
        <div style="background: #f8fafc; border: 1px solid #cbd5e1; border-radius: 12px; padding: 16px 18px; margin: 20px 0;">
          <div style="font-weight: 700; color: #0f172a; margin-bottom: 8px;">Tus credenciales de acceso</div>
          <p style="margin: 0 0 8px 0;">Tus credenciales son <strong>las mismas con las que te registraste</strong>:</p>
          <p style="margin: 0;"><strong>Usuario / Correo:</strong> {html.escape(correo)}</p>
          <p style="margin: 4px 0 0 0;"><strong>Contraseña:</strong> La que definiste al crear tu cuenta</p>
        </div>
        <p style="font-size: 14px; color: #334155; margin: 0 0 16px 0;">
          <strong>Facturación:</strong> Tu factura electrónica será enviada a tu correo electrónico (<strong>{html.escape(correo)}</strong>).
        </p>
        <div style="text-align: center; margin: 26px 0;">
          <a href="{html.escape(portal_url)}" style="display: inline-block; background: #4f46e5; color: #ffffff; text-decoration: none; font-weight: 700; padding: 13px 26px; border-radius: 10px;">
            Ingresar a {html.escape(portal_url)}
          </a>
        </div>
        <p style="font-size: 13px; color: #64748b; margin-bottom: 0;">
          Si necesitas ayuda, escríbenos a <a href="mailto:{html.escape(SOPORTE_EMAIL)}" style="color: #4f46e5;">{html.escape(SOPORTE_EMAIL)}</a>.
        </p>
      </div>
    </div>
    """

    text_body = (
        f"Hola {saludo},\n\n"
        f"{titulo_accion}\n"
        f"Gracias por tu pago{(' de ' + monto_txt) if monto_txt else ''}{ref_txt}.\n\n"
        f"Tus credenciales de acceso son las mismas con las que te registraste:\n"
        f"- Usuario / Correo: {correo}\n"
        f"- Contraseña: La que definiste al crear tu cuenta\n\n"
        f"Ingresa aquí: {portal_url}\n"
    )

    wa_body = (
        f"{wa_intro}\n\n"
        f"🔐 *Tus credenciales de acceso son las mismas con las que te registraste:*\n"
        f"• Usuario: {correo}\n"
        f"• Contraseña: La que creaste en tu registro\n\n"
        f"🌐 Ingresa a tu plataforma aquí:\n{portal_url}\n\n"
        f"🧾 Tu factura electrónica será enviada a tu correo electrónico ({correo}).\n\n"
        f"Cualquier duda estamos para ayudarte."
    )
    return subject, html_body, text_body, wa_body


async def enviar_bienvenida_pago_cliente(
    *,
    cliente_id: int,
    nombre_empresa: str,
    nombres_completos: str,
    correo: str,
    celular: str,
    plan_nombre: str,
    monto_pagado: float = 0.0,
    es_renovacion: bool = False,
    tx_id: str = "",
) -> None:
    """Envía el mensaje de bienvenida/agradecimiento por correo y WhatsApp sin interrumpir el flujo."""
    subject, html_body, text_body, wa_body = _build_welcome_content(
        nombre_empresa=nombre_empresa,
        nombres_completos=nombres_completos,
        correo=correo,
        plan_nombre=plan_nombre,
        monto_pagado=monto_pagado,
        es_renovacion=es_renovacion,
        tx_id=tx_id,
    )

    if correo and "@" in correo:
        try:
            await asyncio.to_thread(
                EmailService().send_email,
                to=correo,
                subject=subject,
                html=html_body,
                text=text_body,
            )
            logger.info("Correo de bienvenida/pago enviado a %s (cliente_id=%s)", correo, cliente_id)
        except Exception as exc:
            logger.warning("No se pudo enviar correo de bienvenida a %s: %s", correo, exc)

    digits = _normalizar_telefono_wa(celular)
    if len(digits) >= 8:
        try:
            await WhatsAppClient().send_message(
                client_id=WHATSAPP_WESBYTE_SENDER_CLIENT_ID,
                category="renewal",
                recipient_phone=digits,
                message_text=wa_body,
            )
            logger.info("WhatsApp de bienvenida/pago enviado a %s (cliente_id=%s)", digits, cliente_id)
        except Exception as exc:
            logger.warning("No se pudo enviar WhatsApp de bienvenida a %s (cliente_id=%s): %s", digits, cliente_id, exc)


async def enviar_bienvenida_renovacion_por_cliente_id(
    *,
    cliente_id: int,
    plan_nombre: str,
    monto_pagado: float = 0.0,
    tx_id: str = "",
) -> None:
    """Busca correo y celular del cliente en BigQuery y envía la bienvenida por renovación/upgrade."""
    try:
        df = await asyncio.to_thread(
            get_query,
            f"""
            SELECT nombre, nombres_contacto, email, celular
            FROM `{BIGQUERY_PROJECT_ID}.{WESBYTES_DATASET_ID}.clientes`
            WHERE id = {int(cliente_id)}
            LIMIT 1
            """,
        )
        if df.empty:
            return
        row = df.iloc[0].to_dict()
        await enviar_bienvenida_pago_cliente(
            cliente_id=cliente_id,
            nombre_empresa=str(row.get("nombre") or ""),
            nombres_completos=str(row.get("nombres_contacto") or row.get("nombre") or ""),
            correo=str(row.get("email") or ""),
            celular=str(row.get("celular") or ""),
            plan_nombre=plan_nombre,
            monto_pagado=monto_pagado,
            es_renovacion=True,
            tx_id=tx_id,
        )
    except Exception as exc:
        logger.warning("No se pudo preparar bienvenida de renovación para cliente_id=%s: %s", cliente_id, exc)
