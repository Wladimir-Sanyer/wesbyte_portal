import re
import uuid
import unicodedata
import datetime
import logging
from typing import Optional, Dict, Any, List
import pandas as pd
import bcrypt
from google.cloud import bigquery
from google.oauth2 import service_account
from google.api_core.exceptions import NotFound, Conflict

from app.config import (
    BIGQUERY_PROJECT_ID,
    WESBYTES_DATASET_ID,
    TEMPLATE_DATASET_ID,
    CREDENTIALS_PATH,
    PLANES_OFICIALES,
)

logger = logging.getLogger("wesbyte_portal.database")

_bq_client: Optional[bigquery.Client] = None


def get_bq_client() -> bigquery.Client:
    """Retorna una instancia singleton del cliente de BigQuery autenticada con el Service Account."""
    global _bq_client
    if _bq_client is None:
        credentials = service_account.Credentials.from_service_account_file(CREDENTIALS_PATH)
        _bq_client = bigquery.Client(credentials=credentials, project=BIGQUERY_PROJECT_ID)
    return _bq_client


def get_query(query: str) -> pd.DataFrame:
    """Ejecuta una consulta SELECT en BigQuery y retorna un DataFrame."""
    client = get_bq_client()
    job = client.query(query)
    return job.to_dataframe()


def execute_query(query: str) -> Any:
    """Ejecuta una sentencia DDL/DML en BigQuery y espera el resultado."""
    client = get_bq_client()
    job = client.query(query)
    return job.result()


def append_rows(dataset_id: str, table_name: str, rows: List[Dict[str, Any]]) -> None:
    """Inserta filas en una tabla de BigQuery usando un LoadJob (permite UPDATE/DML inmediato sin bloqueo de streaming buffer)."""
    if not rows:
        return
    client = get_bq_client()
    table_ref = f"{BIGQUERY_PROJECT_ID}.{dataset_id}.{table_name}"
    df = pd.DataFrame(rows)
    job_config = bigquery.LoadJobConfig(
        write_disposition=bigquery.WriteDisposition.WRITE_APPEND,
    )
    job = client.load_table_from_dataframe(df, table_ref, job_config=job_config)
    job.result()


def get_next_id(table_name: str, dataset_id: str = WESBYTES_DATASET_ID) -> int:
    """Calcula el siguiente ID entero incremental para una tabla en BigQuery."""
    query = f"SELECT IFNULL(MAX(id), 0) + 1 AS next_id FROM `{BIGQUERY_PROJECT_ID}.{dataset_id}.{table_name}`"
    try:
        df = get_query(query)
        if not df.empty and pd.notnull(df["next_id"].iloc[0]):
            return int(df["next_id"].iloc[0])
    except Exception as exc:
        logger.warning(f"No se pudo obtener next_id para {dataset_id}.{table_name}: {exc}")
    return 1


def hash_password(password: str) -> str:
    """Genera un hash bcrypt compatible con Wesbyte App y Wesbyte Portal."""
    salt = bcrypt.gensalt()
    return bcrypt.hashpw(password.encode("utf-8"), salt).decode("utf-8")


def verify_password(plain_password: str, stored_password: str) -> bool:
    """Verifica contraseña contra hash bcrypt o texto plano."""
    if not plain_password or not stored_password:
        return False
    if plain_password == stored_password:
        return True
    if stored_password.startswith(("$2a$", "$2b$", "$2y$")):
        try:
            return bcrypt.checkpw(plain_password.encode("utf-8"), stored_password.encode("utf-8"))
        except Exception:
            return False
    return False


