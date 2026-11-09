import uuid
import json
import datetime
import logging
from typing import Dict, Any, Optional
import httpx

from app.config import (
    PAYPHONE_TOKEN,
    PAYPHONE_STORE_ID,
    PAYPHONE_API_URL,
    PAYPHONE_REDIRECT_URL,
    PAYPHONE_SANDBOX_MODE,
)

logger = logging.getLogger("wesbyte_portal.payphone")


def generate_client_tx_id(cliente_id: int, modalidad: str) -> str:
    """
    Genera un identificador único de transacción (máx 15-20 caracteres alfanuméricos para PayPhone).
    """
    prefix = " AN" if modalidad == "anual_unico" else "MR"
    prefix = prefix.strip()
    short_uid = uuid.uuid4().hex[:8].upper()
    return f"WB{cliente_id}{prefix}{short_uid}"[:15]


def build_payphone_box_config(
    client_tx_id: str,
    monto_usd: float,
    descripcion: str,
    email: str,
    celular: str,
    documento: str,
    modalidad_pago: str,
    custom_token: Optional[str] = None,
    custom_store_id: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Construye el objeto de configuración para la Cajita de Pagos PayPhone (PPPaymentButtonBox)
    y los metadatos de cobro único anual (10% descuento) o cobro mensual recurrente.
    Nota: PayPhone recibe todos los montos en centavos (enteros). Ej: $216.00 -> 21600.
    """
    monto_centavos = int(round(float(monto_usd) * 100))
    token = (custom_token or PAYPHONE_TOKEN or "").strip()
    store_id = (custom_store_id or PAYPHONE_STORE_ID or "").strip()

    # Normalizar teléfono para formato internacional Ecuador (+593...)
    phone_clean = "".join(ch for ch in (celular or "") if ch.isdigit())
    if phone_clean.startswith("09") and len(phone_clean) == 10:
        phone_formatted = f"+593{phone_clean[1:]}"
    elif phone_clean.startswith("593"):
        phone_formatted = f"+{phone_clean}"
    elif phone_clean:
        phone_formatted = f"+593{phone_clean}"
    else:
        phone_formatted = "+593999999999"

    return {
        "token": token,
        "storeId": store_id,
        "clientTransactionId": client_tx_id,
        "amount": monto_centavos,
        "amountWithoutTax": monto_centavos,
        "amountWithTax": 0,
        "tax": 0,
        "service": 0,
        "tip": 0,
        "currency": "USD",
        "reference": descripcion[:100],
        "email": email,
        "phoneNumber": phone_formatted,
        "documentId": documento or "9999999999",
        "identificationType": 1 if len(documento or "") == 10 else 2,
        "modalidad_pago": modalidad_pago,
        "es_recurrente": modalidad_pago == "mensual_recurrente",
        "monto_usd": round(float(monto_usd), 2),
        "sandbox_mode": PAYPHONE_SANDBOX_MODE or not bool(token),
        "redirect_url": PAYPHONE_REDIRECT_URL,
    }


async def confirm_payphone_transaction(
    payphone_id: int,
    client_tx_id: str,
    custom_token: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Llama al endpoint oficial de confirmación de PayPhone:
    POST https://pay.payphonetodoesfacil.com/api/button/V2/Confirm
    Body: { "id": <int>, "clientTxId": "<str>" }
    IMPORTANTE: PayPhone exige ejecutar esta confirmación dentro de los primeros 5 minutos
    de lo contrario reversa automáticamente la transacción.
    """
    token = (custom_token or PAYPHONE_TOKEN or "").strip()
    if not token:
        raise ValueError("No se ha configurado PAYPHONE_TOKEN para confirmar en producción.")

    url = f"{PAYPHONE_API_URL}/button/V2/Confirm"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }
    payload = {
        "id": int(payphone_id),
        "clientTxId": str(client_tx_id),
    }

    async with httpx.AsyncClient(timeout=25.0) as client:
        response = await client.post(url, headers=headers, json=payload)
        data = response.json() if response.content else {}
        if response.status_code != 200:
            logger.error(f"Error PayPhone Confirm ({response.status_code}): {data}")
            return {
                "approved": False,
                "status_code": response.status_code,
                "transaction_status": data.get("transactionStatus", "ERROR"),
                "message": data.get("message") or "No se pudo confirmar la transacción con PayPhone",
                "raw": data,
            }

        tx_status = str(data.get("transactionStatus", "")).upper()
        status_code_pp = int(data.get("statusCode", 0))
        approved = tx_status == "APPROVED" or status_code_pp == 3

        return {
            "approved": approved,
            "payphone_transaction_id": str(data.get("transactionId") or payphone_id),
            "client_transaction_id": str(data.get("clientTransactionId") or client_tx_id),
            "authorization_code": str(data.get("authorizationCode") or ""),
            "transaction_status": tx_status or ("APPROVED" if approved else "REJECTED"),
            "amount_cents": int(data.get("amount") or 0),
            "card_brand": str(data.get("cardBrand") or "PayPhone"),
            "card_type": str(data.get("cardType") or ""),
            "last_digits": str(data.get("lastDigits") or ""),
            "email": str(data.get("email") or ""),
            "phone_number": str(data.get("phoneNumber") or ""),
            "subscription_token": str(data.get("cardToken") or data.get("optionalParameter4") or ""),
            "raw": data,
        }


async def create_payphone_payment_link(
    client_tx_id: str,
    monto_usd: float,
    descripcion: str,
    custom_token: Optional[str] = None,
    custom_store_id: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Genera un Link de Pago directo vía API de PayPhone (/api/Links) o Botón (/api/button/Prepare)
    para cobro único o recurrente.
    """
    token = (custom_token or PAYPHONE_TOKEN or "").strip()
    store_id = (custom_store_id or PAYPHONE_STORE_ID or "").strip()
    monto_centavos = int(round(float(monto_usd) * 100))

    if not token:
        return {
            "success": False,
            "sandbox": True,
            "message": "Modo Sandbox activo (configura PAYPHONE_TOKEN para generar links reales en PayPhone).",
        }

    url = f"{PAYPHONE_API_URL}/button/Prepare"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }
    payload = {
        "amount": monto_centavos,
        "amountWithoutTax": monto_centavos,
        "clientTransactionId": client_tx_id,
        "responseUrl": PAYPHONE_REDIRECT_URL,
        "cancellationUrl": PAYPHONE_REDIRECT_URL,
        "reference": descripcion[:100],
    }
    if store_id:
        payload["storeId"] = store_id

    async with httpx.AsyncClient(timeout=20.0) as client:
        resp = await client.post(url, headers=headers, json=payload)
        data = resp.json() if resp.content else {}
        if resp.status_code == 200 and data.get("payWithCard"):
            return {
                "success": True,
                "payment_id": data.get("paymentId"),
                "pay_with_card_url": data.get("payWithCard"),
                "pay_with_payphone_url": data.get("payWithPayPhone"),
            }
        return {
            "success": False,
            "status_code": resp.status_code,
            "raw": data,
        }
