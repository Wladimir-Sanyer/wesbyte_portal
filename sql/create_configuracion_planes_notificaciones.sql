-- ============================================================================
-- TABLA DE CONFIGURACIÓN DE NOTIFICACIONES POR PLANES COMERCIALES
-- PROYECTO: wesbyte
-- DATASET: administracion
-- TABLA: configuracion_planes_notificaciones
-- ============================================================================

CREATE TABLE IF NOT EXISTS `wesbyte.administracion.configuracion_planes_notificaciones` (
    client_id INT64,
    id_plan STRING NOT NULL,
    nombre_plan STRING NOT NULL,
    correo_cumpleanios_activo BOOL NOT NULL,
    correo_renovaciones_activo BOOL NOT NULL,
    correo_cobros_activo BOOL NOT NULL,
    whatsapp_cumpleanios_activo BOOL NOT NULL,
    whatsapp_renovaciones_activo BOOL NOT NULL,
    whatsapp_cobros_activo BOOL NOT NULL,
    fecha_creacion TIMESTAMP DEFAULT CURRENT_TIMESTAMP() NOT NULL,
    fecha_actualizacion TIMESTAMP DEFAULT CURRENT_TIMESTAMP() NOT NULL
);

-- ============================================================================
-- SEMILLAS INICIALES (Planes Oficiales Wesbyte y Categorías Estándar)
-- ============================================================================
MERGE `wesbyte.administracion.configuracion_planes_notificaciones` T
USING (
    SELECT '1' AS id_plan, 'Plan Prime (Bronze)' AS nombre_plan, TRUE AS correo_cumpleanios_activo, TRUE AS correo_renovaciones_activo, TRUE AS correo_cobros_activo, FALSE AS whatsapp_cumpleanios_activo, TRUE AS whatsapp_renovaciones_activo, TRUE AS whatsapp_cobros_activo UNION ALL
    SELECT 'PRIME', 'Plan Prime', TRUE, TRUE, TRUE, FALSE, TRUE, TRUE UNION ALL
    SELECT 'PRIME_BRONZE', 'Plan Prime (Bronze)', TRUE, TRUE, TRUE, FALSE, TRUE, TRUE UNION ALL
    SELECT '2', 'Plan Medium (Silver)', TRUE, TRUE, TRUE, TRUE, TRUE, TRUE UNION ALL
    SELECT 'MEDIUM', 'Plan Medium', TRUE, TRUE, TRUE, TRUE, TRUE, TRUE UNION ALL
    SELECT 'MEDIUM_SILVER', 'Plan Medium (Silver)', TRUE, TRUE, TRUE, TRUE, TRUE, TRUE UNION ALL
    SELECT '3', 'Plan Super Gold (Gold)', TRUE, TRUE, TRUE, TRUE, TRUE, TRUE UNION ALL
    SELECT 'SUPER_GOLD', 'Plan Super Gold', TRUE, TRUE, TRUE, TRUE, TRUE, TRUE UNION ALL
    SELECT 'FREE', 'Plan Gratuito / Trial', FALSE, FALSE, FALSE, FALSE, FALSE, FALSE UNION ALL
    SELECT 'PRO', 'Plan Profesional', TRUE, TRUE, TRUE, TRUE, TRUE, TRUE UNION ALL
    SELECT 'ENTERPRISE', 'Plan Enterprise Corporativo', TRUE, TRUE, TRUE, TRUE, TRUE, TRUE
) S
ON T.id_plan = S.id_plan
WHEN MATCHED THEN
    UPDATE SET
        nombre_plan = S.nombre_plan,
        correo_cumpleanios_activo = S.correo_cumpleanios_activo,
        correo_renovaciones_activo = S.correo_renovaciones_activo,
        correo_cobros_activo = S.correo_cobros_activo,
        whatsapp_cumpleanios_activo = S.whatsapp_cumpleanios_activo,
        whatsapp_renovaciones_activo = S.whatsapp_renovaciones_activo,
        whatsapp_cobros_activo = S.whatsapp_cobros_activo,
        fecha_actualizacion = CURRENT_TIMESTAMP()
WHEN NOT MATCHED THEN
    INSERT (
        id_plan,
        nombre_plan,
        correo_cumpleanios_activo,
        correo_renovaciones_activo,
        correo_cobros_activo,
        whatsapp_cumpleanios_activo,
        whatsapp_renovaciones_activo,
        whatsapp_cobros_activo,
        fecha_creacion,
        fecha_actualizacion
    )
    VALUES (
        S.id_plan,
        S.nombre_plan,
        S.correo_cumpleanios_activo,
        S.correo_renovaciones_activo,
        S.correo_cobros_activo,
        S.whatsapp_cumpleanios_activo,
        S.whatsapp_renovaciones_activo,
        S.whatsapp_cobros_activo,
        CURRENT_TIMESTAMP(),
        CURRENT_TIMESTAMP()
    );
