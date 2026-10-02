#!/usr/bin/env bash
set -Eeuo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TMP_BASE="${RUNNER_TEMP:-${TMPDIR:-/tmp}}"
TMP_DIR="${TMP_BASE%/}/hostonion-tor-php-${RANDOM}-$$"
PHP_PID=""
TOR_PID=""

cleanup() {
  local status=$?
  trap - EXIT
  if [[ -n "$TOR_PID" ]]; then
    kill "$TOR_PID" 2>/dev/null || true
    wait "$TOR_PID" 2>/dev/null || true
  fi
  if [[ -n "$PHP_PID" ]]; then
    kill "$PHP_PID" 2>/dev/null || true
    wait "$PHP_PID" 2>/dev/null || true
  fi
  if [[ "$status" -ne 0 ]]; then
    echo "--- Tor log ---" >&2
    cat "$TMP_DIR/tor.log" "$TMP_DIR/tor.stdout.log" 2>/dev/null || true
    echo "--- PHP log ---" >&2
    cat "$TMP_DIR/php.log" 2>/dev/null || true
  fi
  rm -rf "$TMP_DIR"
  exit "$status"
}
trap cleanup EXIT

for binary in php tor curl python3; do
  command -v "$binary" >/dev/null 2>&1 || {
    echo "Required command not found: $binary" >&2
    exit 1
  }
done

mkdir -p "$TMP_DIR/data" "$TMP_DIR/hidden_service"
chmod 700 "$TMP_DIR/data" "$TMP_DIR/hidden_service"
PHP_PORT="$(python3 - <<'PY'
import socket
sock = socket.socket()
sock.bind(("127.0.0.1", 0))
print(sock.getsockname()[1])
sock.close()
PY
)"

php -S "127.0.0.1:${PHP_PORT}" -t "$ROOT_DIR/demo_site" \
  >"$TMP_DIR/php.log" 2>&1 &
PHP_PID=$!

cat > "$TMP_DIR/torrc" <<EOF
DataDirectory $TMP_DIR/data
SocksPort 0
Log notice file $TMP_DIR/tor.log
HiddenServiceDir $TMP_DIR/hidden_service
HiddenServiceVersion 3
HiddenServicePort 80 127.0.0.1:$PHP_PORT
EOF
chmod 600 "$TMP_DIR/torrc"

tor --verify-config -f "$TMP_DIR/torrc"
tor -f "$TMP_DIR/torrc" >"$TMP_DIR/tor.stdout.log" 2>&1 &
TOR_PID=$!

# Verify the demo PHP endpoint is healthy on loopback.
php_ready=0
for _ in $(seq 1 30); do
  if curl --fail --silent --max-time 2 "http://127.0.0.1:${PHP_PORT}/health.php" \
      -o "$TMP_DIR/health.txt"; then
    php_ready=1
    break
  fi
  kill -0 "$PHP_PID" 2>/dev/null || break
  sleep 1
done
[[ "$php_ready" -eq 1 ]] || {
  echo "PHP demo did not become ready" >&2
  exit 1
}
grep -qx 'OK' "$TMP_DIR/health.txt"
echo "PHP health endpoint passed on 127.0.0.1:${PHP_PORT}."

# Tor creates the v3 service identity before it needs to publish over the network.
hostname_file="$TMP_DIR/hidden_service/hostname"
hostname_ready=0
for _ in $(seq 1 30); do
  if [[ -s "$hostname_file" ]]; then
    hostname_ready=1
    break
  fi
  kill -0 "$TOR_PID" 2>/dev/null || break
  sleep 1
done
[[ "$hostname_ready" -eq 1 ]] || {
  echo "Tor did not create an onion hostname" >&2
  exit 1
}
ONION_HOST="$(tr -d '\r\n' < "$hostname_file")"
[[ "$ONION_HOST" =~ ^[a-z2-7]{56}\.onion$ ]] || {
  echo "Tor generated an invalid v3 onion hostname" >&2
  exit 1
}
[[ -s "$TMP_DIR/hidden_service/hs_ed25519_secret_key" ]] || {
  echo "Tor did not create the v3 onion private key" >&2
  exit 1
}
kill -0 "$TOR_PID" || {
  echo "Tor exited before the smoke test completed" >&2
  exit 1
}
grep -Fq "HiddenServicePort 80 127.0.0.1:${PHP_PORT}" "$TMP_DIR/torrc"

echo "Tor v3 hidden-service startup passed: http://${ONION_HOST}/"
echo "Tor is configured to forward the onion service to the passing PHP health endpoint."
