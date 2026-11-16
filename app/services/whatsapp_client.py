"""
Cliente HTTP hacia el microservicio wesbyte_api (WhatsApp / Evolution API)
y sincronización con la base de datos administradora (wesbyte.administracion).
"""
from __future__ import annotations

import logging
from typing import Any, Dict, Optional

import httpx
from google.cloud import bigquery

from app.config import (
    BIGQUERY_PROJECT_ID,
    WESBYTES_DATASET_ID,
    WHATSAPP_API_URL,
    WHATSAPP_SERVICE_API_KEY,
)
from app.database import get_bq_client
from app.services.notificaciones_config_service import get_cliente_notificaciones_y_plantillas

logger = logging.getLogger("wesbyte_portal.whatsapp_client")


class WhatsAppClientError(Exception):
    def __init__(self, status_code: int, detail: str):
        self.status_code = status_code
        self.detail = detail
        super().__init__(detail)


class WhatsAppClient:
    """Cliente síncrono/asíncrono para consumir wesbyte_api."""

    FALLBACK_URLS = (
        "http://172.17.0.1:8000",
        "http://whatsapp_api:8000",
        "http://whatsapp.local:8000",
        "https://apiwhatsapp.wesbyte.com",
    )

    def __init__(
        self,
        base_url: str = WHATSAPP_API_URL,
        api_key: str = WHATSAPP_SERVICE_API_KEY,
        timeout: float = 20.0,
    ):
        self.base_url = base_url.rstrip("/")
        self.headers = {"X-API-Key": api_key, "Content-Type": "application/json"}
        self.timeout = timeout

    async def _request(
        self,
        method: str,
        path: str,
        timeout: Optional[float] = None,
        json: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        urls = [self.base_url] + [u for u in self.FALLBACK_URLS if u != self.base_url]
        last_exc: Optional[Exception] = None
        async with httpx.AsyncClient(timeout=timeout or self.timeout) as client:
            for base in urls:
                try:
                    resp = await client.request(
                        method,
                        f"{base}{path}",
                        headers=self.headers,
                        json=json,
                    )
                    return self._handle_response(resp)
                except httpx.ConnectError as exc:
                    last_exc = exc
                    logger.warning(f"No se pudo conectar a {base}{path}: {exc}. Probando siguiente ruta...")
                    continue
        raise last_exc or httpx.ConnectError("No se pudo conectar con wesbyte_api")

    def _handle_response(self, resp: httpx.Response) -> Dict[str, Any]:
        if resp.status_code >= 400:
            try:
                data = resp.json()
                detail = data.get("detail") or data.get("message") or resp.text
            except Exception:
                detail = resp.text or f"Error HTTP {resp.status_code}"
            raise WhatsAppClientError(resp.status_code, str(detail))
        return resp.json()

    async def provision_client(
        self,
        client_id: int,
        plan_code: str,
        monthly_limit: Optional[int] = None,
        daily_limit: Optional[int] = None,
        notify_birthdays: Optional[bool] = None,
        notify_reports: Optional[bool] = None,
        notify_renewals: Optional[bool] = None,
        notify_pending_fees: Optional[bool] = None,
    ) -> Dict[str, Any]:
        payload: Dict[str, Any] = {"plan_code": plan_code or "MEDIUM_SILVER"}
        if monthly_limit and monthly_limit > 0:
            payload["monthly_limit"] = int(monthly_limit)
        if daily_limit and daily_limit > 0:
            payload["daily_limit"] = int(daily_limit)
        if notify_birthdays is not None:
            payload["notify_birthdays"] = bool(notify_birthdays)
        if notify_reports is not None:
            payload["notify_reports"] = bool(notify_reports)
        if notify_renewals is not None:
            payload["notify_renewals"] = bool(notify_renewals)
        if notify_pending_fees is not None:
            payload["notify_pending_fees"] = bool(notify_pending_fees)

        return await self._request("POST", f"/api/v1/whatsapp/{client_id}/provision", json=payload)

    async def get_status(self, client_id: int) -> Dict[str, Any]:
        return await self._request("GET", f"/api/v1/whatsapp/{client_id}/status")

    async def connect_qr(self, client_id: int) -> Dict[str, Any]:
        return await self._request("POST", f"/api/v1/whatsapp/{client_id}/connect", timeout=35.0)

    async def update_preferences(
        self,
        client_id: int,
        notify_birthdays: Optional[bool] = None,
        notify_reports: Optional[bool] = None,
        notify_renewals: Optional[bool] = None,
        notify_pending_fees: Optional[bool] = None,
    ) -> Dict[str, Any]:
        payload: Dict[str, Any] = {}
        if notify_birthdays is not None:
            payload["notify_birthdays"] = bool(notify_birthdays)
        if notify_reports is not None:
            payload["notify_reports"] = bool(notify_reports)
        if notify_renewals is not None:
            payload["notify_renewals"] = bool(notify_renewals)
        if notify_pending_fees is not None:
            payload["notify_pending_fees"] = bool(notify_pending_fees)
        if not payload:
            return {}

        return await self._request("PATCH", f"/api/v1/whatsapp/{client_id}/preferences", json=payload)

    async def send_message(
        self,
        client_id: int,
        category: str,
        recipient_phone: str,
        message_text: str = "",
        pdf_base64: Optional[str] = None,
        pdf_filename: Optional[str] = None,
    ) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "client_id": int(client_id),
            "category": category,
            "recipient_phone": recipient_phone,
            "message_text": message_text,
        }
        if pdf_base64:
            payload["pdf_base64"] = pdf_base64
            payload["pdf_filename"] = pdf_filename or "documento.pdf"

        return await self._request("POST", "/api/v1/whatsapp/send", json=payload)


