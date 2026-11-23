"""
Servicio para el Módulo de Diseño y Personalización de Plantillas de Correo Electrónico:
- 3 Plantillas HTML Base Email-Safe (clasica, moderna, minimalista) 100% compatibles con Gmail, Outlook y Apple Mail (tablas + CSS inline).
- Control de acceso por Plan de Suscripción (Plan Prime vs Planes 2 y 3: Medium / Super Gold) y cuotas mensuales de generación IA.
- Integración con Gemini (gemini-2.5-flash-lite con Structured Outputs JSON) inyectando dinámicamente solo el diccionario de variables reales de la notificación.
- Validación estricta de seguridad (Regex Whitelist) antes de guardar en base de datos.
"""
from __future__ import annotations

import datetime
import html as _html
import json
import logging
import re
from typing import Any, Dict, List, Optional, Tuple

import httpx
from google.cloud import bigquery
from pydantic import BaseModel, Field

from app.config import (
    BIGQUERY_PROJECT_ID,
    ECUADOR_TZ,
    GEMINI_API_KEY,
    GEMINI_MODEL_EMAIL,
    WESBYTES_DATASET_ID,
)
from app.database import append_rows, get_bq_client, get_next_id
from app.services.google_drive_service import normalize_drive_logo_url
from app.services.notificaciones_config_service import (
    CATALOGO_POR_CODIGO,
    PLANTILLA_VARIABLES,
)

logger = logging.getLogger("wesbyte_portal.email_template_designer")

# ============================================================================
# REGLAS DE NEGOCIO POR PLAN Y CUOTAS MENSUALES DE IA
# ============================================================================
# Plan Prime (Plan 1 / Demo): 0 generaciones IA (modo bloqueado con badge PRO)
# Plan Medium (Plan 2): 10 generaciones IA / mes
# Plan Super Gold / Custom (Plan 3+): 10 generaciones IA / mes
CUOTA_IA_POR_PLAN: Dict[int, int] = {
    0: 0,   # Demo
    1: 0,   # Plan Prime
    2: 10,  # Plan Medium
    3: 10,  # Plan Super Gold
}


MAX_AI_GENERATIONS_PER_MONTH = 5


def get_ai_quota_for_plan(plan_id: int, plan_codigo: str = "", es_demo: bool = False) -> Tuple[bool, int]:
    """
    Determina si el plan del cliente tiene acceso al Editor Inteligente con IA
    y cuál es su cuota mensual máxima de generaciones (máx. 5/mes).
    """
    if es_demo:
        return False, 0
    cod = str(plan_codigo or "").strip().upper()
    pid = int(plan_id or 0)
    if pid >= 3 or "GOLD" in cod or "SUPER" in cod or "CUSTOM" in cod or "ENTERPRISE" in cod:
        return True, MAX_AI_GENERATIONS_PER_MONTH
    if pid == 2 or "MEDIUM" in cod or "SILVER" in cod or "PRO" in cod:
        return True, MAX_AI_GENERATIONS_PER_MONTH
    return False, 0


def get_ai_usage_this_month(client_id: int) -> int:
    """Cuenta cuántas generaciones con IA ha realizado el tenant en el mes actual (YYYY-MM)."""
    periodo = datetime.datetime.now(ECUADOR_TZ).strftime("%Y-%m")
    sql = f"""
        SELECT COUNT(1) AS usados
        FROM `{BIGQUERY_PROJECT_ID}.{WESBYTES_DATASET_ID}.logs_ia_plantillas`
        WHERE client_id = @cid AND periodo = @per
    """
    try:
        cfg = bigquery.QueryJobConfig(
            query_parameters=[
                bigquery.ScalarQueryParameter("cid", "INT64", int(client_id)),
                bigquery.ScalarQueryParameter("per", "STRING", periodo),
            ]
        )
        rows = list(get_bq_client().query(sql, job_config=cfg).result())
        return int(rows[0]["usados"]) if rows else 0
    except Exception as exc:
        logger.debug(f"No se pudo leer logs_ia_plantillas para cliente {client_id}: {exc}")
        return 0


def record_ai_usage(client_id: int, notificacion_codigo: str, prompt_resumen: str, modelo: str) -> None:
    """Registra una generación exitosa de IA en logs_ia_plantillas."""
    periodo = datetime.datetime.now(ECUADOR_TZ).strftime("%Y-%m")
    try:
        nid = get_next_id("logs_ia_plantillas", WESBYTES_DATASET_ID)
        append_rows(
            WESBYTES_DATASET_ID,
            "logs_ia_plantillas",
            [
                {
                    "id": nid,
                    "client_id": int(client_id),
                    "notificacion_codigo": str(notificacion_codigo),
                    "periodo": periodo,
                    "prompt_resumen": str(prompt_resumen or "")[:400],
                    "modelo": str(modelo or GEMINI_MODEL_EMAIL),
                    "created_at": datetime.datetime.now(datetime.timezone.utc),
                }
            ],
        )
    except Exception as exc:
        logger.warning(f"No se pudo registrar uso de IA para cliente {client_id}: {exc}")


# ============================================================================
# DICCIONARIO DE VARIABLES OFICIALES POR NOTIFICACIÓN Y TEXTOS BASE
# ============================================================================
VARIABLE_REGULARIZACIONES: Dict[str, str] = {
    "key": "regularizaciones_faltantes",
    "label": "Requisitos pendientes",
    "ejemplo": "• Firma de contrato\n• Pago de cuota inicial",
}

VARIABLE_LOGO_EMAIL: Dict[str, str] = {
    "key": "logo",
    "label": "Logo de la agencia",
    "ejemplo": "[Logo de la Agencia]",
}

