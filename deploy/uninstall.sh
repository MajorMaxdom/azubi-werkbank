#!/usr/bin/env bash
# Azubi-Werkbank uninstaller — the counterpart of deploy/install.sh.
#
#   sudo ./deploy/uninstall.sh                      # remove the installation, KEEP the data
#   sudo ./deploy/uninstall.sh --backup /root/werkbank-backup.tar.gz --purge-data
#
# Always removed:
#   - systemd service werkbank (stopped, disabled, unit file deleted)
#   - command /usr/local/bin/werkbank
#   - configuration /etc/werkbank
#   - Caddy site /etc/caddy/werkbank.caddy and its "import" line in the main
#     Caddyfile (backed up first; Caddy itself and other sites stay untouched)
#   - fail2ban filter/jail "werkbank" and cron files /etc/cron.d/werkbank-*
#   - the Python environment (.venv) in the code directory
#   - the system user and group werkbank
#
# Only with options:
#   --backup FILE     write a .tar.gz of the data directory and config first
#   --purge-data      also delete the data directory (users, answers, workbooks)
#                     and the Caddy access log — this cannot be undone
#   --remove-code     also delete the code directory (the clone this script is in)
#   --data-dir DIR    data directory, if it cannot be found any more (e.g. when
#                     purging after an earlier run that kept the data)
#   --yes             do not ask (purging data still needs --purge-data)
#
# Running it again is safe; missing parts are skipped.

set -euo pipefail

APP_USER="werkbank"
SERVICE="werkbank"
CONFIG_DIR="/etc/werkbank"
CONFIG_FILE="$CONFIG_DIR/config.yaml"
UNIT="/etc/systemd/system/$SERVICE.service"
WRAPPER="/usr/local/bin/werkbank"
APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# Caddy integration (same overrides as install.sh, for testing).
CADDY_DIR="${CADDY_DIR:-/etc/caddy}"
CADDY_BIN="${CADDY_BIN:-caddy}"
CADDY_RELOAD="${CADDY_RELOAD:-1}"

BACKUP=""
PURGE_DATA=0
REMOVE_CODE=0
ASSUME_YES=0
DATA_DIR=""

info() { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
ok()   { printf '\033[1;32m ok\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m !!\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31mERR\033[0m %s\n' "$*" >&2; exit 1; }

usage() { sed -n '2,29p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit "${1:-0}"; }

confirm() {  # confirm "Question" -> 0 if yes
    [[ "$ASSUME_YES" == 1 ]] && return 0
    [[ -t 0 ]] || die "Not running interactively – use --yes to confirm."
    local answer
    read -r -p "$1 [y/N]: " answer
    [[ "$answer" == y || "$answer" == Y || "$answer" == yes || "$answer" == j ]]
}

parse_args() {
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --backup) BACKUP="${2:?}"; shift 2 ;;
            --purge-data) PURGE_DATA=1; shift ;;
            --remove-code) REMOVE_CODE=1; shift ;;
            --data-dir) DATA_DIR="${2:?}"; shift 2 ;;
            --yes|-y) ASSUME_YES=1; shift ;;
            -h|--help) usage 0 ;;
            *) warn "Unknown option: $1"; usage 1 ;;
        esac
    done
}

# --------------------------------------------------------------------------- discovery

config_value() {  # config_value KEY -> value of "  key: value" in config.yaml
    [[ -f "$CONFIG_FILE" ]] || return 0
    sed -n "s/^[[:space:]]*$1:[[:space:]]*\([^#]*\).*/\1/p" "$CONFIG_FILE" | head -n 1 | sed 's/[[:space:]]*$//'
}

