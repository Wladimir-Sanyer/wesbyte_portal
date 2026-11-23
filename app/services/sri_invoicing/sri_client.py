"""
Cliente SOAP (Zeep) para los Web Services Offline del SRI Ecuador:
  - RecepcionComprobantesOffline (validarComprobante)
  - AutorizacionComprobantesOffline (autorizacionComprobante)
Soporta Ambiente 1 (Pruebas: celcer.sri.gob.ec) y Ambiente 2 (Producción: cel.sri.gob.ec).
"""
from __future__ import annotations

import datetime
import logging
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

import requests
from lxml import etree
from zeep import Client
from zeep.helpers import serialize_object
from zeep.transports import Transport

logger = logging.getLogger("wesbyte_portal.sri_client")

WSDL_DIR = Path(__file__).resolve().parent / "wsdl"
LOCAL_PROD_WSDLS = {
    "recepcion": WSDL_DIR / "RecepcionComprobantesOffline_prod.wsdl",
    "autorizacion": WSDL_DIR / "AutorizacionComprobantesOffline_prod.wsdl",
}

WSDL_URLS = {
    "1": {
        "recepcion": "https://celcer.sri.gob.ec/comprobantes-electronicos-ws/RecepcionComprobantesOffline?wsdl",
        "autorizacion": "https://celcer.sri.gob.ec/comprobantes-electronicos-ws/AutorizacionComprobantesOffline?wsdl",
    },
    "2": {
        "recepcion": "https://cel.sri.gob.ec/comprobantes-electronicos-ws/RecepcionComprobantesOffline?wsdl",
        "autorizacion": "https://cel.sri.gob.ec/comprobantes-electronicos-ws/AutorizacionComprobantesOffline?wsdl",
    },
}


def _extraer_mensajes_sri(mensajes_node: Any) -> List[Dict[str, str]]:
    if not mensajes_node:
        return []
    raw_list = mensajes_node.get("mensaje") if isinstance(mensajes_node, dict) else getattr(mensajes_node, "mensaje", None)
    if not raw_list:
        return []
    if not isinstance(raw_list, list):
        raw_list = [raw_list]
    out: List[Dict[str, str]] = []
    for m in raw_list:
        d = serialize_object(m) if not isinstance(m, dict) else m
        out.append(
            {
                "identificador": str(d.get("identificador") or ""),
                "mensaje": str(d.get("mensaje") or ""),
                "informacionAdicional": str(d.get("informacionAdicional") or ""),
                "tipo": str(d.get("tipo") or ""),
            }
        )
    return out


def _formatear_resumen_mensajes(mensajes: List[Dict[str, str]]) -> str:
    partes = []
    for m in mensajes:
        cod = m.get("identificador") or ""
        msg = m.get("mensaje") or ""
        info = m.get("informacionAdicional") or ""
        txt = f"[{cod}] {msg}"
        if info:
            txt += f" ({info})"
        partes.append(txt.strip())
    return " | ".join(partes)


def construir_xml_autorizado_sri(
    *,
    estado: str,
    numero_autorizacion: str,
    fecha_autorizacion: str,
    ambiente: str,
    xml_firmado_bytes: bytes,
) -> bytes:
    """Construye el contenedor oficial <autorizacion> con el comprobante firmado dentro de CDATA."""
    root = etree.Element("autorizacion")
    etree.SubElement(root, "estado").text = estado
    etree.SubElement(root, "numeroAutorizacion").text = numero_autorizacion
    etree.SubElement(root, "fechaAutorizacion").text = fecha_autorizacion
    etree.SubElement(root, "ambiente").text = "PRODUCCIÓN" if str(ambiente) == "2" else "PRUEBAS"
    comp_el = etree.SubElement(root, "comprobante")
    xml_str = xml_firmado_bytes.decode("utf-8", errors="replace")
    comp_el.text = etree.CDATA(xml_str)
    etree.SubElement(root, "mensajes")
    return etree.tostring(root, encoding="UTF-8", xml_declaration=True, pretty_print=True)