DEFAULT_PRESET_TEXTS: Dict[str, Dict[str, str]] = {
    "proxima_renovacion": {
        "titulo": "Aviso de Próxima Renovación",
        "saludo": "Estimado/a {{nombres_completos_razon_social}} (CI: {{cedula}}),",
        "intro": (
            "Le informamos que su póliza {{numero_contrato}} correspondiente al ramo {{ramos}} "
            "con la aseguradora {{aseguradora}} ({{plan}}) tiene fecha de vigencia hasta el {{fecha_vigencia}}."
        ),
        "cierre": (
            "Nuestro asesor {{nombre_agente}} de {{broker}} se pondrá en contacto con usted para "
            "gestionar su renovación a tiempo y mantener su respaldo sin interrupciones."
        ),
    },
    "regularizaciones": {
        "titulo": "Recordatorio de Regularización de Póliza",
        "saludo": "Estimado/a {{nombres_completos_razon_social}} (CI: {{cedula}}),",
        "intro": (
            "Le recordamos que para completar el proceso de su póliza {{numero_contrato}} "
            "({{ramos}} · {{aseguradora}}) aún se encuentran pendientes los siguientes requisitos:\n"
            "{{regularizaciones_faltantes}}"
        ),
        "cierre": (
            "Por favor, compártanos esta documentación a la brevedad. Si requiere asistencia, "
            "su agente {{nombre_agente}} de {{broker}} está a su entera disposición."
        ),
    },
    "envio_poliza": {
        "titulo": "Entrega Oficial de su Póliza de Seguro",
        "saludo": "Estimado/a {{nombres_completos_razon_social}} (CI: {{cedula}}),",
        "intro": (
            "Nos complace hacerle entrega oficial de su póliza {{numero_contrato}} del ramo {{ramos}} "
            "emitida con {{aseguradora}} ({{plan}}), con vigencia hasta el {{fecha_vigencia}}."
        ),
        "cierre": (
            "Adjunto a este correo encontrará el documento digital de su póliza. "
            "Gracias por confiar su tranquilidad a {{nombre_agente}} y {{broker}}."
        ),
    },
    "condiciones_renovacion": {
        "titulo": "Propuesta y Condiciones de Renovación",
        "saludo": "Estimado/a {{nombres_completos_razon_social}} (CI: {{cedula}}),",
        "intro": (
            "Le compartimos las condiciones propuestas para la renovación de su póliza {{numero_contrato}} "
            "({{ramos}} · {{aseguradora}}, {{plan}}) con fecha de vigencia {{fecha_vigencia}}."
        ),
        "cierre": (
            "Quedamos atentos a su amable confirmación para proceder con la emisión. "
            "Su asesor {{nombre_agente}} ({{broker}}) queda atento a cualquier inquietud."
        ),
    },
    "cumpleanios": {
        "titulo": "¡Feliz Cumpleaños de parte de {{broker}}!",
        "saludo": "Estimado/a {{nombres_completos_razon_social}},",
        "intro": (
            "Hoy es un día muy especial y queremos enviarle un cordial saludo en su cumpleaños, "
            "deseándole un año lleno de salud, bienestar, metas cumplidas y grandes satisfacciones."
        ),
        "cierre": (
            "Para todo el equipo de {{broker}} es un privilegio acompañarle y velar día a día "
            "por la seguridad y tranquilidad de lo que más valora."
        ),
    },
}


def get_allowed_variables_for_notification(notificacion_codigo: str, canal: str = "email") -> List[Dict[str, str]]:
    """
    Devuelve la lista estricta de variables oficiales que soporta una notificación específica.
    """
    cod = str(notificacion_codigo or "").strip()
    if cod == "cumpleanios":
        # En cumpleaños solo existen datos del cliente y del broker
        allowed_keys = {"nombres_completos_razon_social", "cedula", "broker", "nombre_agente"}
        vars_list = [v.copy() for v in PLANTILLA_VARIABLES if v["key"] in allowed_keys]
    else:
        vars_list = [v.copy() for v in PLANTILLA_VARIABLES]
        if cod == "regularizaciones":
            vars_list.append(VARIABLE_REGULARIZACIONES.copy())

    if canal == "email":
        vars_list.append(VARIABLE_LOGO_EMAIL.copy())
    return vars_list


def get_allowed_variable_keys(notificacion_codigo: str, canal: str = "email") -> set[str]:
    return {v["key"] for v in get_allowed_variables_for_notification(notificacion_codigo, canal)}


def normalize_variable_brackets_to_single(text: str) -> str:
    """
    Normaliza variables con doble llave {{variable}} a llave simple {variable} o viceversa,
    para que tanto {{variable}} como {variable} funcionen de forma transparente en todo el backend.
    Internamente convertimos {{var}} -> {var} al despachar o mantenemos {{var}} compatible.
    """
    if not text:
        return ""
    return re.sub(r"\{\{\s*([a-zA-Z0-9_]+)\s*\}\}", r"{\1}", text)


def normalize_variable_brackets_to_double(text: str) -> str:
    """Convierte {variable} de lista blanca a {{variable}} para visualización uniforme en el diseñador."""
    if not text:
        return ""
    all_keys = {v["key"] for v in PLANTILLA_VARIABLES} | {"regularizaciones_faltantes", "logo"}
    out = re.sub(r"\{\{\s*([a-zA-Z0-9_]+)\s*\}\}", r"{{\1}}", text)

    def _repl(m: re.Match[str]) -> str:
        k = m.group(1)
        if k in all_keys:
            return "{{" + k + "}}"
        return m.group(0)

    return re.sub(r"(?<!\{)\{([a-zA-Z0-9_]+)\}(?!\})", _repl, out)


