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
#   --https MODE          caddy | cert | none                      (asked)
#                           caddy: Caddy in front, automatic certificate
#                           cert:  the app serves HTTPS itself with your certificate
#                           none:  plain HTTP on 127.0.0.1 only (SSH tunnel)
#   --domain NAME         public host name, e.g. werkbank.firma.de
#   --cert FILE           --https cert: certificate (PEM with chain), wildcard or for the domain
#   --key FILE            --https cert: private key (PEM, unencrypted)
#   --tls-internal        Caddy uses its own CA instead of Let's Encrypt (intranet/VPN only)
#   --no-caddy            with --domain: do not configure Caddy (own reverse proxy)
#   --port PORT           port of the app (asked; caddy/none: local port, default
#                         8000 – cert: public HTTPS port, default 443)
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
# (CADDY_DIR, CADDY_BIN, CADDY_RELOAD and CADDY_LOG can be overridden for testing.)
CADDY_DIR="${CADDY_DIR:-/etc/caddy}"
CADDY_BIN="${CADDY_BIN:-caddy}"
CADDY_RELOAD="${CADDY_RELOAD:-1}"
CADDY_LOG="${CADDY_LOG:-/var/log/caddy/werkbank.log}"
CADDY_BACKUP=""

DATA_DIR=""
DOMAIN=""
HTTPS_MODE=""
TLS_CERT=""
TLS_KEY=""
TLS_UNIT="$SERVICE-tls"
TLS_INTERNAL=0
WITH_CADDY=1
PORT=""
ADMIN_USER=""
ADMIN_NAME=""
TIMEZONE="Europe/Berlin"
ASSUME_YES=0

# --------------------------------------------------------------------------- output

info() { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
ok()   { printf '\033[1;32m ok\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m !!\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31mERR\033[0m %s\n' "$*" >&2; exit 1; }

usage() { sed -n '2,29p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit "${1:-0}"; }

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
            --https) HTTPS_MODE="${2:?}"; shift 2 ;;
            --cert) TLS_CERT="${2:?}"; shift 2 ;;
            --key) TLS_KEY="${2:?}"; shift 2 ;;
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

# Port the app listens on: 127.0.0.1 only behind Caddy, or the public HTTPS
# port with an own certificate. A re-run defaults to the port of the existing
# config.
port_in_use() {  # port_in_use PORT -> 0 if another program listens on it
    command -v ss >/dev/null || return 1
    [[ -n "$(ss -Hltn "sport = :$1" 2>/dev/null)" ]] || return 1
    # Our own running service on its configured port is fine.
    [[ "$1" == "$(config_port)" ]] && systemctl is-active --quiet "$SERVICE" && return 1
    return 0
}

config_port() {
    [[ -f "$CONFIG_FILE" ]] && sed -n 's/^listen_port:[[:space:]]*\([0-9]*\).*/\1/p' "$CONFIG_FILE" | head -n 1
    return 0
}

choose_port() {
    local default given="$PORT" lowest=1024
    local question="Local port of the web server (only 127.0.0.1, Caddy forwards to it)"
    default="$(config_port)"
    if [[ "$HTTPS_MODE" == cert ]]; then
        question="Public HTTPS port of the web server (443 = no port in the address)"
        lowest=1
        default="${default:-443}"
    fi
    default="${default:-8000}"
    while true; do
        [[ -n "$given" ]] && PORT="$given" || ask PORT "$question" "$default"
        given=""
        local problem=""
        if ! [[ "$PORT" =~ ^[0-9]+$ && "$PORT" -ge "$lowest" && "$PORT" -le 65535 ]]; then
            problem="Invalid port '$PORT' ($lowest–65535)."
        elif port_in_use "$PORT"; then
            problem="Port $PORT is already in use by another program."
        fi
        [[ -z "$problem" ]] && break
        [[ "$ASSUME_YES" == 1 || ! -t 0 ]] && die "$problem Use --port."
        warn "$problem"
    done
    if [[ -n "$(config_port)" && "$PORT" != "$(config_port)" ]]; then
        warn "Existing config uses port $(config_port) and is kept – using that port."
        PORT="$(config_port)"
    fi
}

