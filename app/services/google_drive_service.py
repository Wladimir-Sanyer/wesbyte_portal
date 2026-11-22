import json
import logging
import re
import threading
import time
import uuid
from typing import Dict, Optional, Tuple
import httpx

from app.config import (
    GOOGLE_DRIVE_CLIENT_ID,
    GOOGLE_DRIVE_CLIENT_SECRET,
    GOOGLE_DRIVE_REFRESH_TOKEN,
    MEDIA_PUBLIC_BASE_URL,
)

logger = logging.getLogger("wesbyte_portal.services.google_drive")

_DRIVE_LOCK = threading.Lock()
_TOKEN_CACHE: Dict[str, Tuple[float, str]] = {}
_FOLDER_ID_CACHE: Dict[str, str] = {}
_TOKEN_TTL_SECONDS = 3000  # 50 minutos (OAuth2 access_token dura 60 min)


class GoogleDriveServiceError(Exception):
    """Error al interactuar con Google Drive."""
    pass


class GoogleDriveService:
    """Servicio para gestionar carpetas y subir logos de clientes a Google Drive."""

    TOKEN_URL = "https://oauth2.googleapis.com/token"
    FILES_URL = "https://www.googleapis.com/drive/v3/files"
    UPLOAD_URL = "https://www.googleapis.com/upload/drive/v3/files"

    def __init__(
        self,
        client_id: Optional[str] = None,
        client_secret: Optional[str] = None,
        refresh_token: Optional[str] = None,
    ):
        self.client_id = (client_id or GOOGLE_DRIVE_CLIENT_ID or "").strip()
        self.client_secret = (client_secret or GOOGLE_DRIVE_CLIENT_SECRET or "").strip()
        self.refresh_token = (refresh_token or GOOGLE_DRIVE_REFRESH_TOKEN or "").strip()

    def _get_access_token(self) -> str:
        if not self.client_id or not self.client_secret or not self.refresh_token:
            raise GoogleDriveServiceError(
                "Faltan credenciales de Google Drive (GOOGLE_DRIVE_CLIENT_ID / SECRET / REFRESH_TOKEN)."
            )
        cache_key = f"{self.client_id}:{self.refresh_token[:12]}"
        now = time.time()
        with _DRIVE_LOCK:
            cached = _TOKEN_CACHE.get(cache_key)
            if cached and (now - cached[0]) < _TOKEN_TTL_SECONDS:
                return cached[1]

        resp = httpx.post(
            self.TOKEN_URL,
            data={
                "client_id": self.client_id,
                "client_secret": self.client_secret,
                "refresh_token": self.refresh_token,
                "grant_type": "refresh_token",
            },
            timeout=20.0,
        )
        if resp.status_code >= 400:
            raise GoogleDriveServiceError(f"Error obteniendo access_token de Google Drive: {resp.text}")
        token = resp.json().get("access_token")
        if not token:
            raise GoogleDriveServiceError("Google OAuth2 no devolvió access_token.")

        with _DRIVE_LOCK:
            _TOKEN_CACHE[cache_key] = (now, token)
        return token

    def _find_or_create_folder(self, name: str, parent_id: Optional[str], token: str) -> str:
        headers = {"Authorization": f"Bearer {token}"}
        safe_name = name.replace("'", "\\'")
        q_parts = [
            f"name='{safe_name}'",
            "mimeType='application/vnd.google-apps.folder'",
            "trashed=false",
        ]
        if parent_id:
            q_parts.append(f"'{parent_id}' in parents")
        else:
            q_parts.append("'root' in parents")

        resp = httpx.get(
            self.FILES_URL,
            headers=headers,
            params={"q": " and ".join(q_parts), "fields": "files(id,name)", "pageSize": 1},
            timeout=20.0,
        )
        if resp.status_code >= 400:
            raise GoogleDriveServiceError(f"Error buscando carpeta '{name}' en Drive: {resp.text}")
        files = resp.json().get("files", [])
        if files:
            return files[0]["id"]

        metadata: Dict[str, object] = {
            "name": name,
            "mimeType": "application/vnd.google-apps.folder",
        }
        if parent_id:
            metadata["parents"] = [parent_id]

        create_resp = httpx.post(
            self.FILES_URL,
            headers={**headers, "Content-Type": "application/json"},
            json=metadata,
            params={"fields": "id,name"},
            timeout=20.0,
        )
        if create_resp.status_code >= 400:
            raise GoogleDriveServiceError(f"Error creando carpeta '{name}' en Drive: {create_resp.text}")
        return create_resp.json()["id"]

    def ensure_folder_path(self, folder_path: str = "clientes/logos", token: Optional[str] = None) -> str:
        """Asegura la jerarquía de carpetas (ej. clientes/logos) y retorna el ID de la carpeta final."""
        norm_path = "/".join(p.strip() for p in folder_path.split("/") if p.strip())
        if not norm_path:
            raise GoogleDriveServiceError("La ruta de carpeta en Drive no puede estar vacía.")
        cache_key = f"{self.client_id}:{norm_path}"
        with _DRIVE_LOCK:
            cached_fid = _FOLDER_ID_CACHE.get(cache_key)
        if cached_fid:
            return cached_fid

        tok = token or self._get_access_token()
        parent_id: Optional[str] = None
        for part in norm_path.split("/"):
            parent_id = self._find_or_create_folder(part, parent_id, tok)
        if not parent_id:
            raise GoogleDriveServiceError("La ruta de carpeta en Drive no puede estar vacía.")

        with _DRIVE_LOCK:
            _FOLDER_ID_CACHE[cache_key] = parent_id
        return parent_id

    def upload_logo(
        self,
        file_bytes: bytes,
        filename: str,
        mime_type: str = "image/png",
        folder_path: str = "clientes/logos",
    ) -> Dict[str, str]:
        """Sube un logo a Google Drive dentro de clientes/logos, lo hace público y retorna su URL."""
        token = self._get_access_token()
        folder_id = self.ensure_folder_path(folder_path, token=token)

        boundary = f"wesbyte_{uuid.uuid4().hex}"
        metadata = json.dumps({"name": filename, "parents": [folder_id]}).encode("utf-8")
        body = (
            f"--{boundary}\r\n"
            f"Content-Type: application/json; charset=UTF-8\r\n\r\n"
        ).encode("utf-8") + metadata + (
            f"\r\n--{boundary}\r\n"
            f"Content-Type: {mime_type}\r\n\r\n"
        ).encode("utf-8") + file_bytes + f"\r\n--{boundary}--\r\n".encode("utf-8")

        upload_resp = httpx.post(
            self.UPLOAD_URL,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": f"multipart/related; boundary={boundary}",
            },
            params={"uploadType": "multipart", "fields": "id,name,webViewLink,webContentLink"},
            content=body,
            timeout=30.0,
        )
        if upload_resp.status_code >= 400:
            raise GoogleDriveServiceError(f"Error subiendo archivo a Drive: {upload_resp.text}")

        data = upload_resp.json()
        file_id = data["id"]

        # Permitir lectura pública para que pueda renderizarse en correos y portal
        perm_resp = httpx.post(
            f"{self.FILES_URL}/{file_id}/permissions",
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            json={"role": "reader", "type": "anyone"},
            timeout=20.0,
        )
        if perm_resp.status_code >= 400:
            logger.warning(f"No se pudo asignar permiso público al archivo {file_id}: {perm_resp.text}")

        public_url = get_proxied_drive_url(file_id)
        return {
            "file_id": file_id,
            "filename": data.get("name", filename),
            "url": public_url,
            "web_view_link": data.get("webViewLink", ""),
        }

    def upload_private_file(
        self,
        file_bytes: bytes,
        filename: str,
        mime_type: str = "application/octet-stream",
        folder_path: str = "Facturas_SRI",
    ) -> Dict[str, str]:
        """Sube un archivo privado (ej. XML o PDF RIDE de factura SRI) a Google Drive sin permisos públicos."""
        token = self._get_access_token()
        folder_id = self.ensure_folder_path(folder_path, token=token)

        boundary = f"wesbyte_{uuid.uuid4().hex}"
        metadata = json.dumps({"name": filename, "parents": [folder_id]}).encode("utf-8")
        body = (
            f"--{boundary}\r\n"
            f"Content-Type: application/json; charset=UTF-8\r\n\r\n"
        ).encode("utf-8") + metadata + (
            f"\r\n--{boundary}\r\n"
            f"Content-Type: {mime_type}\r\n\r\n"
        ).encode("utf-8") + file_bytes + f"\r\n--{boundary}--\r\n".encode("utf-8")

        upload_resp = httpx.post(
            self.UPLOAD_URL,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": f"multipart/related; boundary={boundary}",
            },
            params={"uploadType": "multipart", "fields": "id,name,webViewLink"},
            content=body,
            timeout=30.0,
        )
        if upload_resp.status_code >= 400:
            raise GoogleDriveServiceError(f"Error subiendo archivo privado a Drive: {upload_resp.text}")

        data = upload_resp.json()
        return {
            "file_id": data["id"],
            "filename": data.get("name", filename),
            "web_view_link": data.get("webViewLink", ""),
        }


