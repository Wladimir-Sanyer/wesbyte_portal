import os
from pathlib import Path
from typing import Dict, Any, List
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")

BIGQUERY_PROJECT_ID = os.getenv("BIGQUERY_PROJECT_ID", "broker-pmp")
WESBYTES_DATASET_ID = os.getenv("WESBYTES_DATASET_ID", "wesbytes")
TEMPLATE_DATASET_ID = os.getenv("TEMPLATE_DATASET_ID", "broker_pmp")

CREDENTIALS_PATH = os.getenv(
    "GOOGLE_APPLICATION_CREDENTIALS",
    str(BASE_DIR / ".secrets" / "broker-pmp-2aec3024d9c6.json"),
)
if not os.path.isabs(CREDENTIALS_PATH):
    CREDENTIALS_PATH = str(BASE_DIR / CREDENTIALS_PATH)

SECRET_KEY = os.getenv("SECRET_KEY", "wesbyte-portal-jwt-secret-key-2026")
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 60 * 24 * 7

APP_VERSION = os.getenv("APP_VERSION", "v1.0.0")
WESBYTE_APP_URL = os.getenv("WESBYTE_APP_URL", "http://localhost:80")
SOPORTE_EMAIL = os.getenv("SOPORTE_EMAIL", "soporte@wesbyte.app")

# Configuración PayPhone
PAYPHONE_TOKEN = os.getenv("PAYPHONE_TOKEN", "").strip()
PAYPHONE_STORE_ID = os.getenv("PAYPHONE_STORE_ID", "").strip()
PAYPHONE_API_URL = os.getenv("PAYPHONE_API_URL", "https://pay.payphonetodoesfacil.com/api").rstrip("/")
PAYPHONE_REDIRECT_URL = os.getenv("PAYPHONE_REDIRECT_URL", "http://localhost:8080/checkout/payphone/callback")
PAYPHONE_SANDBOX_MODE = os.getenv("PAYPHONE_SANDBOX_MODE", "true").lower() in ("true", "1", "yes") or not PAYPHONE_TOKEN

# Acceso interno (panel de administración). Si no se define, el panel queda deshabilitado.
STAFF_USER = os.getenv("PORTAL_STAFF_USER", "").strip()
STAFF_PASSWORD = os.getenv("PORTAL_STAFF_PASSWORD", "").strip()

# Descuento por pago único anual (10%)
DESCUENTO_ANUAL_PCT = 10.0

# Catálogo oficial de los 3 planes comerciales Wesbyte
PLANES_OFICIALES: List[Dict[str, Any]] = [
    {
        "id": 1,
        "codigo": "PRIME_BRONZE",
        "nombre": "Plan Prime",
        "tier": "Bronze",
        "badge": "Inicial",
        "color": "bronze",
        "subtitulo": "Ideal para agentes independientes y brokers en crecimiento que inician su automatización.",
        "rango_clientes_min": 20,
        "rango_clientes_max": 50,
        "rango_clientes_label": "20 a 50 clientes mensuales",
        "max_usuarios": 1,
        "limite_mensual_whatsapp": 50,
        "limite_mensual_emails": 50,
        "limite_mensual_api_ecuador": 50,
        "limite_diario_whatsapp": 10,
        "limite_diario_emails": 10,
        "limite_diario_api_ecuador": 10,
        "precio_mensual": 20.00,
        "precio_anual": 240.00,
        "descuento_anual_pct": 10.00,
        "precio_anual_con_descuento": 216.00,
        "ahorro_anual": 24.00,
        "equivalente_mensual_anual": 18.00,
        "valor_individual_estimado_anual": 240.00,
        "ahorro_por_volumen_pct": 0,
        "costo_por_interaccion": 0.133,
        "es_destacado": False,
        "es_personalizado": False,
    },
    {
        "id": 2,
        "codigo": "MEDIUM_SILVER",
        "nombre": "Plan Medium",
        "tier": "Silver",
        "badge": "Crecimiento",
        "color": "silver",
        "subtitulo": "Para agencias consolidadas con equipo operativo y mayor volumen de consultas estatales.",
        "rango_clientes_min": 50,
        "rango_clientes_max": 200,
        "rango_clientes_label": "50 a 200 clientes mensuales",
        "max_usuarios": 3,
        "limite_mensual_whatsapp": 100,
        "limite_mensual_emails": 100,
        "limite_mensual_api_ecuador": 200,
        "limite_diario_whatsapp": 20,
        "limite_diario_emails": 20,
        "limite_diario_api_ecuador": 35,
        "precio_mensual": 30.00,
        "precio_anual": 360.00,
        "descuento_anual_pct": 10.00,
        "precio_anual_con_descuento": 324.00,
        "ahorro_anual": 36.00,
        "equivalente_mensual_anual": 27.00,
        "valor_individual_estimado_anual": 552.00,
        "ahorro_por_volumen_pct": 35,
        "costo_por_interaccion": 0.075,
        "es_destacado": False,
        "es_personalizado": False,
    },
    {
        "id": 3,
        "codigo": "SUPER_GOLD",
        "nombre": "Plan Super Gold",
        "tier": "Gold",
        "badge": "Más Beneficioso · Mejor Valor",
        "color": "gold",
        "subtitulo": "Máxima rentabilidad por usuario y consulta. Multiplica x6 tu capacidad a una fracción del costo unitario.",
        "rango_clientes_min": 200,
        "rango_clientes_max": 500,
        "rango_clientes_label": "200 a 500+ clientes mensuales",
        "max_usuarios": 5,
        "limite_mensual_whatsapp": 300,
        "limite_mensual_emails": 200,
        "limite_mensual_api_ecuador": 400,
        "limite_diario_whatsapp": 50,
        "limite_diario_emails": 40,
        "limite_diario_api_ecuador": 70,
        "precio_mensual": 45.00,
        "precio_anual": 540.00,
        "descuento_anual_pct": 10.00,
        "precio_anual_con_descuento": 486.00,
        "ahorro_anual": 54.00,
        "equivalente_mensual_anual": 40.50,
        "valor_individual_estimado_anual": 1128.00,
        "ahorro_por_volumen_pct": 52,
        "costo_por_interaccion": 0.050,
        "es_destacado": True,
        "es_personalizado": False,
    },
]