def normalize_dataset_name(nombre_empresa: str) -> str:
    """
    Convierte el nombre de la empresa a un identificador de dataset BigQuery válido:
    - Todo en minúsculas
    - Sin tildes ni diacríticos (á->a, é->e, ñ->n, etc.)
    - Sin espacios ni caracteres especiales (reemplazados por guion bajo o unidos)
    - Ejemplo: 'Seguros Águila S.A.' -> 'seguros_aguila_sa' o sin espacios 'segurosaguilasa'
    Nota: BigQuery admite letras minúsculas, números y guiones bajos.
    Si el usuario pidió 'en minuscula, sin espacios ni tildes', eliminamos tildes, puntuación y espacios.
    """
    if not nombre_empresa:
        return "empresa_wesbyte"
    # Eliminar tildes y diacríticos (NFKD)
    sin_tildes = (
        unicodedata.normalize("NFKD", nombre_empresa.strip())
        .encode("ascii", "ignore")
        .decode("ascii")
    )
    limpio = sin_tildes.lower()
    # Eliminar espacios y caracteres no alfanuméricos (dejando solo letras minúsculas, números y guiones bajos si los escribió)
    limpio = re.sub(r"\s+", "", limpio)
    limpio = re.sub(r"[^a-z0-9_]", "", limpio)
    if not limpio:
        limpio = f"tenant_{uuid.uuid4().hex[:6]}"
    if limpio[0].isdigit():
        limpio = f"emp_{limpio}"
    return limpio[:60]


def ensure_columns_exist(dataset_id: str, table_name: str, required_columns: Dict[str, str]) -> None:
    """Agrega columnas faltantes a una tabla existente en BigQuery sin perder datos."""
    client = get_bq_client()
    table_ref = f"{BIGQUERY_PROJECT_ID}.{dataset_id}.{table_name}"
    try:
        table = client.get_table(table_ref)
    except NotFound:
        return
    existing_cols = {field.name.lower() for field in table.schema}
    for col_name, col_type in required_columns.items():
        if col_name.lower() not in existing_cols:
            alter_sql = f"ALTER TABLE `{table_ref}` ADD COLUMN IF NOT EXISTS {col_name} {col_type}"
            try:
                execute_query(alter_sql)
                logger.info(f"Columna agregada: {table_ref}.{col_name} ({col_type})")
            except Exception as exc:
                logger.warning(f"No se pudo agregar columna {col_name} en {table_ref}: {exc}")