def validate_template_variables_whitelist(
    html_or_text: str,
    notificacion_codigo: str,
    canal: str = "email",
    is_subject: bool = False,
) -> List[str]:
    """
    Validación de Seguridad al Guardar:
    Extrae todas las expresiones {{variable}} (y también {variable} fuera de bloques CSS/HTML)
    y verifica que pertenezcan estrictamente a la lista blanca oficial de esa notificación.
    Lanza ValueError si encuentra variables no autorizadas o scripts inseguros.
    Retorna la lista de variables válidas detectadas.
    """
    raw = str(html_or_text or "")
    if not raw.strip():
        return []

    # 1. Bloquear etiquetas <script>, handlers on*= o javascript: en plantillas de correo
    if canal == "email" and not is_subject:
        if re.search(r"<\s*script\b", raw, re.IGNORECASE):
            raise ValueError("Por seguridad, las plantillas de correo no pueden contener etiquetas <script>.")
        if re.search(r"\bon[a-z]+\s*=", raw, re.IGNORECASE):
            raise ValueError("Por seguridad, el HTML no puede contener atributos de eventos JavaScript (ej. onclick, onload).")

    allowed = get_allowed_variable_keys(notificacion_codigo, canal=canal)
    if is_subject and "logo" in allowed:
        allowed = allowed - {"logo"}

    # 2. Detectar todas las variables con doble llave {{...}}
    double_matches = re.findall(r"\{\{\s*([^{}\s]+)\s*\}\}", raw)

    # 3. Detectar variables con llave simple {variable} eliminando primero bloques <style>...</style>
    #    y atributos style="..." para no confundir propiedades CSS con variables.
    cleaned_for_single = re.sub(r"<\s*style\b[^>]*>[\s\S]*?<\s*/\s*style\s*>", "", raw, flags=re.IGNORECASE)
    cleaned_for_single = re.sub(r'style\s*=\s*"[^"]*"', "", cleaned_for_single, flags=re.IGNORECASE)
    cleaned_for_single = re.sub(r"style\s*=\s*'[^']*'", "", cleaned_for_single, flags=re.IGNORECASE)
    single_matches = re.findall(r"(?<!\{)\{\s*([a-zA-Z_][a-zA-Z0-9_]*)\s*\}(?!\})", cleaned_for_single)

    found_vars: List[str] = []
    invalid_vars: List[str] = []

    for v in double_matches + single_matches:
        v_clean = v.strip()
        if not v_clean:
            continue
        if v_clean not in allowed:
            if v_clean not in invalid_vars:
                invalid_vars.append(v_clean)
        else:
            if v_clean not in found_vars:
                found_vars.append(v_clean)

    if invalid_vars:
         formatted_inv = ", ".join(f"{{{{{x}}}}}" for x in invalid_vars)
         formatted_ok = ", ".join(f"{{{{{x}}}}}" for x in sorted(allowed))
         raise ValueError(
             f"La plantilla contiene variables no autorizadas para esta notificación: {formatted_inv}. "
             f"Variables oficiales permitidas: {formatted_ok}."
         )

    return found_vars


# ============================================================================
# LAS 3 PLANTILLAS HTML BASE (100% EMAIL-SAFE: GMAIL, OUTLOOK, APPLE MAIL)
# ============================================================================
def _sanitize_hex_color(color: Optional[str], default: str = "#1e3a8a") -> str:
    c = str(color or "").strip()
    if re.fullmatch(r"#[0-9a-fA-F]{6}", c):
        return c.lower()
    if re.fullmatch(r"#[0-9a-fA-F]{3}", c):
        return "#" + "".join(ch * 2 for ch in c[1:]).lower()
    return default


def _text_to_safe_html_paragraphs(text: str, text_color: str = "#334155", font_size: str = "15px") -> str:
    """
    Convierte texto editable con saltos de línea y variables {{...}} en HTML seguro con estilos inline,
    preservando las variables {{variable}} intactas.
    """
    raw = normalize_variable_brackets_to_double(str(text or "").strip())
    if not raw:
        return ""
    escaped = _html.escape(raw).replace("\r\n", "\n").replace("\n", "<br />")
    # Resaltar visualmente {{regularizaciones_faltantes}} si está en bloque
    return (
        f'<div style="font-family:Arial,Helvetica,sans-serif;font-size:{font_size};'
        f'line-height:1.65;color:{text_color};margin:0;">{escaped}</div>'
    )


