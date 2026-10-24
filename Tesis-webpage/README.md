# AquaControl - Dashboard Web de Acuicultura & Telemetría IoT

AquaControl es una plataforma web profesional para la monitorización en tiempo real y control remoto de sistemas de aireación en acuicultura de alta precisión. Conecta microcontroladores **ESP32** (medición de Oxígeno Disuelto y propulsión mediante Thruster **Blue Robotics T200**) con **Supabase PostgreSQL** y se despliega de forma serverless en **Vercel**.

---

## 1. Justificación de la Arquitectura

Para este sistema se seleccionó una arquitectura basada en **Next.js 14 (App Router) + TypeScript + Tailwind CSS + Supabase PostgreSQL + Vercel**:

1. **Next.js 14 Full-Stack Serverless en Vercel:**
   - **Frontend & Backend unificados:** Permite alojar tanto la interfaz gráfica reactiva como los endpoints REST seguros (`/api/telemetry/*`, `/api/commands/*`) en un único repositorio sin gestionar servidores Linux dedicados.
   - **Compatibilidad nativa con Vercel:** Tiempos de arranque en frío cercanos a cero, escalabilidad instantánea y latencia mínima global gracias a Vercel Edge Network.
2. **Supabase PostgreSQL & Row Level Security (RLS):**
   - Base de datos relacional robusta con soporte para tipos numéricos de alta precisión (`NUMERIC(6,3)`), índices B-Tree de alta velocidad sobre series temporales y seguridad por políticas RLS.
   - Autenticación administrativa de endpoints mediante *Service Role* y hashing SHA-256 de claves de dispositivo.
3. **Escalabilidad Futura (Render / Workers dedicados):**
   - La arquitectura actual opera 100% serverless en Vercel y Supabase, eliminando costos y complejidad de servidores en la fase inicial.
   - **Fase Futura en Render:** Cuando se requiera procesamiento de series de tiempo de alto volumen, reportes PDF/Excel periódicos en background, procesamiento por lotes con Python/Pandas o alertas vía SMS/WhatsApp mediante websockets persistentes, se puede desplegar un worker ligero en Render conectado a la misma base de datos de Supabase.

---

## 2. Configuración de Base de Datos en Supabase