discover() {
    [[ $EUID -eq 0 ]] || die "Please run as root (sudo ./deploy/uninstall.sh)."
    local users_file
    users_file="$(config_value users)"
    if [[ -n "$DATA_DIR" ]]; then
        DATA_DIR="$(realpath -m "$DATA_DIR")"
    elif [[ -n "$users_file" ]]; then
        DATA_DIR="$(dirname "$users_file")"
    elif [[ -f "$UNIT" ]]; then
        DATA_DIR="$(sed -n 's/^WorkingDirectory=//p' "$UNIT" | head -n 1)"
    fi
    if [[ -f "$UNIT" ]]; then
        local exec_dir
        exec_dir="$(sed -n 's#^ExecStart=\(.*\)/\.venv/bin/werkbank serve#\1#p' "$UNIT" | head -n 1)"
        [[ -n "$exec_dir" ]] && APP_DIR="$exec_dir"
    fi
    info "Azubi-Werkbank – uninstall"
    printf '   Code   %s\n   Data   %s\n   Config %s\n' \
        "$APP_DIR" "${DATA_DIR:-(unknown)}" "$CONFIG_DIR"
}

# A data directory is only deleted if it really looks like ours.
safe_data_dir() {
    local dir="$1"
    [[ -n "$dir" && "$dir" == /* && -d "$dir" ]] || return 1
    case "$dir" in
        /|/etc|/etc/*|/usr|/usr/*|/bin|/sbin|/lib|/lib64|/boot|/home|/root|/var|/var/lib|/srv|/opt|/tmp)
            return 1 ;;
    esac
    [[ -e "$dir/users.yaml" || -d "$dir/data" || -d "$dir/progress" || -d "$dir/workbooks" ]]
}

# --------------------------------------------------------------------------- steps

make_backup() {
    [[ -n "$BACKUP" ]] || return 0
    info "Writing backup $BACKUP"
    local parts=()
    [[ -n "$DATA_DIR" && -d "$DATA_DIR" ]] && parts+=("$DATA_DIR")
    [[ -d "$CONFIG_DIR" ]] && parts+=("$CONFIG_DIR")
    [[ ${#parts[@]} -gt 0 ]] || { warn "Nothing to back up."; return 0; }
    install -d -m 0700 "$(dirname "$BACKUP")"
    tar -czf "$BACKUP" --absolute-names "${parts[@]}"
    chmod 0600 "$BACKUP"
    ok "Backup written ($(du -h "$BACKUP" | cut -f1)) – contains password hashes, keep it safe"
}

remove_service() {
    if [[ -f "$UNIT" ]]; then
        systemctl disable --now "$SERVICE" >/dev/null 2>&1 || true
        rm -f "$UNIT"
        systemctl daemon-reload
        systemctl reset-failed "$SERVICE" >/dev/null 2>&1 || true
        ok "Service $SERVICE stopped and removed"
    else
        ok "No service $SERVICE installed"
    fi
}

remove_caddy_site() {
    local main="$CADDY_DIR/Caddyfile" site="$CADDY_DIR/werkbank.caddy"
    local line="import $site" changed=0
    if [[ -f "$main" ]] && grep -qxF "$line" "$main"; then
        local backup="$main.bak-werkbank-uninstall-$(date +%Y%m%d-%H%M%S)"
        cp -p "$main" "$backup"
        # Drop the import line and the comment line install.sh put above it.
        awk -v a="$line" -v b="# Azubi-Werkbank (added by deploy/install.sh)" \
            '$0 != a && $0 != b' "$main" > "$main.tmp"
        cat "$main.tmp" > "$main"
        rm -f "$main.tmp"
        if command -v "$CADDY_BIN" >/dev/null && \
           ! "$CADDY_BIN" validate --config "$main" --adapter caddyfile >/dev/null 2>&1; then
            cp -p "$backup" "$main"
            die "Caddy config invalid after removing the import – restored $main (backup: $backup)."
        fi
        ok "Import line removed from $main (backup: $backup)"
        changed=1
    fi
    if [[ -f "$site" ]]; then
        rm -f "$site"
        ok "Caddy site $site removed"
        changed=1
    fi
    if [[ "$changed" == 1 && "$CADDY_RELOAD" == 1 ]] && systemctl is-active --quiet caddy; then
        systemctl reload caddy && ok "Caddy reloaded (other sites keep running)"
    fi
    [[ "$changed" == 1 ]] || ok "No Caddy site configured"
}

remove_extras() {
    local f removed=0
    for f in /etc/fail2ban/filter.d/werkbank.conf /etc/fail2ban/jail.d/werkbank.conf; do
        [[ -f "$f" ]] && { rm -f "$f"; removed=1; }
    done
    if [[ "$removed" == 1 ]]; then
        systemctl is-active --quiet fail2ban && systemctl restart fail2ban || true
        ok "fail2ban filter/jail removed"
    fi
    for f in /etc/cron.d/werkbank-*; do
        [[ -f "$f" ]] || continue
        rm -f "$f"
        ok "Cron file $f removed (existing backups are not touched)"
    done
    if [[ -f "$WRAPPER" ]]; then
        rm -f "$WRAPPER"
        ok "Command $WRAPPER removed"
    fi
    if [[ -d "$CONFIG_DIR" ]]; then
        rm -rf "$CONFIG_DIR"
        ok "Configuration $CONFIG_DIR removed"
    fi
}

remove_venv() {
    if [[ -d "$APP_DIR/.venv" ]]; then
        rm -rf "$APP_DIR/.venv" "$APP_DIR"/*.egg-info
        ok "Python environment $APP_DIR/.venv removed"
    fi
}

remove_user() {
    if id -u "$APP_USER" >/dev/null 2>&1; then
        userdel "$APP_USER" 2>/dev/null || warn "Could not remove user $APP_USER (still running processes?)"
        getent group "$APP_USER" >/dev/null && groupdel "$APP_USER" 2>/dev/null || true
        ok "System user $APP_USER removed"
    else
        ok "No system user $APP_USER"
    fi
}

purge_data() {
    if [[ "$PURGE_DATA" != 1 ]]; then
        if [[ -n "$DATA_DIR" && -d "$DATA_DIR" ]]; then
            warn "Data kept in $DATA_DIR (users, answers, workbooks). Delete it with --purge-data."
            warn "Its files now belong to a numeric owner; reinstalling recreates the user and fixes this."
            warn "To delete it later: $0 --data-dir $DATA_DIR --purge-data"
        fi
        return
    fi
    if ! safe_data_dir "$DATA_DIR"; then
        warn "Refusing to delete '$DATA_DIR' – it does not look like a Werkbank data directory."
        return
    fi
    confirm "Delete $DATA_DIR with ALL users, answers and workbooks for good?" \
        || { warn "Data kept in $DATA_DIR"; return; }
    rm -rf "$DATA_DIR"
    rm -f /var/log/caddy/werkbank.log
    ok "Data directory $DATA_DIR deleted"
}

remove_code() {
    [[ "$REMOVE_CODE" == 1 ]] || return 0
    case "$APP_DIR" in
        /|/opt|/srv|/usr|/home|/root|"") warn "Refusing to delete code directory '$APP_DIR'"; return ;;
    esac
    [[ -f "$APP_DIR/pyproject.toml" && -f "$APP_DIR/deploy/install.sh" ]] \
        || { warn "Refusing to delete '$APP_DIR' – not a Werkbank checkout."; return; }
    confirm "Delete the code directory $APP_DIR?" || return 0
    cd /
    rm -rf "$APP_DIR"
    ok "Code directory $APP_DIR deleted"
}

main() {
    parse_args "$@"
    discover
    if [[ "$PURGE_DATA" == 1 && -z "$BACKUP" ]]; then
        warn "--purge-data without --backup: the data is gone afterwards."
    fi
    confirm "Remove Azubi-Werkbank from this server?" || die "Aborted – nothing changed."
    make_backup
    remove_service
    remove_caddy_site
    remove_extras
    remove_venv
    purge_data
    remove_user
    remove_code
    info "Azubi-Werkbank has been removed."
    [[ "$PURGE_DATA" == 1 ]] || printf '   Data kept: %s\n' "${DATA_DIR:-(none found)}"
    [[ -n "$BACKUP" ]] && printf '   Backup:    %s\n' "$BACKUP"
    return 0
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
    main "$@"
fi
