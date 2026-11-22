"""
Generador de Clave de Acceso de 49 dígitos (Módulo 11) para comprobantes electrónicos del SRI Ecuador.
"""
from __future__ import annotations

import datetime
import secrets
from typing import Optional
from zoneinfo import ZoneInfo

TZ_ECUADOR = ZoneInfo("America/Guayaquil")


def calcular_digito_modulo11(cadena_48: str) -> int:
    """
    Calcula el dígito verificador Módulo 11 del SRI sobre los primeros 48 dígitos.
    Factores de ponderación de derecha a izquierda: 2, 3, 4, 5, 6, 7 (cíclicos).
    Si el resultado es 11 -> 0; si es 10 -> 1.
    """
    if len(cadena_48) != 48 or not cadena_48.isdigit():
        raise ValueError("La base de la clave de acceso debe tener exactamente 48 dígitos numéricos.")
    factores = (2, 3, 4, 5, 6, 7)
    suma = 0
    for idx, ch in enumerate(reversed(cadena_48)):
        suma += int(ch) * factores[idx % 6]
    residuo = suma % 11
    digito = 11 - residuo
    if digito == 11:
        return 0
    if digito == 10:
        return 1
    return digito


def generar_clave_acceso_sri(
    *,
    ruc_emisor: str,
    ambiente: str,
    establecimiento: str,
    punto_emision: str,
    secuencial: str,
    fecha_emision: Optional[datetime.date] = None,
    tipo_comprobante: str = "01",
    codigo_numerico: Optional[str] = None,
    tipo_emision: str = "1",
) -> str:
    """
    Construye la clave de acceso de 49 dígitos conforme a la Tabla 1 de la Ficha Técnica del SRI:
      - Fecha de emisión (ddmmyyyy): 8 dígitos (en zona horaria America/Guayaquil)
      - Tipo de comprobante: 2 dígitos ('01' = Factura)
      - RUC del emisor: 13 dígitos
      - Tipo de ambiente: 1 dígito ('1' Pruebas, '2' Producción)
      - Serie (establecimiento + punto_emision): 6 dígitos (ej. '001001')
      - Número de comprobante (secuencial): 9 dígitos (ej. '000000001')
      - Código numérico: 8 dígitos
      - Tipo de emisión: 1 dígito ('1' Emisión normal)
      - Dígito verificador (Módulo 11): 1 dígito
    """
    if fecha_emision is None:
        fecha_emision = datetime.datetime.now(TZ_ECUADOR).date()

    fecha_str = fecha_emision.strftime("%d%m%Y")
    tipo_comp = str(tipo_comprobante or "01").strip().zfill(2)[:2]
    ruc_clean = "".join(c for c in str(ruc_emisor or "") if c.isdigit())
    if len(ruc_clean) != 13:
        raise ValueError(f"El RUC del emisor debe tener 13 dígitos numéricos (recibido: '{ruc_emisor}').")

    amb = "2" if str(ambiente).strip() == "2" else "1"
    estab = "".join(c for c in str(establecimiento or "001") if c.isdigit()).zfill(3)[:3]
    pto = "".join(c for c in str(punto_emision or "001") if c.isdigit()).zfill(3)[:3]
    sec = "".join(c for c in str(secuencial or "1") if c.isdigit()).zfill(9)[-9:]

    if codigo_numerico:
        cod_num = "".join(c for c in str(codigo_numerico) if c.isdigit()).zfill(8)[:8]
    else:
        # Código determinístico/seguro de 8 dígitos
        cod_num = f"{secrets.randbelow(90_000_000) + 10_000_000:08d}"

    t_emision = "1"
    base_48 = f"{fecha_str}{tipo_comp}{ruc_clean}{amb}{estab}{pto}{sec}{cod_num}{t_emision}"
    verificador = calcular_digito_modulo11(base_48)
    return f"{base_48}{verificador}"
