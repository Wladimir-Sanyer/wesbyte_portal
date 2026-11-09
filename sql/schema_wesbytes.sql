-- ============================================================================
-- WESBYTE PORTAL & MULTI-TENANT SAAS SCHEMA (MEJORADO)
-- PROYECTO: broker-pmp
-- DATASET: wesbytes
-- ============================================================================

-- ============================================================================
-- 1. CATÁLOGO COMERCIAL Y PLANES (ESTÁNDAR Y PERSONALIZADOS)
-- ============================================================================
CREATE TABLE IF NOT EXISTS `broker-pmp.wesbytes.planes` (
    id INT64 NOT NULL,
    codigo STRING NOT NULL,                       -- 'PRIME_BRONZE', 'MEDIUM_SILVER', 'SUPER_GOLD', 'CUSTOM'
    nombre STRING NOT NULL,                       -- 'Plan Prime (Bronze)', 'Plan Medium (Silver)', 'Plan Super (Gold)'
    subtitulo STRING,                             -- Descripción comercial corta
    rango_clientes_min INT64 DEFAULT 20,          -- Ej. 20, 50, 200
    rango_clientes_max INT64 DEFAULT 50,          -- Ej. 50, 200, 500
    rango_clientes_label STRING,                  -- '20 - 50 clientes mensuales'
    tipo_periodo STRING DEFAULT 'anual' NOT NULL, -- 'anual', 'mensual', 'ambos'
    max_usuarios INT64 DEFAULT 1 NOT NULL,        -- Límite de usuarios permitidos (1, 3, 5, N)
    limite_diario_emails INT64 DEFAULT 10 NOT NULL,
    limite_diario_whatsapp INT64 DEFAULT 10 NOT NULL,
    limite_diario_api_ecuador INT64 DEFAULT 15 NOT NULL,
    limite_mensual_emails INT64 NOT NULL,         -- 50, 100, 200
    limite_mensual_whatsapp INT64 NOT NULL,       -- 50, 100, 300
    limite_mensual_api_ecuador INT64 NOT NULL,    -- 50, 200, 400
    precio_mensual NUMERIC(10, 2) DEFAULT 0.00 NOT NULL,     -- 20.00, 30.00, 45.00
    precio_anual NUMERIC(10, 2) DEFAULT 0.00 NOT NULL,       -- 240.00, 360.00, 540.00
    descuento_anual_pct NUMERIC(5, 2) DEFAULT 10.00 NOT NULL,-- 10% descuento pago único anual
    precio_anual_con_descuento NUMERIC(10, 2) DEFAULT 0.00 NOT NULL, -- 216.00, 324.00, 486.00
    es_destacado BOOL DEFAULT FALSE NOT NULL,     -- TRUE para Plan Super (Gold)
    es_personalizado BOOL DEFAULT FALSE NOT NULL, -- TRUE si fue generado por la calculadora
    activo BOOL DEFAULT TRUE NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP()
);

-- ============================================================================
-- 2. TENANTS / CLIENTES CORPORATIVOS
-- ============================================================================
CREATE TABLE IF NOT EXISTS `broker-pmp.wesbytes.clientes` (
    id INT64 NOT NULL,
    plan_id INT64 NOT NULL,                       -- Plan activo contratado
    nombre STRING NOT NULL,                       -- Razón social / Nombre de la empresa
    nombres_contacto STRING,                      -- Nombres completos del representante / admin
    ruc_cedula STRING,                            -- RUC o Cédula de la empresa
    email STRING NOT NULL,                        -- Email de contacto principal
    celular STRING,                               -- Celular / WhatsApp de contacto
    ciudad STRING,                                -- Ciudad / País
    dataset_id STRING NOT NULL,                   -- Dataset asignado en BigQuery (ej. 'seguros_aguila')
    proyecto STRING DEFAULT 'broker-pmp',         -- Proyecto GCP donde reside el dataset
    estado STRING DEFAULT 'activo' NOT NULL,      -- 'pendiente_pago', 'activo', 'por_vencer', 'vencido', 'cancelado'
    max_usuarios INT64 DEFAULT 1 NOT NULL,
    limite_diario_emails INT64 DEFAULT 30 NOT NULL,
    limite_diario_whatsapp INT64 DEFAULT 100 NOT NULL,
    limite_diario_api_ecuador INT64 DEFAULT 200 NOT NULL,
    limite_mensual_emails INT64,
    limite_mensual_whatsapp INT64,
    limite_mensual_api_ecuador INT64,
    acuerdo_confidencialidad_aceptado BOOL DEFAULT TRUE NOT NULL,
    fecha_acuerdo_confidencialidad TIMESTAMP DEFAULT CURRENT_TIMESTAMP(),
    requiere_nda_personalizado BOOL DEFAULT FALSE,
    activo BOOL DEFAULT TRUE NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP()
);

