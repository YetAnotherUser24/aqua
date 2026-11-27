#!/usr/bin/env bash
# =============================================================================
# deploy_gateway.sh — instala en la estación EXACTAMENTE un commit de git
# =============================================================================
# Reemplaza al despliegue por rsync del árbol de trabajo: copia `git archive` de un commit
# (por defecto HEAD), escribe /opt/aqua-station/VERSION (lo leen los eventos y los manifest de
# exportación), reinicia el gateway y verifica byte a byte que lo instalado es ese commit.
#
# Uso:  AQUA_PI_PASS=... scripts/deploy_gateway.sh [commit]
#       (sin AQUA_PI_PASS usa la clave SSH)
# Se niega si hay cambios sin commit en el gateway o si hay un ensayo activo.
# =============================================================================
set -euo pipefail
REPO=$(git -C "$(dirname "$0")" rev-parse --show-toplevel)
REF=${1:-HEAD}
HOST=${AQUA_PI_HOST:-aqua@192.168.4.1}
SRC=aqua-station/gateway/aqua_gateway
DST=/opt/aqua-station/gateway/aqua_gateway

if [ -n "${AQUA_PI_PASS:-}" ]; then
  export SSHPASS=$AQUA_PI_PASS; SSH=(sshpass -e ssh -o ConnectTimeout=8 "$HOST"); RSH="sshpass -e ssh"
else
  SSH=(ssh -o ConnectTimeout=8 "$HOST"); RSH="ssh"
fi

COMMIT=$(git -C "$REPO" rev-parse "$REF^{commit}")
DESCRIBE=$(git -C "$REPO" describe --tags --always "$COMMIT")
if [ "$REF" = "HEAD" ] && ! git -C "$REPO" diff --quiet HEAD -- "$SRC"; then
  echo "ERROR: hay cambios sin commit en $SRC. Haz commit antes de desplegar." >&2; exit 1
fi

STATE=$("${SSH[@]}" 'curl -s localhost:8000/api/state' | python3 -c 'import sys,json; print(json.load(sys.stdin).get("state","?"))')
if [ "$STATE" != "IDLE" ]; then
  echo "ERROR: el gateway está en estado $STATE (ensayo en curso). No se despliega." >&2; exit 1
fi

TMP=$(mktemp -d); trap 'rm -rf "$TMP"' EXIT
git -C "$REPO" archive "$COMMIT" "$SRC" | tar -x -C "$TMP"
echo "== Desplegando $DESCRIBE ($COMMIT) en $HOST"
rsync -rlt --checksum --exclude __pycache__ -e "$RSH" "$TMP/$SRC/" "$HOST:$DST/"

"${SSH[@]}" "cat > /opt/aqua-station/VERSION" <<EOF
describe=$DESCRIBE
commit=$COMMIT
deployed_at=$(date -Is)
source=git archive (scripts/deploy_gateway.sh)
EOF

"${SSH[@]}" 'sudo -n systemctl restart aqua-gateway && sleep 4 && systemctl is-active aqua-gateway'

# verificación: lo instalado == el commit
"${SSH[@]}" "cd $DST && find . -name '*.py' ! -path '*__pycache__*' -exec sha256sum {} \; | sort -k2" > "$TMP/pi.sha"
if (cd "$TMP/$SRC" && sha256sum -c --quiet "$TMP/pi.sha"); then
  echo "OK: código instalado IDÉNTICO a $DESCRIBE"
else
  echo "ERROR: el código instalado difiere del commit" >&2; exit 1
fi
EXTRA=$(comm -13 <(cd "$TMP/$SRC" && find . -name '*.py' | sort) <(awk '{print $2}' "$TMP/pi.sha" | sort))
[ -z "$EXTRA" ] || echo "AVISO: archivos en la Pi que no están en el commit: $EXTRA"
"${SSH[@]}" 'curl -s localhost:8000/api/recipes' | python3 -c 'import sys,json; d=json.load(sys.stdin); r=d.get("recipes",d); print("recetas:", [x.get("recipe_id") for x in r] if isinstance(r,list) else list(r)[:10])'