config_value() {  # config_value KEY -> value of a top-level key in the existing config
    [[ -f "$CONFIG_FILE" ]] && sed -n "s/^$1:[[:space:]]*\([^#[:space:]]*\).*/\1/p" "$CONFIG_FILE" | head -n 1
    return 0
}

# caddy | cert | none. Without questions (--yes): cert if --cert is given,
# caddy if --domain is given, otherwise none. A re-run suggests the mode of
# the existing config.
choose_https_mode() {
    if [[ -z "$HTTPS_MODE" ]]; then
        local default=1 choice="" cert_in_config
        cert_in_config="$(config_value tls_cert)"
        if [[ -n "$TLS_CERT" || "$cert_in_config" == /* ]]; then
            default=2
        elif [[ "$(config_value base_url)" == http://* ]]; then
            default=3
        fi
        if [[ "$ASSUME_YES" == 1 || ! -t 0 ]]; then
            if [[ -n "$TLS_CERT" ]]; then HTTPS_MODE=cert
            elif [[ -n "$DOMAIN" ]]; then HTTPS_MODE=caddy
            else HTTPS_MODE=none; fi
        else
            printf '%s\n' "How should users reach the web server?" \
                "  1) HTTPS via Caddy – automatic certificate (Let's Encrypt)" \
                "  2) HTTPS with an own certificate file (wildcard or for the domain), no Caddy" \
                "  3) Only locally on 127.0.0.1 (SSH tunnel), no HTTPS"
            while true; do
                ask choice "Choice" "$default"
                case "$choice" in
                    1|caddy) HTTPS_MODE=caddy; break ;;
                    2|cert) HTTPS_MODE=cert; break ;;
                    3|none) HTTPS_MODE=none; break ;;
                    *) warn "Please answer 1, 2 or 3." ;;
                esac
            done
        fi
    fi
    if [[ -f "$CONFIG_FILE" ]]; then
        local configured=caddy
        [[ "$(config_value base_url)" == http://* ]] && configured=none
        [[ "$(config_value tls_cert)" == /* ]] && configured=cert
        if [[ "$configured" != "$HTTPS_MODE" ]]; then
            warn "The existing config ($CONFIG_FILE) uses '$configured' and is kept – '$HTTPS_MODE' is ignored."
            warn "To switch, edit base_url/listen_host/listen_port/tls_cert/tls_key there (deploy/README.md)."
            HTTPS_MODE="$configured"
        fi
    fi
    case "$HTTPS_MODE" in
        caddy) ;;
        cert) WITH_CADDY=0 ;;
        none) WITH_CADDY=0; DOMAIN="" ;;
        *) die "Invalid --https mode '$HTTPS_MODE' (caddy, cert or none)." ;;
    esac
}

# Checks a certificate/key pair: readable PEM, key belongs to the certificate,
# not expired, valid for the domain (openssl also matches wildcards).
certificate_problem() {  # certificate_problem CERT KEY DOMAIN -> prints the problem, if any
    local cert="$1" key="$2" domain="$3"
    [[ "$cert" == /* && "$key" == /* ]] || { echo "Please give absolute paths."; return; }
    [[ -r "$cert" ]] || { echo "Certificate not found or not readable: $cert"; return; }
    [[ -r "$key" ]] || { echo "Key not found or not readable: $key"; return; }
    openssl x509 -in "$cert" -noout >/dev/null 2>&1 \
        || { echo "$cert is not a PEM certificate."; return; }
    openssl pkey -in "$key" -noout -passin pass: >/dev/null 2>&1 \
        || { echo "$key is not an unencrypted PEM private key."; return; }
    [[ "$(openssl x509 -in "$cert" -noout -pubkey 2>/dev/null)" == "$(openssl pkey -in "$key" -pubout 2>/dev/null)" ]] \
        || { echo "The key does not belong to the certificate."; return; }
    openssl x509 -in "$cert" -noout -checkend 0 >/dev/null 2>&1 \
        || { echo "The certificate has expired ($(openssl x509 -in "$cert" -noout -enddate | cut -d= -f2))."; return; }
    if ! openssl x509 -in "$cert" -noout -checkhost "$domain" 2>/dev/null | grep -q "does match"; then
        local names
        names="$(openssl x509 -in "$cert" -noout -ext subjectAltName 2>/dev/null | tail -n +2 | sed 's/^ *//')"
        echo "The certificate is not valid for $domain (it covers: ${names:-no subjectAltName})."
    fi
}

choose_certificate() {
    local default_cert default_key problem
    default_cert="$(config_value tls_cert)"; [[ "$default_cert" == /* ]] || default_cert=""
    default_key="$(config_value tls_key)"; [[ "$default_key" == /* ]] || default_key=""
    while true; do
        [[ -n "$TLS_CERT" ]] || ask TLS_CERT "Certificate file (PEM with intermediate chain, e.g. fullchain.pem)" "$default_cert"
        [[ -n "$TLS_KEY" ]] || ask TLS_KEY "Private key file (PEM, e.g. privkey.pem)" "$default_key"
        problem="$(certificate_problem "$TLS_CERT" "$TLS_KEY" "$DOMAIN")"
        [[ -z "$problem" ]] && break
        [[ "$ASSUME_YES" == 1 || ! -t 0 ]] && die "$problem"
        warn "$problem"
        TLS_CERT=""; TLS_KEY=""
    done
    ok "Certificate valid for $DOMAIN until $(openssl x509 -in "$TLS_CERT" -noout -enddate | cut -d= -f2)"
}

valid_id() { [[ "$1" =~ ^[a-z0-9][a-z0-9-]{0,62}$ ]]; }

collect_settings() {
    info "Azubi-Werkbank – Installation from $APP_DIR"
    check_location "The code directory" "$APP_DIR"

    [[ -n "$DATA_DIR" ]] || ask DATA_DIR "Data directory (users, answers, workbooks)" "/var/lib/werkbank"
    DATA_DIR="${DATA_DIR%/}"
    check_location "The data directory" "$DATA_DIR"

    choose_https_mode
    if [[ "$HTTPS_MODE" != none && -z "$DOMAIN" ]]; then
        local known=""
        if [[ "$(config_value base_url)" == https://* ]]; then
            known="$(config_value base_url)"; known="${known#https://}"; known="${known%%[:/]*}"
        fi
        [[ "$ASSUME_YES" == 1 || ! -t 0 ]] && [[ -z "$known" ]] \
            && die "--domain is required for --https $HTTPS_MODE."
        while [[ -z "$DOMAIN" ]]; do
            ask DOMAIN "Domain users open in the browser (e.g. werkbank.firma.de)" "$known"
        done
    fi
    if [[ -n "$DOMAIN" && ! "$DOMAIN" =~ ^[A-Za-z0-9.-]+$ ]]; then
        die "Invalid domain: $DOMAIN"
    fi
    [[ "$HTTPS_MODE" == cert ]] && choose_certificate
    choose_port

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
    apt-get install -y -qq git python3 python3-venv curl ca-certificates openssl >/dev/null
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
            "$APP_DIR/workbooks/linux-basics.yaml" "$APP_DIR/workbooks/_template.yaml" \
            "$DATA_DIR/workbooks/"
        ok "Example workbooks copied (linux-basics.yaml, _template.yaml)"
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
    local base_url secure listen_host="127.0.0.1" tls=""
    if [[ "$HTTPS_MODE" == cert ]]; then
        base_url="https://$DOMAIN"; secure=true; listen_host="0.0.0.0"
        [[ "$PORT" == 443 ]] || base_url="$base_url:$PORT"
        tls="
# HTTPS directly with an own certificate (no Caddy). systemd hands both files
# to the service (/etc/systemd/system/$SERVICE.service.d/tls.conf).
tls_cert: $TLS_CERT
tls_key: $TLS_KEY
"
    elif [[ -n "$DOMAIN" ]]; then
        base_url="https://$DOMAIN"; secure=true
    else
        base_url="http://127.0.0.1:$PORT"; secure=false
    fi
    cat > "$CONFIG_FILE" <<EOF
# Azubi-Werkbank configuration (written by deploy/install.sh).
# All keys: $APP_DIR/config.example.yaml

# Exactly the address users type in the browser.
base_url: $base_url
listen_host: $listen_host
listen_port: $PORT
$tls
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
    write_tls_units
    systemctl daemon-reload
    systemctl enable "$SERVICE" >/dev/null 2>&1
    systemctl restart "$SERVICE"
    local i check="http://127.0.0.1:$PORT/login"
    [[ "$(config_value tls_cert)" == /* ]] && check="https://127.0.0.1:$PORT/login"
    for i in $(seq 1 60); do
        if curl -fsk -o /dev/null "$check"; then
            ok "Service running on ${check%/login}"
            [[ "$check" == https://* ]] && check_public_certificate
            return
        fi
        sleep 0.5
    done
    journalctl -u "$SERVICE" -n 30 --no-pager >&2 || true
    die "The service did not start – see the log above."
}

# Own certificate: systemd hands certificate and key to the service
# (LoadCredential – the originals keep their permissions), ports below 1024
# get CAP_NET_BIND_SERVICE, and a path unit restarts the service when the
# files are renewed. The directories are watched as well: certbot & co. renew
# by replacing symlinks, which a watch on the file alone does not notice.
# Everything follows the config, so a kept config keeps its setup.
write_tls_units() {
    local dropin="/etc/systemd/system/$SERVICE.service.d"
    local cert key port
    cert="$(config_value tls_cert)"; key="$(config_value tls_key)"
    port="$(config_port)"
    if ! [[ "$cert" == /* && "$key" == /* ]]; then
        if [[ -f "$dropin/tls.conf" || -f "/etc/systemd/system/$TLS_UNIT.path" ]]; then
            systemctl disable --now "$TLS_UNIT.path" >/dev/null 2>&1 || true
            rm -f "$dropin/tls.conf" "/etc/systemd/system/$TLS_UNIT.path" "/etc/systemd/system/$TLS_UNIT.service"
            rmdir "$dropin" 2>/dev/null || true
            ok "Own-certificate setup removed (the config has no tls_cert)"
        fi
        return
    fi
    install -d -m 0755 "$dropin"
    {
        echo "# Written by deploy/install.sh: HTTPS with an own certificate."
        echo "[Service]"
        echo "LoadCredential=tls.crt:$cert"
        echo "LoadCredential=tls.key:$key"
        if [[ "${port:-0}" -lt 1024 ]]; then
            echo "AmbientCapabilities=CAP_NET_BIND_SERVICE"
            echo "CapabilityBoundingSet=CAP_NET_BIND_SERVICE"
        fi
    } > "$dropin/tls.conf"
    {
        echo "# Written by deploy/install.sh: restart $SERVICE when the certificate is renewed."
        echo "[Unit]"
        echo "Description=Azubi-Werkbank – watch the TLS certificate"
        echo
        echo "[Path]"
        echo "PathChanged=$cert"
        echo "PathChanged=$key"
        echo "PathChanged=$(dirname "$cert")"
        [[ "$(dirname "$key")" == "$(dirname "$cert")" ]] || echo "PathChanged=$(dirname "$key")"
        echo "Unit=$TLS_UNIT.service"
        echo
        echo "[Install]"
        echo "WantedBy=multi-user.target"
    } > "/etc/systemd/system/$TLS_UNIT.path"
    {
        echo "# Written by deploy/install.sh: started by $TLS_UNIT.path."
        echo "[Unit]"
        echo "Description=Azubi-Werkbank – load the renewed TLS certificate"
        echo
        echo "[Service]"
        echo "Type=oneshot"
        echo "# Renewal tools write certificate and key one after the other."
        echo "ExecStartPre=/bin/sleep 10"
        echo "ExecStart=/bin/systemctl try-restart $SERVICE.service"
    } > "/etc/systemd/system/$TLS_UNIT.service"
    systemctl daemon-reload
    systemctl enable "$TLS_UNIT.path" >/dev/null 2>&1
    systemctl restart "$TLS_UNIT.path"
    ok "Certificate handed to the service; renewals restart it automatically"
}

# The browser's view: does the served certificate verify for the domain?
check_public_certificate() {
    local domain
    domain="$(config_value base_url)"; domain="${domain#https://}"; domain="${domain%%[:/]*}"
    if curl -fs -o /dev/null --resolve "$domain:$PORT:127.0.0.1" "https://$domain:$PORT/login"; then
        ok "Certificate verified for https://$domain:$PORT"
    else
        warn "curl does not trust the certificate for $domain – intermediate chain missing (use fullchain.pem) or a private CA?"
    fi
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
        systemctl enable caddy >/dev/null 2>&1 || true
        reload_caddy
    fi
    ok "Caddy configured ($CADDY_DIR/werkbank.caddy)"
}

# The access log must belong to the caddy user. "caddy validate" runs as root
# and would otherwise create it as root:root 0600 – the running Caddy then
# cannot open it and rejects the whole configuration.
prepare_caddy_log() {
    local owner="caddy"
    id -u "$owner" >/dev/null 2>&1 || return 0
    if [[ ! -d "$(dirname "$CADDY_LOG")" ]]; then
        install -d -o "$owner" -g "$owner" -m 0755 "$(dirname "$CADDY_LOG")"
    fi
    [[ -e "$CADDY_LOG" ]] || install -o "$owner" -g "$owner" -m 0600 /dev/null "$CADDY_LOG"
    chown "$owner:$owner" "$CADDY_LOG"
}

# Never restart: a failing restart would take down every site on this Caddy.
# A failed reload keeps the old configuration running; then our import is
# taken back so that the next Caddy restart cannot fail because of it.
reload_caddy() {
    local result=0
    if systemctl is-active --quiet caddy; then
        timeout 120 systemctl reload caddy >/dev/null 2>&1 || result=$?
    else
        timeout 120 systemctl start caddy >/dev/null 2>&1 || result=$?
    fi
    [[ "$result" == 0 ]] && { ok "Caddy reloaded (other sites keep running)"; return; }
    if [[ -n "$CADDY_BACKUP" ]]; then
        cp -p "$CADDY_BACKUP" "$CADDY_DIR/Caddyfile"
        rm -f "$CADDY_DIR/werkbank.caddy"
        warn "Import taken back – $CADDY_DIR/Caddyfile restored from $CADDY_BACKUP."
    fi
    [[ "$result" == 124 ]] && warn "systemctl did not answer within 120 s (job may still be pending: systemctl list-jobs)."
    die "Caddy did not accept the configuration – see: journalctl -u caddy -n 30"
}

install_caddy_site() {
    local main="$CADDY_DIR/Caddyfile" site="$CADDY_DIR/werkbank.caddy"
    local line="import $site" backup=""
    install -d -m 0755 "$CADDY_DIR"
    prepare_caddy_log
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
    prepare_caddy_log
    CADDY_BACKUP="$backup"
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
    url="$(config_value base_url)/"
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
    if [[ "$(config_value tls_cert)" == /* ]]; then
        cat <<EOF

 The app serves HTTPS itself on port $PORT (all interfaces). Open that port
 in the firewall and point the DNS entry for the domain to this server.
 Certificate: $(config_value tls_cert)
 Renewed certificates are picked up automatically ($TLS_UNIT.path).
EOF
    elif [[ -z "$DOMAIN" ]]; then
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
