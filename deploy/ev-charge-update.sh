#!/usr/bin/env bash
# ev-charge-update.sh — install the latest EV Charge build from GitHub, if it changed.
#
# Run by ev-charge-update.timer (as root). Steps:
#   1. download the checksum of the build for this server's Python (ev_charge-py3.X.pyz.sha256)
#   2. same as the installed .pyz? nothing to do
#   3. download the .pyz, check its checksum (tried again while the build is being published), check it starts
#   4. stop EV Charge, keep the old .pyz and a copy of the database, put the new .pyz in place, start
#   5. wait for /api/health; if it doesn't answer, put the old .pyz and the database back
#
# Usage: ev-charge-update.sh [config file]   (default /etc/ev-charge/update.conf; see update.conf.example)
#        FORCE=1 ev-charge-update.sh          reinstall even if the checksum is the same

set -euo pipefail

CONF="${1:-/etc/ev-charge/update.conf}"
# shellcheck source=/dev/null
[[ -f "$CONF" ]] && source "$CONF"

REPO="${REPO:-Fabiancrg/EV-charge}"
TAG="${TAG:-latest}"
PYZ="${PYZ:-/opt/ev-charge/ev_charge.pyz}"
SERVICE="${SERVICE:-ev-charge.service}"
PYTHON="${PYTHON:-python3}"
HEALTH_URL="${HEALTH_URL:-http://127.0.0.1:8000/api/health}"
HEALTH_TIMEOUT="${HEALTH_TIMEOUT:-90}"
DB_FILE="${DB_FILE:-}"            # e.g. /home/evcharge/.ev-charge/ev_charge.db: copied before the update, restored on rollback
SHIV_DIR="${SHIV_DIR:-}"          # e.g. /home/evcharge/.shiv: old extracted versions are removed (the 2 newest are kept)
GITHUB_TOKEN="${GITHUB_TOKEN:-}"  # only for a private repository
RETRIES="${RETRIES:-6}"           # downloads tried while a build is being published…
RETRY_DELAY="${RETRY_DELAY:-20}"  # …and the seconds between them

log() { echo "ev-charge-update: $*"; }

PYVER="$("$PYTHON" -c 'import sys; print(f"{sys.version_info[0]}.{sys.version_info[1]}")')"
ASSET="ev_charge-py${PYVER}.pyz"
BASE="${RELEASE_URL:-https://github.com/${REPO}/releases/download/${TAG}}"
CURL=(curl -fsSL --retry 3 --retry-delay 5 --connect-timeout 20)
[[ -n "$GITHUB_TOKEN" ]] && CURL+=(-H "Authorization: Bearer ${GITHUB_TOKEN}")

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

# While a build is being published its files are replaced one after the other: for a moment the checksum is
# still the previous one, or the .pyz is missing (404). So a failed download is tried again a few times.
attempt=1
while :; do
    if ! "${CURL[@]}" -o "$TMP/sha256" "$BASE/$ASSET.sha256"; then
        log "no build $ASSET in $REPO release '$TAG' (Python $PYVER on this server); nothing done"
        exit 1
    fi
    WANT="$(cut -d' ' -f1 "$TMP/sha256")"
    HAVE="$( [[ -f "$PYZ" ]] && sha256sum "$PYZ" | cut -d' ' -f1 || echo none )"
    if [[ "$WANT" == "$HAVE" && -z "${FORCE:-}" ]]; then
        exit 0  # up to date
    fi

    log "build $ASSET (${WANT:0:12}), installed ${HAVE:0:12}: downloading"
    rm -f "$TMP/new.pyz"
    if "${CURL[@]}" -o "$TMP/new.pyz" "$BASE/$ASSET" 2>"$TMP/err" && [[ "$(sha256sum "$TMP/new.pyz" | cut -d' ' -f1)" == "$WANT" ]]; then
        break
    fi
    if (( attempt >= RETRIES )); then
        log "the build is still being published after $RETRIES tries ($(tail -n 1 "$TMP/err")): will try again next time"
        exit 1
    fi
    log "the build is being published right now (file missing or checksum mismatch): trying again in ${RETRY_DELAY}s"
    sleep "$RETRY_DELAY"
    attempt=$((attempt + 1))
done
if ! SHIV_ROOT="$TMP/shiv" "$PYTHON" "$TMP/new.pyz" --help >/dev/null 2>"$TMP/err"; then
    log "the new build doesn't start with $PYTHON: $(tail -n 3 "$TMP/err")"
    exit 1
fi

healthy() {
    local until=$((SECONDS + HEALTH_TIMEOUT))
    while (( SECONDS < until )); do
        if curl -fsk --max-time 5 "$HEALTH_URL" >/dev/null 2>&1; then return 0; fi
        sleep 3
    done
    return 1
}

log "stopping $SERVICE"
systemctl stop "$SERVICE"
[[ -f "$PYZ" ]] && cp -p "$PYZ" "$PYZ.prev"
if [[ -n "$DB_FILE" && -f "$DB_FILE" ]]; then
    cp -p "$DB_FILE" "$DB_FILE.pre-update"  # the new version may migrate the database at startup
fi
mkdir -p "$(dirname "$PYZ")"
cp "$TMP/new.pyz" "$PYZ.new"
if [[ -f "$PYZ.prev" ]]; then
    chown --reference="$PYZ.prev" "$PYZ.new"
    chmod --reference="$PYZ.prev" "$PYZ.new"
else
    chmod 755 "$PYZ.new"
fi
mv -f "$PYZ.new" "$PYZ"

log "starting $SERVICE"
systemctl start "$SERVICE"
if healthy; then
    log "updated to ${WANT:0:12}; $HEALTH_URL answers"
    if [[ -n "$SHIV_DIR" && -d "$SHIV_DIR" ]]; then
        name="$(basename "$PYZ")"
        # shellcheck disable=SC2012
        ls -1dt "$SHIV_DIR/${name}_"* 2>/dev/null | tail -n +3 | while read -r old; do rm -rf -- "$old" "$SHIV_DIR/.$(basename "$old")_lock"; done
    fi
    exit 0
fi

log "$HEALTH_URL doesn't answer after ${HEALTH_TIMEOUT}s: rolling back"
systemctl stop "$SERVICE" || true
if [[ -f "$PYZ.prev" ]]; then
    mv -f "$PYZ.prev" "$PYZ"
    if [[ -n "$DB_FILE" && -f "$DB_FILE.pre-update" ]]; then
        cp -p "$DB_FILE.pre-update" "$DB_FILE"
    fi
    systemctl start "$SERVICE"
    if healthy; then
        log "previous version back in service"
    else
        log "the previous version doesn't answer either: check 'journalctl -u $SERVICE'"
    fi
fi
exit 1
