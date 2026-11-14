import logging
from typing import List, Optional, Any, Dict
from fastapi import APIRouter, Depends, Header, HTTPException, status
from pydantic import BaseModel, EmailStr, Field

from app.config import INTERNAL_EMAIL_SERVICE_KEY
from app.services.email_service import EmailService, EmailServiceError
from app.services.birthday_notification_service import BirthdayNotificationService

logger = logging.getLogger("wesbyte_portal.routers.internal_notifications")

router = APIRouter(
    prefix="/api/v1/internal",
    tags=["Servicios Internos & Notificaciones"],
)


# ============================================================================
# SEGURIDAD Y CONTROL DE ACCESO
# ============================================================================
async def verify_internal_api_key(
    x_api_key: Optional[str] = Header(None, alias="x-api-key"),
) -> str:
    """Valida que el header 'x-api-key' coincida con INTERNAL_EMAIL_SERVICE_KEY."""
    if not x_api_key or x_api_key.strip() != INTERNAL_EMAIL_SERVICE_KEY:
        logger.warning("Intento de acceso no autorizado a endpoints internos con x-api-key inválida.")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Acceso no autorizado: Header 'x-api-key' ausente o inválido.",
        )
    return x_api_key


# ============================================================================
# ESQUEMAS PYDANTIC V2
# ============================================================================
class EmailAttachment(BaseModel):
    filename: str = Field(..., min_length=1, description="Nombre del archivo adjunto (ej. 'poliza.pdf')")
    content_base64: Optional[str] = Field(None, description="Contenido del PDF/archivo codificado en Base64")
    content: Optional[Any] = Field(None, description="Contenido en Base64 (str) o lista de enteros (List[int])")
    path: Optional[str] = Field(None, description="URL pública remota del archivo (opcional si no se envía Base64)")
    content_type: Optional[str] = Field("application/pdf", description="MIME type del adjunto (por defecto application/pdf)")


class DirectEmailSendRequest(BaseModel):
    to: List[str] = Field(..., min_length=1, description="Lista de destinatarios principales (emails)")
    subject: str = Field(..., min_length=1, max_length=255, description="Asunto del correo")
    html: str = Field(..., min_length=1, description="Cuerpo del mensaje en formato HTML")
    text: Optional[str] = Field(None, description="Versión en texto plano para clientes sin soporte HTML")
    cc: Optional[List[str]] = Field(None, description="Lista de correos en copia (CC)")
    reply_to: Optional[str] = Field(None, description="Dirección de respuesta (Reply-To: si el cliente responde, le llega directo al broker)")
    from_sender: Optional[str] = Field(None, description="Remitente completo (ej. 'Seguros Cóndor <notifications@wesbyte.app>')")
    from_name: Optional[str] = Field(None, description="Nombre del broker (ej. 'Seguros Cóndor'); autoformatea a '{from_name} <notifications@wesbyte.app>'")
    attachments: Optional[List[EmailAttachment]] = Field(None, description="Lista opcional de archivos adjuntos (ej. PDFs en Base64 o URL)")


class BirthdayDryRunRequest(BaseModel):
    forzar_fecha: Optional[str] = Field(
        None,
        pattern=r"^\d{4}-\d{2}-\d{2}$",
        description="Fecha forzada para pruebas en formato YYYY-MM-DD (ej. 2026-11-26). Si no se envía, usa hoy.",
    )
    email_prueba: Optional[str] = Field(
        None,
        description="Si se envía, en lugar de despachar a clientes reales, se envía un correo de muestra a esta dirección.",
    )
    solo_lectura: bool = Field(
        False,
        description="Si es True, solo retorna la lista de cumpleañeros detectados y tenants evaluados sin invocar a Resend.",
    )


# ============================================================================
# ENDPOINTS
# ============================================================================
@router.post(
    "/emails/send",
    summary="Despacho genérico directo de correos vía Resend",
    description="Permite enviar correos transaccionales directos usando la API oficial de Resend y la identidad de Wesbyte.",
    dependencies=[Depends(verify_internal_api_key)],
)
async def send_direct_email(payload: DirectEmailSendRequest) -> Dict[str, Any]:
    """Endpoint directo para envío de correos transaccionales con Resend."""
    service = EmailService()

    sender = payload.from_sender
    if not sender and payload.from_name:
        sender = f"{payload.from_name.strip()} <notifications@wesbyte.app>"

    try:
        atts = [a.model_dump(exclude_none=True) for a in payload.attachments] if payload.attachments else None
        resultado = service.send_email(
            to=payload.to,
            subject=payload.subject,
            html=payload.html,
            text=payload.text,
            cc=payload.cc,
            reply_to=payload.reply_to,
            from_sender=sender,
            attachments=atts,
        )
        return resultado
    except EmailServiceError as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Error despachando correo con Resend: {str(exc)}",
        )


@router.post(
    "/notifications/birthday-dry-run",
    summary="Simulación y prueba manual del flujo de cumpleaños multi-tenant",
    description=(
        "Consulta la tabla central de clientes en BigQuery, valida la configuración del plan, "
        "recorre dinámicamente las tablas maestras de cada tenant activo y despacha o simula los correos de felicitación."
    ),
    dependencies=[Depends(verify_internal_api_key)],
)
async def birthday_dry_run(payload: BirthdayDryRunRequest) -> Dict[str, Any]:
    """Endpoint de simulación y prueba controlada de felicitaciones de cumpleaños."""
    birthday_service = BirthdayNotificationService()
    try:
        resultado = birthday_service.run_birthday_process(
            forzar_fecha=payload.forzar_fecha,
            email_prueba=str(payload.email_prueba) if payload.email_prueba else None,
            solo_lectura=payload.solo_lectura,
        )
        return resultado
    except ValueError as val_err:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(val_err),
        )
    except Exception as exc:
        logger.error(f"Error procesando dry-run de cumpleaños: {exc}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Error durante el procesamiento del flujo multi-tenant: {str(exc)}",
        )


@router.get(
    "/clientes/{client_id}/notificaciones",
    summary="Obtener notificaciones habilitadas y plantillas de un cliente",
    dependencies=[Depends(verify_internal_api_key)],
)
async def get_internal_client_notifications(client_id: int) -> Dict[str, Any]:
    """Devuelve las 5 notificaciones, su estado por canal y las plantillas personalizadas del cliente."""
    from app.services.notificaciones_config_service import get_cliente_notificaciones_y_plantillas

    data = get_cliente_notificaciones_y_plantillas(client_id=client_id)
    return {"success": True, "client_id": client_id, **data}
