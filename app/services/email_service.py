import logging
import datetime
from typing import List, Optional, Union, Dict, Any
import resend

from app.config import (
    RESEND_API_KEY,
    EMAIL_SENDER_DEFAULT,
    SOPORTE_EMAIL,
    WESBYTE_APP_URL,
)

logger = logging.getLogger("wesbyte_portal.services.email")


class EmailServiceError(Exception):
    """Excepción base para errores en el servicio de correo."""
    pass


class EmailService:
    """
    Servicio centralizado para el despacho de correos transaccionales
    usando la API oficial de Resend.
    """

    def __init__(
        self,
        api_key: Optional[str] = None,
        sender_default: Optional[str] = None,
    ):
        self.api_key = (api_key or RESEND_API_KEY or "").strip()
        self.sender_default = (sender_default or EMAIL_SENDER_DEFAULT or "Wesbyte <notifications@wesbyte.app>").strip()

        if not self.api_key:
            logger.warning("EmailService inicializado sin RESEND_API_KEY. Los envíos fallarán hasta configurar la clave.")
        else:
            resend.api_key = self.api_key

    def send_email(
        self,
        to: Union[List[str], str],
        subject: str,
        html: str,
        text: Optional[str] = None,
        cc: Optional[Union[List[str], str]] = None,
        reply_to: Optional[Union[List[str], str]] = None,
        from_sender: Optional[str] = None,
        attachments: Optional[List[Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        """
        Envía un correo electrónico a través de Resend.
        Retorna un diccionario con el id de despacho y metadatos de respuesta.
        Permite personalizar 'from_sender' (ej. f"{nombre_broker} <notifications@wesbyte.app>")
        y 'reply_to' (ej. email_broker) para que las respuestas del cliente lleguen directo al broker.
        """
        if not self.api_key:
            raise EmailServiceError("No se ha configurado la variable RESEND_API_KEY en el entorno.")

        # Determinar remitente autenticado
        sender = (from_sender or self.sender_default).strip()

        # Normalizar destinatarios 'to'
        to_list: List[str] = []
        if isinstance(to, str):
            to_list = [addr.strip() for addr in to.split(",") if addr.strip()]
        elif isinstance(to, list):
            to_list = [str(addr).strip() for addr in to if str(addr).strip()]

        if not to_list:
            raise EmailServiceError("La lista de destinatarios ('to') no puede estar vacía.")

        # Normalizar 'cc' si existe
        cc_list: Optional[List[str]] = None
        if cc:
            if isinstance(cc, str):
                cc_list = [addr.strip() for addr in cc.split(",") if addr.strip()]
            elif isinstance(cc, list):
                cc_list = [str(addr).strip() for addr in cc if str(addr).strip()]
            if not cc_list:
                cc_list = None

        # Normalizar 'reply_to' si existe
        reply_to_val: Optional[str] = None
        if reply_to:
            if isinstance(reply_to, list) and reply_to:
                reply_to_val = str(reply_to[0]).strip()
            elif isinstance(reply_to, str) and reply_to.strip():
                reply_to_val = reply_to.strip()

        # Construir payload para Resend
        params: Dict[str, Any] = {
            "from": sender,
            "to": to_list,
            "subject": subject.strip(),
            "html": html,
        }
        if text:
            params["text"] = text.strip()
        if cc_list:
            params["cc"] = cc_list
        if reply_to_val:
            params["reply_to"] = reply_to_val

        # Normalizar adjuntos (ej. PDFs) para Resend
        if attachments:
            norm_atts: List[Dict[str, Any]] = []
            for att in attachments:
                if not isinstance(att, dict):
                    continue
                fname = str(att.get("filename") or "documento.pdf").strip()
                item: Dict[str, Any] = {"filename": fname}
                if att.get("content_type"):
                    item["content_type"] = str(att["content_type"]).strip()
                if att.get("path"):
                    item["path"] = str(att["path"]).strip()
                else:
                    raw_content = att.get("content_base64")
                    if raw_content is None:
                        raw_content = att.get("content")
                    if raw_content is None:
                        raise EmailServiceError(f"El adjunto '{fname}' debe incluir 'content_base64', 'content' o 'path'.")
                    if isinstance(raw_content, (bytes, bytearray)):
                        item["content"] = list(raw_content)
                    else:
                        item["content"] = raw_content
                norm_atts.append(item)
            if norm_atts:
                params["attachments"] = norm_atts

        try:
            logger.info(
                f"Despachando correo Resend: from='{sender}' to={to_list} reply_to='{reply_to_val}' cc={cc_list} subject='{subject}'"
            )
            # Asegurar API key en cliente global de Resend
            resend.api_key = self.api_key

            response = resend.Emails.send(params)

            # Normalizar respuesta (objeto Resend o diccionario)
            email_id = None
            if isinstance(response, dict):
                email_id = response.get("id")
            elif hasattr(response, "id"):
                email_id = getattr(response, "id")
            elif hasattr(response, "response"):
                # Compatibilidad con versiones que devuelven envoltorio
                email_id = getattr(response, "response", {}).get("id")

            logger.info(f"Correo enviado exitosamente por Resend. ID: {email_id} a {to_list}")
            return {
                "success": True,
                "id": str(email_id) if email_id else "unknown_id",
                "to": to_list,
                "from": sender,
                "sender": sender,
                "reply_to": reply_to_val,
                "cc": cc_list or [],
                "subject": subject,
                "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            }

        except Exception as exc:
            err_msg = f"Error despachando correo con Resend a {to_list}: {str(exc)}"
            logger.error(err_msg, exc_info=True)
            raise EmailServiceError(err_msg) from exc

    @staticmethod
    def generate_birthday_html(
        nombre_cliente: str,
        nombre_broker: str,
        email_broker: Optional[str] = None,
        logo_cliente_url: Optional[str] = None,
    ) -> str:
        """
        Genera la plantilla HTML responsive con diseño moderno, elegante y formal,
        con el logo del cliente (si existe), el logo de Wesbyte y footer 'enviado por wesbyte.app'.
        """
        nombre_limpio = (nombre_cliente or "Estimado/a Cliente").strip().title()
        broker_limpio = (nombre_broker or "Tu Asesor de Seguros").strip()
        wesbyte_logo_url = f"{WESBYTE_APP_URL.rstrip('/')}/static/img/wesbyte-logo-wave-sine-integrated.png"
        cliente_logo_html = (
            f'<img src="{logo_cliente_url.strip()}" alt="{broker_limpio}" '
            f'style="max-height:56px;max-width:180px;object-fit:contain;border-radius:8px;background:#fff;padding:6px 10px;" />'
            if logo_cliente_url and logo_cliente_url.strip()
            else f'<span style="font-size:16px;font-weight:800;color:#ffffff;letter-spacing:0.02em;">{broker_limpio}</span>'
        )
        broker_email_html = (
            f'<p style="margin: 4px 0 0 0; font-size: 13px; color: #64748b;">📧 <a href="mailto:{email_broker.strip()}" style="color: #2563eb; text-decoration: none;">{email_broker.strip()}</a></p>'
            if email_broker and email_broker.strip()
            else ""
        )

        return f"""<!DOCTYPE html>
<html lang="es">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>¡Feliz Cumpleaños {nombre_limpio}!</title>
  <style>
    body {{
      margin: 0;
      padding: 0;
      background-color: #f1f5f9;
      font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif;
      color: #1e293b;
      -webkit-font-smoothing: antialiased;
    }}
    .email-container {{
      max-width: 600px;
      margin: 32px auto;
      background: #ffffff;
      border-radius: 18px;
      overflow: hidden;
      box-shadow: 0 10px 25px -5px rgba(15, 23, 42, 0.08), 0 8px 10px -6px rgba(15, 23, 42, 0.04);
      border: 1px solid #e2e8f0;
    }}
    .header-banner {{
      background: linear-gradient(135deg, #0f172a 0%, #1e3a8a 50%, #2563eb 100%);
      padding: 44px 32px 36px 32px;
      text-align: center;
      color: #ffffff;
    }}
    .brand-pill {{
      display: inline-block;
      padding: 6px 14px;
      background: rgba(255, 255, 255, 0.16);
      backdrop-filter: blur(8px);
      border: 1px solid rgba(255, 255, 255, 0.25);
      border-radius: 9999px;
      font-size: 11px;
      font-weight: 700;
      letter-spacing: 0.1em;
      text-transform: uppercase;
      color: #93c5fd;
      margin-bottom: 16px;
    }}
    .birthday-icon {{
      font-size: 52px;
      line-height: 1;
      margin-bottom: 12px;
      display: inline-block;
      filter: drop-shadow(0 4px 6px rgba(0,0,0,0.2));
    }}
    .main-title {{
      margin: 0;
      font-size: 26px;
      font-weight: 800;
      letter-spacing: -0.02em;
      line-height: 1.25;
      color: #ffffff;
    }}
    .content-body {{
      padding: 36px 36px 28px 36px;
    }}
    .greeting {{
      font-size: 19px;
      font-weight: 700;
      color: #0f172a;
      margin-top: 0;
      margin-bottom: 16px;
    }}
    .paragraph {{
      font-size: 15px;
      line-height: 1.65;
      color: #334155;
      margin-bottom: 18px;
    }}
    .highlight-card {{
      background: linear-gradient(135deg, #eff6ff 0%, #f8fafc 100%);
      border-left: 4px solid #2563eb;
      border-radius: 12px;
      padding: 20px 22px;
      margin: 24px 0;
      border-top: 1px solid #e2e8f0;
      border-right: 1px solid #e2e8f0;
      border-bottom: 1px solid #e2e8f0;
    }}
    .highlight-card p {{
      margin: 0;
      font-size: 14px;
      line-height: 1.6;
      color: #1e3a8a;
      font-weight: 500;
    }}
    .broker-box {{
      margin-top: 28px;
      padding: 18px 20px;
      background-color: #f8fafc;
      border-radius: 12px;
      border: 1px dashed #cbd5e1;
      display: flex;
      align-items: center;
    }}
    .broker-label {{
      font-size: 11px;
      text-transform: uppercase;
      font-weight: 700;
      color: #64748b;
      letter-spacing: 0.05em;
      margin: 0 0 4px 0;
    }}
    .broker-name {{
      font-size: 15px;
      font-weight: 700;
      color: #0f172a;
      margin: 0;
    }}
    .footer {{
      background-color: #f8fafc;
      border-top: 1px solid #e2e8f0;
      padding: 24px 32px;
      text-align: center;
      font-size: 12px;
      color: #94a3b8;
      line-height: 1.6;
    }}
    .footer a {{
      color: #2563eb;
      text-decoration: none;
      font-weight: 600;
    }}
    @media only screen and (max-width: 600px) {{
      .email-container {{
        margin: 0;
        border-radius: 0;
        border: none;
      }}
      .content-body {{
        padding: 24px 20px;
      }}
      .header-banner {{
        padding: 32px 20px;
      }}
      .main-title {{
        font-size: 22px;
      }}
    }}
  </style>
</head>
<body>
  <div style="background-color: #f1f5f9; padding: 24px 12px;">
    <div class="email-container">
      
      <!-- HEADER CON LOGO DEL CLIENTE Y DE WESBYTE -->
      <div class="header-banner">
        <div style="display:flex;justify-content:space-between;align-items:center;gap:12px;margin-bottom:20px;flex-wrap:wrap;">
          <div>{cliente_logo_html}</div>
          <div>
            <img src="{wesbyte_logo_url}" alt="Wesbyte" style="max-height:34px;object-fit:contain;filter:brightness(0) invert(1);" />
          </div>
        </div>
        <div class="birthday-icon">🎉</div>
        <h1 class="main-title">¡Feliz Cumpleaños, {nombre_limpio}!</h1>
      </div>

      <!-- CUERPO PRINCIPAL DEL MENSAJE -->
      <div class="content-body">
        <p class="greeting">Estimado/a {nombre_limpio},</p>
        
        <p class="paragraph">
          Hoy es un día muy especial y queremos hacer una pausa en el camino para enviarle un cordial saludo en su cumpleaños, deseándole un año lleno de salud, bienestar, metas cumplidas y grandes satisfacciones tanto personales como profesionales.
        </p>

        <!-- TARJETA DESTACADA CON LOS MEJORES DESEOS -->
        <div class="highlight-card">
          <p>
            ✨ <em>"Que este nuevo año de vida le traiga abundantes bendiciones, felicidad junto a los suyos y la absoluta tranquilidad de saber que sus proyectos están bien respaldados y en las mejores manos."</em>
          </p>
        </div>

        <p class="paragraph">
          Para nosotros en <strong>{broker_limpio}</strong>, en alianza tecnológica con <strong>Wesbyte</strong>, es un auténtico privilegio acompañarle y velar día a día por la seguridad y el respaldo integral de lo que más valora.
        </p>

        <!-- FIRMA DEL BROKER / ASESOR -->
        <div class="broker-box">
          <div>
            <p class="broker-label">Con el aprecio de tu Asesor de Seguros</p>
            <p class="broker-name">{broker_limpio}</p>
            {broker_email_html}
          </div>
        </div>
      </div>

      <!-- FOOTER INSTITUCIONAL -->
      <div class="footer">
        <p style="margin: 0;">
          Enviado por <a href="https://wesbyte.app">wesbyte.app</a>
        </p>
      </div>

    </div>
  </div>
</body>
</html>
"""

    @staticmethod
    def generate_birthday_text(
        nombre_cliente: str,
        nombre_broker: str,
        email_broker: Optional[str] = None,
    ) -> str:
        """Versión en texto plano para clientes de correo sin soporte HTML."""
        nombre_limpio = (nombre_cliente or "Estimado/a Cliente").strip().title()
        broker_limpio = (nombre_broker or "Tu Asesor de Seguros").strip()
        contacto = f" ({email_broker.strip()})" if email_broker and email_broker.strip() else ""

        return (
            f"¡Feliz Cumpleaños, {nombre_limpio}!\n\n"
            f"De parte de todo el equipo de {broker_limpio} y la plataforma Wesbyte, "
            f"le enviamos un caluroso saludo en su día especial, deseándole salud, éxitos "
            f"y prosperidad en este nuevo año de vida.\n\n"
            f"Agradecemos su confianza y le reiteramos nuestro compromiso de estar siempre a su lado.\n\n"
            f"Atentamente,\n"
            f"{broker_limpio}{contacto}\n"
            f"Plataforma Wesbyte ({WESBYTE_APP_URL})\n"
        )