1. Crea un proyecto en [Supabase](https://supabase.com/).
2. Ve al **SQL Editor** en el panel lateral de Supabase.
3. Abre el archivo [`supabase/schema.sql`](file:///supabase/schema.sql) de este repositorio, copia todo su contenido y ejecútalo en el editor SQL.
4. Esto creará:
   - Tablas: `devices`, `sensor_readings`, `motor_telemetry`, `motor_events`, `alerts`, `control_commands`, `system_settings`.
   - Índices para consultas instantáneas en series de tiempo.
   - Dispositivos iniciales de prueba y configuraciones de calibración.

---

## 3. Configuración de Variables de Entorno

Copia el archivo `.env.example` como `.env.local`:

```bash
cp .env.example .env.local
```

Configura los valores con los datos de tu proyecto de Supabase (**Settings -> API**):

```env
NEXT_PUBLIC_SUPABASE_URL=https://tu-proyecto.supabase.co
NEXT_PUBLIC_SUPABASE_ANON_KEY=eyJhbGciOi...
SUPABASE_SERVICE_ROLE_KEY=eyJhbGciOi...

# Tokens secretos y aliases de microcontroladores ESP32 (leídos de process.env o hash SHA-256 en BD)
ESP32_OD_SENSOR=ESP32_OD_SENSOR
ESP32_ODRIVE=ESP32_ODRIVE
ESP32_T_200=ESP32_T_200

# Tokens de retrocompatibilidad / desarrollo local
ESP32_SENSOR_DEVICE_KEY=ESP32_SENSOR_KEY_2026
ESP32_MOTOR_DEVICE_KEY=ESP32_MOTOR_KEY_2026
ESP32_ESC_DEVICE_KEY=ESP32_ESC_KEY_2026
```

---

## 4. Despliegue en Vercel

1. Sube este repositorio a tu cuenta de **GitHub**.
2. Ingresa a [Vercel](https://vercel.com/) y selecciona **Add New Project**.
3. Importa el repositorio de GitHub.
4. En la sección **Environment Variables**, añade las variables:
   - `NEXT_PUBLIC_SUPABASE_URL`
   - `NEXT_PUBLIC_SUPABASE_ANON_KEY`
   - `SUPABASE_SERVICE_ROLE_KEY`
   - `ESP32_OD_SENSOR`
   - `ESP32_ODRIVE`
   - `ESP32_T_200`
5. Haz clic en **Deploy**. ¡Tu dashboard quedará pública bajo HTTPS en segundos!

---

## 5. Guía de Conexión para los Microcontroladores ESP32

Todos los microcontroladores se comunican mediante peticiones HTTPS seguras incluyendo la cabecera `X-Device-Key`.

### A. ESP32 del Sensor Óptico de Oxígeno Disuelto
- **Endpoint:** `POST https://tu-dominio.vercel.app/api/telemetry/sensor`  
- **Header:** `X-Device-Key: ESP32_OD_SENSOR` (o `ESP32_SENSOR_KEY_2026`)  
- **ID en Base de Datos:** `a0000000-0000-0000-0000-000000000001`
- **Payload JSON:**
```json
{
  "datetime": "2026-09-06T15:00:00Z",
  "seconds_since_2000": 842048000,
  "water_temp_centi": 2305,
  "do_milli_mg_l": 7874,
  "do_sat_deci_pct": 985,
  "param3_centi": 0,
  "param4_centi": 0,
  "battery_mv": 4246,
  "rtc_temp_centi": 2410,
  "status": 0,
  "sent": true
}
```

---

### B. ESP32 del Actuador Principal (ODrive S1 - Motor M8325s)
- **Endpoint de Telemetría:** `POST https://tu-dominio.vercel.app/api/telemetry/motor`  
- **Endpoint de Comandos:** `GET https://tu-dominio.vercel.app/api/commands/pending`  
- **Header:** `X-Device-Key: ESP32_ODRIVE` (o `ESP32_MOTOR_KEY_2026`)  
- **ID en Base de Datos:** `b0000000-0000-0000-0000-000000000002`

---

### C. ESP32 del Actuador Auxiliar (ESC T-200 - PWM 50Hz)
- **Endpoint de Telemetría:** `POST https://tu-dominio.vercel.app/api/telemetry/motor`  
- **Endpoint de Comandos:** `GET https://tu-dominio.vercel.app/api/commands/pending`  
- **Header:** `X-Device-Key: ESP32_T_200` (o `ESP32_ESC_KEY_2026`)  
- **ID en Base de Datos:** `c0000000-0000-0000-0000-000000000003`

---

## 6. Pruebas Automatizadas y Auto-Verificación

El proyecto incluye scripts independientes de aserción (`assert`) para verificar la integridad del sistema sin necesidad de frameworks pesados:

```bash
# 1. Verificar autenticación híbrida y hashing SHA-256 (6 pruebas)
node scripts/verify-device-auth.js

# 2. Verificar comunicación y separación de comandos ODrive S1 vs ESC T-200 (5 pruebas)
node scripts/verify-motor-comms.js

# 3. Verificar heartbeat de 60s, supresión de telemetría y zona horaria GMT-5 (5 pruebas)
node scripts/verify-dashboard-fixes.js

# 4. Compilación completa de Next.js
npm run build
```

---

## 7. Zona Horaria y Sincronización en Tiempo Real

- **Zona Horaria de Perú (GMT-5):** Todas las marcas de tiempo en gráficos, tablas (`EventsTable`, `AlertsPanel`) y tarjetas de KPI se formatean en hora local de Lima mediante [`src/lib/dateUtils.ts`](file:///src/lib/dateUtils.ts).
- **Heartbeat Dinámico (60 segundos):** Si un dispositivo no transmite telemetría durante más de 60 segundos, su estado cambia automáticamente a `offline`.
- **Supresión de Estados Fantasma:** Si un actuador está desconectado físicamente, la API y la interfaz web fuerzan `is_on = false`, `0%` de velocidad y `0 W` de potencia para evitar reportar erróneamente un estado activo.
- **Prevención de Caché:** Las consultas en `/api/dashboard/summary` implementan directivas HTTP `no-store` y `revalidate = 0` para garantizar que cada refresco cada 15 segundos refleje el estado real e instantáneo del estanque.
