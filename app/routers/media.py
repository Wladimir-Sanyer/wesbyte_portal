"""
Proxy público de imágenes alojadas en Google Drive bajo el dominio propio de Wesbyte.
Evita alertas de entregabilidad de Resend/Gmail causadas por enlaces externos a lh3.googleusercontent.com
y cachea en disco/memoria por 24 horas para no golpear rate limits de Google Drive.
"""
from __future__ import annotations

import json
import logging
import re
import time
from pathlib import Path
from typing import Optional, Tuple

import httpx
from fastapi import APIRouter, HTTPException
from fastapi.responses import Response

from app.services.google_drive_service import GoogleDriveService

logger = logging.getLogger("wesbyte_portal.routers.media")

router = APIRouter(tags=["Media Proxy"])

CACHE_TTL_SECONDS = 86400  # 24 horas
CACHE_DIR = Path("/tmp/wesbyte_media_cache")
CACHE_DIR.mkdir(parents=True, exist_ok=True)

ALLOWED_IMAGE_TYPES = {
    "image/png": "image/png",
    "image/jpeg": "image/jpeg",
    "image/jpg": "image/jpeg",
    "image/webp": "image/webp",
    "image/gif": "image/gif",
    "image/svg+xml": "image/svg+xml",
}
_MEM_CACHE: dict[str, Tuple[float, str, bytes]] = {}
_VALID_ID_RE = re.compile(r"^[A-Za-z0-9_-]{10,120}$")


def _clean_content_type(raw_ct: Optional[str]) -> Optional[str]:
    if not raw_ct:
        return None
    base = raw_ct.split(";")[0].strip().lower()
    return ALLOWED_IMAGE_TYPES.get(base)


def _read_cache(file_id: str) -> Optional[Tuple[str, bytes]]:
    now = time.time()
    mem_hit = _MEM_CACHE.get(file_id)
    if mem_hit and (now - mem_hit[0]) < CACHE_TTL_SECONDS:
        return mem_hit[1], mem_hit[2]

    bin_path = CACHE_DIR / f"{file_id}.bin"
    meta_path = CACHE_DIR / f"{file_id}.json"
    if bin_path.exists() and meta_path.exists():
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            ts = float(meta.get("ts", 0))
            ct = _clean_content_type(meta.get("content_type"))
            if ct and (now - ts) < CACHE_TTL_SECONDS:
                data = bin_path.read_bytes()
                if data:
                    _MEM_CACHE[file_id] = (ts, ct, data)
                    return ct, data
        except Exception:
            pass
    return None


def _write_cache(file_id: str, content_type: str, data: bytes) -> None:
    now = time.time()
    _MEM_CACHE[file_id] = (now, content_type, data)
    try:
        (CACHE_DIR / f"{file_id}.bin").write_bytes(data)
        (CACHE_DIR / f"{file_id}.json").write_text(
            json.dumps({"ts": now, "content_type": content_type}),
            encoding="utf-8",
        )
    except Exception as exc:
        logger.debug(f"No se pudo escribir caché en disco para {file_id}: {exc}")


async def _fetch_drive_image(file_id: str) -> Optional[Tuple[str, bytes]]:
    urls = (
        f"https://lh3.googleusercontent.com/d/{file_id}",
        f"https://drive.google.com/uc?export=view&id={file_id}",
    )
    async with httpx.AsyncClient(follow_redirects=True, timeout=15.0) as client:
        for url in urls:
            try:
                resp = await client.get(url)
                if resp.status_code == 200 and resp.content:
                    ct = _clean_content_type(resp.headers.get("content-type"))
                    if ct:
                        return ct, resp.content
            except Exception as exc:
                logger.debug(f"Error descargando {url}: {exc}")

    # Fallback con credenciales OAuth2 de Google Drive si el archivo no es público o hubo rate limit
    try:
        drv = GoogleDriveService()
        token = drv._get_access_token()
        async with httpx.AsyncClient(follow_redirects=True, timeout=20.0) as client:
            resp = await client.get(
                f"{drv.FILES_URL}/{file_id}",
                params={"alt": "media"},
                headers={"Authorization": f"Bearer {token}"},
            )
            if resp.status_code == 200 and resp.content:
                ct = _clean_content_type(resp.headers.get("content-type"))
                if ct:
                    return ct, resp.content
    except Exception as exc:
        logger.warning(f"Fallback Drive API falló para {file_id}: {exc}")

    return None


@router.get("/api/media/{file_id}", summary="Proxy público de imágenes desde Google Drive")
@router.get("/media/{file_id}", include_in_schema=False)
async def serve_drive_media(file_id: str):
    """Sirve imágenes públicas de Google Drive bajo el dominio propio con caché de 24 horas."""
    if not _VALID_ID_RE.match(file_id):
        raise HTTPException(status_code=400, detail="Identificador de archivo inválido.")

    cached = _read_cache(file_id)
    if cached:
        ct, data = cached
        return Response(
            content=data,
            media_type=ct,
            headers={"Cache-Control": f"public, max-age={CACHE_TTL_SECONDS}"},
        )

    fetched = await _fetch_drive_image(file_id)
    if not fetched:
        raise HTTPException(status_code=404, detail="Imagen no encontrada o formato no válido.")

    ct, data = fetched
    _write_cache(file_id, ct, data)
    return Response(
        content=data,
        media_type=ct,
        headers={"Cache-Control": f"public, max-age={CACHE_TTL_SECONDS}"},
    )
