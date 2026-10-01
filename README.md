<div align="center">

# HostOnion

### Host a PHP website as a Tor v3 onion service.

![HostOnion privacy-focused hosting illustration](assets/hero.png)

[![Platforms](https://img.shields.io/badge/platform-Ubuntu%20%7C%20Debian%20%7C%20Termux%20%7C%20Windows-6c63ff?style=for-the-badge)](#platform-support)
[![Tor](https://img.shields.io/badge/Tor-Onion%20Service-7d4698?style=for-the-badge&logo=torproject&logoColor=white)](https://community.torproject.org/onion-services/)
[![PHP](https://img.shields.io/badge/PHP-FPM-777bb4?style=for-the-badge&logo=php&logoColor=white)](https://www.php.net/)

**A practical, security-conscious deployment starter for serving PHP over Tor.**

</div>

---

HostOnion connects a web server bound to loopback with a Tor v3 onion service. It includes an automated Ubuntu/Debian installer, a Termux setup script, and a Windows Server guide.

> **Production note:** These files provide a production-oriented baseline, not a security certification or a fully managed hosting platform. Review the application, test the deployment on your own server, and add the monitoring, backups, capacity planning, and app-specific controls your service needs.

## Choose your platform

| Platform | Recommended stack | Setup | Notes |
| --- | --- | --- | --- |
| Ubuntu / Debian | Nginx + PHP-FPM + Tor | [`install.sh`](install.sh) | Automated installer for a dedicated Linux server. |
| Termux / Android | Nginx + PHP-FPM + Tor + `termux-services` | [`termux/setup.sh`](termux/setup.sh) | Works for experiments and personal hosting; Android may stop background processes. Not recommended for important 24/7 services. |
| Windows Server | IIS + PHP FastCGI + Tor Expert Bundle | [`WINDOWS.md`](WINDOWS.md) | Manual guide. Uses IIS rather than Nginx because Nginx for Windows is documented as beta with scalability limitations. |

## Ubuntu / Debian quick start

Copy the repository to the server, then run the installer with the website's **public/document root**:

```bash
sudo bash install.sh /path/to/site-public-directory
```

The installer installs Nginx, PHP-FPM, Tor, and rsync; configures Nginx to listen only on `127.0.0.1:8080`; creates a Tor v3 hidden service; and prints the `.onion` address. First-time Tor service publication can take a few minutes.

To view the resulting address later:

```bash
sudo cat /var/lib/tor/hostonion/hostname
```

## Termux quick start

Run inside Termux, using the public/document root of your site:

```bash
bash termux/setup.sh /path/to/site-public-directory
```

The script creates runit service definitions through `termux-services`. If this is your first install of `termux-services`, fully close and reopen Termux, then enable services one by one:

```bash
sv-enable hostonion-php-fpm
sv-enable hostonion-nginx
sv-enable hostonion-tor
cat ~/.hostonion/hidden_service/hostname
```

Android battery management, Doze, network changes, and process lifecycle can interrupt hosting. Exempt Termux from battery optimization and consider Termux:Boot for reboot startup, but treat a phone as a best-effort host—not a dependable 24/7 production server.

## Windows Server

Follow [`WINDOWS.md`](WINDOWS.md) for IIS + PHP FastCGI + Tor Expert Bundle setup. The web site should bind to `127.0.0.1:8080` only. Configure Tor to forward the onion's virtual port 80 to that local endpoint, then supervise Tor with an approved Windows service wrapper and a dedicated least-privileged account.

## How it works

```mermaid
flowchart LR
    V[Visitor using Tor Browser] -->|Onion connection| T[Tor v3 onion service]
    T -->|127.0.0.1:8080| W[Nginx or IIS loopback site]
    W -->|FastCGI| P[PHP-FPM or PHP FastCGI]
```

The web server port is local-only in the supplied configurations; Tor handles the onion endpoint. Onion services are not automatically private to selected visitors: anyone who knows the address can connect unless the application authenticates them or Tor client authorization is configured.

## Security essentials

- **Protect the onion identity.** The hidden-service directory contains private keys. Keep it private, back it up securely, and never commit or share it. Losing it can change the onion address.
- **Keep secrets outside the document root.** Do not publish `.env` files, credentials, source-control metadata, logs, database dumps, or backups.
- **Limit write access.** Give the PHP/IIS runtime write access only to specific upload, cache, or framework storage directories—not the whole site.
- **Patch and monitor.** Keep the OS, Tor, web server, PHP, and application dependencies updated. Monitor service health and logs; configure backups and test restoration.
- **Audit the application.** Tor does not prevent application vulnerabilities or data leaks. Review authentication, sessions, uploads, CSRF, XSS, SQL injection, outbound requests, analytics, and embedded resources.
- **Avoid identity leaks.** The onion address hides the network endpoint from ordinary visitors, but application content, external assets, DNS lookups, or operational mistakes can reveal information about the host.

## Deployment behavior

The Ubuntu/Debian installer copies new files into `/var/www/hostonion/public` but intentionally does not delete files that disappeared from the source. Review and remove stale files manually after backing them up. The Termux script serves the provided directory in place. The Windows guide is manual; it does not install Tor or PHP automatically.

The generic setup uses HTTP on the onion endpoint. Tor encrypts onion connections, but app-specific HTTPS, HSTS, secure-cookie, proxy, and framework settings should be chosen and tested for the application rather than imposed blindly.

## References

- [Tor Project: Set up your Onion Service](https://community.torproject.org/onion-services/setup/)
- [Termux Wiki: termux-services](https://wiki.termux.com/wiki/Termux-services)
- [Nginx for Windows: limitations](https://nginx.org/en/docs/windows.html)
- [Microsoft IIS FastCGI documentation](https://learn.microsoft.com/en-us/iis/configuration/system.webserver/fastcgi/)
- [PHP on Windows](https://www.php.net/manual/en/install.windows.php)
