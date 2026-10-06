#!/usr/bin/env bash
# Azubi-Werkbank installer.
#
# Run as root from a clone of the repository, e.g.
#
#   sudo git clone https://github.com/<account>/azubi-werkbank.git /opt/werkbank
#   cd /opt/werkbank && sudo ./deploy/install.sh
#
# Without options the script asks for everything it needs. All options:
#
#   --data-dir DIR        where users, answers and catalogs live   (default /var/lib/werkbank)
#   --domain NAME         public host name, e.g. werkbank.firma.de (enables Caddy + HTTPS)
#   --tls-internal        Caddy uses its own CA instead of Let's Encrypt (intranet/VPN only)
#   --no-caddy            do not install or configure Caddy
#   --port PORT           local port of the app                    (default 8000)
#   --admin USERNAME      username of the first Fachbetreuer       (e.g. mmustermann)
#   --admin-name "NAME"   display name of the first Fachbetreuer   (e.g. "Max Mustermann")
#   --timezone ZONE       time zone for dates in the UI            (default Europe/Berlin)
#   --yes                 never ask, use defaults/options (for automation)
#
# Running it again is safe: an existing config, existing data and existing
# users are kept; the code is reinstalled and the service restarted.

set -euo pipefail

APP_USER="werkbank"
SERVICE="werkbank"
CONFIG_DIR="/etc/werkbank"
CONFIG_FILE="$CONFIG_DIR/config.yaml"
WRAPPER="/usr/local/bin/werkbank"
APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# Caddy integration: our site lives in its own file that the main Caddyfile
# imports, so existing sites on the same server stay untouched.
# (CADDY_DIR, CADDY_BIN and CADDY_RELOAD can be overridden for testing.)
CADDY_DIR="${CADDY_DIR:-/etc/caddy}"
CADDY_BIN="${CADDY_BIN:-caddy}"
CADDY_RELOAD="${CADDY_RELOAD:-1}"

DATA_DIR=""
DOMAIN=""
TLS_INTERNAL=0
WITH_CADDY=1
PORT=8000
ADMIN_USER=""
ADMIN_NAME=""
TIMEZONE="Europe/Berlin"
ASSUME_YES=0

# --------------------------------------------------------------------------- output

info() { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
ok()   { printf '\033[1;32m ok\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m !!\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31mERR\033[0m %s\n' "$*" >&2; exit 1; }

usage() { sed -n '2,24p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit "${1:-0}"; }

ask() {  # ask VAR "Question" "default"
    local var="$1" question="$2" default="${3:-}" answer=""
    if [[ "$ASSUME_YES" == 1 || ! -t 0 ]]; then
        printf -v "$var" '%s' "$default"
        return
    fi
    if [[ -n "$default" ]]; then
        read -r -p "$question [$default]: " answer
    else
        read -r -p "$question: " answer
    fi
    printf -v "$var" '%s' "${answer:-$default}"
}

# --------------------------------------------------------------------------- checks

parse_args() {
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --data-dir) DATA_DIR="${2:?}"; shift 2 ;;
            --domain) DOMAIN="${2:?}"; shift 2 ;;
            --tls-internal) TLS_INTERNAL=1; shift ;;
            --no-caddy) WITH_CADDY=0; shift ;;
            --port) PORT="${2:?}"; shift 2 ;;
            --admin) ADMIN_USER="${2:?}"; shift 2 ;;
            --admin-name) ADMIN_NAME="${2:?}"; shift 2 ;;
            --timezone) TIMEZONE="${2:?}"; shift 2 ;;
            --yes|-y) ASSUME_YES=1; shift ;;
            -h|--help) usage 0 ;;
            *) warn "Unknown option: $1"; usage 1 ;;
        esac
    done
}

check_system() {
    [[ $EUID -eq 0 ]] || die "Please run as root (sudo ./deploy/install.sh)."
    command -v apt-get >/dev/null || die "This installer supports Debian/Ubuntu (apt) only."
    command -v systemctl >/dev/null || die "systemd is required."
    [[ -f "$APP_DIR/pyproject.toml" && -f "$APP_DIR/deploy/werkbank.service" ]] \
        || die "Run the script from a clone of the repository ($APP_DIR looks incomplete)."
}

