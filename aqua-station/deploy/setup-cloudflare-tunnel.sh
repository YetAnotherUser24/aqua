#!/usr/bin/env bash
# =============================================================================
# setup-cloudflare-tunnel.sh — Configura Cloudflare Tunnel en la Raspberry Pi
# =============================================================================
# Target: Raspberry Pi 3B+ (aqua-station) detrás del firewall Fortinet de UTEC.
#
# Este script:
#   1. Instala el repositorio oficial y binario de cloudflared si no existe.
#   2. Instala el conector de servicio systemd con el token del túnel.
#   3. Fuerza la bandera '--protocol http2' en la unidad systemd para garantizar
#      que el túnel opere sobre TCP 443 estándar (evitando que Fortinet descarte
#      el tráfico UDP / QUIC en el puerto 7844).
#   4. Inicia y habilita cloudflared al arranque del sistema.
#
# Uso:
#   sudo ./setup-cloudflare-tunnel.sh [TOKEN_OPCIONAL]
# =============================================================================

set -euo pipefail

# Token predeterminado para el túnel 'aqua-station' (aqua-station.live):
DEFAULT_TOKEN="eyJhIjoiMjQ2NmE1NTY3MjRhZjdjYmQwMGQzYzMxOTdhMzVkZTIiLCJ0IjoiYWFmZjkwZGItYTEyNC00ZTVlLTk4YmItY2UzOGZjNzk1NDQwIiwicyI6Ik9USTFPVFJsTVRNdE9XVXpZeTAwTkdZMUxXRXdZVFF0TWpNMVlUaGlNbVppTWpBMCJ9"
TOKEN="${1:-$DEFAULT_TOKEN}"

if [[ $EUID -ne 0 ]]; then
    printf '\033[31mEste script debe ejecutarse como root (con sudo).\033[0m\n' >&2
    exit 1
fi

echo "== 1. Verificando / Instalando cloudflared =="
if ! command -v cloudflared &>/dev/null; then
    echo "Agregando repositorio oficial de Cloudflare..."
    mkdir -p --mode=0755 /usr/share/keyrings
    curl -fsSL https://pkg.cloudflare.com/cloudflare-main.gpg | tee /usr/share/keyrings/cloudflare-main.gpg >/dev/null
    echo 'deb [signed-by=/usr/share/keyrings/cloudflare-main.gpg] https://pkg.cloudflare.com/cloudflared any main' | tee /etc/apt/sources.list.d/cloudflared.list
    apt-get update -qq
    apt-get install -y -qq cloudflared
else
    echo "cloudflared ya está instalado en: $(command -v cloudflared)"
fi

echo "== 2. Configurando servicio systemd de cloudflared =="
# Si ya existía un servicio previo, detenerlo y desinstalarlo limpiamente
if systemctl is-active --quiet cloudflared 2>/dev/null; then
    echo "Deteniendo servicio cloudflared previo..."
    systemctl stop cloudflared || true
fi

# Instalar el servicio con el token
cloudflared service install "$TOKEN"

# CRÍTICO PARA FORTINET: Forzar protocolo HTTP/2 (TCP 443) en vez de QUIC (UDP 7844)
SERVICE_FILE="/etc/systemd/system/cloudflared.service"
if [[ -f "$SERVICE_FILE" ]]; then
    echo "Asegurando bandera '--protocol http2' para compatibilidad con Fortinet..."
    if ! grep -q -- "--protocol http2" "$SERVICE_FILE"; then
        sed -i 's|tunnel run|--protocol http2 tunnel run|' "$SERVICE_FILE"
    fi
fi

echo "== 3. Habilitando e iniciando cloudflared =="
systemctl daemon-reload
systemctl enable --now cloudflared

sleep 3

if systemctl is-active --quiet cloudflared; then
    printf '\n\033[32m[OK] Cloudflare Tunnel iniciado y activo con éxito.\033[0m\n'
    printf 'Tu dashboard está conectado a: \033[1;36mhttps://aqua-station.live\033[0m (o el hostname configurado)\n'
else
    printf '\n\033[31m[ADVERTENCIA] El servicio no parece activo. Revisa con: systemctl status cloudflared\033[0m\n'
fi
