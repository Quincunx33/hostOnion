#!/usr/bin/env bash
set -Eeuo pipefail

# HostOnion production-style installer for Ubuntu/Debian.
# Usage: sudo ./install.sh /path/to/public-site

readonly WEB_ROOT="/var/www/hostonion/public"
readonly NGINX_SITE="/etc/nginx/sites-available/hostonion"
readonly NGINX_ENABLED="/etc/nginx/sites-enabled/hostonion"
readonly TOR_DROPIN_DIR="/etc/tor/torrc.d"
readonly TOR_DROPIN="${TOR_DROPIN_DIR}/hostonion.conf"
readonly TOR_CONFIG="/etc/tor/torrc"
readonly HS_DIR="/var/lib/tor/hostonion"
readonly LOGROTATE_CONFIG="/etc/logrotate.d/hostonion"
readonly BACKUP_STAMP="$(date +%Y%m%d%H%M%S)"

BACKED_UP_FILES=()
CONFIG_WRITTEN=0

usage() {
  echo "Usage: sudo $0 /path/to/public-site-directory" >&2
  echo "The supplied directory should be the site's public/document root." >&2
}

fail() { echo "[ERROR] $*" >&2; exit 1; }

rollback() {
  local status=$?
  [[ "$status" -eq 0 || "$CONFIG_WRITTEN" -eq 0 ]] && return
  echo "[WARN] Installation failed; restoring managed configuration backups..." >&2
  local entry original backup
  for entry in "${BACKED_UP_FILES[@]}"; do
    original="${entry%%|*}"
    backup="${entry#*|}"
    if [[ -n "$backup" && -f "$backup" ]]; then
      cp -a "$backup" "$original" || true
    else
      rm -f "$original" || true
    fi
  done
  return "$status"
}
trap rollback EXIT

