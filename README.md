# HostOnion

Host PHP websites as **Tor v3 onion services** with a single Python launcher.

> **Status:** HostOnion is a practical personal/demo hosting tool. Review and harden your PHP application before using it for production workloads.

## Features

- Host one or multiple PHP sites over Tor
- Automatically creates Tor v3 onion services
- Keeps PHP bound to `127.0.0.1`; the public endpoint is Tor-only
- Automatic PHP process restart with backoff and restart limits
- Site and port validation
- Refuses system paths such as `/etc`, `/proc`, `/root`, and `/var/log`
- Hidden-service directory, private-key, PID, log, and `torrc` permission hardening
- Single-instance lock to prevent conflicting Tor/PHP processes
- Tor bootstrap and local HTTP health checks
- Onion identity reset and backup restore
- Status, verbose logging, clipboard copy, and multi-site configuration support

## Requirements

- Linux or another Unix-like system with process-group and file-lock support
- Python 3.9+
- PHP CLI (`php`)
- Tor (`tor`) version 0.4.6 or newer
- Optional: `pyperclip` for `--copy`

### Ubuntu/Debian

```bash
sudo apt update
sudo apt install -y python3 php-cli tor
```

## Quick start

Make the launcher executable and host a PHP site:

```bash
chmod 700 hostonion.py
./hostonion.py /path/to/your/site --verbose
```

The launcher starts PHP on a loopback port, starts Tor, waits for the Tor bootstrap, and prints the onion URL:

```text
[+] default      → http://xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx.onion
```

Press `Ctrl+C` to stop PHP and Tor cleanly.

### Continue after a Tor bootstrap timeout

Use `--force` only when you understand that Tor may not be reachable yet:

```bash
./hostonion.py /path/to/your/site --force
```

A generated hostname does not guarantee that the onion service is reachable until Tor has fully bootstrapped.

## Multi-site hosting

Pass a repeated `name=path` argument:

```bash
./hostonion.py \
  --site blog=/srv/www/blog \
  --site shop=/srv/www/shop \
  --verbose
```

Site names may contain 1–64 ASCII letters, numbers, `_`, and `-` characters. Each site receives its own onion identity and a separate local PHP port.

You can also use a `hostonion.toml` file next to `hostonion.py`:

```toml
verbose = true
copy = false

[sites.blog]
path = "/srv/www/blog"
port = 9000

[sites.shop]
path = "/srv/www/shop"
port = 9001
```

## Useful commands

```bash
# Generate new onion identities for all configured sites
./hostonion.py /path/to/site --new

# Reset one site's onion identity
./hostonion.py --site blog=/srv/www/blog --new-site blog

# Restore the saved hidden-service backup
./hostonion.py --restore

# Show the current PID and known onion hostnames
./hostonion.py --status

# Copy onion URLs to the clipboard when pyperclip is available
./hostonion.py /path/to/site --copy

# Disable PHP auto-restart
./hostonion.py /path/to/site --no-restart
```

## Demo site

A minimal PHP demo is included in [`demo_site/`](demo_site/). Run it with:

```bash
./hostonion.py demo_site --verbose
```

The demo exposes:

- `index.php` — displays a success message, PHP version, and UTC time
- `health.php` — returns `OK` with HTTP 200

For a temporary non-Tor preview only:

```bash
php -S 127.0.0.1:8080 -t demo_site
curl http://127.0.0.1:8080/health.php
```

## Runtime files

The launcher stores runtime data beside the script in `tor/`:

- `torrc` — generated Tor configuration
- `hidden_service/` — onion identities and private keys
- `hidden_service_backup/` — first-run backup of existing identities
- `hostonion.log` — launcher log
- `tor.log` — Tor log
- `php_<site>.log` — verbose PHP output

Never commit `tor/`, private keys, onion hostnames, logs, or other runtime state. The repository `.gitignore` excludes common sensitive runtime files; review it before publishing a deployment directory.

## Security notes

- The hidden-service directory contains private keys. Protect it with filesystem permissions and secure backups.
- Do not place `.env` files, credentials, database dumps, source-control metadata, or logs inside a public site directory.
- The PHP built-in server is intended for personal/demo use. For a production deployment, consider a carefully configured PHP-FPM plus nginx/Caddy stack bound to loopback.
- Tor does not fix application vulnerabilities. Audit authentication, uploads, sessions, CSRF, XSS, SQL injection, dependencies, and outbound requests.
- An onion URL is reachable by anyone who knows it unless the application or Tor client authorization restricts access.
- Keep Linux, PHP, Tor, and application dependencies patched.

## License

No license file is currently included. Add a license before redistributing this project.