def render_preset_email_html(
    preset_id: str,
    notificacion_codigo: str,
    primary_color: str = "#1e3a8a",
    logo_url: str = "",
    broker_nombre: str = "{{broker}}",
    titulo: Optional[str] = None,
    saludo: Optional[str] = None,
    intro: Optional[str] = None,
    cierre: Optional[str] = None,
    mostrar_tabla_resumen: bool = True,
) -> str:
    """
    Genera el HTML 100% compatible con Gmail, Outlook y Apple Mail (arquitectura de <table> + CSS inline)
    para cualquiera de las 3 plantillas preestablecidas: 'clasica', 'moderna' o 'minimalista'.
    Conserva las variables oficiales {{...}} para que sean reemplazadas en tiempo de envío o en el Live Preview.
    """
    preset = str(preset_id or "clasica").strip().lower()
    if preset not in ("clasica", "moderna", "minimalista"):
        preset = "clasica"

    cod = str(notificacion_codigo or "envio_poliza").strip()
    defaults = DEFAULT_PRESET_TEXTS.get(cod, DEFAULT_PRESET_TEXTS["envio_poliza"])

    color_hex = _sanitize_hex_color(primary_color, "#1e3a8a")
    titulo_txt = normalize_variable_brackets_to_double((titulo if titulo is not None else defaults["titulo"]).strip())
    saludo_txt = normalize_variable_brackets_to_double((saludo if saludo is not None else defaults["saludo"]).strip())
    intro_txt = normalize_variable_brackets_to_double((intro if intro is not None else defaults["intro"]).strip())
    cierre_txt = normalize_variable_brackets_to_double((cierre if cierre is not None else defaults["cierre"]).strip())

    titulo_html = _html.escape(titulo_txt)
    saludo_html = _html.escape(saludo_txt).replace("\n", "<br />")
    intro_html = _text_to_safe_html_paragraphs(intro_txt, "#334155", "15px")
    cierre_html = _text_to_safe_html_paragraphs(cierre_txt, "#475569", "14px")

    logo_norm = normalize_drive_logo_url(logo_url) if logo_url else ""
    if logo_norm:
        logo_header_clasica = (
            f'<img src="{_html.escape(logo_norm)}" alt="Logo Agencia" width="160" '
            f'style="display:block;width:160px;max-width:160px;height:auto;margin:0 auto;padding:0;border:0;" />'
        )
        logo_header_light = (
            f'<img src="{_html.escape(logo_norm)}" alt="Logo Agencia" '
            f'style="max-height:60px;max-width:210px;height:auto;width:auto;display:inline-block;border:0;" />'
        )
    else:
        logo_header_clasica = (
            f'<div style="display:inline-block;padding:10px 16px;color:{color_hex};'
            "font-family:'Segoe UI',Roboto,Helvetica,Arial,sans-serif;font-size:18px;font-weight:bold;letter-spacing:0.4px;\">"
            "{{broker}}</div>"
        )
        logo_header_light = (
            f'<div style="display:inline-block;color:{color_hex};'
            'font-family:Arial,Helvetica,sans-serif;font-size:18px;font-weight:bold;letter-spacing:0.3px;">'
            '{{broker}}</div>'
        )

    # Tabla resumen de datos de la póliza (excepto para cumpleaños)
    include_policy_card = mostrar_tabla_resumen and (cod != "cumpleanios")
    policy_table_clasica = ""
    policy_table_moderna = ""
    policy_table_minimalista = ""

    if include_policy_card:
        policy_table_clasica = f"""
          <!-- INFORMACION DE POLIZA -->
          <tr>
            <td style="padding:10px 18px 0;">
              <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="border:1px solid #D6E2EE;border-radius:6px;font-size:13px;border-collapse:separate;overflow:hidden;">
                <tr>
                  <td style="font-family:'Segoe UI',Roboto,Helvetica,Arial,sans-serif;padding:7px 11px;background-color:#F3F7FB;color:#5B6B7C;width:38%;border-bottom:1px solid #E3EBF3;">Titular</td>
                  <td style="font-family:'Segoe UI',Roboto,Helvetica,Arial,sans-serif;padding:7px 11px;font-weight:bold;color:#0F172A;border-bottom:1px solid #E3EBF3;">{{{{nombres_completos_razon_social}}}}</td>
                </tr>
                <tr>
                  <td style="font-family:'Segoe UI',Roboto,Helvetica,Arial,sans-serif;padding:7px 11px;background-color:#F3F7FB;color:#5B6B7C;border-bottom:1px solid #E3EBF3;">Aseguradora</td>
                  <td style="font-family:'Segoe UI',Roboto,Helvetica,Arial,sans-serif;padding:7px 11px;font-weight:bold;color:#0F172A;border-bottom:1px solid #E3EBF3;">{{{{aseguradora}}}}</td>
                </tr>
                <tr>
                  <td style="font-family:'Segoe UI',Roboto,Helvetica,Arial,sans-serif;padding:7px 11px;background-color:#F3F7FB;color:#5B6B7C;border-bottom:1px solid #E3EBF3;">Ramo</td>
                  <td style="font-family:'Segoe UI',Roboto,Helvetica,Arial,sans-serif;padding:7px 11px;font-weight:bold;color:#0F172A;border-bottom:1px solid #E3EBF3;">{{{{ramos}}}}</td>
                </tr>
                <tr>
                  <td style="font-family:'Segoe UI',Roboto,Helvetica,Arial,sans-serif;padding:7px 11px;background-color:#F3F7FB;color:#5B6B7C;border-bottom:1px solid #E3EBF3;">Plan</td>
                  <td style="font-family:'Segoe UI',Roboto,Helvetica,Arial,sans-serif;padding:7px 11px;font-weight:bold;color:#0F172A;border-bottom:1px solid #E3EBF3;">{{{{plan}}}}</td>
                </tr>
                <tr>
                  <td style="font-family:'Segoe UI',Roboto,Helvetica,Arial,sans-serif;padding:7px 11px;background-color:#F3F7FB;color:#5B6B7C;border-bottom:1px solid #E3EBF3;">Póliza N.°</td>
                  <td style="font-family:'Segoe UI',Roboto,Helvetica,Arial,sans-serif;padding:7px 11px;font-weight:bold;color:#0F172A;border-bottom:1px solid #E3EBF3;">{{{{numero_contrato}}}}</td>
                </tr>
                <tr>
                  <td style="font-family:'Segoe UI',Roboto,Helvetica,Arial,sans-serif;padding:7px 11px;background-color:#F3F7FB;color:#5B6B7C;">Vigencia</td>
                  <td style="font-family:'Segoe UI',Roboto,Helvetica,Arial,sans-serif;padding:7px 11px;font-weight:bold;color:{color_hex};">{{{{fecha_vigencia}}}}</td>
                </tr>
              </table>
            </td>
          </tr>"""

        policy_table_moderna = f"""
        <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="margin:24px 0;border-collapse:separate;border-spacing:0;background-color:#ffffff;border:1px solid #e2e8f0;border-radius:12px;overflow:hidden;">
          <tr>
            <td colspan="2" style="background-color:{color_hex};padding:10px 18px;font-family:Arial,Helvetica,sans-serif;font-size:12px;font-weight:bold;color:#ffffff;letter-spacing:0.6px;text-transform:uppercase;">
              Resumen de Cobertura
            </td>
          </tr>
          <tr>
            <td width="50%" style="padding:14px 18px;border-bottom:1px solid #f1f5f9;border-right:1px solid #f1f5f9;font-family:Arial,Helvetica,sans-serif;">
              <div style="font-size:11px;color:#64748b;text-transform:uppercase;">Póliza / Contrato</div>
              <div style="font-size:14px;font-weight:bold;color:#0f172a;margin-top:3px;">{{{{numero_contrato}}}}</div>
            </td>
            <td width="50%" style="padding:14px 18px;border-bottom:1px solid #f1f5f9;font-family:Arial,Helvetica,sans-serif;">
              <div style="font-size:11px;color:#64748b;text-transform:uppercase;">Vigencia</div>
              <div style="font-size:14px;font-weight:bold;color:{color_hex};margin-top:3px;">{{{{fecha_vigencia}}}}</div>
            </td>
          </tr>
          <tr>
            <td width="50%" style="padding:14px 18px;border-right:1px solid #f1f5f9;font-family:Arial,Helvetica,sans-serif;">
              <div style="font-size:11px;color:#64748b;text-transform:uppercase;">Aseguradora</div>
              <div style="font-size:14px;font-weight:bold;color:#0f172a;margin-top:3px;">{{{{aseguradora}}}}</div>
            </td>
            <td width="50%" style="padding:14px 18px;font-family:Arial,Helvetica,sans-serif;">
              <div style="font-size:11px;color:#64748b;text-transform:uppercase;">Ramo / Plan</div>
              <div style="font-size:14px;font-weight:bold;color:#0f172a;margin-top:3px;">{{{{ramos}}}} ({{{{plan}}}})</div>
            </td>
          </tr>
        </table>
        """

        policy_table_minimalista = f"""
        <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="margin:20px 0;border-top:1px solid #e2e8f0;border-bottom:1px solid #e2e8f0;">
          <tr>
            <td style="padding:14px 0;font-family:Arial,Helvetica,sans-serif;font-size:13px;line-height:1.8;color:#334155;">
              <strong style="color:#0f172a;">Contrato:</strong> {{{{numero_contrato}}}} &nbsp;·&nbsp;
              <strong style="color:#0f172a;">Aseguradora:</strong> {{{{aseguradora}}}} &nbsp;·&nbsp;
              <strong style="color:#0f172a;">Ramo:</strong> {{{{ramos}}}} ({{{{plan}}}}) &nbsp;·&nbsp;
              <strong style="color:{color_hex};">Vigencia:</strong> {{{{fecha_vigencia}}}}
            </td>
          </tr>
        </table>
        """

    # ------------------------------------------------------------------------
    # 1. PLANTILLA CLÁSICA (Logo limpio en blanco arriba, franja título color primario, tabla 6 filas y bloque contacto)
    # ------------------------------------------------------------------------
    if preset == "clasica":
        intro_clasica_html = _html.escape(intro_txt).replace("\r\n", "\n").replace("\n", "<br />")
        cierre_clasica_html = _html.escape(cierre_txt).replace("\r\n", "\n").replace("\n", "<br />")
        return f"""<!DOCTYPE html>
<html lang="es">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width,initial-scale=1" />
  <title>{titulo_html}</title>
</head>
<body style="margin:0;padding:0;background-color:#EEF2F7;font-family:'Segoe UI',Roboto,Helvetica,Arial,sans-serif;color:#1F2937;">
  <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="background-color:#EEF2F7;padding:14px 8px;margin:0;">
    <tr>
      <td align="center" style="padding:0;margin:0;">
        <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="width:100%;max-width:600px;background-color:#FFFFFF;border-radius:8px;overflow:hidden;border:1px solid #D6E2EE;margin:0;">
          <!-- LOGO -->
          <tr>
            <td align="center" style="padding:4px 10px;background-color:#FFFFFF;border-bottom:0px solid {color_hex};font-size:0;line-height:0;">
              {logo_header_clasica}
            </td>
          </tr>
          <!-- TITULO -->
          <tr>
            <td align="center" style="background-color:{color_hex};padding:8px 15px;color:#FFFFFF;font-size:17px;font-weight:700;line-height:20px;text-align:center;font-family:'Segoe UI',Roboto,Helvetica,Arial,sans-serif;">
              {titulo_html}
            </td>
          </tr>
          <!-- INTRODUCCION -->
          <tr>
            <td style="font-family:'Segoe UI',Roboto,Helvetica,Arial,sans-serif;padding:10px 18px 4px;font-size:14px;line-height:1.45;color:#334155;text-align:justify;">
              <p style="margin:0 0 7px;text-align:left;font-weight:700;color:#0F172A;">
                {saludo_html}
              </p>
              <p style="margin:0;text-align:justify;">
                {intro_clasica_html}
              </p>
            </td>
          </tr>{policy_table_clasica}
          <!-- CONTACTO -->
          <tr>
            <td style="padding:10px 18px 0;">
              <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="background-color:#F3F7FB;border-left:3px solid {color_hex};border-radius:4px;">
                <tr>
                  <td style="font-family:'Segoe UI',Roboto,Helvetica,Arial,sans-serif;padding:9px 12px;font-size:13px;line-height:1.45;color:#334155;text-align:justify;">
                    {cierre_clasica_html}
                  </td>
                </tr>
              </table>
            </td>
          </tr>
          <!-- DESPEDIDA -->
          <tr>
            <td style="font-family:'Segoe UI',Roboto,Helvetica,Arial,sans-serif;padding:10px 18px;color:{color_hex};">
              <p style="margin:0;font-weight:700;font-size:14px;">
                ¡Gracias por confiar en nosotros!
              </p>
            </td>
          </tr>
          <!-- FOOTER -->
          <tr>
            <td align="center" style="background-color:#0f172a;padding:8px 12px;text-align:center;font-family:'Segoe UI',Roboto,Helvetica,Arial,sans-serif;font-size:10px;color:#94a3b8;">
              Enviado por <strong style="color:#ffffff;">{{{{broker}}}}</strong> a través de <a href="https://wesbyte.app" style="color:#cbd5e1;text-decoration:none;font-weight:600;">wesbyte.app</a>
            </td>
          </tr>
        </table>
      </td>
    </tr>
  </table>
</body>
</html>"""

    # ------------------------------------------------------------------------
    # 2. PLANTILLA MODERNA (Tarjetas SaaS, acento superior, grilla 2x2)
    # ------------------------------------------------------------------------
    if preset == "moderna":
        return f"""<!DOCTYPE html>
<html lang="es">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>{titulo_html}</title>
</head>
<body style="margin:0;padding:0;background-color:#eef2f6;font-family:'Segoe UI',Roboto,Helvetica,Arial,sans-serif;-webkit-font-smoothing:antialiased;">
  <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="background-color:#eef2f6;padding:32px 12px;">
    <tr>
      <td align="center">
        <table role="presentation" width="600" cellpadding="0" cellspacing="0" border="0" style="max-width:600px;width:100%;background-color:#ffffff;border-radius:16px;overflow:hidden;border:1px solid #e2e8f0;">
          <!-- Barra de acento superior -->
          <tr>
            <td style="background-color:{color_hex};height:8px;font-size:1px;line-height:1px;">&nbsp;</td>
          </tr>
          <!-- Encabezado con Logo y Badge -->
          <tr>
            <td style="padding:28px 36px 16px 36px;border-bottom:1px solid #f1f5f9;">
              <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0">
                <tr>
                  <td align="left" valign="middle">
                    {logo_header_light}
                  </td>
                  <td align="right" valign="middle">
                    <span style="display:inline-block;padding:5px 12px;background-color:#f8fafc;border:1px solid #e2e8f0;border-radius:999px;font-family:Arial,Helvetica,sans-serif;font-size:11px;font-weight:bold;color:{color_hex};">
                      Gestión de Seguros
                    </span>
                  </td>
                </tr>
              </table>
            </td>
          </tr>
          <!-- Contenido Principal -->
          <tr>
            <td style="padding:28px 36px 32px 36px;">
              <h1 style="margin:0 0 18px 0;font-family:Arial,Helvetica,sans-serif;font-size:22px;font-weight:bold;color:#0f172a;line-height:1.3;">
                {titulo_html}
              </h1>
              <p style="margin:0 0 14px 0;font-family:Arial,Helvetica,sans-serif;font-size:15px;font-weight:bold;color:{color_hex};">
                {saludo_html}
              </p>
              {intro_html}
              {policy_table_moderna}
              <div style="margin-top:16px;">
                {cierre_html}
              </div>
              <!-- Tarjeta del Asesor -->
              <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="margin-top:26px;background-color:#f8fafc;border:1px dashed #cbd5e1;border-radius:12px;">
                <tr>
                  <td style="padding:16px 20px;font-family:Arial,Helvetica,sans-serif;">
                    <div style="font-size:11px;font-weight:bold;text-transform:uppercase;color:#64748b;letter-spacing:0.5px;">Tu Asesor de Seguros</div>
                    <div style="font-size:15px;font-weight:bold;color:#0f172a;margin-top:3px;">{{{{nombre_agente}}}} · <span style="color:{color_hex};">{{{{broker}}}}</span></div>
                  </td>
                </tr>
              </table>
            </td>
          </tr>
          <!-- Footer -->
          <tr>
            <td align="center" style="background-color:#0f172a;padding:16px 24px;text-align:center;font-family:Arial,Helvetica,sans-serif;font-size:11px;color:#94a3b8;">
              Enviado por <strong style="color:#ffffff;">{{{{broker}}}}</strong> · Tecnología <a href="https://wesbyte.app" style="color:#cbd5e1;text-decoration:underline;">wesbyte.app</a>
            </td>
          </tr>
        </table>
      </td>
    </tr>
  </table>
</body>
</html>"""

    # ------------------------------------------------------------------------
    # 3. PLANTILLA MINIMALISTA (Editorial limpia, fondo blanco puro, tipografía aireada)
    # ------------------------------------------------------------------------
    return f"""<!DOCTYPE html>
<html lang="es">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>{titulo_html}</title>
</head>
<body style="margin:0;padding:0;background-color:#ffffff;font-family:Arial,Helvetica,sans-serif;-webkit-font-smoothing:antialiased;">
  <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="background-color:#ffffff;padding:24px 12px;">
    <tr>
      <td align="center">
        <table role="presentation" width="580" cellpadding="0" cellspacing="0" border="0" style="max-width:580px;width:100%;background-color:#ffffff;border:1px solid #e2e8f0;border-radius:6px;">
          <tr>
            <td style="padding:36px 40px;">
              <!-- Logo Minimalista -->
              <div style="margin-bottom:24px;padding-bottom:18px;border-bottom:2px solid {color_hex};">
                {logo_header_light}
              </div>
              <!-- Título -->
              <h2 style="margin:0 0 20px 0;font-family:Arial,Helvetica,sans-serif;font-size:19px;font-weight:bold;color:#0f172a;letter-spacing:-0.2px;">
                {titulo_html}
              </h2>
              <!-- Saludo -->
              <p style="margin:0 0 16px 0;font-family:Arial,Helvetica,sans-serif;font-size:15px;font-weight:bold;color:#1e293b;">
                {saludo_html}
              </p>
              <!-- Párrafo Introductorio -->
              {intro_html}
              <!-- Cinta Resumen Minimalista -->
              {policy_table_minimalista}
              <!-- Cierre -->
              <div style="margin-top:18px;">
                {cierre_html}
              </div>
              <!-- Firma Minimalista -->
              <div style="margin-top:32px;padding-top:16px;border-top:1px solid #f1f5f9;font-family:Arial,Helvetica,sans-serif;">
                <div style="font-size:14px;font-weight:bold;color:#0f172a;">{{{{nombre_agente}}}}</div>
                <div style="font-size:13px;color:{color_hex};margin-top:2px;">{{{{broker}}}}</div>
              </div>
              <!-- Pie Discreto -->
              <div style="margin-top:28px;font-family:Arial,Helvetica,sans-serif;font-size:10px;color:#94a3b8;">
                Enviado a través de <a href="https://wesbyte.app" style="color:#94a3b8;text-decoration:underline;">wesbyte.app</a>
              </div>
            </td>
          </tr>
        </table>
      </td>
    </tr>
  </table>
</body>
</html>"""