[[ "$(id -u)" -eq 0 ]] || fail "Run this installer with sudo/root privileges."
[[ $# -eq 1 ]] || { usage; exit 2; }
[[ -d "$1" ]] || fail "Site directory does not exist: $1"
command -v realpath >/dev/null || fail "The realpath command is required."
command -v systemctl >/dev/null || fail "systemd/systemctl is required on this host."

SITE_DIR="$(realpath "$1")"
[[ "$SITE_DIR" != "$WEB_ROOT" ]] || fail "Source directory must not be the install destination."
case "$SITE_DIR" in
  /|/bin|/boot|/dev|/etc|/home|/lib|/lib64|/media|/mnt|/opt|/proc|/root|/run|/sbin|/srv|/sys|/tmp|/usr|/var)
    fail "Refusing to deploy a system-level directory: $SITE_DIR" ;;
esac
[[ "$WEB_ROOT" != "$SITE_DIR"/* ]] || {
  fail "The source directory must not contain the install destination: $WEB_ROOT"
}
[[ -f "$SITE_DIR/index.php" || -f "$SITE_DIR/index.html" ]] || {
  echo "[WARN] No index.php or index.html found. Continuing; confirm this is the intended document root." >&2
}

export DEBIAN_FRONTEND=noninteractive
echo "[+] Installing Nginx, Tor, PHP-FPM, and deployment utilities..."
apt-get update
apt-get install -y nginx tor php-fpm php-cli rsync

command -v php >/dev/null || fail "PHP CLI was not installed."
PHP_VERSION="$(php -r 'echo PHP_MAJOR_VERSION.".".PHP_MINOR_VERSION;')"
PHP_FPM_SERVICE="$(systemctl list-unit-files 'php*-fpm.service' --no-legend | awk 'NR == 1 {print $1}')"
[[ -n "$PHP_FPM_SERVICE" ]] || fail "No PHP-FPM systemd service was found after installation."
PHP_FPM_SERVICE="${PHP_FPM_SERVICE%.service}"
PHP_FPM_VERSION="${PHP_FPM_SERVICE#php}"
PHP_FPM_VERSION="${PHP_FPM_VERSION%-fpm}"
systemctl enable --now "$PHP_FPM_SERVICE"
getent passwd debian-tor >/dev/null || fail "The Debian/Ubuntu Tor service account (debian-tor) was not found."

# Back up managed configuration files before replacing/updating them.
backup_if_present() {
  local file="$1"
  if [[ -f "$file" ]]; then
    local backup="${file}.hostonion-backup.${BACKUP_STAMP}"
    cp -a "$file" "$backup"
    BACKED_UP_FILES+=("$file|$backup")
  else
    BACKED_UP_FILES+=("$file|")
  fi
}

mkdir -p "$WEB_ROOT" "$TOR_DROPIN_DIR" "/etc/php/${PHP_VERSION}/fpm/conf.d"
# Mirror the source so deleted files do not remain exposed in the deployed document root.
rsync -a --delete --safe-links \
  --exclude='.git/' --exclude='.env' --exclude='.env.*' \
  --exclude='*.key' --exclude='*.pem' --exclude='*.log' \
  "$SITE_DIR"/ "$WEB_ROOT"/
chown -hR root:www-data /var/www/hostonion
find /var/www/hostonion -type d -exec chmod 0750 {} +
find /var/www/hostonion -type f -exec chmod 0640 {} +

# PHP-FPM baseline hardening. App-specific writable directories are deliberately not made writable here.
PHP_HARDENING="/etc/php/${PHP_VERSION}/fpm/conf.d/99-hostonion-hardening.ini"
backup_if_present "$PHP_HARDENING"
cat > "$PHP_HARDENING" <<'EOF'
expose_php=Off
display_errors=Off
log_errors=On
cgi.fix_pathinfo=0
session.use_strict_mode=1
session.cookie_httponly=1
session.cookie_samesite=Lax
EOF
chmod 0644 "$PHP_HARDENING"

# Nginx listens only on loopback; Tor forwards onion requests to this port.
backup_if_present "$NGINX_SITE"
cat > "$NGINX_SITE" <<EOF
server {
    listen 127.0.0.1:8080 default_server;
    server_name _;
    root ${WEB_ROOT};
    index index.php index.html;

    server_tokens off;
    client_max_body_size 10m;
    access_log /var/log/nginx/hostonion.access.log;
    error_log  /var/log/nginx/hostonion.error.log warn;

    add_header X-Content-Type-Options "nosniff" always;
    add_header Referrer-Policy "strict-origin-when-cross-origin" always;
    add_header X-Frame-Options "SAMEORIGIN" always;
    add_header Permissions-Policy "camera=(), microphone=(), geolocation=()" always;

    location / {
        try_files \$uri \$uri/ /index.php?\$query_string;
    }

    # Do not serve hidden files (including .env and VCS metadata), except ACME-style well-known paths.
    location ~ /\.(?!well-known(?:/|\$)) {
        deny all;
    }

    location ~* \.(?:ini|log|sql|bak|old|orig|save|swp|dist)$ {
        deny all;
    }

    location ~* \.(?:conf|config|crt|key|pem|sh|sqlite|sqlite3|tmp)$ {
        deny all;
    }

    location ~ \.php$ {
        try_files \$uri =404;
        include snippets/fastcgi-php.conf;
        fastcgi_pass unix:/run/php/php${PHP_FPM_VERSION}-fpm.sock;
    }

    # Do not allow arbitrary files to be interpreted as PHP.
    location ~ \.ph(?:ar|p|tml)$ {
        return 404;
    }
}
EOF
ln -sfn "$NGINX_SITE" "$NGINX_ENABLED"

# Add an isolated Tor config via a managed include; preserve the rest of torrc.
backup_if_present "$TOR_CONFIG"
if ! grep -Fq '%include /etc/tor/torrc.d/*.conf' "$TOR_CONFIG"; then
  printf '\n# HostOnion managed include\n%%include /etc/tor/torrc.d/*.conf\n' >> "$TOR_CONFIG"
fi
cat > "$TOR_DROPIN" <<EOF
# HostOnion managed onion service
HiddenServiceDir ${HS_DIR}
HiddenServiceVersion 3
HiddenServicePort 80 127.0.0.1:8080
EOF
chmod 0644 "$TOR_DROPIN"
install -d -o debian-tor -g debian-tor -m 0700 "$HS_DIR"
chown -R debian-tor:debian-tor "$HS_DIR"
chmod 0700 "$HS_DIR"

# Rotate service logs without needing an application restart.
backup_if_present "$LOGROTATE_CONFIG"
cat > "$LOGROTATE_CONFIG" <<'EOF'
/var/log/nginx/hostonion.access.log /var/log/nginx/hostonion.error.log {
    daily
    rotate 14
    compress
    delaycompress
    missingok
    notifempty
    create 0640 www-data adm
    sharedscripts
    postrotate
        systemctl reload nginx >/dev/null 2>&1 || true
    endscript
}
EOF
chmod 0644 "$LOGROTATE_CONFIG"

# All managed files now exist; validation or service startup failures can roll them back.
CONFIG_WRITTEN=1

# Validate before applying configuration.
nginx -t
if command -v tor >/dev/null 2>&1; then
  tor --verify-config -f "$TOR_CONFIG"
fi

systemctl enable --now nginx
systemctl restart "$PHP_FPM_SERVICE"
[[ -S "/run/php/php${PHP_FPM_VERSION}-fpm.sock" ]] || fail "Expected PHP-FPM socket was not created: /run/php/php${PHP_FPM_VERSION}-fpm.sock"
systemctl enable tor
systemctl restart tor

# Wait for Tor to create/preserve the v3 onion hostname.
HOSTNAME_FILE="${HS_DIR}/hostname"
for _ in $(seq 1 180); do
  if [[ -s "$HOSTNAME_FILE" ]]; then break; fi
  if ! systemctl is-active --quiet tor; then
    journalctl -u tor -n 40 --no-pager >&2 || true
    fail "Tor service failed to start; inspect the Tor journal above."
  fi
  sleep 1
done
[[ -s "$HOSTNAME_FILE" ]] || {
  journalctl -u tor -n 40 --no-pager >&2 || true
  fail "Timed out waiting for Tor to create the onion hostname."
}

ONION_HOST="$(tr -d '\r\n' < "$HOSTNAME_FILE")"
[[ "$ONION_HOST" =~ ^[a-z2-7]{56}\.onion$ ]] || fail "Tor produced an unexpected hostname; check ${HOSTNAME_FILE}."

cat <<EOF

[+] Deployment configured.
    Onion URL: http://${ONION_HOST}/
    Web root:  ${WEB_ROOT}
    Nginx:     127.0.0.1:8080 only (not exposed on public interfaces)
    PHP-FPM:   ${PHP_FPM_SERVICE}

Next steps:
  - Test the onion URL using Tor Browser; initial reachability can take a few minutes.
  - Review logs: journalctl -u tor -u nginx -u ${PHP_FPM_SERVICE}
                 tail -f /var/log/nginx/hostonion.error.log
  - Keep ${HS_DIR} private; it contains the onion service identity.
  - Grant www-data write access only to specific application cache/upload directories
    that genuinely require it; do not make the whole document root writable.
EOF
