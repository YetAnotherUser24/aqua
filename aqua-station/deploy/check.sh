#!/usr/bin/env bash
# =============================================================================
# aqua-station dashboard — check, start, restart, and fix a changed LAN address
# =============================================================================
# Runs on the LAPTOP, not on the Pi. It reaches the station over SSH and leaves
# every decision to systemd there; this script only asks questions and, when you
# say so, repairs the one failure that actually recurs.
#
# The dashboard is not something you launch. It is four systemd units
# (aqua-gateway, caddy, mosquitto, postgresql) that come up with the board. So
# "start the dashboard" means "confirm the units are up and the site answers",
# and "restart" means bouncing them in the right order.
#
# Usage:
#   ./deploy/check.sh                    # report state, change nothing
#   ./deploy/check.sh --restart          # restart the stack, in dependency order
#   ./deploy/check.sh --fix-ip           # re-materialise Caddy's bind for the
#                                        # station's *current* LAN address
#   ./deploy/check.sh --url               # print the URL to open, then exit
#
#   AQUA_HOST=aqua@aqua-station ./deploy/check.sh
#
# Exit codes: 0 healthy, 1 a check failed, 2 could not reach the station.
# =============================================================================

set -euo pipefail

# The SSH target. Override with AQUA_HOST when the station is somewhere else or
# addressed differently (an IP, a tailnet name, a different user).
AQUA_HOST="${AQUA_HOST:-aqua@aqua-station}"
# The certificate Caddy issues is for this name, so it is what the browser must
# use. Opening the bare IP gives a TLS error that looks like the server is down.
AQUA_NAME="${AQUA_NAME:-aqua-station.local}"
INSTALL_ROOT="/opt/aqua-station"
UNITS=(mosquitto postgresql aqua-gateway caddy avahi-daemon)

DO_RESTART=0
DO_FIX_IP=0
URL_ONLY=0
for arg in "$@"; do
	case "$arg" in
	--restart) DO_RESTART=1 ;;
	--fix-ip) DO_FIX_IP=1 ;;
	--url) URL_ONLY=1 ;;
	-h | --help)
		sed -n '2,25p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
		exit 0
		;;
	*)
		printf 'unknown option: %s (try --help)\n' "$arg" >&2
		exit 64
		;;
	esac
done

# -- output helpers, matching deploy/install.sh -------------------------------
red() { printf '\033[31m%s\033[0m\n' "$*"; }
grn() { printf '\033[32m%s\033[0m\n' "$*"; }
yel() { printf '\033[33m%s\033[0m\n' "$*"; }
sec() { printf '\n\033[1;36m== %s ==\033[0m\n' "$*"; }
ok() { printf '  \033[32mok\033[0m    %s\n' "$*"; }
warn() { printf '  \033[33mwarn\033[0m  %s\n' "$*"; }
bad() { printf '  \033[31mfail\033[0m  %s\n' "$*"; }
info() { printf '  %s\n' "$*"; }

FAILURES=0
note_fail() {
	FAILURES=$((FAILURES + 1))
	bad "$*"
}

# One SSH command, reused everywhere. `BatchMode=yes` means a missing key fails
# immediately instead of dropping into a password prompt that would hang the
# script with no output.
S="$AQUA_HOST"
SSH_OPTS=(-o ConnectTimeout=10 -o BatchMode=yes)
remote() { ssh "${SSH_OPTS[@]}" "$S" "$@"; }

# -- the station's current address --------------------------------------------
# `hostname -I` lists every address; the first that is not loopback, not the
# tailnet (100.64/10) and not IPv6 is the LAN one Caddy should bind to. This is
# the same expression the installer uses, kept identical on purpose: if the two
# ever disagree, this script would "fix" the config into a state the next install
# undoes.
lan_ip() {
	remote "hostname -I 2>/dev/null | tr ' ' '\n' | grep -vE '^(127\.|100\.|::)' | head -1"
}

# -- connectivity -------------------------------------------------------------
sec "Station"
if ! lan=$(lan_ip 2>/dev/null) || [[ -z "$lan" ]]; then
	note_fail "cannot reach $S over SSH, or it has no LAN address"
	info "The station is off, on another network, or its address changed."
	info "Try:  ping ${AQUA_HOST#*@}   and   ssh $S"
	exit 2
fi
ok "reachable at $lan"

URL="https://$AQUA_NAME/"
if [[ "$URL_ONLY" == 1 ]]; then
	printf '%s\n' "$URL"
	exit 0
fi

# -- services -----------------------------------------------------------------
sec "Services"
for unit in "${UNITS[@]}"; do
	state=$(remote "systemctl is-active $unit 2>/dev/null || true")
	if [[ "$state" == "active" ]]; then
		ok "$unit"
	else
		note_fail "$unit is ${state:-unknown}"
	fi
done

failed_count=$(remote "systemctl list-units --state=failed --no-legend --no-pager 2>/dev/null | wc -l")
if [[ "$failed_count" == "0" ]]; then
	ok "no failed units"
else
	warn "$failed_count failed unit(s): $(remote "systemctl list-units --state=failed --no-legend --no-pager 2>/dev/null | awk '{print \$1}' | tr '\n' ' '")"
fi