-- ============================================================================
-- 3. SUSCRIPCIONES Y VIGENCIAS (ANUAL PAGO ÚNICO O MENSUAL RECURRENTE)
-- ============================================================================
CREATE TABLE IF NOT EXISTS `broker-pmp.wesbytes.suscripciones` (
    id INT64 NOT NULL,
    cliente_id INT64 NOT NULL,
    plan_id INT64 NOT NULL,
    modalidad_pago STRING DEFAULT 'anual_unico' NOT NULL, -- 'anual_unico' (10% desc) o 'mensual_recurrente'
    monto_subtotal NUMERIC(10, 2) NOT NULL,
    descuento_aplicado NUMERIC(10, 2) DEFAULT 0.00 NOT NULL,
    monto_total_cobrado NUMERIC(10, 2) NOT NULL,
    moneda STRING DEFAULT 'USD' NOT NULL,
    fecha_inicio DATE NOT NULL,
    fecha_fin DATE NOT NULL,
    fecha_proximo_cobro DATE,
    renovacion_automatica BOOL DEFAULT FALSE NOT NULL,
    desea_renovar BOOL DEFAULT TRUE NOT NULL,     -- FALSE si el cliente decide no renovar
    motivo_no_renovacion STRING,                  -- Motivo por el cual no desea renovar
    estado STRING DEFAULT 'activa' NOT NULL,      -- 'pendiente', 'activa', 'por_vencer', 'vencida', 'cancelada'
    payphone_transaction_id STRING,
    payphone_client_tx_id STRING,
    payphone_subscription_token STRING,
    notificacion_enviada_30d BOOL DEFAULT FALSE NOT NULL,
    notificacion_enviada_15d BOOL DEFAULT FALSE NOT NULL,
    notificacion_enviada_7d BOOL DEFAULT FALSE NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP(),
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP()
);

-- ============================================================================
-- 4. USUARIOS Y CREDENCIALES POR CLIENTE (PORTAL Y MULTI-TENANT)
-- ============================================================================
CREATE TABLE IF NOT EXISTS `broker-pmp.wesbytes.usuarios` (
    id INT64 NOT NULL,
    cliente_id INT64 NOT NULL,
    username STRING NOT NULL,                     -- Nombre de usuario para login en la app
    nombre STRING NOT NULL,                       -- Nombres completos
    email STRING NOT NULL,
    celular STRING,
    password_hash STRING NOT NULL,                -- Hash bcrypt
    rol STRING DEFAULT 'admin' NOT NULL,          -- 'admin', 'operador', 'consulta'
    dataset_id STRING,                            -- Dataset del tenant al que pertenece
    activo BOOL DEFAULT TRUE NOT NULL,
    ultimo_login TIMESTAMP,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP()
);

