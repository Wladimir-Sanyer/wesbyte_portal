# Wesbyte Portal Comercial & Onboarding Multi-Tenant

Aplicación web desarrollada en **Python (FastAPI + Gunicorn/Uvicorn + Google Cloud BigQuery + PayPhone)** para la comercialización, personalización de planes, cobro y aprovisionamiento automático de entornos Multi-Tenant en el proyecto `broker-pmp`.

Corre por defecto en el **puerto `8085`** como contenedor Docker individual (`portal-wesbyte`).

## Ejecución con Docker (Sin docker-compose)

Para compilar y correr directamente con Docker:

```bash
# 1. Construir la imagen
docker build -t portal-wesbyte .

# 2. Levantar el contenedor
docker run -d \
  --name portal-wesbyte \
  --restart unless-stopped \
  -p 8085:8085 \
  --env-file .env \
  -v $(pwd)/.secrets:/app/.secrets:ro \
  portal-wesbyte
```

- **Portal Comercial**: `http://localhost:8085`
- **Healthcheck**: `http://localhost:8085/health`

## Automatización CI/CD con GitHub Actions (`.github/workflows/deploy.yml`)

Cada `push` a la rama `main` ejecuta:
1. **Validación**: sintaxis Python, lógica de planes, calculadora y build de Docker.
2. **Despliegue directo por SSH**: en `/opt/wesbyte_portal` ejecuta `docker build` y `docker run -d --name portal-wesbyte -p 8085:8085 ...`.

### Secrets requeridos en el repositorio de GitHub
- `CONTABO_HOST`: IP del servidor VPS
- `CONTABO_USER`: Usuario SSH (`root`)
- `CONTABO_SSH_KEY`: Clave privada SSH
- `CONTABO_PORT`: Puerto SSH (por defecto `22`)
- `CONTABO_PORTAL_DIR`: Ruta del proyecto en el VPS (por defecto `/opt/wesbyte_portal`)
