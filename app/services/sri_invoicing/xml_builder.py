"""
Generador del XML de Factura Electrónica (<factura id="comprobante" version="1.1.0">)
conforme al esquema XSD oficial del SRI Ecuador.
"""
from __future__ import annotations

import datetime
from typing import Any, Dict, Optional
from lxml import etree

from app.services.sri_invoicing.access_key import TZ_ECUADOR


def codigo_porcentaje_iva_sri(iva_pct: float) -> str:
    """
    Tabla 17 Ficha Técnica SRI:
      - 0%  -> '0'
      - 12% -> '2'
      - 14% -> '3'
      - 15% -> '4'
      - 5%  -> '5'
    """
    pct = round(float(iva_pct or 0.0), 2)
    if pct <= 0.0:
        return "0"
    if abs(pct - 15.0) < 0.1:
        return "4"
    if abs(pct - 12.0) < 0.1:
        return "2"
    if abs(pct - 5.0) < 0.1:
        return "5"
    return "4"


def construir_xml_factura_sri(
    *,
    ambiente: str,
    razon_social_emisor: str,
    nombre_comercial_emisor: str,
    ruc_emisor: str,
    clave_acceso: str,
    establecimiento: str,
    punto_emision: str,
    secuencial: str,
    dir_matriz: str,
    dir_establecimiento: str,
    obligado_contabilidad: str,
    contribuyente_rimpe: str = "",
    fecha_emision: Optional[datetime.date] = None,
    tipo_identificacion_comprador: str,
    razon_social_comprador: str,
    identificacion_comprador: str,
    direccion_comprador: str = "",
    correo_comprador: str = "",
    telefono_comprador: str = "",
    codigo_principal: str = "WB-SAAS",
    descripcion_item: str = "Suscripción Plataforma SaaS Wesbyte",
    monto_base: float,
    descuento: float = 0.0,
    iva_pct: float = 15.0,
    iva_monto: float,
    monto_total: float,
    forma_pago_sri: str = "19",
    referencia_pago: str = "",
) -> bytes:
    """Construye y retorna en bytes UTF-8 el XML <factura id="comprobante" version="1.1.0"> sin firmar."""
    if fecha_emision is None:
        fecha_emision = datetime.datetime.now(TZ_ECUADOR).date()

    fecha_str = fecha_emision.strftime("%d/%m/%Y")
    cod_porcentaje = codigo_porcentaje_iva_sri(iva_pct)
    tarifa_str = f"{float(iva_pct):.2f}" if float(iva_pct) % 1 else f"{int(round(float(iva_pct)))}"
    base_str = f"{float(monto_base):.2f}"
    desc_str = f"{float(descuento):.2f}"
    iva_str = f"{float(iva_monto):.2f}"
    total_str = f"{float(monto_total):.2f}"

    factura = etree.Element("factura", id="comprobante", version="1.1.0")

    # 1. infoTributaria
    info_trib = etree.SubElement(factura, "infoTributaria")
    etree.SubElement(info_trib, "ambiente").text = "2" if str(ambiente).strip() == "2" else "1"
    etree.SubElement(info_trib, "tipoEmision").text = "1"
    etree.SubElement(info_trib, "razonSocial").text = (razon_social_emisor or "WESBYTE").strip()[:300]
    if nombre_comercial_emisor:
        etree.SubElement(info_trib, "nombreComercial").text = nombre_comercial_emisor.strip()[:300]
    etree.SubElement(info_trib, "ruc").text = ruc_emisor.strip()
    etree.SubElement(info_trib, "claveAcceso").text = clave_acceso.strip()
    etree.SubElement(info_trib, "codDoc").text = "01"
    etree.SubElement(info_trib, "estab").text = establecimiento.strip().zfill(3)[:3]
    etree.SubElement(info_trib, "ptoEmi").text = punto_emision.strip().zfill(3)[:3]
    etree.SubElement(info_trib, "secuencial").text = secuencial.strip().zfill(9)[-9:]
    etree.SubElement(info_trib, "dirMatriz").text = (dir_matriz or "Ecuador").strip()[:300]
    if contribuyente_rimpe and contribuyente_rimpe.strip():
        etree.SubElement(info_trib, "contribuyenteRimpe").text = contribuyente_rimpe.strip()

    # 2. infoFactura
    info_fac = etree.SubElement(factura, "infoFactura")
    etree.SubElement(info_fac, "fechaEmision").text = fecha_str
    etree.SubElement(info_fac, "dirEstablecimiento").text = (dir_establecimiento or dir_matriz or "Ecuador").strip()[:300]
    etree.SubElement(info_fac, "obligadoContabilidad").text = "SI" if str(obligado_contabilidad).upper() == "SI" else "NO"
    etree.SubElement(info_fac, "tipoIdentificacionComprador").text = tipo_identificacion_comprador.strip()
    etree.SubElement(info_fac, "razonSocialComprador").text = (razon_social_comprador or "CLIENTE").strip()[:300]
    etree.SubElement(info_fac, "identificacionComprador").text = identificacion_comprador.strip()
    if direccion_comprador and direccion_comprador.strip():
        etree.SubElement(info_fac, "direccionComprador").text = direccion_comprador.strip()[:300]
    etree.SubElement(info_fac, "totalSinImpuestos").text = base_str
    etree.SubElement(info_fac, "totalDescuento").text = desc_str

    total_con_imp = etree.SubElement(info_fac, "totalConImpuestos")
    total_imp = etree.SubElement(total_con_imp, "totalImpuesto")
    etree.SubElement(total_imp, "codigo").text = "2"  # 2 = IVA
    etree.SubElement(total_imp, "codigoPorcentaje").text = cod_porcentaje
    etree.SubElement(total_imp, "baseImponible").text = base_str
    etree.SubElement(total_imp, "valor").text = iva_str

    etree.SubElement(info_fac, "propina").text = "0.00"
    etree.SubElement(info_fac, "importeTotal").text = total_str
    etree.SubElement(info_fac, "moneda").text = "DOLAR"

    pagos_el = etree.SubElement(info_fac, "pagos")
    pago_el = etree.SubElement(pagos_el, "pago")
    etree.SubElement(pago_el, "formaPago").text = (forma_pago_sri or "19").strip().zfill(2)
    etree.SubElement(pago_el, "total").text = total_str
    etree.SubElement(pago_el, "plazo").text = "0"
    etree.SubElement(pago_el, "unidadTiempo").text = "dias"

    # 3. detalles
    detalles_el = etree.SubElement(factura, "detalles")
    det = etree.SubElement(detalles_el, "detalle")
    etree.SubElement(det, "codigoPrincipal").text = (codigo_principal or "WB-SAAS")[:25]
    etree.SubElement(det, "descripcion").text = (descripcion_item or "Suscripción Plataforma SaaS Wesbyte")[:300]
    etree.SubElement(det, "cantidad").text = "1.00"
    etree.SubElement(det, "precioUnitario").text = base_str
    etree.SubElement(det, "descuento").text = desc_str
    etree.SubElement(det, "precioTotalSinImpuesto").text = base_str

    impuestos_det = etree.SubElement(det, "impuestos")
    imp_det = etree.SubElement(impuestos_det, "impuesto")
    etree.SubElement(imp_det, "codigo").text = "2"
    etree.SubElement(imp_det, "codigoPorcentaje").text = cod_porcentaje
    etree.SubElement(imp_det, "tarifa").text = tarifa_str
    etree.SubElement(imp_det, "baseImponible").text = base_str
    etree.SubElement(imp_det, "valor").text = iva_str

    # 4. infoAdicional
    campos_adicionales = []
    if correo_comprador and correo_comprador.strip():
        campos_adicionales.append(("Email", correo_comprador.strip()[:300]))
    if telefono_comprador and telefono_comprador.strip():
        campos_adicionales.append(("Telefono", telefono_comprador.strip()[:300]))
    if referencia_pago and referencia_pago.strip():
        campos_adicionales.append(("TransaccionPayPhone", referencia_pago.strip()[:300]))

    if campos_adicionales:
        info_ad = etree.SubElement(factura, "infoAdicional")
        for nombre_campo, valor_campo in campos_adicionales:
            campo = etree.SubElement(info_ad, "campoAdicional", nombre=nombre_campo)
            campo.text = valor_campo

    return etree.tostring(factura, encoding="UTF-8", xml_declaration=True, pretty_print=False)