-- ============================================================================
-- 5. TRANSACCIONES Y AUDITORÍA DE PAGOS PAYPHONE
-- ============================================================================
CREATE TABLE IF NOT EXISTS `broker-pmp.wesbytes.pagos_payphone` (
    id INT64 NOT NULL,
    cliente_id INT64 NOT NULL,
    suscripcion_id INT64,
    plan_id INT64 NOT NULL,
    modalidad_pago STRING NOT NULL,               -- 'anual_unico' o 'mensual_recurrente'
    client_transaction_id STRING NOT NULL,        -- ID único generado por Wesbyte Portal
    payphone_transaction_id STRING,               -- ID devuelto por PayPhone
    authorization_code STRING,
    monto_centavos INT64 NOT NULL,                -- Monto en centavos requerido por PayPhone (ej. 21600 = $216.00)
    monto_usd NUMERIC(10, 2) NOT NULL,
    estado_pago STRING DEFAULT 'PENDIENTE' NOT NULL, -- 'PENDIENTE', 'APPROVED', 'CANCELED', 'REJECTED'
    metodo_pago STRING,                           -- 'CARD', 'PAYPHONE_APP', 'SUBSCRIPTION'
    respuesta_raw_json STRING,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP(),
    confirmed_at TIMESTAMP
);

-- ============================================================================
-- 6. CONSUMOS DIARIOS (CONTROL ATÓMICO EN TIEMPO REAL)
-- ============================================================================
CREATE TABLE IF NOT EXISTS `broker-pmp.wesbytes.consumos_diarios` (
    id INT64 NOT NULL,
    cliente_id INT64 NOT NULL,
    fecha DATE DEFAULT CURRENT_DATE() NOT NULL,
    total_emails INT64 DEFAULT 0 NOT NULL,
    total_whatsapp INT64 DEFAULT 0 NOT NULL,
    total_api_ecuador INT64 DEFAULT 0 NOT NULL,
    limite_alcanzado_email BOOL DEFAULT FALSE NOT NULL,
    limite_alcanzado_whatsapp BOOL DEFAULT FALSE NOT NULL,
    limite_alcanzado_api BOOL DEFAULT FALSE NOT NULL,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP()
);

-- ============================================================================
-- 7. CONSUMOS MENSUALES (HISTÓRICO Y CIERRE)
-- ============================================================================
CREATE TABLE IF NOT EXISTS `broker-pmp.wesbytes.consumos_mensuales` (
    id INT64 NOT NULL,
    cliente_id INT64 NOT NULL,
    periodo STRING NOT NULL,                      -- Formato 'YYYY-MM' (ej. '2026-11')
    total_emails INT64 DEFAULT 0 NOT NULL,
    total_whatsapp INT64 DEFAULT 0 NOT NULL,
    total_api_ecuador INT64 DEFAULT 0 NOT NULL,
    bloqueado_mensual_email BOOL DEFAULT FALSE NOT NULL,
    bloqueado_mensual_whatsapp BOOL DEFAULT FALSE NOT NULL,
    bloqueado_mensual_api BOOL DEFAULT FALSE NOT NULL,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP()
);

-- ============================================================================
-- 8. AUDITORÍA Y LOGS TRANSACCIONALES
-- ============================================================================
CREATE TABLE IF NOT EXISTS `broker-pmp.wesbytes.logs_emails` (
    id INT64 NOT NULL,
    cliente_id INT64 NOT NULL,
    usuario_id INT64,
    destinatario STRING NOT NULL,
    motivo STRING DEFAULT 'cumpleanos',
    estado STRING NOT NULL,                       -- 'enviado', 'fallido', 'bloqueado'
    error_detalle STRING,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP()
);

CREATE TABLE IF NOT EXISTS `broker-pmp.wesbytes.logs_whatsapp` (
    id INT64 NOT NULL,
    cliente_id INT64 NOT NULL,
    usuario_id INT64,
    telefono_destino STRING NOT NULL,
    mensaje_id STRING,
    estado STRING NOT NULL,                       -- 'enviado', 'entregado', 'fallido', 'bloqueado'
    error_detalle STRING,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP()
);

CREATE TABLE IF NOT EXISTS `broker-pmp.wesbytes.logs_api_ecuador` (
    id INT64 NOT NULL,
    cliente_id INT64 NOT NULL,
    usuario_id INT64,
    endpoint STRING NOT NULL,
    metodo STRING NOT NULL,
    status_code INT64 NOT NULL,
    tiempo_respuesta_ms INT64 NOT NULL,
    bloqueado_por_limite BOOL DEFAULT FALSE NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP()
);
