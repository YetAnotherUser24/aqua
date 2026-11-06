#!/usr/bin/env bash
# =============================================================================
# apply_audit_fixes_to_pi.sh
# =============================================================================
# Script de sincronización y saneamiento para el gateway de AquaControl (aqua-pi).
# Aplica las soluciones de la auditoría de forma completamente idempotente.
#
# Uso:
#   bash scripts/apply_audit_fixes_to_pi.sh
# =============================================================================

set -e

PI_HOST="aqua-pi"
LOCAL_ROOT="/root/aqua"

echo "=== 1. Verificando conectividad con $PI_HOST ==="
if ! ssh -o ConnectTimeout=8 -o BatchMode=yes "$PI_HOST" uptime >/dev/null 2>&1; then
    echo "ERROR: No se puede conectar a $PI_HOST."
    echo "Asegúrate de que la Raspberry Pi esté encendida y conectada a la red."
    exit 1
fi
echo "Conexión SSH exitosa."

echo "=== 2. Sincronizando código actualizado del Gateway al Pi ==="
rsync -avz --exclude "__pycache__" "$LOCAL_ROOT/aqua-station/gateway/aqua_gateway/" "$PI_HOST:/opt/aqua-station/gateway/aqua_gateway/"
rsync -avz "$LOCAL_ROOT/aqua-station/tools/r2_sync.py" "$PI_HOST:/opt/aqua-station/tools/r2_sync.py"
ssh "$PI_HOST" "sudo cp /opt/aqua-station/tools/r2_sync.py /usr/local/bin/aqua-r2-sync && sudo chmod +x /usr/local/bin/aqua-r2-sync"

echo "=== 3. Saneando SQLite manifest.db y eliminando archivo corrupto de Diciembre ==="
ssh "$PI_HOST" bash << 'EOF'
set -e
# Marcar como sincronizados/ignorados en manifest.db los archivos anómalos
sudo sqlite3 /var/lib/aqua-gateway/manifest.db \
  "UPDATE files SET flushed_at = strftime('%s','now'), verified = 1 WHERE path LIKE '%1969%' OR path LIKE '%2026-12-17%';"

# Eliminar archivo erróneo en disco
sudo rm -f /data/telemetry/aerator/2026-12-17.csv
EOF

echo "=== 4. Actualizando PostgreSQL (Cierre de ensayo abandonado y backfill rollup_1h) ==="
ssh "$PI_HOST" sudo -u postgres psql -d aqua << 'EOF'
-- A. Marcar EXP-2026-10-27-02 como aborted
UPDATE experiments
   SET status = 'aborted',
       ended_at = COALESCE(ended_at, now()),
       notes = COALESCE(notes, '') || ' [Auto-aborted: abandoned run]'
 WHERE id = 'EXP-2026-10-27-02' AND status = 'active';

-- B. Consolidar retroactivamente todas las filas de rollup_1min hacia rollup_1h
INSERT INTO rollup_1h (
    bucket, role, experiment_id, minutes_with_data,
    od_do_mg_l_avg, od_do_mg_l_min, od_temp_c_avg,
    drv_rpm_avg, drv_power_w_avg, drv_running_pct, pump_dosed_ml_sum
)
SELECT
    date_trunc('hour', bucket) AS hour_bucket,
    role,
    MAX(experiment_id) AS experiment_id,
    COUNT(*)::integer AS minutes_with_data,
    ROUND(AVG(od_do_mg_l_avg)::numeric, 4)::double precision,
    MIN(od_do_mg_l_min),
    ROUND(AVG(od_temp_c_avg)::numeric, 4)::double precision,
    ROUND(AVG(drv_rpm_avg)::numeric, 4)::double precision,
    ROUND(AVG(drv_power_w_avg)::numeric, 4)::double precision,
    ROUND(AVG(drv_running_pct)::numeric, 4)::double precision,
    ROUND(SUM(pump_dosed_ml_sum)::numeric, 4)::double precision
FROM rollup_1min
GROUP BY hour_bucket, role
ON CONFLICT (role, bucket) DO UPDATE SET
    experiment_id = EXCLUDED.experiment_id,
    minutes_with_data = EXCLUDED.minutes_with_data,
    od_do_mg_l_avg = EXCLUDED.od_do_mg_l_avg,
    od_do_mg_l_min = EXCLUDED.od_do_mg_l_min,
    od_temp_c_avg = EXCLUDED.od_temp_c_avg,
    drv_rpm_avg = EXCLUDED.drv_rpm_avg,
    drv_power_w_avg = EXCLUDED.drv_power_w_avg,
    drv_running_pct = EXCLUDED.drv_running_pct,
    pump_dosed_ml_sum = EXCLUDED.pump_dosed_ml_sum;
EOF

echo "=== 5. Configurando Chrony (NTP Local 192.168.4.1 y Sincronización DS3231 RTC) ==="
ssh "$PI_HOST" bash << 'EOF'
set -e
sudo mkdir -p /etc/chrony/conf.d
sudo tee /etc/chrony/conf.d/10-rtc.conf > /dev/null << 'CHRONY_CONF'
# Synchronize system clock to hardware RTC every 11 minutes
rtcsync
# Authoritative local fallback when offline
local stratum 8
# Allow bench network nodes to sync time locally
allow 192.168.4.0/24
CHRONY_CONF

# Deshabilitar systemd-timesyncd para evitar colisión en puerto 123
sudo systemctl disable --now systemd-timesyncd 2>/dev/null || true
sudo systemctl restart chrony 2>/dev/null || true
echo "Chrony configurado y reiniciado."
EOF

echo "=== 6. Reiniciando servicio aqua-gateway ==="
ssh "$PI_HOST" "sudo systemctl restart aqua-gateway.service"

echo "=== 7. Verificando estado y salud del Gateway ==="
sleep 3
ssh "$PI_HOST" "curl -s http://127.0.0.1:8000/healthz"
echo ""
echo "=== Despliegue y saneamiento completado con éxito ==="

