"""
Firmador electrónico XAdES-BES (enveloped) para comprobantes XML del SRI Ecuador (.p12 / PKCS#12).
Implementa la estructura exacta exigida por el validador XAdES-BES del SRI:
  - ds:SignedInfo con 3 referencias:
    1) Reference al nodo #comprobante (con Transform enveloped-signature)
    2) Reference al nodo #SignedProperties (Type="http://uri.etsi.org/01903#SignedProperties")
    3) Reference al nodo #Certificate1 (ds:KeyInfo)
  - ds:SignatureValue (RSA-SHA1 sobre C14N de ds:SignedInfo)
  - ds:KeyInfo (X509Certificate + RSAKeyValue Mod/Exp)
  - ds:Object -> etsi:QualifyingProperties -> etsi:SignedProperties (SigningTime en America/Guayaquil,
    CertDigest, IssuerSerial y SignedDataObjectProperties)
"""
from __future__ import annotations

import base64
import datetime
import hashlib
import secrets
from pathlib import Path
from typing import Tuple

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.hazmat.primitives.serialization import pkcs12
from lxml import etree

from app.services.sri_invoicing.access_key import TZ_ECUADOR

DS_NS = "http://www.w3.org/2000/09/xmldsig#"
ETSI_NS = "http://uri.etsi.org/01903/v1.3.2#"
NSMAP_SIG = {"ds": DS_NS, "etsi": ETSI_NS}


class SriSignerError(RuntimeError):
    """Error al cargar el certificado .p12 o firmar el comprobante XML."""


def _b64_lines(raw_bytes: bytes, line_len: int = 76) -> str:
    b64 = base64.b64encode(raw_bytes).decode("ascii")
    return "\n".join(b64[i : i + line_len] for i in range(0, len(b64), line_len))


def _int_to_b64(val: int) -> str:
    byte_len = (val.bit_length() + 7) // 8
    return _b64_lines(val.to_bytes(byte_len, byteorder="big"))


def _c14n(element: etree._Element) -> bytes:
    """Canonicalización C14N inclusiva hereda namespaces del documento raíz."""
    return etree.tostring(element, method="c14n", exclusive=False, with_comments=False)


def _sha1_b64(data: bytes) -> str:
    return base64.b64encode(hashlib.sha1(data).digest()).decode("ascii")


def cargar_certificado_p12(
    p12_path: str,
    p12_password: str,
) -> Tuple[rsa.RSAPrivateKey, x509.Certificate]:
    """Carga la llave privada RSA y el certificado X.509 principal desde un archivo .p12."""
    path = Path(p12_path)
    if not path.is_file():
        raise SriSignerError(f"No se encontró el archivo de firma electrónica (.p12) en: {p12_path}")

    raw_p12 = path.read_bytes()
    pw_bytes = p12_password.encode("utf-8") if p12_password else None

    try:
        private_key, cert, additional_certs = pkcs12.load_key_and_certificates(raw_p12, pw_bytes)
    except Exception as exc:
        raise SriSignerError(f"No se pudo abrir el certificado .p12 (verifica contraseña o formato): {exc}") from exc

    if private_key is None or cert is None:
        raise SriSignerError("El archivo .p12 no contiene llave privada o certificado X.509 válido.")

    if not isinstance(private_key, rsa.RSAPrivateKey):
        raise SriSignerError("La llave privada del certificado .p12 debe ser RSA.")

    now_utc = datetime.datetime.now(datetime.timezone.utc)
    not_after = getattr(cert, "not_valid_after_utc", None) or cert.not_valid_after.replace(
        tzinfo=datetime.timezone.utc
    )
    if now_utc > not_after:
        raise SriSignerError(f"El certificado de firma electrónica expiró el {not_after.isoformat()}.")

    return private_key, cert


