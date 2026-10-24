#!/usr/bin/env bash
# =============================================================================
# aqua-fix-caddy-ip — re-materialise Caddy's LAN bind for the current address
# =============================================================================
# Runs ON the station. Idempotent and silent when nothing has changed, so it is
# safe as a boot job and as a periodic timer.
#
# Why this exists as a unit rather than a line in the installer:
#
#   Caddy's `bind` must be a concrete address — it is what keeps the LAN listener
#   off the tailnet interface, where `tailscale serve` needs :443. But the
#   address is a property of the *network*, not of the machine: DHCP hands out a
#   different one whenever the lease turns over, and this board moves between
#   networks. The installer writes the address once, so after the first lease
#   change Caddy tries to bind an address it no longer has, stops with
#   `bind: cannot assign requested address`, and every other unit still reports
#   healthy — the dashboard is down while nothing looks broken.
#
#   `deploy/check.sh --fix-ip` repairs this, but only when a human notices and
#   runs it from the laptop. This makes the repair automatic, which is what the
#   failure demands: it recurs on a timer nobody controls.
#
# The Caddyfile is regenerated from the repo TEMPLATE (`@LAN_IP@`), never edited
# in place. That is deliberate: the installer and this script then converge on
# identical bytes instead of drifting, and a site block added to the template
# later cannot be lost by a sed that only knows how to rewrite one line.
# =============================================================================

set -euo pipefail

CADDYFILE=/etc/caddy/Caddyfile
TEMPLATE=/opt/aqua-station/deploy/caddy/Caddyfile

log() { printf '%s\n' "$*"; }

# The LAN address: the first of `hostname -I` that is not loopback, not the
# tailnet (100.64/10) and not IPv6. Kept byte-identical to the expression in
# deploy/install.sh and deploy/check.sh: if the three ever disagree, one of them
# would "repair" the config into a state the next of them undoes.
lan_ip() {
	hostname -I 2>/dev/null | tr ' ' '\n' | grep -vE '^(127\.|100\.|::)' | head -1
}

current_bind() {
	grep -oE '^[[:space:]]*bind[[:space:]]+[0-9.]+' "$CADDYFILE" 2>/dev/null \
		| head -1 | awk '{print $2}'
}

new=$(lan_ip || true)
if [[ -z "$new" ]]; then
	# No LAN address at all (unplugged, no lease yet). Leave the config alone:
	# rewriting it to 127.0.0.1 would take the LAN listener away permanently and
	# the next boot, with an address, would not put it back.
	log "aqua-fix-caddy-ip: no LAN address yet; leaving $CADDYFILE unchanged"
	exit 0
fi

if [[ ! -f "$TEMPLATE" ]]; then
	log "aqua-fix-caddy-ip: template $TEMPLATE missing; cannot regenerate"
	exit 1
fi

old=$(current_bind || true)
if [[ "$old" == "$new" ]]; then
	# The common case. Silent and cheap, so the timer can run often.
	exit 0
fi

log "aqua-fix-caddy-ip: LAN address changed ${old:-<none>} -> $new; regenerating"

tmp=$(mktemp)
trap 'rm -f "$tmp"' EXIT
sed -e "s/@LAN_IP@/$new/" "$TEMPLATE" > "$tmp"

# Validate BEFORE installing. A config the template cannot produce correctly
# should not be able to take the dashboard down; if it does not validate, keep
# the working file and report, rather than swapping in something broken.
if ! caddy validate --adapter caddyfile --config "$tmp" >/dev/null 2>&1; then
	log "aqua-fix-caddy-ip: regenerated config does not validate; keeping the old one"
	exit 1
fi

install -m 0644 "$tmp" "$CADDYFILE"

# Reload, not restart: a reload keeps the listener up if the new config is bad.
# `reload` failing leaves Caddy DEAD, so the result is checked and a restart is
# the fallback — the one place a restart is the safer choice.
if systemctl reload caddy 2>/dev/null; then
	log "aqua-fix-caddy-ip: caddy reloaded on $new"
else
	log "aqua-fix-caddy-ip: reload failed; restarting caddy"
	systemctl restart caddy
fi

if systemctl is-active --quiet caddy; then
	log "aqua-fix-caddy-ip: caddy active on $new"
else
	log "aqua-fix-caddy-ip: caddy NOT active after the fix"
	systemctl --no-pager -n 12 status caddy 2>&1 | sed 's/^/    /' || true
	exit 1
fi