# ============================================================================
# INTEGRACIÓN CON GEMINI (gemini-2.5-flash-lite + STRUCTURED OUTPUTS JSON)
# ============================================================================
class GeminiEmailDesignOutput(BaseModel):
    html_final: str = Field(
        ...,
        description="Código HTML completo del correo electrónico (100% email-safe usando estructura de tablas <table> y estilos CSS inline, sin scripts).",
    )
    variables_usadas: List[str] = Field(
        default_factory=list,
        description="Lista de nombres de variables oficiales de nuestra app (sin llaves) que quedaron incluidas en html_final.",
    )
    variables_descartadas: List[str] = Field(
        default_factory=list,
        description="Lista de variables externas o inventadas que venían en el HTML/prompt del usuario y que fueron omitidas por no tener equivalente en el diccionario oficial.",
    )
    resumen_cambios: str = Field(
        ...,
        description="Explicación breve, amigable y clara en español (1 a 3 oraciones) de los ajustes de diseño y mapeo de variables realizados.",
    )


def _postprocess_and_sanitize_ai_html(
    raw_html: str,
    notificacion_codigo: str,
) -> Tuple[str, List[str], List[str]]:
    """
    Garantiza que el HTML devuelto por la IA solo contenga variables de la lista blanca
    de esa notificación (en formato {{variable}}), eliminando cualquier variable no permitida
    y cualquier etiqueta <script>.
    """
    allowed_keys = get_allowed_variable_keys(notificacion_codigo, canal="email")
    html_out = str(raw_html or "").strip()

    # Eliminar scripts o handlers inseguros
    html_out = re.sub(r"<\s*script\b[^>]*>[\s\S]*?<\s*/\s*script\s*>", "", html_out, flags=re.IGNORECASE)
    html_out = re.sub(r"\s+on[a-z]+\s*=\s*\"[^\"]*\"", "", html_out, flags=re.IGNORECASE)
    html_out = re.sub(r"\s+on[a-z]+\s*=\s*'[^']*'", "", html_out, flags=re.IGNORECASE)

    # Normalizar {var} conocidas a {{var}}
    html_out = normalize_variable_brackets_to_double(html_out)

    auto_discarded: List[str] = []
    used_valid: List[str] = []

    def _check_double(m: re.Match[str]) -> str:
        var_name = m.group(1).strip()
        if var_name in allowed_keys:
            if var_name not in used_valid:
                used_valid.append(var_name)
            return "{{" + var_name + "}}"
        if var_name not in auto_discarded:
            auto_discarded.append(var_name)
        return ""

    html_out = re.sub(r"\{\{\s*([^{}\s]+)\s*\}\}", _check_double, html_out)

    # También revisar si quedaron llaves simples {var_invalida} fuera de bloques <style> y atributos style
    # Para no tocar CSS, reemplazamos solo fuera de <style>...</style>
    parts = re.split(r"(<\s*style\b[^>]*>[\s\S]*?<\s*/\s*style\s*>)", html_out, flags=re.IGNORECASE)
    cleaned_parts: List[str] = []
    for p in parts:
        if re.match(r"<\s*style\b", p, flags=re.IGNORECASE):
            cleaned_parts.append(p)
        else:
            def _check_single(m: re.Match[str]) -> str:
                v = m.group(1).strip()
                if v in allowed_keys:
                    if v not in used_valid:
                        used_valid.append(v)
                    return "{{" + v + "}}"
                if v not in auto_discarded:
                    auto_discarded.append(v)
                return ""
            # Evitar reemplazar dentro de style="..."
            cleaned_parts.append(re.sub(r"(?<!\{)\{([a-zA-Z_][a-zA-Z0-9_]*)\}(?!\})", _check_single, p))
    html_out = "".join(cleaned_parts)

    return html_out, used_valid, auto_discarded