# -- the address Caddy is bound to --------------------------------------------
# This is the recurring failure, so it gets its own section. Caddy's `bind` is a
# literal address in /etc/caddy/Caddyfile, written by the installer from whatever
# LAN address the board had *at install time*. DHCP does not promise the same
# address twice, and this board demonstrably moves between networks
# (192.168.18.x and 192.168.137.x have both been seen). When they disagree Caddy
# stops with `bind: cannot assign requested address` -- and every other unit still
# reports healthy, so the outage looks like the web app rather than the address.
sec "Caddy bind address"
bound=$(remote "grep -oE '^[[:space:]]*bind[[:space:]]+[0-9.]+' /etc/caddy/Caddyfile 2>/dev/null | head -1 | awk '{print \$2}'")
BIND_MISMATCH=0
if [[ -z "$bound" ]]; then
	warn "no literal bind found in /etc/caddy/Caddyfile"
	info "Either it is unconfigured or it uses a different form; check by hand."
elif [[ "$bound" == "$lan" ]]; then
	ok "bound to $bound, which is this station's address"
else
	BIND_MISMATCH=1
	note_fail "bound to $bound but the station is at $lan"
	info "This is why the dashboard is down: Caddy cannot bind an address it does not have."
	info "Fix with:  $0 --fix-ip"
fi

# -- repair: re-materialise the bind for the current address ------------------
if [[ "$DO_FIX_IP" == 1 ]]; then
	sec "Fix IP"
	if [[ "$BIND_MISMATCH" == 0 ]]; then
		info "already correct; nothing to do"
	else
		# The template in the repo carries `@LAN_IP@`, never a literal, precisely
		# so this can be regenerated on any host. Substituting into the *template*
		# rather than editing the live file is what the installer does, so the two
		# paths converge on the same bytes instead of drifting apart.
		if remote "test -f $INSTALL_ROOT/deploy/caddy/Caddyfile"; then
			remote "set -e
				sed -e 's/@LAN_IP@/$lan/' $INSTALL_ROOT/deploy/caddy/Caddyfile \
					| sudo -n tee /etc/caddy/Caddyfile >/dev/null
				sudo -n chmod 0644 /etc/caddy/Caddyfile"
			ok "Caddyfile regenerated for $lan"
			remote "sudo -n systemctl restart caddy"
			sleep 3
			state=$(remote "systemctl is-active caddy 2>/dev/null || true")
			if [[ "$state" == "active" ]]; then
				ok "caddy restarted"
			else
				note_fail "caddy still not active after the fix"
				info "Last lines from its journal:"
				remote "sudo -n journalctl -u caddy -n 12 --no-pager 2>&1 | tail -12" | sed 's/^/        /'
			fi
		else
			note_fail "no template at $INSTALL_ROOT/deploy/caddy/Caddyfile"
			info "Re-deploy the tree, or edit /etc/caddy/Caddyfile by hand."
		fi
	fi
fi

# -- restart ------------------------------------------------------------------
# Order matters and is not alphabetical: the gateway opens its MQTT session and
# its database pool at startup, so restarting it before the broker and PostgreSQL
# are up leaves it degraded until something prods it again. Caddy goes last
# because it fronts the gateway.
if [[ "$DO_RESTART" == 1 ]]; then
	sec "Restart"
	remote "sudo -n systemctl restart mosquitto postgresql && sleep 2 && sudo -n systemctl restart aqua-gateway && sleep 3 && sudo -n systemctl restart caddy"
	sleep 2
	for unit in "${UNITS[@]}"; do
		state=$(remote "systemctl is-active $unit 2>/dev/null || true")
		if [[ "$state" == "active" ]]; then ok "$unit"; else note_fail "$unit is ${state:-unknown}"; fi
	done
fi

# -- does it actually serve ---------------------------------------------------
# `systemctl is-active caddy` is not evidence the dashboard works: Caddy has been
# observed `active` while refusing every request. The only proof is a status
# code, and the host name has to be the one the certificate covers -- the bare IP
# fails the TLS handshake and reads as "down" when the site is fine.
sec "Dashboard"
code_for() {
	# `|| true` on the far side: curl exits non-zero when the connection is
	# refused, and with `set -e` that would abort the whole script -- the exact
	# case this section exists to report. The body stays empty on failure and the
	# caller supplies the "no answer" code, rather than the two colliding into
	# "000000".
	remote "curl -skL --resolve $AQUA_NAME:443:$lan -o /dev/null -w '%{http_code}' -m 8 https://$AQUA_NAME$1 || true"
}
for path in / /api/state /healthz; do
	code=$(code_for "$path" 2>/dev/null || true)
	if [[ "$code" == "200" ]]; then
		ok "GET $path -> 200"
	else
		note_fail "GET $path -> ${code:-no answer}"
	fi
done

# -- summary ------------------------------------------------------------------
# The count is of what is *still* wrong. The service and bind sections run before
# any repair, so a `--fix-ip` that succeeds would otherwise leave the run
# reporting the failures it just cleared -- telling you it failed at the exact
# moment it worked. The dashboard section runs last and is the honest answer, so
# the final tally is re-derived here rather than reused.
sec "Result"
still_bad=0
for unit in "${UNITS[@]}"; do
	state=$(remote "systemctl is-active $unit 2>/dev/null || true")
	[[ "$state" == "active" ]] || still_bad=$((still_bad + 1))
done
for path in / /api/state /healthz; do
	code=$(code_for "$path" 2>/dev/null || true)
	[[ "$code" == "200" ]] || still_bad=$((still_bad + 1))
done

if [[ "$still_bad" == 0 ]]; then
	grn "  Healthy. Open: $URL"
	if [[ "$FAILURES" != 0 ]]; then
		yel "  (repaired during this run; the failures above were the state before)"
	fi
	exit 0
fi

red "  $still_bad check(s) still failing."
if [[ "$BIND_MISMATCH" == 1 && "$DO_FIX_IP" == 0 ]]; then
	yel "  The address mismatch is the likely cause. Try:  $0 --fix-ip"
fi
yel "  To bounce the stack:  $0 --restart"
exit 1