def map_notificaciones_to_wa_toggles(notificaciones: list[Dict[str, Any]]) -> Dict[str, bool]:
    """Convierte las 5 notificaciones del portal en los 4 toggles de wesbyte_api."""
    by_code = {n.get("codigo"): bool(n.get("whatsapp_habilitado")) for n in notificaciones}
    return {
        "notify_birthdays": by_code.get("cumpleanios", False),
        "notify_renewals": by_code.get("proxima_renovacion", True) or by_code.get("condiciones_renovacion", True),
        "notify_pending_fees": by_code.get("regularizaciones", True),
        "notify_reports": by_code.get("envio_poliza", True),
    }


async def sync_client_whatsapp_from_admin(client_id: int) -> Dict[str, Any]:
    """
    Valida al cliente en BigQuery (wesbyte.administracion.clientes + planes + notificaciones_habilitadas)
    y sincroniza su configuración/cuota con wesbyte_api.
    """
    sql = f"""
    SELECT
        c.id,
        c.activo,
        c.estado,
        c.plan_id,
        IFNULL(p.codigo, CAST(c.plan_id AS STRING)) AS plan_codigo,
        COALESCE(c.limite_mensual_whatsapp, p.limite_mensual_whatsapp, 50) AS limite_mensual_whatsapp
    FROM `{BIGQUERY_PROJECT_ID}.{WESBYTES_DATASET_ID}.clientes` c
    LEFT JOIN `{BIGQUERY_PROJECT_ID}.{WESBYTES_DATASET_ID}.planes` p
        ON c.plan_id = p.id
    WHERE c.id = @cid
    LIMIT 1
    """
    cfg = bigquery.QueryJobConfig(
        query_parameters=[bigquery.ScalarQueryParameter("cid", "INT64", int(client_id))]
    )
    rows = [dict(r) for r in get_bq_client().query(sql, job_config=cfg).result()]
    if not rows:
        raise WhatsAppClientError(404, "Cliente no encontrado en la base de datos administradora.")

    cli = rows[0]
    if not cli.get("activo") or (cli.get("estado") and cli.get("estado") not in ("activo",)):
        raise WhatsAppClientError(403, "Tu cuenta no se encuentra activa en la base de datos administradora.")

    plan_codigo = str(cli.get("plan_codigo") or "MEDIUM").strip().upper()
    limite_wa = int(cli.get("limite_mensual_whatsapp") or 50)

    notif_data = get_cliente_notificaciones_y_plantillas(
        client_id=client_id,
        plan_id=str(cli.get("plan_id") or ""),
        plan_codigo=plan_codigo,
    )
    toggles = map_notificaciones_to_wa_toggles(notif_data.get("notificaciones", []))

    wa = WhatsAppClient()
    return await wa.provision_client(
        client_id=client_id,
        plan_code=plan_codigo,
        monthly_limit=limite_wa,
        **toggles,
    )
