import os
from pathlib import Path
from typing import Dict, Any, List, Optional
from zoneinfo import ZoneInfo
from dotenv import load_dotenv

ECUADOR_TZ = ZoneInfo("America/Guayaquil")

DEMO_PLAN_CONFIG: Dict[str, Any] = {
    "id": 0,
    "codigo": "DEMO",
    "slug": "demo",
    "nombre": "Plan Demo (5 días)",
    "duracion_dias": 5,
    "max_usuarios": 1,
    "limite_clientes": 10,
    "limite_mensual_whatsapp": 15,
    "limite_mensual_emails": 25,
    "limite_mensual_api_ecuador": 20,
}

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")

BIGQUERY_PROJECT_ID = os.getenv("BIGQUERY_PROJECT_ID", "wesbyte")
WESBYTES_DATASET_ID = os.getenv("WESBYTES_DATASET_ID", "administracion")
TEMPLATE_DATASET_ID = os.getenv("TEMPLATE_DATASET_ID", "prototipo")

# Variables GCP / BigQuery estándar
GCP_PROJECT_ID = os.getenv("GCP_PROJECT_ID", BIGQUERY_PROJECT_ID).strip()
WESBYTE_CENTRAL_DATASET = os.getenv("WESBYTE_CENTRAL_DATASET", WESBYTES_DATASET_ID).strip()

# Servicio Transaccional de Correos (Resend)
RESEND_API_KEY = os.getenv("RESEND_API_KEY", "").strip()
INTERNAL_EMAIL_SERVICE_KEY = os.getenv("INTERNAL_EMAIL_SERVICE_KEY", "wesbyte_internal_email_service_key_2026").strip()
EMAIL_SENDER_DEFAULT = os.getenv("EMAIL_SENDER_DEFAULT", "Wesbyte <notifications@wesbyte.app>").strip()

# Microservicio de WhatsApp (wesbyte_api)
WHATSAPP_API_URL = os.getenv("WHATSAPP_API_URL", "http://whatsapp_api:8000").strip().rstrip("/")
WHATSAPP_SERVICE_API_KEY = os.getenv("WHATSAPP_SERVICE_API_KEY", "WesbyteServiceKey2026").strip()

CREDENTIALS_PATH = os.getenv(
    "GOOGLE_APPLICATION_CREDENTIALS",
    str(BASE_DIR / ".secrets" / "wesbyte-e1e002799cb2.json"),
)
if not os.path.isabs(CREDENTIALS_PATH):
    CREDENTIALS_PATH = str(BASE_DIR / CREDENTIALS_PATH)

# Google Drive OAuth2 (Almacenamiento de logos en clientes/logos)
GOOGLE_DRIVE_CLIENT_ID = os.getenv("GOOGLE_DRIVE_CLIENT_ID", "").strip()
GOOGLE_DRIVE_CLIENT_SECRET = os.getenv("GOOGLE_DRIVE_CLIENT_SECRET", "").strip()
GOOGLE_DRIVE_REFRESH_TOKEN = os.getenv("GOOGLE_DRIVE_REFRESH_TOKEN", "").strip()

# Gemini IA (Diseño y Adaptación Inteligente de Plantillas de Correo)
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()
GEMINI_MODEL_EMAIL = os.getenv("GEMINI_MODEL_EMAIL", "gemini-3.8-flash").strip() or "gemini-3.8-flash"

SECRET_KEY = os.getenv("SECRET_KEY", "wesbyte-portal-jwt-secret-key-2026")
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 60 * 24 * 7

APP_VERSION = os.getenv("APP_VERSION", "v1.0.0")
WESBYTE_APP_URL = os.getenv("WESBYTE_APP_URL", "https://portal.wesbyte.app")
MEDIA_PUBLIC_BASE_URL = os.getenv("MEDIA_PUBLIC_BASE_URL", "https://wesbyte.app").strip().rstrip("/")
SOPORTE_EMAIL = os.getenv("SOPORTE_EMAIL", "soporte@wesbyte.app")

