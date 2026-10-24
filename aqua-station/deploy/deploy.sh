#!/usr/bin/env bash
# =============================================================================
# aqua-station — fast redeploy of code and dashboard (no dependency work)
# =============================================================================
# Runs ON THE PI, as the `aqua` user with sudo. This is the sibling of
# `install.sh`, split off for the one thing you do over and over after the first
# setup: push new gateway code and a new dashboard build and bounce the services.
#
# What it does NOT do, deliberately:
#   * no apt, no `pip install`, no venv rebuild, no database tuning, no
#     migrations. Those are `install.sh`'s job and they belong to first setup or
#     a dependency change, not to "I changed a route".
#
# Why that matters here specifically: the Pi is a 3B+ with 905 MB of RAM and no
# wheel cache. Re-running `pip install -e` re-consulted PyPI for every package,
# and on a flaky link that became minutes of retries that read as a hang -- while
# starving the board. A redeploy that only moves files and restarts one unit runs
# in seconds and cannot wedge the machine.
#
# The gateway is installed editable (`pip install -e`), so new `.py` under
# gateway/ is live the moment the service restarts; no reinstall is needed to
# pick up code changes. Only a change to `pyproject.toml` needs `install.sh`.
#
# Usage:
#   sudo ./deploy/deploy.sh            # sync code + web, restart the gateway
#   sudo ./deploy/deploy.sh --no-web   # gateway only (skip the dashboard copy)
#   sudo ./deploy/deploy.sh --check    # report what would change, change nothing
#
# Exit codes: 0 ok, 1 a step failed, 2 bad usage / not root.
# =============================================================================

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
INSTALL_ROOT="/opt/aqua-station"
WEB_ROOT="/srv/aqua-web"
AQUA_USER="aqua"

DO_WEB=1
CHECK_ONLY=0
for arg in "$@"; do
	case "$arg" in
	--no-web) DO_WEB=0 ;;
	--check) CHECK_ONLY=1 ;;
	-h | --help)
		sed -n '2,29p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
		exit 0
		;;
	*)
		printf 'unknown option: %s (try --help)\n' "$arg" >&2
		exit 2
		;;
	esac
done

# -- output helpers, matching deploy/install.sh -------------------------------
section() { printf '\n\033[1;36m== %s ==\033[0m\n' "$*"; }
ok() { printf '  \033[32mok\033[0m    %s\n' "$*"; }
warn() { printf '  \033[33mwarn\033[0m  %s\n' "$*"; }
bad() { printf '  \033[31mfail\033[0m  %s\n' "$*"; }
skip() { printf '  \033[90mskip\033[0m  %s\n' "$*"; }

if ((CHECK_ONLY == 0)) && ((EUID != 0)); then
	bad "must run as root (sudo ./deploy/deploy.sh)"
	exit 2
fi

printf '\033[1;36mAquaControl redeploy\033[0m\n'
printf 'repo: %s\n' "$REPO_ROOT"

# A missing venv means first setup never ran or the gateway was never installed.
# Refuse rather than half-deploy: this script carries no dependency work, so it
# cannot recover from that here.
if [[ ! -x "$INSTALL_ROOT/gateway/.venv/bin/python" ]]; then
	bad "no venv at $INSTALL_ROOT/gateway/.venv — run install.sh once first"
	exit 1
fi

# =============================================================================
# 1. Gateway code, docs, units
# =============================================================================
section "Gateway"

RSYNC_DRY=""
((CHECK_ONLY)) && RSYNC_DRY="--dry-run"

# `--delete-excluded` so a stale `__pycache__` cannot block its parent directory
# from being replaced (see the note in install.sh). `--exclude '.venv'` keeps the
# installed environment, which this script does not touch.
rsync -a --delete --delete-excluded $RSYNC_DRY \
	--exclude '.venv' --exclude '__pycache__' --exclude '.pytest_cache' \
	"$REPO_ROOT/gateway/" "$INSTALL_ROOT/gateway/"
rsync -a --delete $RSYNC_DRY "$REPO_ROOT/docs/" "$INSTALL_ROOT/docs/"
((CHECK_ONLY)) || ok "code synchronised to $INSTALL_ROOT"

# Units are cheap to reinstall and keep the timer set in step with the repo.
if ((CHECK_ONLY == 0)); then
	install -m 0644 "$REPO_ROOT"/deploy/systemd/* /etc/systemd/system/
	systemctl daemon-reload
	ok "systemd units refreshed"
fi

# =============================================================================
# 2. Dashboard (skip with --no-web)
# =============================================================================
if ((DO_WEB)); then
	section "Web"
	if [[ -f "$REPO_ROOT/web/out/index.html" ]]; then
		# The Pi has no npm, so the build happens on the laptop and is rsynced in.
		# Warn if it looks stale rather than shipping it silently.
		if find "$REPO_ROOT/web/src" -newer "$REPO_ROOT/web/out/index.html" -name '*.tsx' -print -quit 2>/dev/null | grep -q .; then
			warn "web/out/ is older than web/src/ — build it on the laptop and rsync again"
		fi
		if ((CHECK_ONLY)); then
			rsync -a --delete --dry-run "$REPO_ROOT/web/out/" "$WEB_ROOT/" >/dev/null
			ok "dashboard would deploy to $WEB_ROOT"
		else
			rsync -a --delete "$REPO_ROOT/web/out/" "$WEB_ROOT/"
			ok "dashboard deployed to $WEB_ROOT"
		fi
	else
		warn "web/out/ not built — skipping the dashboard (build it on the laptop)"
	fi
else
	skip "web (--no-web)"
fi

# =============================================================================
# 3. Restart the gateway
# =============================================================================
section "Restart"
if ((CHECK_ONLY)); then
	skip "not restarting (--check)"
	printf '\nrunning services:\n'
	for unit in postgresql mosquitto caddy aqua-gateway; do
		if systemctl is-active --quiet "$unit"; then ok "$unit active"; else bad "$unit not active"; fi
	done
	exit 0
fi

# Only the gateway holds the code that just changed. postgresql, mosquitto and
# caddy are unrelated to a gateway deploy, so bouncing them would cost seconds of
# downtime and a Caddy cert re-issue for nothing. Caddy is reloaded (cheap, no
# drop) because the static dashboard under it changed.
systemctl restart aqua-gateway.service
ok "aqua-gateway restarted"
if ((DO_WEB)); then
	systemctl reload caddy 2>/dev/null || true
	ok "caddy reloaded"
fi

sleep 3
failed=0
for unit in postgresql mosquitto caddy aqua-gateway; do
	if systemctl is-active --quiet "$unit"; then
		ok "$unit active"
	else
		bad "$unit is not active"
		failed=1
	fi
done

if ((failed)); then
	printf '\n  inspect with:  journalctl -u aqua-gateway -n 50 --no-pager\n'
	exit 1
fi

printf '\n\033[32mredeploy complete\033[0m\n'
