FROM python:3.12-slim

WORKDIR /app

# Dependencias del sistema para healthcheck
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Copiar e instalar dependencias Python
COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

# Crear usuario sin privilegios
RUN groupadd -r appuser && \
    useradd -r -g appuser appuser

# Copiar código con permisos adecuados
COPY --chown=appuser:appuser . .

USER appuser

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=8085 \
    WORKERS=2 \
    TIMEOUT=120

EXPOSE 8085

CMD ["sh", "-c", "gunicorn app.main:app -k uvicorn.workers.UvicornWorker -w ${WORKERS:-2} -b 0.0.0.0:${PORT:-8085} --timeout ${TIMEOUT:-120} --access-logfile - --error-logfile -"]
