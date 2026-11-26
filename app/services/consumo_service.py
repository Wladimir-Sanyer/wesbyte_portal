import datetime
import logging
from typing import Optional

from google.cloud import bigquery

from app.config import ECUADOR_TZ, GCP_PROJECT_ID, WESBYTE_CENTRAL_DATASET
from app.database import append_rows, get_bq_client, get_next_id

logger = logging.getLogger("wesbyte_portal.services.consumo")

_ALLOWED_CONSUMO_FIELDS = {"total_emails", "total_whatsapp", "total_api_ecuador"}


def resolve_cliente_id(
    cliente_id: Optional[int] = None,
    dataset_id: Optional[str] = None,
    broker_email: Optional[str] = None,
) -> Optional[int]:
    """Resuelve el `cliente_id` en `wesbyte.administracion.clientes` a partir de id, dataset_id o email."""
    if cliente_id is not None:
        return int(cliente_id)

    client = get_bq_client()
    table = f"`{GCP_PROJECT_ID}.{WESBYTE_CENTRAL_DATASET}.clientes`"

    if dataset_id and dataset_id.strip():
        sql = f"SELECT id FROM {table} WHERE dataset_id = @ds LIMIT 1"
        cfg = bigquery.QueryJobConfig(
            query_parameters=[bigquery.ScalarQueryParameter("ds", "STRING", dataset_id.strip())]
        )
        rows = list(client.query(sql, job_config=cfg).result())
        if rows:
            return int(rows[0].id)

    if broker_email and "@" in broker_email:
        sql = f"SELECT id FROM {table} WHERE LOWER(email) = LOWER(@em) LIMIT 1"
        cfg = bigquery.QueryJobConfig(
            query_parameters=[bigquery.ScalarQueryParameter("em", "STRING", broker_email.strip())]
        )
        rows = list(client.query(sql, job_config=cfg).result())
        if rows:
            return int(rows[0].id)

    return None


def increment_consumo_mensual(cliente_id: int, campo: str, delta: int = 1) -> None:
    """Incrementa atómicamente el contador mensual (`total_emails`, `total_whatsapp` o `total_api_ecuador`)."""
    if campo not in _ALLOWED_CONSUMO_FIELDS:
        raise ValueError(f"Campo de consumo no permitido: {campo}")
    if delta <= 0:
        return

    periodo = datetime.datetime.now(ECUADOR_TZ).strftime("%Y-%m")
    table = f"`{GCP_PROJECT_ID}.{WESBYTE_CENTRAL_DATASET}.consumos_mensuales`"
    init_em = delta if campo == "total_emails" else 0
    init_wa = delta if campo == "total_whatsapp" else 0
    init_api = delta if campo == "total_api_ecuador" else 0

    sql = f"""
    MERGE {table} T
    USING (SELECT @cid AS cliente_id, @periodo AS periodo) S
    ON T.cliente_id = S.cliente_id AND T.periodo = S.periodo
    WHEN MATCHED THEN
      UPDATE SET
        {campo} = COALESCE(T.{campo}, 0) + @delta,
        updated_at = CURRENT_TIMESTAMP()
    WHEN NOT MATCHED THEN
      INSERT (
        id, cliente_id, periodo,
        total_emails, total_whatsapp, total_api_ecuador,
        bloqueado_mensual_email, bloqueado_mensual_whatsapp, bloqueado_mensual_api,
        updated_at
      )
      VALUES (
        COALESCE((SELECT MAX(id) FROM {table}), 0) + 1,
        @cid, @periodo,
        @init_em, @init_wa, @init_api,
        FALSE, FALSE, FALSE,
        CURRENT_TIMESTAMP()
      )
    """
    cfg = bigquery.QueryJobConfig(
        query_parameters=[
            bigquery.ScalarQueryParameter("cid", "INT64", int(cliente_id)),
            bigquery.ScalarQueryParameter("periodo", "STRING", periodo),
            bigquery.ScalarQueryParameter("delta", "INT64", int(delta)),
            bigquery.ScalarQueryParameter("init_em", "INT64", int(init_em)),
            bigquery.ScalarQueryParameter("init_wa", "INT64", int(init_wa)),
            bigquery.ScalarQueryParameter("init_api", "INT64", int(init_api)),
        ]
    )
    get_bq_client().query(sql, job_config=cfg).result()


def log_email_event(
    cliente_id: int,
    destinatario: str,
    motivo: str,
    estado: str,
    error_detalle: Optional[str] = None,
    usuario_id: Optional[int] = None,
    increment_quota: bool = True,
) -> None:
    """Registra el envío en `logs_emails` y, si fue exitoso, incrementa `consumos_mensuales.total_emails`."""
    try:
        next_id = get_next_id("logs_emails", WESBYTE_CENTRAL_DATASET)
        append_rows(
            WESBYTE_CENTRAL_DATASET,
            "logs_emails",
            [
                {
                    "id": next_id,
                    "cliente_id": int(cliente_id),
                    "destinatario": (destinatario or "")[:500],
                    "motivo": (motivo or "")[:255] if motivo else None,
                    "estado": estado,
                    "error_detalle": error_detalle[:1000] if error_detalle else None,
                    "created_at": datetime.datetime.now(datetime.timezone.utc),
                    "usuario_id": int(usuario_id) if usuario_id is not None else None,
                }
            ],
        )
    except Exception as exc:
        logger.warning("No se pudo guardar log de email en BigQuery para %s: %s", destinatario, exc)

    if increment_quota and str(estado).strip().lower() in ("enviado", "sent"):
        try:
            increment_consumo_mensual(int(cliente_id), "total_emails", 1)
        except Exception as exc:
            logger.warning("No se pudo incrementar consumos_mensuales.total_emails para cliente %s: %s", cliente_id, exc)
