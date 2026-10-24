#!/usr/bin/env bash
#
# Build the host virtual node.
#
# The device models live in the four firmware repositories and are compiled here **unmodified**,
# by include path only — the same discipline `test/host/` uses to link `control_engine.cpp` from
# production. That is the point: if `motor_sim.cpp` changes, this binary changes with it, so the
# virtual node cannot drift away from the firmware it stands in for.
#
# No PlatformIO, no Arduino, no external library. The sims are Arduino-free by design
# (`VIRTUAL_NODES.md` principle 4) and `aqua_protocol` is too.
#
# Usage:
#   ./build.sh              # -> ./virtual_node
#   ./build.sh --run 30     # build, then run for 30 simulated seconds

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$HERE/../.." && pwd)"
WORKSPACE="$(cd "$REPO_ROOT/.." && pwd)"

OUT="${OUT:-$HERE/virtual_node}"

# Firmware repositories. Override with AQUA_WORKSPACE if they live elsewhere.
WORKSPACE="${AQUA_WORKSPACE:-$WORKSPACE}"

# label:repository:model — the model basename differs per node (`motor_sim`, not `aerator_sim`),
# so the mapping has to be explicit rather than derived from the role name.
NODES=(
  "aerator:nodo-aerador:motor_sim"
  "sensor:nodo-sensor-od:sensor_sim"
  "mixer:nodo-mezclador:mixer_sim"
  "pump:nodo-bomba:pump_sim"
)

SOURCES=(
  "$HERE/virtual_node.cpp"
  "$REPO_ROOT/firmware/aqua_protocol/aqua_protocol.cpp"
)
INCLUDES=("-I$REPO_ROOT/firmware/aqua_protocol")
MODELS=()

missing=0
for entry in "${NODES[@]}"; do
  IFS=: read -r label repo model <<<"$entry"
  dir="$WORKSPACE/$repo"
  if [[ ! -f "$dir/src/$model.cpp" ]]; then
    printf 'missing device model: %s/src/%s.cpp (for %s)\n' "$dir" "$model" "$label" >&2
    missing=1
    continue
  fi
  SOURCES+=("$dir/src/$model.cpp")
  INCLUDES+=("-I$dir/src")
  MODELS+=("$label=$repo/src/$model.cpp")
done

if (( missing )); then
  printf '\nSet AQUA_WORKSPACE to the directory containing the nodo-* repositories.\n' >&2
  exit 1
fi

CXX="${CXX:-g++}"

# -Wall -Wextra is not optional here. This code is meant to run on nodes nobody can attach a
# debugger to, and the same source compiles into the ESP32 build.
echo "building host virtual node"
"$CXX" -std=c++17 -O2 -Wall -Wextra -Wpedantic \
  "${INCLUDES[@]}" \
  "${SOURCES[@]}" \
  -o "$OUT"

echo "  -> $OUT"
echo "  device models: ${MODELS[*]}"

if [[ "${1:-}" == "--run" ]]; then
  shift
  echo
  exec "$OUT" "$@"
fi