class SriSoapClient:
    """Cliente SOAP con timeouts estrictos para Recepción y Autorización Offline del SRI."""

    def __init__(self, ambiente: str = "1", timeout_seconds: float = 25.0):
        self.ambiente = "2" if str(ambiente).strip() == "2" else "1"
        self.timeout_seconds = timeout_seconds
        self.urls = WSDL_URLS[self.ambiente]

    def _get_transport(self) -> Transport:
        session = requests.Session()
        # Evitar seguir redirecciones 302 rotas del balanceador SRI (ej. hacia 181.113.227.222)
        session.max_redirects = 0
        return Transport(session=session, timeout=self.timeout_seconds, operation_timeout=self.timeout_seconds)

    def _get_wsdl_source(self, service_key: str) -> str:
        if self.ambiente == "2":
            local_path = LOCAL_PROD_WSDLS.get(service_key)
            if local_path and local_path.exists():
                return str(local_path)
        return self.urls[service_key]

    def _call_with_retry(self, fn: Callable[[], Any], max_attempts: int = 3) -> Any:
        last_exc: Optional[Exception] = None
        for attempt in range(1, max_attempts + 1):
            try:
                return fn()
            except (requests.exceptions.TooManyRedirects, requests.exceptions.ConnectionError, requests.exceptions.Timeout) as exc:
                last_exc = exc
                logger.warning("Reintento SRI (%s/%s) por fallo/redirect de nodo SRI: %s", attempt, max_attempts, exc)
                if attempt < max_attempts:
                    time.sleep(1.0)
        raise last_exc  # type: ignore[misc]

    def enviar_recepcion(self, xml_firmado_bytes: bytes) -> Dict[str, Any]:
        """Envía el XML firmado a RecepcionComprobantesOffline.validarComprobante."""
        wsdl_src = self._get_wsdl_source("recepcion")
        client = Client(wsdl=wsdl_src, transport=self._get_transport())
        res = self._call_with_retry(lambda: client.service.validarComprobante(xml=xml_firmado_bytes))
        data = serialize_object(res) if not isinstance(res, dict) else res

        estado = str(data.get("estado") or "ERROR").upper()
        mensajes: List[Dict[str, str]] = []
        comp_node = data.get("comprobantes") or {}
        comp_list = comp_node.get("comprobante") if isinstance(comp_node, dict) else []
        if comp_list and not isinstance(comp_list, list):
            comp_list = [comp_list]
        for c in comp_list or []:
            mensajes.extend(_extraer_mensajes_sri(c.get("mensajes")))

        # Si el SRI responde error 43 (CLAVE ACCESO REGISTRADA) o 70 (EN PROCESAMIENTO),
        # significa que ya fue recibido previamente y podemos pasar a consultar autorización.
        codigos = {m.get("identificador") for m in mensajes}
        ya_registrada = bool(codigos & {"43", "70"})

        return {
            "estado": "RECIBIDA" if (estado == "RECIBIDA" or ya_registrada) else estado,
            "estado_original": estado,
            "ya_registrada": ya_registrada,
            "mensajes": mensajes,
            "detalle_error": _formatear_resumen_mensajes(mensajes),
        }

    def consultar_autorizacion(self, clave_acceso: str) -> Dict[str, Any]:
        """Consulta AutorizacionComprobantesOffline.autorizacionComprobante por clave de acceso."""
        wsdl_src = self._get_wsdl_source("autorizacion")
        client = Client(wsdl=wsdl_src, transport=self._get_transport())
        res = self._call_with_retry(lambda: client.service.autorizacionComprobante(claveAccesoComprobante=clave_acceso))
        data = serialize_object(res) if not isinstance(res, dict) else res

        aut_node = data.get("autorizaciones") or {}
        aut_list = aut_node.get("autorizacion") if isinstance(aut_node, dict) else []
        if not aut_list:
            return {
                "estado": "EN_PROCESO",
                "numero_autorizacion": "",
                "fecha_autorizacion": None,
                "mensajes": [],
                "detalle_error": "El SRI aún no registra respuesta de autorización para la clave de acceso.",
            }
        if not isinstance(aut_list, list):
            aut_list = [aut_list]

        # Priorizar si alguna entrada está AUTORIZADO
        elegida = next((a for a in aut_list if str(a.get("estado") or "").upper() == "AUTORIZADO"), aut_list[0])
        estado = str(elegida.get("estado") or "NO AUTORIZADO").upper()
        num_aut = str(elegida.get("numeroAutorizacion") or clave_acceso)
        fecha_aut = elegida.get("fechaAutorizacion")
        if isinstance(fecha_aut, datetime.datetime):
            fecha_aut_iso = fecha_aut.isoformat()
        elif fecha_aut:
            fecha_aut_iso = str(fecha_aut)
        else:
            fecha_aut_iso = datetime.datetime.now(datetime.timezone.utc).isoformat()

        mensajes = _extraer_mensajes_sri(elegida.get("mensajes"))
        return {
            "estado": estado,
            "numero_autorizacion": num_aut,
            "fecha_autorizacion": fecha_aut_iso,
            "mensajes": mensajes,
            "detalle_error": _formatear_resumen_mensajes(mensajes),
        }

    def emitir_comprobante_completo(
        self,
        xml_firmado_bytes: bytes,
        clave_acceso: str,
        espera_segundos: float = 3.0,
    ) -> Dict[str, Any]:
        """
        Ejecuta el ciclo Offline del SRI:
          1) validarComprobante
          2) espera 3s
          3) autorizacionComprobante (con hasta 2 consultas si sigue EN_PROCESO)
        """
        recep = self.enviar_recepcion(xml_firmado_bytes)
        if recep["estado"] != "RECIBIDA":
            return {
                "autorizado": False,
                "estado_sri": recep["estado"],  # DEVUELTA
                "numero_autorizacion": "",
                "fecha_autorizacion": None,
                "detalle_error": recep["detalle_error"] or "Comprobante devuelto en Recepción SRI.",
                "xml_autorizado_bytes": None,
            }

        time.sleep(max(1.0, float(espera_segundos)))
        aut = self.consultar_autorizacion(clave_acceso)
        if aut["estado"] == "EN_PROCESO":
            time.sleep(3.0)
            aut = self.consultar_autorizacion(clave_acceso)

        if aut["estado"] == "AUTORIZADO":
            xml_aut = construir_xml_autorizado_sri(
                estado="AUTORIZADO",
                numero_autorizacion=aut["numero_autorizacion"] or clave_acceso,
                fecha_autorizacion=str(aut["fecha_autorizacion"] or ""),
                ambiente=self.ambiente,
                xml_firmado_bytes=xml_firmado_bytes,
            )
            return {
                "autorizado": True,
                "estado_sri": "AUTORIZADO",
                "numero_autorizacion": aut["numero_autorizacion"] or clave_acceso,
                "fecha_autorizacion": aut["fecha_autorizacion"],
                "detalle_error": "",
                "xml_autorizado_bytes": xml_aut,
            }

        return {
            "autorizado": False,
            "estado_sri": "RECIBIDO" if aut["estado"] == "EN_PROCESO" else aut["estado"].replace(" ", "_"),
            "numero_autorizacion": "",
            "fecha_autorizacion": aut.get("fecha_autorizacion"),
            "detalle_error": aut["detalle_error"] or f"Estado SRI: {aut['estado']}",
            "xml_autorizado_bytes": None,
        }