async def generate_or_adapt_email_template_with_ai(
    notificacion_codigo: str,
    user_prompt_or_html: str,
    current_html: str = "",
    primary_color: str = "#1e3a8a",
    logo_url: str = "",
    broker_nombre: str = "Seguros Alianza",
) -> Dict[str, Any]:
    """
    Invoca a Gemini (`gemini-2.5-flash-lite` con Structured Outputs JSON) inyectando
    ÚNICAMENTE el diccionario de variables reales que existen en nuestra app para esa notificación.
    """
    meta = CATALOGO_POR_CODIGO.get(notificacion_codigo)
    if not meta:
        raise ValueError(f"Notificación no válida: {notificacion_codigo}")

    allowed_vars = get_allowed_variables_for_notification(notificacion_codigo, canal="email")
    diccionario_str = "\n".join(
        f"- {{{{{v['key']}}}}}: {v['label']} (Ejemplo: \"{v['ejemplo']}\")"
        for v in allowed_vars
    )

    color_hex = _sanitize_hex_color(primary_color, "#1e3a8a")
    logo_norm = normalize_drive_logo_url(logo_url) if logo_url else ""

    system_instruction = f"""Eres un Senior Email Designer & Product Engineer experto en diseño de plantillas HTML transaccionales para corredurías de seguros (CRM B2B).

NOTIFICACIÓN ACTUAL: "{meta['nombre']}" (código: `{notificacion_codigo}`)
DESCRIPCIÓN DE LA NOTIFICACIÓN: {meta['descripcion']}
COLOR PRIMARIO DE LA AGENCIA: {color_hex}
NOMBRE DE LA AGENCIA / BROKER: {broker_nombre}
URL DEL LOGO DE LA AGENCIA: {logo_norm or 'Usar {{broker}} en texto estilizado si no hay URL de imagen'}

DICCIONARIO ESTRICTO Y EXCLUSIVO DE VARIABLES OFICIALES PERMITIDAS PARA ESTA NOTIFICACIÓN:
{diccionario_str}

REGLAS OBLIGATORIAS E INQUEBRANTABLES:
1. LISTA BLANCA ESTRICTA DE VARIABLES:
   - ÚNICAMENTE puedes usar las variables listadas arriba, escritas exactamente con doble llave: `{{{{nombre_variable}}}}`.
   - Si el usuario pega un HTML externo o texto con variables de otros sistemas (ej. `{{{{client_name}}}}`, `{{{{nombre}}}}`, `{{{{policy_number}}}}`, `{{{{poliza}}}}`, `{{{{expiration_date}}}}`, `{{{{company}}}}`, `*|FNAME|*`, `%NOMBRE%`, etc.) o datos ficticios fijos, TRADÚCELOS automáticamente a nuestra variable oficial equivalente (por ejemplo: nombre del cliente -> `{{{{nombres_completos_razon_social}}}}`, número de póliza -> `{{{{numero_contrato}}}}`, vencimiento/vigencia -> `{{{{fecha_vigencia}}}}`, aseguradora -> `{{{{aseguradora}}}}`, ramo -> `{{{{ramos}}}}`, plan -> `{{{{plan}}}}`, asesor/agente -> `{{{{nombre_agente}}}}`, agencia/corredor -> `{{{{broker}}}}`).
   - Si el usuario incluye variables externas que NO tienen ningún equivalente en nuestro diccionario oficial para esta notificación (por ejemplo `{{{{descuento_vip}}}}`, `{{{{placa_vehiculo}}}}`, `{{{{link_pago_tarjeta}}}}`, `{{{{telefono_siniestros}}}}`), ELIMÍNALAS/OMÍTELAS limpiamente del diseño para que el correo sea coherente y regístralas en `variables_descartadas`.
   - NUNCA inventes variables nuevas que no estén en el diccionario oficial de arriba.

2. COMPATIBILIDAD 100% EMAIL-SAFE (GMAIL, OUTLOOK, APPLE MAIL):
   - Construye el diseño con estructura de tablas (`<table role="presentation" cellpadding="0" cellspacing="0" border="0">`) centrada con `max-width: 600px`.
   - Aplica TODOS los estilos con CSS inline (`style="..."`).
   - Usa fuentes seguras con respaldo (`Arial, Helvetica, sans-serif` o `'Segoe UI', Roboto, Arial, sans-serif`).
   - PROHIBIDO incluir `<script>`, eventos JavaScript (`onclick`, `onload`), formularios o iframes.
   - Incluye un pie discreto que indique: Enviado por {{{{broker}}}} a través de wesbyte.app.

3. FORMATO DE SALIDA:
   - Responde estrictamente con el objeto JSON del esquema solicitado: `html_final`, `variables_usadas`, `variables_descartadas` y `resumen_cambios`."""

    user_message = f"""SOLICITUD O CÓDIGO HTML DEL USUARIO:
{user_prompt_or_html.strip()}

{"PLANTILLA HTML ACTUAL COMO BASE (si el usuario pide modificar la plantilla actual):" + chr(10) + current_html.strip() if current_html and current_html.strip() else ""}
"""

    schema_json = {
        "type": "OBJECT",
        "properties": {
            "html_final": {
                "type": "STRING",
                "description": "Código HTML completo del correo electrónico (tablas + CSS inline, 100% email-safe).",
            },
            "variables_usadas": {
                "type": "ARRAY",
                "items": {"type": "STRING"},
                "description": "Lista de claves de variables oficiales usadas en html_final (sin llaves).",
            },
            "variables_descartadas": {
                "type": "ARRAY",
                "items": {"type": "STRING"},
                "description": "Lista de variables externas o sin equivalente que fueron descartadas/omitidas.",
            },
            "resumen_cambios": {
                "type": "STRING",
                "description": "Resumen corto en español de los cambios realizados y variables conectadas.",
            },
        },
        "required": ["html_final", "variables_usadas", "variables_descartadas", "resumen_cambios"],
    }

    if not GEMINI_API_KEY:
        raise RuntimeError("No se ha configurado GEMINI_API_KEY en el servidor.")

    models_to_try = ["gemini-3.5-flash-lite"]
    # Eliminar duplicados manteniendo orden
    seen_m = set()
    unique_models = [m for m in models_to_try if not (m in seen_m or seen_m.add(m))]

    last_err = None
    raw_result: Optional[Dict[str, Any]] = None
    used_model = unique_models[0]

    async with httpx.AsyncClient(timeout=45.0) as client:
        for model_name in unique_models:
            url = f"https://generativelanguage.googleapis.com/v1beta/models/{model_name}:generateContent?key={GEMINI_API_KEY}"
            payload = {
                "system_instruction": {"parts": [{"text": system_instruction}]},
                "contents": [{"role": "user", "parts": [{"text": user_message}]}],
                "generationConfig": {
                    "responseMimeType": "application/json",
                    "responseSchema": schema_json,
                    "temperature": 0.25,
                },
            }
            try:
                resp = await client.post(url, json=payload)
                if resp.status_code >= 400:
                    last_err = f"HTTP {resp.status_code} ({model_name}): {resp.text[:300]}"
                    logger.warning(f"Fallo invocando modelo {model_name}: {last_err}")
                    continue
                data = resp.json()
                candidates = data.get("candidates") or []
                if not candidates:
                    last_err = f"Respuesta sin candidates de {model_name}"
                    continue
                text_part = candidates[0].get("content", {}).get("parts", [{}])[0].get("text", "")
                parsed_obj = GeminiEmailDesignOutput(**json.loads(text_part))
                raw_result = parsed_obj.model_dump()
                used_model = model_name
                break
            except Exception as exc:
                last_err = str(exc)
                logger.warning(f"Excepción invocando {model_name}: {exc}")
                continue

    if not raw_result:
        raise RuntimeError(f"No se pudo generar la plantilla con Gemini IA: {last_err}")

    # Post-procesar y sanitizar para garantizar cumplimiento 100% de la lista blanca
    clean_html, detected_valid, extra_discarded = _postprocess_and_sanitize_ai_html(
        raw_result["html_final"],
        notificacion_codigo,
    )

    allowed_key_set = get_allowed_variable_keys(notificacion_codigo, canal="email")
    final_used: List[str] = []
    for k in (raw_result.get("variables_usadas") or []) + detected_valid:
        k_clean = str(k).strip().strip("{}").strip()
        if k_clean in allowed_key_set and k_clean not in final_used and (f"{{{{{k_clean}}}}}" in clean_html):
            final_used.append(k_clean)

    final_discarded: List[str] = []
    for k in (raw_result.get("variables_descartadas") or []) + extra_discarded:
        k_clean = str(k).strip().strip("{}").strip()
        if k_clean and k_clean not in allowed_key_set and k_clean not in final_discarded:
            final_discarded.append(k_clean)

    return {
        "html_final": clean_html,
        "variables_usadas": final_used,
        "variables_descartadas": final_discarded,
        "resumen_cambios": raw_result.get("resumen_cambios") or "Plantilla adaptada y optimizada para correo electrónico.",
        "modelo_usado": used_model,
    }