def init_wesbytes_schema_and_seeds() -> None:
    """
    Inicializa y actualiza todas las tablas del dataset `broker-pmp.wesbytes`
    e inserta los 3 planes comerciales oficiales si aún no existen.
    """
    client = get_bq_client()
    dataset_ref = f"{BIGQUERY_PROJECT_ID}.{WESBYTES_DATASET_ID}"
    try:
        client.get_dataset(dataset_ref)
    except NotFound:
        ds = bigquery.Dataset(dataset_ref)
        ds.location = "US"
        client.create_dataset(ds, exists_ok=True)

    # 1. Tabla planes
    execute_query(f"""
    CREATE TABLE IF NOT EXISTS `{dataset_ref}.planes` (
        id INT64 NOT NULL,
        codigo STRING NOT NULL,
        nombre STRING NOT NULL,
        subtitulo STRING,
        rango_clientes_min INT64,
        rango_clientes_max INT64,
        rango_clientes_label STRING,
        tipo_periodo STRING NOT NULL,
        max_usuarios INT64 NOT NULL,
        limite_diario_emails INT64 NOT NULL,
        limite_diario_whatsapp INT64 NOT NULL,
        limite_diario_api_ecuador INT64 NOT NULL,
        limite_mensual_emails INT64,
        limite_mensual_whatsapp INT64,
        limite_mensual_api_ecuador INT64,
        precio_mensual NUMERIC(10, 2),
        precio_anual NUMERIC(10, 2) NOT NULL,
        descuento_anual_pct NUMERIC(5, 2),
        precio_anual_con_descuento NUMERIC(10, 2),
        es_destacado BOOL,
        es_personalizado BOOL,
        activo BOOL NOT NULL,
        created_at TIMESTAMP
    )
    """)

    # 2. Tabla clientes (asegurar tabla y evolucionar columnas si ya existía)
    execute_query(f"""
    CREATE TABLE IF NOT EXISTS `{dataset_ref}.clientes` (
        id INT64 NOT NULL,
        plan_id INT64,
        nombre STRING NOT NULL,
        nombres_contacto STRING,
        ruc_cedula STRING,
        email STRING NOT NULL,
        celular STRING,
        ciudad STRING,
        dataset_id STRING,
        proyecto STRING,
        estado STRING,
        max_usuarios INT64,
        limite_diario_emails INT64 NOT NULL,
        limite_diario_whatsapp INT64 NOT NULL,
        limite_diario_api_ecuador INT64 NOT NULL,
        limite_mensual_emails INT64,
        limite_mensual_whatsapp INT64,
        limite_mensual_api_ecuador INT64,
        acuerdo_confidencialidad_aceptado BOOL,
        fecha_acuerdo_confidencialidad TIMESTAMP,
        requiere_nda_personalizado BOOL,
        activo BOOL NOT NULL,
        created_at TIMESTAMP
    )
    """)
    ensure_columns_exist(
        WESBYTES_DATASET_ID,
        "clientes",
        {
            "plan_id": "INT64",
            "nombres_contacto": "STRING",
            "ruc_cedula": "STRING",
            "celular": "STRING",
            "ciudad": "STRING",
            "dataset_id": "STRING",
            "proyecto": "STRING",
            "estado": "STRING",
            "max_usuarios": "INT64",
            "acuerdo_confidencialidad_aceptado": "BOOL",
            "fecha_acuerdo_confidencialidad": "TIMESTAMP",
            "requiere_nda_personalizado": "BOOL",
        },
    )

    # 3. Tabla suscripciones
    execute_query(f"""
    CREATE TABLE IF NOT EXISTS `{dataset_ref}.suscripciones` (
        id INT64 NOT NULL,
        cliente_id INT64 NOT NULL,
        plan_id INT64 NOT NULL,
        modalidad_pago STRING,
        monto_subtotal NUMERIC(10, 2),
        descuento_aplicado NUMERIC(10, 2),
        monto_total_cobrado NUMERIC(10, 2),
        moneda STRING,
        fecha_inicio DATE NOT NULL,
        fecha_fin DATE NOT NULL,
        fecha_proximo_cobro DATE,
        renovacion_automatica BOOL NOT NULL,
        desea_renovar BOOL NOT NULL,
        motivo_no_renovacion STRING,
        estado STRING NOT NULL,
        payphone_transaction_id STRING,
        payphone_client_tx_id STRING,
        payphone_subscription_token STRING,
        notificacion_enviada_30d BOOL NOT NULL,
        notificacion_enviada_15d BOOL NOT NULL,
        notificacion_enviada_7d BOOL NOT NULL,
        created_at TIMESTAMP,
        updated_at TIMESTAMP
    )
    """)

    # 4. Tabla usuarios
    execute_query(f"""
    CREATE TABLE IF NOT EXISTS `{dataset_ref}.usuarios` (
        id INT64 NOT NULL,
        cliente_id INT64 NOT NULL,
        username STRING,
        nombre STRING NOT NULL,
        email STRING NOT NULL,
        celular STRING,
        password_hash STRING NOT NULL,
        rol STRING NOT NULL,
        dataset_id STRING,
        activo BOOL NOT NULL,
        ultimo_login TIMESTAMP,
        created_at TIMESTAMP
    )
    """)
    ensure_columns_exist(
        WESBYTES_DATASET_ID,
        "usuarios",
        {
            "username": "STRING",
            "celular": "STRING",
            "dataset_id": "STRING",
        },
    )

    # 5. Tabla pagos_payphone
    execute_query(f"""
    CREATE TABLE IF NOT EXISTS `{dataset_ref}.pagos_payphone` (
        id INT64 NOT NULL,
        cliente_id INT64 NOT NULL,
        suscripcion_id INT64,
        plan_id INT64 NOT NULL,
        modalidad_pago STRING NOT NULL,
        client_transaction_id STRING NOT NULL,
        payphone_transaction_id STRING,
        authorization_code STRING,
        monto_centavos INT64 NOT NULL,
        monto_usd NUMERIC(10, 2) NOT NULL,
        estado_pago STRING NOT NULL,
        metodo_pago STRING,
        respuesta_raw_json STRING,
        created_at TIMESTAMP,
        confirmed_at TIMESTAMP
    )
    """)

    # 6. Tablas de consumos y logs (asegurar usuario_id en logs)
    ensure_columns_exist(WESBYTES_DATASET_ID, "logs_emails", {"usuario_id": "INT64"})
    ensure_columns_exist(WESBYTES_DATASET_ID, "logs_whatsapp", {"usuario_id": "INT64"})
    ensure_columns_exist(WESBYTES_DATASET_ID, "logs_api_ecuador", {"usuario_id": "INT64"})

    # 7. Sembrar los 3 planes comerciales si la tabla planes está vacía
    df_planes = get_query(f"SELECT id, codigo FROM `{dataset_ref}.planes` ORDER BY id ASC")
    if df_planes.empty:
        now = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        values_sql = []
        for p in PLANES_OFICIALES:
            values_sql.append(f"""(
                {p['id']},
                '{p['codigo']}',
                '{p['nombre']} ({p['tier']})',
                '{p['subtitulo']}',
                {p['rango_clientes_min']},
                {p['rango_clientes_max']},
                '{p['rango_clientes_label']}',
                'anual',
                {p['max_usuarios']},
                {p['limite_diario_emails']},
                {p['limite_diario_whatsapp']},
                {p['limite_diario_api_ecuador']},
                {p['limite_mensual_emails']},
                {p['limite_mensual_whatsapp']},
                {p['limite_mensual_api_ecuador']},
                NUMERIC '{p['precio_mensual']:.2f}',
                NUMERIC '{p['precio_anual']:.2f}',
                NUMERIC '{p['descuento_anual_pct']:.2f}',
                NUMERIC '{p['precio_anual_con_descuento']:.2f}',
                {str(p['es_destacado']).upper()},
                FALSE,
                TRUE,
                TIMESTAMP('{now}')
            )""")
        insert_sql = f"""
        INSERT INTO `{dataset_ref}.planes` (
            id, codigo, nombre, subtitulo, rango_clientes_min, rango_clientes_max,
            rango_clientes_label, tipo_periodo, max_usuarios,
            limite_diario_emails, limite_diario_whatsapp, limite_diario_api_ecuador,
            limite_mensual_emails, limite_mensual_whatsapp, limite_mensual_api_ecuador,
            precio_mensual, precio_anual, descuento_anual_pct, precio_anual_con_descuento,
            es_destacado, es_personalizado, activo, created_at
        ) VALUES {", ".join(values_sql)}
        """
        execute_query(insert_sql)
        logger.info("Planes comerciales sembrados exitosamente en broker-pmp.wesbytes.planes")


