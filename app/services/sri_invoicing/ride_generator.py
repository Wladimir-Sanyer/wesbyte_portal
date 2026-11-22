"""
Generador en memoria (BytesIO) del PDF RIDE (Representación Impresa de Documento Electrónico)
conforme a los requisitos visuales del SRI Ecuador, usando ReportLab + python-barcode (Code128).
"""
from __future__ import annotations

import io
import textwrap
from typing import Optional

import barcode
from barcode.writer import ImageWriter
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen import canvas


def _generar_barcode_code128_png(clave_acceso: str) -> io.BytesIO:
    """Genera en memoria una imagen PNG del código de barras Code128 de los 49 dígitos."""
    buf = io.BytesIO()
    code128_cls = barcode.get_barcode_class("code128")
    writer = ImageWriter()
    bc = code128_cls(clave_acceso, writer=writer)
    bc.write(
        buf,
        options={
            "module_width": 0.22,
            "module_height": 10.0,
            "quiet_zone": 2.0,
            "write_text": False,
            "dpi": 200,
        },
    )
    buf.seek(0)
    return buf


def generar_ride_pdf_bytes(
    *,
    ruc_emisor: str,
    razon_social_emisor: str,
    nombre_comercial_emisor: str,
    dir_matriz: str,
    dir_establecimiento: str,
    obligado_contabilidad: str,
    contribuyente_rimpe: str = "",
    establecimiento: str,
    punto_emision: str,
    secuencial: str,
    numero_autorizacion: str,
    fecha_autorizacion: str,
    ambiente: str,
    clave_acceso: str,
    fecha_emision: str,
    razon_social_comprador: str,
    identificacion_comprador: str,
    direccion_comprador: str = "",
    correo_comprador: str = "",
    telefono_comprador: str = "",
    codigo_item: str = "WB-SAAS",
    descripcion_item: str = "Suscripción Plataforma SaaS Wesbyte",
    monto_base: float,
    descuento: float = 0.0,
    iva_pct: float = 15.0,
    iva_monto: float,
    monto_total: float,
    forma_pago_texto: str = "19 - TARJETA DE CRÉDITO / PAYPHONE",
    referencia_pago: str = "",
) -> bytes:
    """Construye el PDF del RIDE en memoria y retorna sus bytes."""
    pdf_buf = io.BytesIO()
    c = canvas.Canvas(pdf_buf, pagesize=A4)
    width, height = A4

    numero_factura = f"{establecimiento.zfill(3)}-{punto_emision.zfill(3)}-{secuencial.zfill(9)}"
    ambiente_txt = "PRODUCCIÓN" if str(ambiente).strip() == "2" else "PRUEBAS"

    # Colores corporativos Wesbyte / SRI
    navy = colors.HexColor("#12163a")
    border_gray = colors.HexColor("#cbd5e1")
    bg_soft = colors.HexColor("#f8fafc")

    # 1. Bloque izquierdo: Emisor (con isotipo vectorial Wesbyte wave-sine)
    c.setStrokeColor(border_gray)
    c.setFillColor(bg_soft)
    c.roundRect(14 * mm, height - 88 * mm, 88 * mm, 74 * mm, 4 * mm, stroke=1, fill=1)

    # Dibujar isotipo Wesbyte (viewBox 64x64 -> 12mm x 12mm en (18mm, height - 30mm))
    c.saveState()
    logo_size = 12 * mm
    scale = logo_size / 64.0
    c.translate(18 * mm, height - 18 * mm)
    c.scale(scale, -scale)
    c.setStrokeColor(colors.HexColor("#09090B"))
    c.setLineWidth(7.2)
    c.setLineCap(1)
    c.setLineJoin(1)
    p = c.beginPath()
    p.moveTo(9, 19)
    p.curveTo(13, 38.5, 16.5, 48, 21, 48)
    p.curveTo(25.5, 48, 28, 24.5, 32, 24.5)
    p.curveTo(36, 24.5, 38.5, 48, 43, 48)
    p.curveTo(47.5, 48, 51, 38.5, 55, 19)
    c.drawPath(p, stroke=1, fill=0)
    c.setFillColor(colors.HexColor("#2563EB"))
    c.circle(32, 12.5, 4.2, stroke=0, fill=1)
    c.restoreState()

    c.setFillColor(navy)
    c.setFont("Helvetica-Bold", 15)
    c.drawString(33 * mm, height - 26 * mm, (nombre_comercial_emisor or razon_social_emisor or "WESBYTE")[:24])

    c.setFillColor(colors.black)
    c.setFont("Helvetica-Bold", 8.8)
    c.drawString(18 * mm, height - 36 * mm, (razon_social_emisor or "WESBYTE")[:46])

    c.setFont("Helvetica", 7.8)
    y_dir = height - 43 * mm
    for line in textwrap.wrap(f"Dir. Matriz: {dir_matriz or 'Ecuador'}", width=48)[:2]:
        c.drawString(18 * mm, y_dir, line)
        y_dir -= 3.8 * mm
    y_dir -= 1.2 * mm
    for line in textwrap.wrap(f"Dir. Sucursal: {dir_establecimiento or dir_matriz or 'Ecuador'}", width=48)[:2]:
        c.drawString(18 * mm, y_dir, line)
        y_dir -= 3.8 * mm

    c.setFont("Helvetica", 8.0)
    c.drawString(
        18 * mm,
        height - 64 * mm,
        f"Obligado a llevar contabilidad: {'SI' if str(obligado_contabilidad).upper() == 'SI' else 'NO'}",
    )
    if contribuyente_rimpe:
        c.setFont("Helvetica-Bold", 8)
        c.drawString(18 * mm, height - 70 * mm, contribuyente_rimpe[:48])
        c.setFont("Helvetica-Bold", 8)
        c.drawString(18 * mm, height - 67 * mm, contribuyente_rimpe[:48])

    # 2. Bloque derecho: Datos Tributarios SRI y Clave de Acceso con Barcode
    c.setFillColor(colors.white)
    c.roundRect(106 * mm, height - 88 * mm, 90 * mm, 74 * mm, 4 * mm, stroke=1, fill=1)

    c.setFillColor(colors.black)
    c.setFont("Helvetica-Bold", 10)
    c.drawString(110 * mm, height - 22 * mm, f"R.U.C.: {ruc_emisor}")
    c.setFillColor(navy)
    c.setFont("Helvetica-Bold", 12)
    c.drawString(110 * mm, height - 30 * mm, "FACTURA")
    c.setFillColor(colors.black)
    c.setFont("Helvetica-Bold", 9.5)
    c.drawString(110 * mm, height - 37 * mm, f"No. {numero_factura}")

    c.setFont("Helvetica-Bold", 7.5)
    c.drawString(110 * mm, height - 44 * mm, "NÚMERO DE AUTORIZACIÓN:")
    c.setFont("Helvetica", 6.8)
    c.drawString(110 * mm, height - 48.5 * mm, (numero_autorizacion or clave_acceso)[:52])

    c.setFont("Helvetica", 7.8)
    c.drawString(110 * mm, height - 55 * mm, f"FECHA Y HORA AUTORIZACIÓN: {str(fecha_autorizacion)[:25]}")
    c.drawString(110 * mm, height - 60.5 * mm, f"AMBIENTE: {ambiente_txt}   |   EMISIÓN: NORMAL")
    c.setFont("Helvetica-Bold", 7.5)
    c.drawString(110 * mm, height - 66 * mm, "CLAVE DE ACCESO:")

    try:
        bc_buf = _generar_barcode_code128_png(clave_acceso)
        c.drawImage(ImageReader(bc_buf), 109 * mm, height - 80 * mm, width=84 * mm, height=12 * mm, mask="auto")
    except Exception:
        pass

    c.setFont("Helvetica", 6.8)
    c.drawCentredString(151 * mm, height - 84.5 * mm, clave_acceso)

    # 3. Datos del Comprador
    c.setFillColor(bg_soft)
    c.roundRect(14 * mm, height - 118 * mm, 182 * mm, 25 * mm, 3 * mm, stroke=1, fill=1)
    c.setFillColor(colors.black)
    c.setFont("Helvetica-Bold", 8.5)
    c.drawString(18 * mm, height - 99 * mm, "Razón Social / Nombres:")
    c.setFont("Helvetica", 8.5)
    c.drawString(58 * mm, height - 99 * mm, (razon_social_comprador or "CLIENTE")[:65])

    c.setFont("Helvetica-Bold", 8.5)
    c.drawString(18 * mm, height - 106 * mm, "Identificación (RUC/CI):")
    c.setFont("Helvetica", 8.5)
    c.drawString(58 * mm, height - 106 * mm, identificacion_comprador)

    c.setFont("Helvetica-Bold", 8.5)
    c.drawString(120 * mm, height - 106 * mm, "Fecha de Emisión:")
    c.setFont("Helvetica", 8.5)
    c.drawString(152 * mm, height - 106 * mm, fecha_emision)

    c.setFont("Helvetica-Bold", 8.5)
    c.drawString(18 * mm, height - 113 * mm, "Dirección:")
    c.setFont("Helvetica", 8.5)
    c.drawString(58 * mm, height - 113 * mm, (direccion_comprador or "Ecuador")[:75])

    # 4. Tabla de Detalles
    y_top = height - 126 * mm
    c.setFillColor(navy)
    c.rect(14 * mm, y_top - 7 * mm, 182 * mm, 7 * mm, stroke=0, fill=1)
    c.setFillColor(colors.white)
    c.setFont("Helvetica-Bold", 8)
    c.drawString(17 * mm, y_top - 5 * mm, "Código")
    c.drawString(40 * mm, y_top - 5 * mm, "Cant.")
    c.drawString(55 * mm, y_top - 5 * mm, "Descripción")
    c.drawRightString(148 * mm, y_top - 5 * mm, "P. Unitario")
    c.drawRightString(170 * mm, y_top - 5 * mm, "Descuento")
    c.drawRightString(193 * mm, y_top - 5 * mm, "Total Sin Imp.")

    y_row = y_top - 16 * mm
    c.setFillColor(colors.white)
    c.rect(14 * mm, y_row, 182 * mm, 9 * mm, stroke=1, fill=1)
    c.setFillColor(colors.black)
    c.setFont("Helvetica", 8.2)
    c.drawString(17 * mm, y_row + 3 * mm, codigo_item[:12])
    c.drawString(40 * mm, y_row + 3 * mm, "1.00")
    c.drawString(55 * mm, y_row + 3 * mm, descripcion_item[:52])
    c.drawRightString(148 * mm, y_row + 3 * mm, f"${float(monto_base):.2f}")
    c.drawRightString(170 * mm, y_row + 3 * mm, f"${float(descuento):.2f}")
    c.drawRightString(193 * mm, y_row + 3 * mm, f"${float(monto_base):.2f}")

    # 5. Información Adicional + Forma de Pago (izquierda) y Totales (derecha)
    y_bot = y_row - 48 * mm
    c.setFillColor(bg_soft)
    c.roundRect(14 * mm, y_bot, 108 * mm, 42 * mm, 3 * mm, stroke=1, fill=1)
    c.setFillColor(colors.black)
    c.setFont("Helvetica-Bold", 8.5)
    c.drawString(18 * mm, y_bot + 35 * mm, "Información Adicional")
    c.setFont("Helvetica", 8)
    c.drawString(18 * mm, y_bot + 28 * mm, f"Email: {correo_comprador or '-'}")
    c.drawString(18 * mm, y_bot + 22 * mm, f"Teléfono: {telefono_comprador or '-'}")
    if referencia_pago:
        c.drawString(18 * mm, y_bot + 16 * mm, f"Transacción PayPhone: {referencia_pago}")
    c.setFont("Helvetica-Bold", 8)
    c.drawString(18 * mm, y_bot + 7 * mm, f"Forma de Pago SRI: {forma_pago_texto} (${float(monto_total):.2f})")

    # Cuadro de Totales
    c.setFillColor(colors.white)
    c.roundRect(126 * mm, y_bot, 70 * mm, 42 * mm, 3 * mm, stroke=1, fill=1)
    c.setFillColor(colors.black)
    totales_rows = [
        (f"SUBTOTAL {int(round(float(iva_pct)))}%", f"${float(monto_base):.2f}", False),
        ("SUBTOTAL 0%", "$0.00", False),
        ("TOTAL DESCUENTO", f"${float(descuento):.2f}", False),
        (f"IVA {int(round(float(iva_pct)))}%", f"${float(iva_monto):.2f}", False),
        ("VALOR TOTAL", f"${float(monto_total):.2f}", True),
    ]
    curr_y = y_bot + 33 * mm
    for label, val, is_bold in totales_rows:
        c.setFont("Helvetica-Bold" if is_bold else "Helvetica", 8.8 if is_bold else 8)
        c.drawString(130 * mm, curr_y, label)
        c.drawRightString(192 * mm, curr_y, val)
        curr_y -= 7 * mm

    c.showPage()
    c.save()
    return pdf_buf.getvalue()