# Configuración PayPhone
PAYPHONE_TOKEN = os.getenv("PAYPHONE_TOKEN", "").strip()
PAYPHONE_STORE_ID = os.getenv("PAYPHONE_STORE_ID", "").strip()
PAYPHONE_API_URL = os.getenv("PAYPHONE_API_URL", "https://pay.payphonetodoesfacil.com/api").rstrip("/")
PAYPHONE_REDIRECT_URL = os.getenv("PAYPHONE_REDIRECT_URL", "https://wesbyte.app/checkout/payphone/callback")
PAYPHONE_SANDBOX_MODE = os.getenv("PAYPHONE_SANDBOX_MODE", "true").lower() in ("true", "1", "yes") or not PAYPHONE_TOKEN

# Acceso interno (panel de administración). Si no se define, el panel queda deshabilitado.
STAFF_USER = os.getenv("PORTAL_STAFF_USER", "").strip()
STAFF_PASSWORD = os.getenv("PORTAL_STAFF_PASSWORD", "").strip()

# Impuesto al Valor Agregado (Ecuador 15% por defecto)
IVA_PORCENTAJE = float(os.getenv("IVA_PORCENTAJE", "15.0"))

# Facturación Electrónica SRI Ecuador (Esquema Offline)
def _clean_env(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip().strip('"').strip("'").strip()


SRI_AMBIENTE = _clean_env("SRI_AMBIENTE", "1") or "1"  # "1" Pruebas, "2" Producción
SRI_P12_PATH = _clean_env("SRI_P12_PATH", "/app/.secrets/firma_electronica.p12")
if SRI_P12_PATH and not os.path.isabs(SRI_P12_PATH):
    SRI_P12_PATH = str(BASE_DIR / SRI_P12_PATH)
SRI_P12_PASSWORD = _clean_env("SRI_P12_PASSWORD", "")
SRI_RUC_EMISOR = _clean_env("SRI_RUC_EMISOR", "")
SRI_RAZON_SOCIAL = _clean_env("SRI_RAZON_SOCIAL", "WESBYTE")
SRI_NOMBRE_COMERCIAL = _clean_env("SRI_NOMBRE_COMERCIAL", "WESBYTE")
SRI_DIR_MATRIZ = _clean_env("SRI_DIR_MATRIZ", "Guayaquil - Ecuador")
SRI_DIR_ESTABLECIMIENTO = _clean_env("SRI_DIR_ESTABLECIMIENTO", SRI_DIR_MATRIZ)
SRI_ESTABLECIMIENTO = _clean_env("SRI_ESTABLECIMIENTO", "001").zfill(3)[:3]
SRI_PUNTO_EMISION = _clean_env("SRI_PUNTO_EMISION", "001").zfill(3)[:3]
SRI_OBLIGADO_CONTABILIDAD = _clean_env("SRI_OBLIGADO_CONTABILIDAD", "SI").upper()
SRI_CONTRIBUYENTE_RIMPE = _clean_env("SRI_CONTRIBUYENTE_RIMPE", "")
SRI_INVOICE_HOUR_ECUADOR = _clean_env("SRI_INVOICE_HOUR_ECUADOR", "01:00") or "01:00"

# Opciones de compromiso de pago único (sin suscripción mensual recurrente)
PERIODOS_COMPROMISO: Dict[str, Dict[str, Any]] = {
    "1_year": {"codigo": "1_year", "anios": 1, "label": "1 Año", "descuento_pct": 0.0},
    "2_years": {"codigo": "2_years", "anios": 2, "label": "2 Años", "descuento_pct": 10.0},
    "3_years": {"codigo": "3_years", "anios": 3, "label": "3 Años", "descuento_pct": 15.0},
}


def normalizar_periodo(periodo: Optional[str]) -> str:
    p = str(periodo or "1_year").strip().lower()
    if p in PERIODOS_COMPROMISO:
        return p
    return "1_year"


def calcular_montos_periodo(precio_anual_base: float, periodo: Optional[str] = "1_year") -> Dict[str, Any]:
    """Calcula subtotal, descuento, base imponible, IVA, total, por año y equivalente mensual."""
    p_key = normalizar_periodo(periodo)
    meta = PERIODOS_COMPROMISO[p_key]
    anios = int(meta["anios"])
    desc_pct = float(meta["descuento_pct"])
    subtotal = round(float(precio_anual_base) * anios, 2)
    ahorro = round(subtotal * (desc_pct / 100.0), 2)
    base_imponible = round(subtotal - ahorro, 2)
    iva = round(base_imponible * (IVA_PORCENTAJE / 100.0), 2)
    total = round(base_imponible + iva, 2)
    por_anio = round(base_imponible / anios, 2)
    por_mes = round(base_imponible / (anios * 12), 2)
    cuota_msi_3 = round(total / 3, 2)
    msi_meses = 6 if total > 250 else 3
    cuota_msi = round(total / msi_meses, 2)
    return {
        "periodo": p_key,
        "label": meta["label"],
        "anios": anios,
        "descuento_pct": desc_pct,
        "subtotal": subtotal,
        "ahorro": ahorro,
        "base_imponible": base_imponible,
        "iva_pct": IVA_PORCENTAJE,
        "iva": iva,
        "total": total,
        "por_anio": por_anio,
        "por_mes": por_mes,
        "cuota_msi_3": cuota_msi_3,
        "msi_meses": msi_meses,
        "cuota_msi": cuota_msi,
    }


# Catálogo oficial de los 3 planes comerciales Wesbyte (pago único anual o multianual)
PLANES_OFICIALES: List[Dict[str, Any]] = [
    {
        "id": 1,
        "codigo": "PRIME",
        "slug": "prime",
        "nombre": "Plan Prime",
        "tier": "Bronze",
        "badge": "Inicial",
        "color": "prime",
        "subtitulo": "Ideal para independientes y empresas en crecimiento que inician su automatización.",
        "max_usuarios": 1,
        "limite_mensual_whatsapp": 150,
        "whatsapp_detalle": "desde tu propio WhatsApp corporativo",
        "limite_mensual_emails": 250,
        "limite_mensual_api_ecuador": 100,
        "pdf_detalle": None,
        "precio_mensual": 16.67,
        "precio_anual": 199.99,
        "descuento_anual_pct": 0.00,
        "precio_anual_con_descuento": 199.99,
        "precio_anual_payphone": 199.99,
        "ahorro_anual": 0.00,
        "equivalente_mensual_anual": 16.67,
        "meses_sin_intereses": 3,
        "cuota_sin_intereses": 66.66,
        "periodos": {
            "1_year": calcular_montos_periodo(199.99, "1_year"),
            "2_years": calcular_montos_periodo(199.99, "2_years"),
            "3_years": calcular_montos_periodo(199.99, "3_years"),
        },
        "valor_individual_estimado_anual": 240.00,
        "ahorro_por_volumen_pct": 0,
        "costo_por_interaccion": 0.133,
        "es_destacado": False,
        "es_personalizado": False,
    },
    {
        "id": 2,
        "codigo": "MEDIUM",
        "slug": "medium",
        "nombre": "Plan Medium",
        "tier": "Silver",
        "badge": "Crecimiento",
        "color": "medium",
        "subtitulo": "Para agencias consolidadas con equipo operativo y mayor volumen de consultas estatales.",
        "max_usuarios": 3,
        "limite_mensual_whatsapp": 500,
        "whatsapp_detalle": "desde tu propio WhatsApp corporativo",
        "limite_mensual_emails": 1000,
        "limite_mensual_api_ecuador": 400,
        "pdf_detalle": "Cargos de PDF (Renovaciones)",
        "precio_mensual": 30.00,
        "precio_anual": 359.99,
        "descuento_anual_pct": 0.00,
        "precio_anual_con_descuento": 359.99,
        "precio_anual_payphone": 359.99,
        "ahorro_anual": 0.00,
        "equivalente_mensual_anual": 30.00,
        "meses_sin_intereses": 3,
        "cuota_sin_intereses": 120.00,
        "periodos": {
            "1_year": calcular_montos_periodo(359.99, "1_year"),
            "2_years": calcular_montos_periodo(359.99, "2_years"),
            "3_years": calcular_montos_periodo(359.99, "3_years"),
        },
        "valor_individual_estimado_anual": 552.00,
        "ahorro_por_volumen_pct": 35,
        "costo_por_interaccion": 0.075,
        "es_destacado": False,
        "es_personalizado": False,
    },
    {
        "id": 3,
        "codigo": "SUPER_GOLD",
        "slug": "super_gold",
        "nombre": "Plan Super Gold",
        "tier": "Gold",
        "badge": "EL MÁS CONVENIENTE · MEJOR PRECIO POR USO",
        "color": "gold",
        "subtitulo": "Máxima rentabilidad por usuario y consulta. Multiplica tu capacidad al menor costo unitario.",
        "max_usuarios": 5,
        "limite_mensual_whatsapp": 1500,
        "whatsapp_detalle": "desde tu propio WhatsApp corporativo",
        "limite_mensual_emails": 3000,
        "limite_mensual_api_ecuador": 1000,
        "pdf_detalle": "Cargos de PDF (Renovaciones)",
        "precio_mensual": 41.67,
        "precio_anual": 499.99,
        "descuento_anual_pct": 0.00,
        "precio_anual_con_descuento": 499.99,
        "precio_anual_payphone": 499.99,
        "ahorro_anual": 0.00,
        "equivalente_mensual_anual": 41.67,
        "meses_sin_intereses": 3,
        "cuota_sin_intereses": 166.66,
        "periodos": {
            "1_year": calcular_montos_periodo(499.99, "1_year"),
            "2_years": calcular_montos_periodo(499.99, "2_years"),
            "3_years": calcular_montos_periodo(499.99, "3_years"),
        },
        "valor_individual_estimado_anual": 1128.00,
        "ahorro_por_volumen_pct": 52,
        "costo_por_interaccion": 0.050,
        "es_destacado": True,
        "es_personalizado": False,
    },
]

# Tarifario unitario para la Calculadora de Plan Personalizado
# Calibrado proporcionalmente a los planes actuales ($199.99 / $359.99 / $499.99)
# y a sus cuotas (150-1500 WA, 250-3000 Email, 100-1000 API estatal).
TARIFAS_CALCULADORA = {
    "base_plataforma_mensual": 8.00,       # Incluye infraestructura dedicada BigQuery + 1er usuario
    "costo_usuario_extra_mensual": 4.00,   # Por cada usuario adicional (> 1)
    "costo_whatsapp_unitario": 0.025,      # Por mensaje WhatsApp alerta mensual ($2.50 por cada 100)
    "costo_email_unitario": 0.008,         # Por correo y notificación mensual ($0.80 por cada 100)
    "costo_api_ecuador_unitario": 0.030,   # Por consulta API estatal mensual ($3.00 por cada 100)
    "descuento_anual_pct": 5.0,
}


def calcular_plan_personalizado(
    usuarios: int,
    whatsapp_mensual: int,
    emails_mensual: int,
    api_ecuador_mensual: int,
) -> Dict[str, Any]:
    """
    Calcula el precio mensual y anual de una configuración personalizada
    y compara automáticamente contra los planes empaquetados (especialmente Super Gold)
    para mostrar al usuario cuándo le resulta más beneficioso adquirir Super Gold.
    """
    usuarios = max(1, min(int(usuarios), 100))
    whatsapp_mensual = max(10, min(int(whatsapp_mensual), 10000))
    emails_mensual = max(10, min(int(emails_mensual), 10000))
    api_ecuador_mensual = max(10, min(int(api_ecuador_mensual), 10000))

    t = TARIFAS_CALCULADORA
    costo_base = t["base_plataforma_mensual"]
    costo_usuarios = max(0, usuarios - 1) * t["costo_usuario_extra_mensual"]
    costo_wa = whatsapp_mensual * t["costo_whatsapp_unitario"]
    costo_em = emails_mensual * t["costo_email_unitario"]
    costo_api = api_ecuador_mensual * t["costo_api_ecuador_unitario"]

    precio_mensual_suelto = round(costo_base + costo_usuarios + costo_wa + costo_em + costo_api, 2)

    # Si coincide exactamente con alguno de los 3 planes oficiales, aplicar precio oficial
    plan_equivalente = None
    for p in PLANES_OFICIALES:
        if (
            usuarios == p["max_usuarios"]
            and whatsapp_mensual == p["limite_mensual_whatsapp"]
            and emails_mensual == p["limite_mensual_emails"]
            and api_ecuador_mensual == p["limite_mensual_api_ecuador"]
        ):
            plan_equivalente = p
            break

    # Si los recursos solicitados caben dentro de Super Gold (<=5 usr, <=1500 WA, <=3000 Email, <=1000 API)
    # y el precio suelto supera los $41.67/mes de Super Gold, recomendamos Super Gold
    cabe_en_gold = (
        usuarios <= 5
        and whatsapp_mensual <= 1500
        and emails_mensual <= 3000
        and api_ecuador_mensual <= 1000
    )
    cabe_en_medium = (
        usuarios <= 3
        and whatsapp_mensual <= 500
        and emails_mensual <= 1000
        and api_ecuador_mensual <= 400
    )

    # Si supera los límites de Gold, siempre parte de la base Gold + excedentes corporativos coherentes
    # (nunca puede costar menos que Super Gold si excede cualquiera de sus límites)
    if plan_equivalente:
        precio_mensual_final = plan_equivalente["precio_mensual"]
    elif not cabe_en_gold:
        extra_usr = max(0, usuarios - 5) * 4.00
        extra_wa = max(0, whatsapp_mensual - 1500) * 0.010
        extra_em = max(0, emails_mensual - 3000) * 0.003
        extra_api = max(0, api_ecuador_mensual - 1000) * 0.012
        precio_mensual_final = round(41.67 + extra_usr + extra_wa + extra_em + extra_api, 2)
    else:
        precio_mensual_final = precio_mensual_suelto

    if plan_equivalente:
        precio_anual = float(plan_equivalente["precio_anual"])
        descuento_anual = 0.0
        precio_anual_con_descuento = precio_anual
    else:
        precio_anual = round(precio_mensual_final * 12, 2)
        descuento_anual = round(precio_anual * (t["descuento_anual_pct"] / 100.0), 2)
        precio_anual_con_descuento = round(precio_anual - descuento_anual, 2)
        if not cabe_en_gold and precio_anual_con_descuento < 519.99:
            precio_anual_con_descuento = 519.99
            descuento_anual = round(max(0.0, precio_anual - precio_anual_con_descuento), 2)

    recomendacion_gold = False
    mensaje_recomendacion = ""
    if cabe_en_gold and precio_mensual_final >= 38.00 and not (plan_equivalente and plan_equivalente["codigo"] == "SUPER_GOLD"):
        recomendacion_gold = True
        ahorro_vs_custom = round(precio_anual_con_descuento - 499.99, 2)
        if ahorro_vs_custom > 0:
            mensaje_recomendacion = (
                f"¡El Plan Super Gold te incluye 5 usuarios, 1,500 WhatsApp, 3,000 correos/notificaciones y 1,000 consultas API "
                f"por $499.99/año ($41.67/mes), ahorrándote ${ahorro_vs_custom:.2f} al año frente a armarlo por separado!"
            )
        else:
            mensaje_recomendacion = (
                "Por solo unos dólares más al mes, el Plan Super Gold te entrega 5 usuarios, 1,500 WhatsApp, "
                "3,000 correos/notificaciones y 1,000 consultas API estatales por $499.99/año."
            )
    elif cabe_en_medium and precio_mensual_final > 30.00:
        mensaje_recomendacion = (
            "¡Tip! Tus recursos caben dentro del Plan Medium de $359.99/año ($30.00/mes)."
        )

    return {
        "usuarios": usuarios,
        "limite_mensual_whatsapp": whatsapp_mensual,
        "limite_mensual_emails": emails_mensual,
        "limite_mensual_api_ecuador": api_ecuador_mensual,
        "desglose_mensual": {
            "base_plataforma": round(costo_base, 2),
            "usuarios_adicionales": round(costo_usuarios, 2),
            "whatsapp": round(costo_wa, 2),
            "emails": round(costo_em, 2),
            "api_ecuador": round(costo_api, 2),
            "subtotal_suelto": precio_mensual_suelto,
        },
        "precio_mensual": precio_mensual_final,
        "precio_anual": precio_anual,
        "descuento_anual_pct": t["descuento_anual_pct"],
        "ahorro_anual": descuento_anual,
        "precio_anual_con_descuento": precio_anual_con_descuento,
        "equivalente_mensual_anual": round(precio_anual_con_descuento / 12, 2),
        "recomendacion_gold": recomendacion_gold,
        "mensaje_recomendacion": mensaje_recomendacion,
        "plan_equivalente": plan_equivalente["codigo"] if plan_equivalente else "CUSTOM",
    }