# Tarifario unitario para la Calculadora de Plan Personalizado
# Calibrado para que 1 usr + 50 WA + 50 Email + 50 API = $20/mes ($240/año)
# y para evidenciar el ahorro masivo (52%) al elegir el paquete Super Gold ($45/mes vs $94/mes suelto).
TARIFAS_CALCULADORA = {
    "base_plataforma_mensual": 10.00,      # Incluye infraestructura dedicada BigQuery + 1er usuario
    "costo_usuario_extra_mensual": 5.50,   # Por cada usuario adicional (> 1)
    "costo_whatsapp_unitario": 0.08,       # Por mensaje WhatsApp alerta mensual ($4 por cada 50)
    "costo_email_unitario": 0.05,          # Por correo y notificación mensual ($2.50 por cada 50)
    "costo_api_ecuador_unitario": 0.07,    # Por consulta API estatal mensual ($3.50 por cada 50)
    "descuento_anual_pct": 10.0,
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

    # Si los recursos solicitados caben dentro de Super Gold (<=5 usr, <=300 WA, <=200 Email, <=400 API)
    # y el precio suelto supera los $45/mes de Super Gold, recomendamos Super Gold
    cabe_en_gold = (
        usuarios <= 5
        and whatsapp_mensual <= 300
        and emails_mensual <= 200
        and api_ecuador_mensual <= 400
    )
    cabe_en_silver = (
        usuarios <= 3
        and whatsapp_mensual <= 100
        and emails_mensual <= 100
        and api_ecuador_mensual <= 200
    )

    # Si supera los límites de Gold, calculamos tomando la tarifa bonificada de Gold ($45) + solo los excedentes
    supera_gold = not cabe_en_gold and (
        usuarios >= 5 or whatsapp_mensual >= 300 or emails_mensual >= 200 or api_ecuador_mensual >= 400
    )
    if plan_equivalente:
        precio_mensual_final = plan_equivalente["precio_mensual"]
    elif supera_gold and (
        usuarios >= 5 and whatsapp_mensual >= 300 and emails_mensual >= 200 and api_ecuador_mensual >= 400
    ):
        # Parte de la base Gold ($45) + adicionales con 20% de descuento corporativo
        extra_usr = max(0, usuarios - 5) * (t["costo_usuario_extra_mensual"] * 0.8)
        extra_wa = max(0, whatsapp_mensual - 300) * (t["costo_whatsapp_unitario"] * 0.75)
        extra_em = max(0, emails_mensual - 200) * (t["costo_email_unitario"] * 0.75)
        extra_api = max(0, api_ecuador_mensual - 400) * (t["costo_api_ecuador_unitario"] * 0.75)
        precio_mensual_final = round(45.00 + extra_usr + extra_wa + extra_em + extra_api, 2)
    else:
        precio_mensual_final = precio_mensual_suelto

    precio_anual = round(precio_mensual_final * 12, 2)
    descuento_anual = round(precio_anual * (t["descuento_anual_pct"] / 100.0), 2)
    precio_anual_con_descuento = round(precio_anual - descuento_anual, 2)

    recomendacion_gold = False
    mensaje_recomendacion = ""
    if cabe_en_gold and precio_mensual_final >= 38.00 and not (plan_equivalente and plan_equivalente["codigo"] == "SUPER_GOLD"):
        recomendacion_gold = True
        ahorro_vs_custom = round((precio_mensual_suelto - 45.00) * 12, 2)
        if ahorro_vs_custom > 0:
            mensaje_recomendacion = (
                f"¡El Plan Super Gold te incluye 5 usuarios, 300 WhatsApp, 200 correos/notificaciones y 400 consultas API "
                f"por solo $45/mes ($486/año con descuento), ahorrándote ${ahorro_vs_custom:.2f} al año frente a armarlo por separado!"
            )
        else:
            mensaje_recomendacion = (
                "Por solo unos dólares más al mes, el Plan Super Gold te entrega 5 usuarios, 300 WhatsApp, "
                "200 correos/notificaciones y 400 consultas API estatales."
            )
    elif cabe_en_silver and precio_mensual_final > 30.00:
        mensaje_recomendacion = (
            "¡Tip! Tus recursos caben dentro del Plan Medium (Silver) de $30/mes ($324/año con 10% OFF)."
        )

    return {
        "usuarios": usuarios,
        "limite_mensual_whatsapp": whatsapp_mensual,
        "limite_mensual_emails": emails_mensual,
        "limite_mensual_api_ecuador": api_ecuador_mensual,
        "limite_diario_whatsapp": max(10, int(round(whatsapp_mensual / 5))),
        "limite_diario_emails": max(10, int(round(emails_mensual / 5))),
        "limite_diario_api_ecuador": max(10, int(round(api_ecuador_mensual / 5))),
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