def firmar_xml_xades_bes(
    xml_bytes: bytes,
    p12_path: str,
    p12_password: str,
) -> bytes:
    """
    Aplica la firma electrónica XAdES-BES sobre un comprobante XML del SRI (<factura id="comprobante">)
    y retorna el XML firmado en bytes UTF-8.
    """
    private_key, cert = cargar_certificado_p12(p12_path, p12_password)

    parser = etree.XMLParser(remove_blank_text=False)
    doc_root = etree.fromstring(xml_bytes, parser=parser)

    # 1. Digest del documento original sin el nodo ds:Signature (equivalente a enveloped-signature)
    comprobante_c14n = _c14n(doc_root)
    comprobante_digest_b64 = _sha1_b64(comprobante_c14n)

    # Identificadores únicos para los nodos XAdES-BES
    rnd = lambda: str(secrets.randbelow(899999) + 100000)
    signature_id = f"Signature{rnd()}"
    signed_info_id = f"Signature-SignedInfo{rnd()}"
    signed_props_id = f"{signature_id}-SignedProperties{rnd()}"
    object_id = f"{signature_id}-Object{rnd()}"
    key_info_id = f"Certificate{rnd()}"
    ref_comprobante_id = f"Reference-ID-{rnd()}"
    signed_props_ref_id = f"SignedPropertiesID{rnd()}"
    sig_value_id = f"SignatureValue{rnd()}"

    # Datos del certificado X.509
    cert_der = cert.public_bytes(serialization.Encoding.DER)
    cert_b64 = _b64_lines(cert_der)
    cert_digest_b64 = _sha1_b64(cert_der)
    issuer_dn = cert.issuer.rfc4514_string()
    serial_num = str(cert.serial_number)

    pub_numbers = private_key.public_key().public_numbers()
    modulus_b64 = _int_to_b64(pub_numbers.n)
    exponent_b64 = base64.b64encode(
        pub_numbers.e.to_bytes((pub_numbers.e.bit_length() + 7) // 8, "big")
    ).decode("ascii")

    signing_time_str = datetime.datetime.now(TZ_ECUADOR).isoformat(timespec="seconds")

    # 2. Construir el árbol ds:Signature y anexarlo al documento para que C14N herede el contexto exacto
    sig_el = etree.SubElement(doc_root, f"{{{DS_NS}}}Signature", nsmap=NSMAP_SIG, Id=signature_id)
    signed_info_el = etree.SubElement(sig_el, f"{{{DS_NS}}}SignedInfo", Id=signed_info_id)
    etree.SubElement(
        signed_info_el,
        f"{{{DS_NS}}}CanonicalizationMethod",
        Algorithm="http://www.w3.org/TR/2001/REC-xml-c14n-20010315",
    )
    etree.SubElement(
        signed_info_el,
        f"{{{DS_NS}}}SignatureMethod",
        Algorithm="http://www.w3.org/2000/09/xmldsig#rsa-sha1",
    )

    # Referencia 1: SignedProperties
    ref_sp = etree.SubElement(
        signed_info_el,
        f"{{{DS_NS}}}Reference",
        Id=signed_props_ref_id,
        Type="http://uri.etsi.org/01903#SignedProperties",
        URI=f"#{signed_props_id}",
    )
    etree.SubElement(ref_sp, f"{{{DS_NS}}}DigestMethod", Algorithm="http://www.w3.org/2000/09/xmldsig#sha1")
    ref_sp_digest_el = etree.SubElement(ref_sp, f"{{{DS_NS}}}DigestValue")

    # Referencia 2: KeyInfo (Certificate)
    ref_ki = etree.SubElement(signed_info_el, f"{{{DS_NS}}}Reference", URI=f"#{key_info_id}")
    etree.SubElement(ref_ki, f"{{{DS_NS}}}DigestMethod", Algorithm="http://www.w3.org/2000/09/xmldsig#sha1")
    ref_ki_digest_el = etree.SubElement(ref_ki, f"{{{DS_NS}}}DigestValue")

    # Referencia 3: #comprobante
    ref_doc = etree.SubElement(
        signed_info_el,
        f"{{{DS_NS}}}Reference",
        Id=ref_comprobante_id,
        URI="#comprobante",
    )
    transforms_el = etree.SubElement(ref_doc, f"{{{DS_NS}}}Transforms")
    etree.SubElement(
        transforms_el,
        f"{{{DS_NS}}}Transform",
        Algorithm="http://www.w3.org/2000/09/xmldsig#enveloped-signature",
    )
    etree.SubElement(ref_doc, f"{{{DS_NS}}}DigestMethod", Algorithm="http://www.w3.org/2000/09/xmldsig#sha1")
    ref_doc_digest_el = etree.SubElement(ref_doc, f"{{{DS_NS}}}DigestValue")
    ref_doc_digest_el.text = comprobante_digest_b64

    # Nodo SignatureValue (se llenará tras calcular digests de SignedProperties y KeyInfo)
    sig_val_el = etree.SubElement(sig_el, f"{{{DS_NS}}}SignatureValue", Id=sig_value_id)

    # Nodo KeyInfo
    key_info_el = etree.SubElement(sig_el, f"{{{DS_NS}}}KeyInfo", Id=key_info_id)
    x509_data_el = etree.SubElement(key_info_el, f"{{{DS_NS}}}X509Data")
    x509_cert_el = etree.SubElement(x509_data_el, f"{{{DS_NS}}}X509Certificate")
    x509_cert_el.text = f"\n{cert_b64}\n"
    key_val_el = etree.SubElement(key_info_el, f"{{{DS_NS}}}KeyValue")
    rsa_kv_el = etree.SubElement(key_val_el, f"{{{DS_NS}}}RSAKeyValue")
    mod_el = etree.SubElement(rsa_kv_el, f"{{{DS_NS}}}Modulus")
    mod_el.text = f"\n{modulus_b64}\n"
    exp_el = etree.SubElement(rsa_kv_el, f"{{{DS_NS}}}Exponent")
    exp_el.text = exponent_b64

    # Nodo Object -> QualifyingProperties -> SignedProperties
    obj_el = etree.SubElement(sig_el, f"{{{DS_NS}}}Object", Id=object_id)
    qual_props_el = etree.SubElement(
        obj_el,
        f"{{{ETSI_NS}}}QualifyingProperties",
        Target=f"#{signature_id}",
    )
    signed_props_el = etree.SubElement(
        qual_props_el,
        f"{{{ETSI_NS}}}SignedProperties",
        Id=signed_props_id,
    )
    ssp_el = etree.SubElement(signed_props_el, f"{{{ETSI_NS}}}SignedSignatureProperties")
    etree.SubElement(ssp_el, f"{{{ETSI_NS}}}SigningTime").text = signing_time_str
    sc_el = etree.SubElement(ssp_el, f"{{{ETSI_NS}}}SigningCertificate")
    cert_node = etree.SubElement(sc_el, f"{{{ETSI_NS}}}Cert")
    cd_el = etree.SubElement(cert_node, f"{{{ETSI_NS}}}CertDigest")
    etree.SubElement(cd_el, f"{{{DS_NS}}}DigestMethod", Algorithm="http://www.w3.org/2000/09/xmldsig#sha1")
    etree.SubElement(cd_el, f"{{{DS_NS}}}DigestValue").text = cert_digest_b64
    is_el = etree.SubElement(cert_node, f"{{{ETSI_NS}}}IssuerSerial")
    etree.SubElement(is_el, f"{{{DS_NS}}}X509IssuerName").text = issuer_dn
    etree.SubElement(is_el, f"{{{DS_NS}}}X509SerialNumber").text = serial_num

    sdop_el = etree.SubElement(signed_props_el, f"{{{ETSI_NS}}}SignedDataObjectProperties")
    dof_el = etree.SubElement(
        sdop_el,
        f"{{{ETSI_NS}}}DataObjectFormat",
        ObjectReference=f"#{ref_comprobante_id}",
    )
    etree.SubElement(dof_el, f"{{{ETSI_NS}}}Description").text = "contenido comprobante"
    etree.SubElement(dof_el, f"{{{ETSI_NS}}}MimeType").text = "text/xml"

    # 3. Calcular DigestValue de SignedProperties y KeyInfo en su contexto DOM final
    ref_sp_digest_el.text = _sha1_b64(_c14n(signed_props_el))
    ref_ki_digest_el.text = _sha1_b64(_c14n(key_info_el))

    # 4. Firmar ds:SignedInfo canonicalizado con RSA-SHA1
    signed_info_c14n = _c14n(signed_info_el)
    raw_signature = private_key.sign(
        signed_info_c14n,
        padding.PKCS1v15(),
        hashes.SHA1(),
    )
    sig_val_el.text = f"\n{_b64_lines(raw_signature)}\n"

    return etree.tostring(doc_root, encoding="UTF-8", xml_declaration=True, pretty_print=False)
