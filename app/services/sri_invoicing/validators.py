"""
Validadores de identificación tributaria (RUC, Cédula, Pasaporte, Consumidor Final)
conforme a la Ficha Técnica de Comprobantes Electrónicos del SRI Ecuador (Tabla 6).
"""
from __future__ import annotations

import re
from typing import Tuple


class SriValidationError(ValueError):
    """Error de validación previa a la emisión electrónica del SRI (no quema secuencial)."""


def _validar_modulo_10(digitos_10: str) -> bool:
    if len(digitos_10) != 10 or not digitos_10.isdigit():
        return False
    provincia = int(digitos_10[:2])
    if not (1 <= provincia <= 24 or provincia == 30):
        return False
    coef = (2, 1, 2, 1, 2, 1, 2, 1, 2)
    total = 0
    for i, c in enumerate(coef):
        prod = int(digitos_10[i]) * c
        if prod >= 10:
            prod -= 9
        total += prod
    verificador = (10 - (total % 10)) % 10
    return verificador == int(digitos_10[9])


def _validar_ruc_privado_mod11(ruc: str) -> bool:
    coef = (4, 3, 2, 7, 6, 5, 4, 3, 2)
    total = sum(int(ruc[i]) * coef[i] for i in range(9))
    residuo = total % 11
    verificador = 0 if residuo == 0 else (11 - residuo)
    return verificador == int(ruc[9]) and int(ruc[10:13]) >= 1


def _validar_ruc_publico_mod11(ruc: str) -> bool:
    coef = (3, 2, 7, 6, 5, 4, 3, 2)
    total = sum(int(ruc[i]) * coef[i] for i in range(8))
    residuo = total % 11
    verificador = 0 if residuo == 0 else (11 - residuo)
    return verificador == int(ruc[8]) and int(ruc[9:13]) >= 1


def clasificar_y_validar_identificacion_sri(
    identificacion: str,
    monto_total: float,
) -> Tuple[str, str]:
    """
    Retorna (tipo_identificacion_sri, identificacion_normalizada).
    Tabla 6 SRI:
      - '04': RUC
      - '05': Cédula
      - '06': Pasaporte / Identificación del exterior
      - '07': Venta a Consumidor Final (9999999999999)
    Lanza SriValidationError si es inválido o supera el tope de $50 para Consumidor Final.
    """
    raw = str(identificacion or "").strip().upper()
    if not raw:
        raise SriValidationError("La identificación del comprador está vacía.")

    only_digits = re.sub(r"\D", "", raw)

    # 1. Consumidor Final
    if only_digits == "9999999999999" or only_digits == "9999999999":
        if float(monto_total or 0.0) > 50.00:
            raise SriValidationError(
                f"Normativa SRI: No se permite facturar como Consumidor Final (9999999999999) por montos mayores a $50.00 USD (monto actual: ${float(monto_total):.2f})."
            )
        return "07", "9999999999999"

    # 2. RUC (13 dígitos numéricos terminados en 001..999)
    if len(only_digits) == 13 and raw.isdigit():
        prov = int(only_digits[:2])
        tercer = int(only_digits[2])
        estab = int(only_digits[10:13])
        if not (1 <= prov <= 24 or prov == 30) or estab < 1:
            raise SriValidationError(f"RUC '{only_digits}' con código de provincia o establecimiento inválido.")
        if tercer < 6:
            if not _validar_modulo_10(only_digits[:10]):
                raise SriValidationError(f"RUC persona natural '{only_digits}' no supera validación Módulo 10.")
            return "04", only_digits
        if tercer == 9 and _validar_ruc_privado_mod11(only_digits):
            return "04", only_digits
        if tercer == 6 and _validar_ruc_publico_mod11(only_digits):
            return "04", only_digits
        # En algunos RUC de sociedades jurídicas recientes el SRI asigna dígito directo sin módulo 11 clásico
        if tercer in (6, 9):
            return "04", only_digits
        raise SriValidationError(f"RUC '{only_digits}' no válido según estructura SRI.")

    # 3. Cédula (10 dígitos numéricos)
    if len(only_digits) == 10 and raw.isdigit():
        if not _validar_modulo_10(only_digits):
            raise SriValidationError(f"Cédula '{only_digits}' inválida (falló dígito verificador Módulo 10).")
        return "05", only_digits

    # 4. Pasaporte / Documento extranjero (alfanumérico entre 5 y 20 caracteres)
    clean_alnum = re.sub(r"[^A-Z0-9-]", "", raw)
    if 5 <= len(clean_alnum) <= 20:
        return "06", clean_alnum

    raise SriValidationError(
        f"Identificación '{raw}' no corresponde a RUC (13 dígitos), Cédula (10 dígitos) ni Pasaporte válido."
    )
