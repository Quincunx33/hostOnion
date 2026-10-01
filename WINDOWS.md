# Windows setup (IIS + PHP FastCGI + Tor)

Windows can host an onion site, but its production setup is not the same as Ubuntu. **For Windows Server, use IIS + PHP FastCGI + the Tor Expert Bundle.** Avoid using Nginx for Windows as a production web server: the Nginx project labels its Windows build beta and documents limited scalability and only one worker doing useful work.

This guide is a manual setup, not a one-click installer. It assumes Windows Server with IIS. Windows 10/11 can be used for testing, but a desktop PC that sleeps or shuts down is not a dependable always-on server.

## 1. Install IIS with CGI/FastCGI

In an elevated PowerShell window, install IIS and the CGI role service (Windows Server):

```powershell
Install-WindowsFeature Web-Server, Web-CGI -IncludeManagementTools
```

On client Windows editions, enable **Internet Information Services → World Wide Web Services → Application Development Features → CGI** in “Turn Windows features on or off”. Follow Microsoft's IIS FastCGI setup if the UI differs by Windows version.

## 2. Install PHP

1. Download the official supported Windows PHP release from [php.net](https://www.php.net/downloads.php). For IIS FastCGI, use a compatible **Non-Thread-Safe (NTS)** build and install the required Microsoft Visual C++ runtime stated on the PHP download page.
2. Extract to a stable path such as `C:\PHP` and create/configure `C:\PHP\php.ini` from the production template.
3. In IIS Manager, add a FastCGI application for `C:\PHP\php-cgi.exe` and a handler mapping for `*.php` using `FastCgiModule`. Microsoft documents these exact settings in [IIS FastCGI configuration](https://learn.microsoft.com/en-us/iis/configuration/system.webserver/fastcgi/).
4. Set the app pool to **No Managed Code**. Grant the IIS application-pool identity read/execute access to PHP and the site. Grant write access only to specific upload/cache directories.

## 3. Create an IIS site bound only to loopback

Use IIS Manager to create a site with:

- Physical path: the site's public/document root (not necessarily the project root)
- Binding: `http`, IP address `127.0.0.1`, port `8080`, no public-IP binding
- Application pool: dedicated pool, **No Managed Code**

Confirm the site is reachable locally at `http://127.0.0.1:8080/` before adding Tor. Add IIS request filtering to deny dotfiles and sensitive files such as `.env`, source-control metadata, logs, backups and database dumps. Keep secrets outside the document root.

## 4. Configure Tor onion service

1. Download the **Tor Expert Bundle** from the [Tor Project](https://www.torproject.org/download/). Do not use Tor Browser's browser process as the server daemon.
2. Extract it to a stable path, for example `C:\Tor`. Create a private data directory, for example `C:\ProgramData\HostOnion\TorData`, and a separate service identity directory `C:\ProgramData\HostOnion\HiddenService`.
3. Restrict the HiddenService directory ACL so only the Tor service account and Administrators can read it. It contains private onion-service keys.
4. Create `C:\Tor\torrc` with paths adjusted to the extracted bundle:

```text
DataDirectory C:/ProgramData/HostOnion/TorData
GeoIPFile C:/Tor/Data/geoip
GeoIPv6File C:/Tor/Data/geoip6
Log notice file C:/ProgramData/HostOnion/tor-notice.log
SocksPort 0
HiddenServiceDir C:/ProgramData/HostOnion/HiddenService
HiddenServiceVersion 3
HiddenServicePort 80 127.0.0.1:8080
```

Check the Expert Bundle's actual `geoip` and `geoip6` locations; update those paths if needed. Verify the torrc using the bundle's `tor.exe` before starting it:

```powershell
C:\Tor\tor.exe --verify-config -f C:\Tor\torrc
```

5. Run Tor under a dedicated, least-privileged Windows account and supervise it as a **Windows service** using an approved service wrapper/organization standard. Configure automatic start and recovery on failure. Do not rely on an interactive terminal window for an always-on service. Validate that the Tor process can write its DataDirectory and HiddenServiceDir.
6. Read `C:\ProgramData\HostOnion\HiddenService\hostname` after Tor starts. Test the address from Tor Browser.

## 5. Production operations

- Restrict Windows Firewall and IIS bindings so the site is reachable only on loopback; Tor does not require exposing the web port to the public Internet.
- Keep Windows, IIS, PHP, Tor, and application dependencies patched. Back up the application, configuration, and onion keys securely; test restores.
- Monitor IIS, PHP, and Tor logs and configure log rotation/retention. Set explicit request, upload, worker and timeout limits for your app.
- Do not grant the IIS identity broad write access. Store application secrets outside the web root.
- Test the actual app, database, session behavior, uploads, and restart/recovery before relying on it.

## Platform caveat

The [Nginx for Windows documentation](https://nginx.org/en/docs/windows.html) calls its Windows build beta, notes that high performance/scalability should not be expected, and says only one worker does useful work. That's why this Windows setup uses IIS rather than promising production Nginx on Windows.
