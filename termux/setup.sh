#!/data/data/com.termux/files/usr/bin/bash
set -Eeuo pipefail

# Termux/Android setup for Nginx + PHP-FPM + Tor onion service.
# Usage: bash setup.sh /path/to/public-site

fail() { echo "[ERROR] $*" >&2; exit 1; }

[[ -n "${PREFIX:-}" && "$PREFIX" == "/data/data/com.termux/files/usr" ]] || \
  fail "Run this script inside the official Termux app environment."
[[ $# -le 1 ]] || fail "Usage: bash setup.sh [/path/to/public-site]"
SITE_DIR="$(realpath "${1:-$HOME/public_html}")"
[[ -d "$SITE_DIR" ]] || fail "Site directory not found: $SITE_DIR"

# Termux apps are sandboxed Android processes; no root/systemd/apt assumptions.
echo "[+] Installing Termux packages..."
pkg update -y
pkg install -y nginx php tor termux-services

STATE_DIR="$HOME/.hostonion"
HS_DIR="$STATE_DIR/hidden_service"
TOR_DATA="$STATE_DIR/tor-data"
NGINX_HOME="$HOME/.nginx"
mkdir -p "$HS_DIR" "$TOR_DATA" "$NGINX_HOME" "$HOME/.php"
chmod 700 "$STATE_DIR" "$HS_DIR" "$TOR_DATA"

# Escape path for use in a quoted Nginx string.
nginx_quote() {
  local value="$1"
  value="${value//\\/\\\\}"
  value="${value//\"/\\\"}"
  printf '"%s"' "$value"
}
SITE_NGINX="$(nginx_quote "$SITE_DIR")"

cat > "$NGINX_HOME/nginx.conf" <<EOF
worker_processes 1;
pid $NGINX_HOME/nginx.pid;
error_log $NGINX_HOME/error.log warn;
events { worker_connections 256; }
http {
    include $PREFIX/etc/nginx/mime.types;
    default_type application/octet-stream;
    access_log $NGINX_HOME/access.log;
    sendfile on;
    server {
        listen 127.0.0.1:8080;
        server_name _;
        root $SITE_NGINX;
        index index.php index.html;
        server_tokens off;
        client_max_body_size 10m;
        add_header X-Content-Type-Options "nosniff" always;
        add_header Referrer-Policy "strict-origin-when-cross-origin" always;
        location / { try_files \$uri \$uri/ /index.php?\$query_string; }
        location ~ /\.(?!well-known(?:/|\$)) { deny all; }
        location ~* \.(?:ini|log|sql|bak|old|swp|dist)$ { deny all; }
        location ~ \.php$ {
            try_files \$uri =404;
            include $PREFIX/etc/nginx/fastcgi_params;
            fastcgi_param SCRIPT_FILENAME \$document_root\$fastcgi_script_name;
            fastcgi_pass 127.0.0.1:9000;
        }
        location ~ \.ph(?:ar|p|tml)$ { return 404; }
    }
}
EOF

cat > "$STATE_DIR/torrc" <<EOF
DataDirectory $TOR_DATA
SocksPort 0
RunAsDaemon 0
HiddenServiceDir $HS_DIR
HiddenServiceVersion 3
HiddenServicePort 80 127.0.0.1:8080
EOF
chmod 600 "$STATE_DIR/torrc"

# Termux packages use runit through termux-services. The service daemon starts
# after restarting Termux the first time after installing termux-services.
make_service() {
  local name="$1"
  local command="$2"
  local dir="$PREFIX/var/service/$name"
  mkdir -p "$dir/log"
  printf '#!%s/bin/sh\nexec %s\n' "$PREFIX" "$command" > "$dir/run"
  chmod 700 "$dir/run"
  ln -sfn "$PREFIX/share/termux-services/svlogger" "$dir/log/run"
}

make_service hostonion-php-fpm "php-fpm -F -y '$PREFIX/etc/php-fpm.conf'"
make_service hostonion-nginx "nginx -p '$NGINX_HOME' -g 'daemon off;' -c '$NGINX_HOME/nginx.conf'"
make_service hostonion-tor "tor -f '$STATE_DIR/torrc'"

if command -v nginx >/dev/null && nginx -t -p "$NGINX_HOME" -c "$NGINX_HOME/nginx.conf"; then
  echo "[+] Nginx config test passed."
else
  fail "Nginx config test failed; inspect $NGINX_HOME/error.log"
fi

if command -v sv-enable >/dev/null 2>&1; then
  sv-enable hostonion-php-fpm || true
  sv-enable hostonion-nginx || true
  sv-enable hostonion-tor || true
  sv-enable hostonion-tor || true
else
  echo "[!] termux-services is installed. Fully close/reopen Termux, then run:"
  echo "    sv-enable hostonion-php-fpm hostonion-nginx hostonion-tor"
fi

cat <<EOF

[+] Termux configuration created.
    Site:       $SITE_DIR
    Nginx:      127.0.0.1:8080
    Tor config: $STATE_DIR/torrc
    Onion keys: $HS_DIR (keep private)

Android caveat: Android may stop background apps. For long-running use, install
Termux:Boot from the same trusted Termux source, disable battery optimization
for Termux, and use termux-wake-lock while hosting. This remains less reliable
than a dedicated server. See ../README.md for operations and security notes.
EOF
