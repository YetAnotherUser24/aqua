# AquaStation Deployment & Network Architecture Rules

When deploying, maintaining, or automating tasks for the AquaStation SCADA system, agents MUST adhere to these rules:

1. **Host & SSH Credentials:**
   - Host: `192.168.4.1` (or `aqua-station.local`).
   - Default SSH User: **`aqua`** (`aqua@192.168.4.1`). NEVER assume `pi`.
   - Node credentials for MQTT Broker (port 1883) require username/password defined in `roles.yaml` (e.g. `aerator` / `cNTSrH2JC7gI3pdGdUEwIZ8r`).

2. **No Dynamic Web Server on Raspberry Pi:**
   - There is NO `aqua-web.service` or Node/Next.js runtime on the Raspberry Pi (Pi 3B+ has 905 MB RAM).
   - Frontend is built statically on workstation/laptop (`npm run build` producing `web/out/`) and served directly by **Caddy** from `/srv/aqua-web/`.
   - Backend API runs under `aqua-gateway.service` from `/opt/aqua-station/gateway/` using Python venv `/opt/aqua-station/gateway/.venv/`.

3. **Critical Rsync Rule (Never use --delete-excluded with .venv):**
   - NEVER execute `rsync --delete-excluded --exclude '.venv'`. In rsync, `--delete-excluded` explicitly deletes destination files matching the exclusion, wiping out `/opt/aqua-station/gateway/.venv` and breaking the gateway service.
   - Always deploy using:
     ```bash
     rsync -avz --exclude '.venv' --exclude '__pycache__' "$SRC/gateway/" "aqua@192.168.4.1:/opt/aqua-station/gateway/"
     rsync -avz --delete "$SRC/web/out/" "aqua@192.168.4.1:/srv/aqua-web/"
     ssh aqua@192.168.4.1 "sudo systemctl restart aqua-gateway && sudo systemctl reload caddy"
     ```
   - Or run the official on-Pi script: `sudo /home/aqua/aqua-station/deploy/deploy.sh`.

4. **Systemd Security Context:**
   - `aqua-gateway.service` enforces `ProtectHome=true`. No binary, virtualenv, or dependency located in `/home/` can be accessed by the gateway process. The gateway and its venv must remain within `/opt/aqua-station/gateway/`.

5. **Static IP Assignment Map:**
   - Gateway / Hotspot AP (`wlan0`): `192.168.4.1` (SSID: `AquaStation`, WPA2: `AquaPass`).
   - Aerator Node (ODrive S1 / ESP32-S3): `192.168.4.10`
   - Mixer Node (T-200 / ESP32-S3): `192.168.4.11`
   - DO Sensor Node (OPTOD / LilyGO T3 LoRa32): `192.168.4.12` (DS3231 RTC authority).
   - Dosing Pump Node (L298N / ESP32): `192.168.4.13`