def extract_drive_file_id(drive_url_or_id: Optional[str]) -> str:
    """Extrae el file_id de un enlace de Google Drive, lh3.googleusercontent.com, /api/media/ o ID directo."""
    raw = str(drive_url_or_id or "").strip()
    if not raw:
        return ""
    if re.fullmatch(r"[A-Za-z0-9_-]{15,120}", raw):
        return raw
    patterns = (
        r"lh3\.googleusercontent\.com/d/([A-Za-z0-9_-]+)",
        r"/file/d/([A-Za-z0-9_-]+)",
        r"/api/media/([A-Za-z0-9_-]+)",
        r"/media/([A-Za-z0-9_-]+)",
        r"[?&]id=([A-Za-z0-9_-]+)",
    )
    for pat in patterns:
        m = re.search(pat, raw)
        if m:
            return m.group(1)
    return ""


def get_proxied_drive_url(drive_url_or_id: Optional[str], base_url: Optional[str] = None) -> str:
    """Convierte cualquier URL o ID de Google Drive en una URL pública bajo el dominio propio de Wesbyte."""
    raw = str(drive_url_or_id or "").strip()
    if not raw:
        return ""
    file_id = extract_drive_file_id(raw)
    if file_id:
        base = (base_url or MEDIA_PUBLIC_BASE_URL or "https://wesbyte.app").rstrip("/")
        return f"{base}/api/media/{file_id}"
    return raw


def normalize_drive_logo_url(url: Optional[str]) -> str:
    """Convierte URLs de Google Drive o lh3.googleusercontent.com a URL propia de Wesbyte (/api/media/{file_id})."""
    return get_proxied_drive_url(url)