# systemd runs the app with ProtectHome=true and PrivateTmp=true, so neither the
# code nor the data may live below /home, /root or /tmp.
check_location() {  # check_location LABEL PATH
    local label="$1" path="$2"
    [[ "$path" == /* ]] || die "$label must be an absolute path: $path"
    case "$path" in
        /home|/home/*|/root|/root/*|/tmp|/tmp/*|/var/tmp|/var/tmp/*|/run/*)
            local hint="Use e.g. /var/lib/werkbank or /srv/werkbank."
            [[ "$label" == "The code directory" ]] \
                && hint="Clone the repository to /opt/werkbank, e.g.: sudo git clone <url> /opt/werkbank"
            die "$label must not be below /home, /root or /tmp (the service cannot read it there): $path
     $hint" ;;
    esac
}

valid_id() { [[ "$1" =~ ^[a-z0-9][a-z0-9-]{0,62}$ ]]; }

collect_settings() {
    info "Azubi-Werkbank – Installation from $APP_DIR"
    check_location "The code directory" "$APP_DIR"

    [[ -n "$DATA_DIR" ]] || ask DATA_DIR "Data directory (users, answers, workbooks)" "/var/lib/werkbank"
    DATA_DIR="${DATA_DIR%/}"
    check_location "The data directory" "$DATA_DIR"

    if [[ -z "$DOMAIN" && "$WITH_CADDY" == 1 ]]; then
        ask DOMAIN "Domain for HTTPS via Caddy (empty = no Caddy, app only on localhost)" ""
    fi
    [[ -n "$DOMAIN" ]] || WITH_CADDY=0
    if [[ -n "$DOMAIN" && ! "$DOMAIN" =~ ^[A-Za-z0-9.-]+$ ]]; then
        die "Invalid domain: $DOMAIN"
    fi
    [[ "$PORT" =~ ^[0-9]+$ && "$PORT" -ge 1 && "$PORT" -le 65535 ]] || die "Invalid port: $PORT"

    [[ -n "$ADMIN_NAME" ]] || ask ADMIN_NAME "Name of the first Fachbetreuer" ""
    [[ -n "$ADMIN_USER" ]] || ask ADMIN_USER "Username of the first Fachbetreuer (a-z, 0-9, -)" "$(suggest_username "$ADMIN_NAME")"
    if [[ -n "$ADMIN_USER" ]]; then
        valid_id "$ADMIN_USER" || die "Invalid username '$ADMIN_USER' (only a-z, 0-9 and -)."
        [[ -n "$ADMIN_NAME" ]] || ADMIN_NAME="$ADMIN_USER"
    fi
}

suggest_username() {  # first letter of the first name + last name, like the web UI
    local name="${1:-}"
    [[ -n "$name" ]] || return 0
    python3 - "$name" <<'PY' 2>/dev/null || true
import re, sys, unicodedata
parts = []
for word in sys.argv[1].split():
    w = word.lower().translate(str.maketrans({"ä": "ae", "ö": "oe", "ü": "ue", "ß": "ss"}))
    w = unicodedata.normalize("NFKD", w).encode("ascii", "ignore").decode()
    w = re.sub(r"[^a-z0-9]", "", w)
    if w:
        parts.append(w)
print((parts[0][0] + parts[-1]) if len(parts) > 1 else (parts[0] if parts else ""))
PY
}

# --------------------------------------------------------------------------- steps

install_packages() {
    info "Installing system packages"
    export DEBIAN_FRONTEND=noninteractive
    apt-get update -qq
    apt-get install -y -qq git python3 python3-venv curl ca-certificates >/dev/null
    python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' \
        || die "Python 3.11 or newer is required (found $(python3 --version 2>&1))."
    ok "$(python3 --version)"
}

create_system_user() {
    if id -u "$APP_USER" >/dev/null 2>&1; then
        ok "System user $APP_USER exists"
    else
        useradd --system --home-dir "$DATA_DIR" --shell /usr/sbin/nologin "$APP_USER"
        ok "System user $APP_USER created"
    fi
}

install_app() {
    info "Installing the application into $APP_DIR/.venv"
    [[ -x "$APP_DIR/.venv/bin/python" ]] || python3 -m venv "$APP_DIR/.venv"
    "$APP_DIR/.venv/bin/pip" install -q --upgrade pip
    "$APP_DIR/.venv/bin/pip" install -q -e "$APP_DIR"
    # The service user only needs to read the code.
    chmod -R u+rwX,go+rX,go-w "$APP_DIR"
    ok "$("$APP_DIR/.venv/bin/pip" show azubi-werkbank 2>/dev/null | awk '/^Version/ {print "Version " $2}')"
}

setup_data_dir() {
    info "Preparing data directory $DATA_DIR"
    install -d -o "$APP_USER" -g "$APP_USER" -m 0750 "$DATA_DIR"
    install -d -o "$APP_USER" -g "$APP_USER" -m 2775 "$DATA_DIR/workbooks" "$DATA_DIR/workbooks/assets"
    if ! compgen -G "$DATA_DIR/workbooks/*.yaml" >/dev/null && ! compgen -G "$DATA_DIR/workbooks/*.json" >/dev/null; then
        install -o "$APP_USER" -g "$APP_USER" -m 0664 \
            "$APP_DIR/workbooks/network-security.yaml" "$APP_DIR/workbooks/_template.yaml" \
            "$DATA_DIR/workbooks/"
        ok "Example workbooks copied (network-security.yaml, _template.yaml)"
    else
        ok "Existing workbooks kept"
    fi
}

write_config() {
    install -d -m 0755 "$CONFIG_DIR"
    if [[ -f "$CONFIG_FILE" ]]; then
        ok "Existing config kept: $CONFIG_FILE"
        return
    fi
    local base_url secure
    if [[ -n "$DOMAIN" ]]; then
        base_url="https://$DOMAIN"; secure=true
    else
        base_url="http://127.0.0.1:$PORT"; secure=false
    fi
    cat > "$CONFIG_FILE" <<EOF
# Azubi-Werkbank configuration (written by deploy/install.sh).
# All keys: $APP_DIR/config.example.yaml

# Exactly the address users type in the browser.
base_url: $base_url
listen_host: 127.0.0.1
listen_port: $PORT

paths:
  workbooks: $DATA_DIR/workbooks
  progress: $DATA_DIR/progress
  users: $DATA_DIR/users.yaml
  data: $DATA_DIR/data
  locales: $APP_DIR/locales

secure_cookies: $secure
timezone: $TIMEZONE
EOF
    chown "root:$APP_USER" "$CONFIG_FILE"
    chmod 0640 "$CONFIG_FILE"
    ok "Config written: $CONFIG_FILE"
}

write_wrapper() {
    cat > "$WRAPPER" <<EOF
#!/bin/sh
# Azubi-Werkbank command line, always as the service user (written by deploy/install.sh).
exec runuser -u $APP_USER -- env WERKBANK_CONFIG=$CONFIG_FILE $APP_DIR/.venv/bin/werkbank "\$@"
EOF
    chmod 0755 "$WRAPPER"
    ok "Command 'werkbank' installed ($WRAPPER)"
}

validate_catalogs() {
    if "$WRAPPER" validate "$DATA_DIR/workbooks" >/dev/null; then
        ok "Workbooks valid"
    else
        warn "Some workbooks have errors – see: werkbank validate $DATA_DIR/workbooks"
    fi
}

write_service() {
    info "Installing systemd service $SERVICE"
    local unit="/etc/systemd/system/$SERVICE.service"
    sed -e "s#/opt/werkbank#$APP_DIR#g" \
        -e "s#/etc/werkbank/config.yaml#$CONFIG_FILE#g" \
        -e "s#/var/lib/werkbank#$DATA_DIR#g" \
        "$APP_DIR/deploy/werkbank.service" > "$unit"
    if [[ "$DATA_DIR" != "/var/lib/werkbank" ]]; then
        # StateDirectory= only manages /var/lib/<name>; other locations are
        # covered by ReadWritePaths= alone.
        sed -i -e '/^StateDirectory=/d' -e '/^StateDirectoryMode=/d' "$unit"
    fi
    systemctl daemon-reload
    systemctl enable "$SERVICE" >/dev/null 2>&1
    systemctl restart "$SERVICE"
    local i
    for i in $(seq 1 60); do
        if curl -fs -o /dev/null "http://127.0.0.1:$PORT/login"; then
            ok "Service running on 127.0.0.1:$PORT"
            return
        fi
        sleep 0.5
    done
    journalctl -u "$SERVICE" -n 30 --no-pager >&2 || true
    die "The service did not start – see the log above."
}

caddyfile_content() {  # caddyfile_content DOMAIN PORT TLS_INTERNAL
    local domain="$1" port="$2" internal="$3"
    sed -e "s#werkbank.example.de#$domain#" \
        -e "s#reverse_proxy 127.0.0.1:8000#reverse_proxy 127.0.0.1:$port#" \
        "$APP_DIR/deploy/Caddyfile" \
    | if [[ "$internal" == 1 ]]; then
        sed "s#^$domain {#$domain {\n\ttls internal#"
      else
        cat
      fi
}

setup_caddy() {
    [[ "$WITH_CADDY" == 1 ]] || { ok "Caddy skipped (app only reachable on 127.0.0.1:$PORT)"; return; }
    info "Configuring Caddy for https://$DOMAIN"
    if ! command -v "$CADDY_BIN" >/dev/null; then
        apt-get install -y -qq debian-keyring debian-archive-keyring apt-transport-https gnupg >/dev/null
        curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' \
            | gpg --dearmor --yes -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
        curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' \
            > /etc/apt/sources.list.d/caddy-stable.list
        apt-get update -qq
        apt-get install -y -qq caddy >/dev/null
        ok "Caddy installed"
    fi
    install_caddy_site
    if [[ "$CADDY_RELOAD" == 1 ]]; then
        install -d -o caddy -g caddy /var/log/caddy
        systemctl enable caddy >/dev/null 2>&1
        systemctl reload caddy 2>/dev/null || systemctl restart caddy
    fi
    ok "Caddy configured ($CADDY_DIR/werkbank.caddy)"
}

install_caddy_site() {
    local main="$CADDY_DIR/Caddyfile" site="$CADDY_DIR/werkbank.caddy"
    local line="import $site" backup=""
    install -d -m 0755 "$CADDY_DIR"
    caddyfile_content "$DOMAIN" "$PORT" "$TLS_INTERNAL" > "$site"
    chmod 0644 "$site"
    if [[ ! -f "$main" ]]; then
        printf '%s\n' "$line" > "$main"
    elif ! grep -qxF "$line" "$main"; then
        backup="$main.bak-werkbank-$(date +%Y%m%d-%H%M%S)"
        cp -p "$main" "$backup"
        printf '\n# Azubi-Werkbank (added by deploy/install.sh)\n%s\n' "$line" >> "$main"
        ok "Import line added to $main (backup: $backup)"
    fi
    if ! "$CADDY_BIN" validate --config "$main" --adapter caddyfile >/dev/null 2>&1; then
        if [[ -n "$backup" ]]; then
            cp -p "$backup" "$main"
            warn "Restored $main – the combined configuration was invalid."
        fi
        die "Caddy configuration is invalid – check: $CADDY_BIN validate --config $main"
    fi
}

create_admin() {
    if [[ -z "$ADMIN_USER" ]]; then
        warn "No first Fachbetreuer created. Later: werkbank user add <name> --name \"…\" --role trainer"
        return
    fi
    if "$WRAPPER" user list 2>/dev/null | awk '{print $1}' | grep -qx "$ADMIN_USER"; then
        ok "Fachbetreuer '$ADMIN_USER' exists already (new invite link: werkbank user reset $ADMIN_USER)"
        return
    fi
    info "Creating the first Fachbetreuer '$ADMIN_USER'"
    INVITE_LINK="$("$WRAPPER" user add "$ADMIN_USER" --name "$ADMIN_NAME" --role trainer | tail -n 1)"
    ok "Fachbetreuer created"
}

summary() {
    local url
    if [[ -n "$DOMAIN" ]]; then url="https://$DOMAIN/"; else url="http://127.0.0.1:$PORT/"; fi
    cat <<EOF

────────────────────────────────────────────────────────────────────────────
 Azubi-Werkbank is installed.

   Address        $url
   Code           $APP_DIR
   Data           $DATA_DIR
   Config         $CONFIG_FILE
   Service        systemctl status $SERVICE   ·   journalctl -u $SERVICE -f
   Command line   werkbank user list | user add | user reset | validate
EOF
    if [[ -n "${INVITE_LINK:-}" ]]; then
        cat <<EOF

 Invite link for $ADMIN_NAME ($ADMIN_USER) – single use, valid 72 h:

   $INVITE_LINK

 Open it, set a password (at least 12 characters), then create all other
 users in the browser under "Nutzer".
EOF
    fi
    if [[ -z "$DOMAIN" ]]; then
        cat <<EOF

 Without a domain the app only listens on 127.0.0.1. To reach it from your
 computer use an SSH tunnel:  ssh -L $PORT:127.0.0.1:$PORT <server>
 and open http://127.0.0.1:$PORT/ – or run the installer again with --domain.
EOF
    elif [[ "$TLS_INTERNAL" == 0 ]]; then
        cat <<EOF

 Caddy requests a Let's Encrypt certificate on first access: the DNS entry for
 $DOMAIN must point to this server and ports 80/443 must be reachable.
EOF
    fi
    cat <<EOF

 Back up $DATA_DIR and $CONFIG_DIR regularly (see deploy/README.md).
────────────────────────────────────────────────────────────────────────────
EOF
}

main() {
    parse_args "$@"
    check_system
    collect_settings
    install_packages
    create_system_user
    install_app
    setup_data_dir
    write_config
    write_wrapper
    validate_catalogs
    write_service
    setup_caddy
    create_admin
    summary
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
    main "$@"
fi