def clone_broker_pmp_dataset_empty(
    target_dataset_id: str,
    admin_username: str,
    admin_password_plain: str,
    admin_password_hashed: str,
) -> Dict[str, Any]:
    """
    1. Crea el dataset `broker-pmp.<target_dataset_id>` en la misma ubicación que `broker_pmp`.
    2. Copia todas las tablas físicas (TABLE) de `broker-pmp.broker_pmp` pero VACÍAS.
    3. Recrea las vistas (VIEW) de `broker-pmp.broker_pmp` apuntando al nuevo dataset.
    4. Inserta en `<target_dataset_id>.maestra_usuarios_app`:
       - El usuario `admin` con la misma contraseña registrada.
       - El usuario con el `username` elegido en el registro (si es distinto de `admin`) con la misma contraseña.
    """
    client = get_bq_client()
    source_ds_ref = f"{BIGQUERY_PROJECT_ID}.{TEMPLATE_DATASET_ID}"
    target_ds_ref = f"{BIGQUERY_PROJECT_ID}.{target_dataset_id}"

    # Obtener ubicación del dataset fuente (broker_pmp)
    source_ds = client.get_dataset(source_ds_ref)
    location = source_ds.location or "US"

    # Crear dataset destino si no existe
    target_ds = bigquery.Dataset(target_ds_ref)
    target_ds.location = location
    target_ds.description = f"Dataset Multi-Tenant Wesbyte aprovisionado automáticamente para {target_dataset_id}"
    client.create_dataset(target_ds, exists_ok=True)

    cloned_tables: List[str] = []
    cloned_views: List[str] = []

    source_items = list(client.list_tables(source_ds_ref))

    # Paso 1: Clonar todas las tablas físicas vacías (CREATE TABLE IF NOT EXISTS ... LIKE ...)
    for item in source_items:
        full_source_table = f"{source_ds_ref}.{item.table_id}"
        tbl_meta = client.get_table(full_source_table)
        if tbl_meta.table_type == "TABLE":
            ddl = f"CREATE TABLE IF NOT EXISTS `{target_ds_ref}.{item.table_id}` LIKE `{full_source_table}`"
            execute_query(ddl)
            cloned_tables.append(item.table_id)

    # Paso 2: Recrear todas las vistas apuntando al nuevo dataset
    for item in source_items:
        full_source_table = f"{source_ds_ref}.{item.table_id}"
        tbl_meta = client.get_table(full_source_table)
        if tbl_meta.table_type == "VIEW" and tbl_meta.view_query:
            rewritten_view_sql = tbl_meta.view_query.replace(
                f"{BIGQUERY_PROJECT_ID}.{TEMPLATE_DATASET_ID}.",
                f"{BIGQUERY_PROJECT_ID}.{target_dataset_id}.",
            ).replace(
                f"`{TEMPLATE_DATASET_ID}.",
                f"`{target_dataset_id}.",
            )
            view_ddl = f"CREATE OR REPLACE VIEW `{target_ds_ref}.{item.table_id}` AS {rewritten_view_sql}"
            try:
                execute_query(view_ddl)
                cloned_views.append(item.table_id)
            except Exception as exc:
                logger.warning(f"Advertencia creando vista {item.table_id} en {target_dataset_id}: {exc}")

    # Paso 3: Insertar en `maestra_usuarios_app` del nuevo dataset:
    # - Siempre el usuario 'admin' con la misma contraseña
    # - Y el usuario con el que se registró (si es distinto de 'admin')
    now_ts = datetime.datetime.now(datetime.timezone.utc)
    permisos_all = "registros_medicos,registros_vehiculares,registros_vida_e_inversion,registros_ramos_generales"

    # Verificar qué usuarios ya existen en maestra_usuarios_app del nuevo dataset
    existing_df = get_query(f"SELECT username FROM `{target_ds_ref}.maestra_usuarios_app`")
    existing_usernames = {str(u).strip().lower() for u in existing_df["username"].tolist()} if not existing_df.empty else set()

    users_to_insert = []
    if "admin" not in existing_usernames:
        users_to_insert.append({
            "id": str(uuid.uuid4()),
            "username": "admin",
            "password": admin_password_hashed,
            "access": "administrador",
            "is_hashed_password": True,
            "admin": True,
            "activo": True,
            "fecha_creacion": now_ts,
            "fecha_modificacion": now_ts,
            "usuario_creador": "wesbyte_portal",
            "usuario_modificador": "wesbyte_portal",
            "permisos_seguros": permisos_all,
        })

    clean_reg_user = (admin_username or "").strip()
    if clean_reg_user and clean_reg_user.lower() != "admin" and clean_reg_user.lower() not in existing_usernames:
        users_to_insert.append({
            "id": str(uuid.uuid4()),
            "username": clean_reg_user,
            "password": admin_password_hashed,
            "access": "administrador",
            "is_hashed_password": True,
            "admin": True,
            "activo": True,
            "fecha_creacion": now_ts,
            "fecha_modificacion": now_ts,
            "usuario_creador": "wesbyte_portal",
            "usuario_modificador": "wesbyte_portal",
            "permisos_seguros": permisos_all,
        })

    if users_to_insert:
        append_rows(target_dataset_id, "maestra_usuarios_app", users_to_insert)

    return {
        "dataset_id": target_dataset_id,
        "full_dataset": target_ds_ref,
        "location": location,
        "cloned_tables": cloned_tables,
        "cloned_views": cloned_views,
        "created_app_users": [u["username"] for u in users_to_insert],
    }
