# Wesbyte Portal Comercial & Onboarding Multi-Tenant

Aplicación web desarrollada en **Python (FastAPI + Gunicorn/Uvicorn + Google Cloud BigQuery + PayPhone)** para la comercialización, personalización de planes, cobro y aprovisionamiento automático de entornos Multi-Tenant en el proyecto `broker-pmp`.

Corre por defecto en el **puerto `8085`** para convivir sin conflicto con `broker-pmp-app` (`8080`).

## Ejecución con Docker (Local y VM)

```bash
docker compose up -d --build
```

- **Portal Comercial**: `http://localhost:8085`
- **Panel de Tenants (`wesbytes`)**: `http://localhost:8085/panel-clientes`
- **Healthcheck**: `http://localhost:8085/health`

## Automatización CI/CD con GitHub Actions (`.github/workflows/deploy.yml`)

Cada `push` o `pull_request` a la rama `main` ejecuta:
1. **Job `validate`**:
   - Verifica sintaxis de Python (`compileall`).
   - Valida catálogo de los 3 planes, calculadora proporcional, normalización de nombre de dataset (minúscula, sin espacios ni tildes) y configuración de cobro PayPhone.
   - Construye la imagen Docker para asegurar que el `Dockerfile` no tenga errores.
2. **Job `deploy`** *(al hacer push a `main`)*:
   - Se conecta por SSH al VPS de Contabo y despliega en `/opt/wesbyte_portal` (configurable con `CONTABO_PORTAL_DIR`) en el puerto `8085` sin afectar el contenedor `broker-pmp-app` del puerto `8080`.

### Secrets requeridos en el repositorio de GitHub
- `CONTABO_HOST`: IP del servidor VPS
- `CONTABO_USER`: Usuario SSH (`root`)
- `CONTABO_SSH_KEY`: Clave privada SSH
- `CONTABO_PORT`: Puerto SSH (por defecto `22`)
- `CONTABO_PORTAL_DIR`: Ruta del proyecto en el VPS (por defecto `/opt/wesbyte_portal`)
