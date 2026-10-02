#!/usr/bin/env python3
"""
HostOnion v3.2 - Host PHP sites on Tor
Author: Tasfia (github.com/Quincunx33)
"""

import os
import sys
import time
import signal
import socket
import shutil
import argparse
import subprocess
import logging
import hashlib
import threading
import re
import errno
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows compatibility
    fcntl = None

try:
    import tomllib
except ImportError:
    try:
        import tomli as tomllib  # type: ignore
    except ImportError:
        tomllib = None

try:
    import pyperclip
    HAS_CLIPBOARD = True
except ImportError:
    HAS_CLIPBOARD = False


BASE_DIR = Path(__file__).resolve().parent
TOR_DIR = BASE_DIR / "tor"
HIDDEN_SERVICE_DIR = TOR_DIR / "hidden_service"
HIDDEN_SERVICE_BACKUP = TOR_DIR / "hidden_service_backup"
BACKUP_MARKER = TOR_DIR / ".backup_done"
TORRC_PATH = TOR_DIR / "torrc"
LOG_FILE = TOR_DIR / "hostonion.log"
CONFIG_FILE = BASE_DIR / "hostonion.toml"

_runtime_dir = os.environ.get("XDG_RUNTIME_DIR")
if _runtime_dir and Path(_runtime_dir).is_dir():
    PID_FILE = Path(_runtime_dir) / "hostonion.pid"
else:
    _hash = hashlib.sha1(str(TOR_DIR).encode()).hexdigest()[:8]
    PID_FILE = Path(f"/tmp/hostonion-{os.getuid()}-{_hash}.pid")
LOCK_FILE = PID_FILE.with_suffix(".lock")

DEFAULT_PORT = 9000
BOOTSTRAP_TIMEOUT = 120
HOSTNAME_TIMEOUT = 60
HEALTHCHECK_TIMEOUT = 5
MIN_TOR_VERSION = (0, 4, 6)

DEFAULT_MAX_RESTARTS = 5
DEFAULT_RESTART_WINDOW = 60
BACKOFF_BASE = 1.0
BACKOFF_MAX = 30.0

VERSION = "3.3"

SITE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")

FORBIDDEN_PATHS = (
    Path("/etc"), Path("/sys"), Path("/proc"), Path("/boot"),
    Path("/dev"), Path("/root"), Path("/var/log"),
    Path("/usr"), Path("/bin"), Path("/sbin"), Path("/lib"),
)


log = logging.getLogger("hostonion")
log.setLevel(logging.DEBUG)
log.addHandler(logging.NullHandler())


def setup_logger(verbose: bool = False) -> None:
    TOR_DIR.mkdir(parents=True, exist_ok=True)
    for h in list(log.handlers):
        if not isinstance(h, logging.NullHandler):
            log.removeHandler(h)

    fh = logging.FileHandler(LOG_FILE, encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(logging.Formatter(
        "%(asctime)s [%(levelname)s] %(message)s", "%Y-%m-%d %H:%M:%S"
    ))
    log.addHandler(fh)

    if verbose:
        sh = logging.StreamHandler(sys.stdout)
        sh.setLevel(logging.DEBUG)
        sh.setFormatter(logging.Formatter("[%(levelname)s] %(message)s"))
        log.addHandler(sh)

    log.setLevel(logging.DEBUG if verbose else logging.INFO)


class C:
    GREEN = "\033[1;92m"
    BLUE = "\033[1;94m"
    YELLOW = "\033[1;93m"
    RED = "\033[1;91m"
    DIM = "\033[2m"
    RESET = "\033[0m"


def clear_screen() -> None:
    if not sys.stdout.isatty():
        return
    os.system("cls" if os.name == "nt" else "clear")


def banner() -> None:
    clear_screen()
    print(f"{C.GREEN}🧅 HostOnion v{VERSION} - PHP + Tor Hosting{C.RESET}\n")
    print(f"{C.BLUE}Author: Tasfia")
    print(f"{C.BLUE}GitHub: github.com/Quincunx33{C.RESET}\n")


@dataclass
class SiteConfig:
    name: str
    folder: Path
    port: Optional[int] = None
    php_proc: Optional[subprocess.Popen] = None
    supervisor: Optional["PHPProcessSupervisor"] = None

    @property
    def resolved_folder(self) -> Path:
        return Path(os.path.realpath(self.folder))


def load_config_file() -> dict:
    if not CONFIG_FILE.exists() or tomllib is None:
        return {}
    try:
        with open(CONFIG_FILE, "rb") as f:
            data = tomllib.load(f)
        log.info(f"Config loaded from {CONFIG_FILE}")
        return data
    except Exception as e:
        print(f"{C.YELLOW}[!] Config parse failed: {e}{C.RESET}")
        return {}


def _parse_version(s: str) -> tuple:
    parts = []
    for p in s.split(".")[:3]:
        digits = "".join(ch for ch in p if ch.isdigit())
        parts.append(int(digits) if digits else 0)
    while len(parts) < 3:
        parts.append(0)
    return tuple(parts)


def check_dependencies() -> None:
    missing = []
    if not shutil.which("php"):
        missing.append("php")
    if not shutil.which("tor"):
        missing.append("tor")
    if missing:
        print(f"{C.RED}[!] Missing dependencies: {', '.join(missing)}{C.RESET}")
        print(f"{C.DIM}    Install: sudo apt install {' '.join(missing)}{C.RESET}")
        sys.exit(1)

    try:
        out = subprocess.check_output(
            ["tor", "--version"], text=True, stderr=subprocess.STDOUT
        )
        m = re.search(r"version\s+(\d+\.\d+\.\d+)", out)
        if m:
            ver = _parse_version(m.group(1))
            if ver < MIN_TOR_VERSION:
                print(f"{C.RED}[!] Tor {m.group(1)} too old. "
                      f"Need >= {'.'.join(map(str, MIN_TOR_VERSION))}{C.RESET}")
                sys.exit(1)
    except Exception as e:
        log.warning(f"Tor version check failed: {e}")


def is_port_free(port: int) -> bool:
    s = socket.socket()
    try:
        s.bind(("127.0.0.1", port))
        return True
    except OSError:
        return False
    finally:
        s.close()


def get_free_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def validate_site_name(name: str) -> str:
    name = name.strip()
    if not SITE_NAME_RE.fullmatch(name):
        raise ValueError(
            f"Invalid site name {name!r}; use 1-64 letters, numbers, '_' or '-'"
        )
    return name


def validate_port(port: int) -> int:
    if not 1 <= port <= 65535:
        raise ValueError(f"Invalid port {port}; must be between 1 and 65535")
    return port


def safe_resolve_site(folder: Path) -> Path:
    real = Path(os.path.realpath(folder))
    if not real.exists():
        raise FileNotFoundError(f"Site folder not found: {folder}")
    if not real.is_dir():
        raise NotADirectoryError(f"Not a directory: {folder}")
    for f in FORBIDDEN_PATHS:
        if real == f or f in real.parents:
            raise PermissionError(f"Refusing to serve system path: {real}")
    home = Path.home().resolve()
    if real == home:
        raise PermissionError(f"Refusing to serve home root: {real}")
    return real


def copy_to_clipboard(text: str) -> None:
    if not HAS_CLIPBOARD:
        print(f"{C.DIM}[i] pyperclip not installed — URL not copied{C.RESET}")
        return
    try:
        pyperclip.copy(text)
        print(f"{C.DIM}[+] URL copied to clipboard{C.RESET}")
    except Exception as e:
        log.debug(f"Clipboard copy failed: {e}")
        print(f"{C.DIM}[i] Clipboard unavailable: {e}{C.RESET}")


def wait_for_http(host: str, port: int, timeout: int = HEALTHCHECK_TIMEOUT) -> bool:
    import urllib.request
    import urllib.error
    url = f"http://{host}:{port}/"
    start = time.time()
    while time.time() - start < timeout:
        try:
            with urllib.request.urlopen(url, timeout=1) as response:
                response.read(1)
            return True
        except urllib.error.HTTPError:
            # Server is up, just returned an error code
            return True
        except Exception:
            time.sleep(0.2)
    return False


class ProcessManager:
    @staticmethod
    def spawn(cmd: list, log_file=None) -> subprocess.Popen:
        stdout = subprocess.DEVNULL
        stderr = subprocess.DEVNULL
        if log_file is not None:
            stdout = log_file
            stderr = subprocess.STDOUT
        return subprocess.Popen(
            cmd, stdout=stdout, stderr=stderr, start_new_session=True,
        )

    @staticmethod
    def terminate(proc: Optional[subprocess.Popen], name: str = "proc") -> None:
        if proc is None or proc.poll() is not None:
            return
        try:
            pgid = os.getpgid(proc.pid)
            os.killpg(pgid, signal.SIGTERM)
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                os.killpg(pgid, signal.SIGKILL)
                try:
                    proc.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    pass
        except ProcessLookupError:
            pass
        except Exception as e:
            log.warning(f"Terminate {name} failed: {e}")


class PHPProcessSupervisor:
    def __init__(
        self,
        site: SiteConfig,
        verbose: bool = False,
        max_restarts: int = DEFAULT_MAX_RESTARTS,
        restart_window: int = DEFAULT_RESTART_WINDOW,
        enabled: bool = True,
    ):
        self.site = site
        self.verbose = verbose
        self.max_restarts = max_restarts
        self.restart_window = restart_window
        self.enabled = enabled

        self._proc: Optional[subprocess.Popen] = None
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._lock = threading.Lock()

        self._restart_times: deque = deque()
        self._backoff = BACKOFF_BASE
        self._php_log_file = None

    def start(self) -> bool:
        with self._lock:
            ok = self._spawn()
        if not ok:
            return False
        if self.enabled:
            self._thread = threading.Thread(
                target=self._supervise_loop,
                name=f"php-sup-{self.site.name}",
                daemon=True,
            )
            self._thread.start()
        return True

    def stop(self, timeout: float = 5.0) -> None:
        self._stop_event.set()
        with self._lock:
            proc = self._proc
            self._proc = None
        ProcessManager.terminate(proc, f"php[{self.site.name}]")
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=timeout)

        if self._php_log_file is not None:
            try:
                self._php_log_file.close()
            except Exception:
                pass
            self._php_log_file = None

    @property
    def proc(self) -> Optional[subprocess.Popen]:
        with self._lock:
            return self._proc

    def _spawn(self) -> bool:
        if self.verbose and self._php_log_file is None:
            try:
                self._php_log_file = open(
                    TOR_DIR / f"php_{self.site.name}.log", "ab"
                )
                try:
                    os.chmod(TOR_DIR / f"php_{self.site.name}.log", 0o600)
                except OSError:
                    pass
            except Exception as e:
                log.warning(
                    f"Could not open PHP log for {self.site.name}: {e}"
                )
                self._php_log_file = None

        try:
            self._proc = ProcessManager.spawn(
                ["php", "-S", f"127.0.0.1:{self.site.port}",
                 "-t", str(self.site.resolved_folder)],
                log_file=self._php_log_file,
            )
            self.site.php_proc = self._proc
            log.info(
                f"PHP [{self.site.name}] spawned "
                f"(pid={self._proc.pid}) on port {self.site.port}"
            )
            return True
        except FileNotFoundError:
            log.error(f"php binary not found for site {self.site.name}")
            return False
        except Exception as e:
            log.error(f"Failed to spawn PHP [{self.site.name}]: {e}")
            return False

    def _prune_restart_history(self) -> None:
        now = time.time()
        while self._restart_times and \
                (now - self._restart_times[0]) > self.restart_window:
            self._restart_times.popleft()

    def _supervise_loop(self) -> None:
        while not self._stop_event.is_set():
            with self._lock:
                proc = self._proc

            if proc is None:
                break

            # Wait for process to exit
            while not self._stop_event.is_set():
                if proc.poll() is not None:
                    break
                time.sleep(0.5)

            if self._stop_event.is_set():
                break

            rc = proc.returncode
            log.warning(f"PHP [{self.site.name}] exited (code={rc})")
            print(
                f"{C.YELLOW}[!] PHP [{self.site.name}] crashed "
                f"(code={rc}), restarting...{C.RESET}"
            )

            self._prune_restart_history()
            if len(self._restart_times) >= self.max_restarts:
                msg = (f"PHP [{self.site.name}] exceeded {self.max_restarts} "
                       f"restarts within {self.restart_window}s. Giving up.")
                log.error(msg)
                print(f"{C.RED}[!] {msg}{C.RESET}")
                return

            delay = min(self._backoff, BACKOFF_MAX)
            log.info(f"Backoff {delay:.1f}s before restarting {self.site.name}")
            if self._stop_event.wait(timeout=delay):
                break

            with self._lock:
                ok = self._spawn()
                if ok:
                    self._restart_times.append(time.time())

            if not ok:
                log.error(
                    f"Respawn failed for {self.site.name}. Stopping supervisor."
                )
                return

            self._backoff = min(self._backoff * 2, BACKOFF_MAX)

            # Reset backoff if process survives a full window
            if self._wait_stable(proc_timeout=self.restart_window * 2):
                self._backoff = BACKOFF_BASE
                log.info(f"PHP [{self.site.name}] stable; backoff reset")

    def _wait_stable(self, proc_timeout: float) -> bool:
        start = time.time()
        while time.time() - start < proc_timeout:
            if self._stop_event.is_set():
                return False
            with self._lock:
                proc = self._proc
            if proc is None or proc.poll() is not None:
                return False
            time.sleep(0.5)
        return True


def harden_private_tree(root: Path) -> None:
    """Restrict hidden-service directories and key files to the current user."""
    if not root.exists():
        return
    for path in [root, *root.rglob("*")]:
        try:
            os.chmod(path, 0o700 if path.is_dir() else 0o600)
        except OSError as exc:
            log.warning("Could not harden permissions for %s: %s", path, exc)


def write_torrc_for_multisite(sites: list, control_port: bool = False) -> None:
    content = [
        "SocksPort 0",
        f"Log notice file {TOR_DIR / 'tor.log'}",
    ]
    if control_port:
        content.append("ControlPort 9051")
        content.append("CookieAuthentication 1")

    for site in sites:
        hs_dir = HIDDEN_SERVICE_DIR / site.name
        hs_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        harden_private_tree(hs_dir)
        content.append(f"HiddenServiceDir {hs_dir}")
        content.append("HiddenServiceVersion 3")
        content.append(f"HiddenServicePort 80 127.0.0.1:{site.port}")

    TORRC_PATH.write_text("\n".join(content) + "\n")
    try:
        os.chmod(TORRC_PATH, 0o600)
    except OSError:
        pass


def wait_for_hostname(hs_dir: Path, timeout: int = HOSTNAME_TIMEOUT) -> str:
    hostname_file = hs_dir / "hostname"
    start = time.time()
    while time.time() - start < timeout:
        if hostname_file.exists():
            content = hostname_file.read_text().strip()
            if content.endswith(".onion"):
                return content
        time.sleep(0.5)
    raise TimeoutError(f"Tor did not generate hostname in {timeout}s")


def wait_for_bootstrap(timeout: int = BOOTSTRAP_TIMEOUT) -> bool:
    tor_log = TOR_DIR / "tor.log"
    start = time.time()
    last_pct = -1
    while time.time() - start < timeout:
        if tor_log.exists():
            try:
                with open(tor_log, "r", errors="ignore") as f:
                    lines = f.readlines()
                for line in reversed(lines):
                    m = re.search(r"Bootstrapped (\d+)%", line)
                    if m:
                        pct = int(m.group(1))
                        if pct != last_pct:
                            print(f"{C.DIM}[~] Tor bootstrap: {pct}%{C.RESET}")
                            last_pct = pct
                        if pct == 100:
                            return True
                        break
            except Exception:
                pass
        time.sleep(1)
    return False


def start_tor_service(
    sites: list,
    reset: bool = False,
    reset_sites: Optional[set] = None,
    verbose: bool = False,
    force: bool = False,
) -> tuple:
    reset_sites = reset_sites or set()

    if reset and HIDDEN_SERVICE_DIR.exists():
        print(f"{C.YELLOW}[!] Deleting ALL onion identities...{C.RESET}")
        shutil.rmtree(HIDDEN_SERVICE_DIR)
        BACKUP_MARKER.unlink(missing_ok=True)
        if HIDDEN_SERVICE_BACKUP.exists():
            shutil.rmtree(HIDDEN_SERVICE_BACKUP)

    for name in reset_sites:
        target = HIDDEN_SERVICE_DIR / name
        if target.exists():
            print(f"{C.YELLOW}[!] Resetting onion for site: {name}{C.RESET}")
            shutil.rmtree(target)

    HIDDEN_SERVICE_DIR.mkdir(parents=True, exist_ok=True)

    if not BACKUP_MARKER.exists() and \
            HIDDEN_SERVICE_DIR.exists() and \
            any(HIDDEN_SERVICE_DIR.iterdir()):
        try:
            if HIDDEN_SERVICE_BACKUP.exists():
                shutil.rmtree(HIDDEN_SERVICE_BACKUP)
            shutil.copytree(HIDDEN_SERVICE_DIR, HIDDEN_SERVICE_BACKUP)
            BACKUP_MARKER.touch()
            log.info("Backup created (first-run marker set)")
        except Exception as e:
            log.warning(f"Backup failed: {e}")

    write_torrc_for_multisite(sites)

    print(f"{C.GREEN}[+] Starting Tor hidden service...{C.RESET}")
    log_file = None
    if verbose:
        log_file = open(TOR_DIR / "tor_stdout.log", "ab")
        try:
            os.chmod(TOR_DIR / "tor_stdout.log", 0o600)
        except OSError:
            pass
    Runtime.tor_log_file = log_file

    tor_proc = ProcessManager.spawn(
        ["tor", "-f", str(TORRC_PATH)],
        log_file=log_file,
    )

    try:
        boot_ok = wait_for_bootstrap()
        if not boot_ok:
            msg = "Tor bootstrap timeout"
            if not force:
                print(f"{C.RED}[!] {msg}. Aborting (use --force to continue){C.RESET}")
                raise RuntimeError(msg)
            print(f"{C.YELLOW}[!] {msg} — continuing due to --force{C.RESET}")

        onions = {}
        for site in sites:
            hs_dir = HIDDEN_SERVICE_DIR / site.name
            try:
                onion = wait_for_hostname(hs_dir)
                onions[site.name] = onion
            except TimeoutError as e:
                log.error(f"Hostname timeout for {site.name}: {e}")
                onions[site.name] = None
        return onions, tor_proc
    except Exception:
        ProcessManager.terminate(tor_proc, "tor")
        if log_file is not None:
            try:
                log_file.close()
            except Exception:
                pass
            Runtime.tor_log_file = None
        raise


def acquire_instance_lock() -> None:
    """Prevent two HostOnion instances from sharing the same runtime files."""
    LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    lock = open(LOCK_FILE, "a+")
    if fcntl is not None:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            lock.close()
            if exc.errno in (errno.EACCES, errno.EAGAIN):
                raise RuntimeError(
                    f"Another HostOnion instance is already running (lock: {LOCK_FILE})"
                )
            raise
    Runtime.instance_lock = lock


def write_pid_file() -> None:
    PID_FILE.parent.mkdir(parents=True, exist_ok=True)
    PID_FILE.write_text(str(os.getpid()), encoding="ascii")
    try:
        os.chmod(PID_FILE, 0o600)
    except OSError:
        pass


def remove_pid_file() -> None:
    try:
        PID_FILE.unlink(missing_ok=True)
    except Exception:
        pass
    if Runtime.instance_lock is not None:
        try:
            if fcntl is not None:
                fcntl.flock(Runtime.instance_lock.fileno(), fcntl.LOCK_UN)
            Runtime.instance_lock.close()
        except Exception:
            pass
        Runtime.instance_lock = None


def show_status() -> None:
    if not PID_FILE.exists():
        print(f"{C.YELLOW}[!] HostOnion not running (no PID file at {PID_FILE}).{C.RESET}")
        return
    try:
        pid = int(PID_FILE.read_text().strip())
        os.kill(pid, 0)
    except (ValueError, ProcessLookupError):
        print(f"{C.RED}[!] Stale PID file. Process not running.{C.RESET}")
        return
    except PermissionError:
        pass

    print(f"{C.GREEN}[+] HostOnion running (PID: {pid}){C.RESET}")
    if HIDDEN_SERVICE_DIR.exists():
        for sub in HIDDEN_SERVICE_DIR.iterdir():
            if sub.is_dir():
                hn = sub / "hostname"
                if hn.exists():
                    print(f"    {sub.name}: http://{hn.read_text().strip()}")


class Runtime:
    shutdown_requested = False
    sites: list = []
    tor_proc: Optional[subprocess.Popen] = None
    tor_log_file = None
    instance_lock = None


_cleanup_done = False


def cleanup() -> None:
    global _cleanup_done
    if _cleanup_done:
        return
    _cleanup_done = True

    print(f"\n{C.YELLOW}[!] Stopping services...{C.RESET}")
    log.info("Shutdown initiated")

    for site in Runtime.sites:
        if site.supervisor:
            try:
                site.supervisor.stop(timeout=5.0)
            except Exception as e:
                log.warning(f"Supervisor stop failed for {site.name}: {e}")

    # Terminate any lingering PHP processes (supervisor may have missed)
    for site in Runtime.sites:
        ProcessManager.terminate(site.php_proc, f"php[{site.name}]")

    ProcessManager.terminate(Runtime.tor_proc, "tor")
    if Runtime.tor_log_file is not None:
        try:
            Runtime.tor_log_file.close()
        except Exception:
            pass
        Runtime.tor_log_file = None

    remove_pid_file()
    print(f"{C.GREEN}[+] Stopped cleanly.{C.RESET}")


def install_signal_handlers() -> None:
    def handler(signum, _frame):
        log.info(f"Signal {signum} received; shutting down")
        Runtime.shutdown_requested = True

    signal.signal(signal.SIGINT, handler)
    signal.signal(signal.SIGTERM, handler)


def build_argparser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="hostonion",
        description="HostOnion - Host PHP site(s) on Tor",
    )
    p.add_argument("site_folder", nargs="?", help="Folder containing site files")
    p.add_argument("-p", "--port", type=int, help="PHP port")
    p.add_argument("--new", action="store_true",
                   help="Generate new onion address(es)")
    p.add_argument("--new-site", action="append", default=[],
                   help="Reset onion for a specific site name (repeatable)")
    p.add_argument("--verbose", "-v", action="store_true", help="Verbose logs")
    p.add_argument("--copy", action="store_true",
                   help="Copy onion URL to clipboard")
    p.add_argument("--status", action="store_true", help="Show running status")
    p.add_argument("--force", action="store_true",
                   help="Continue even if Tor bootstrap fails")
    p.add_argument("--restore", action="store_true",
                   help="Restore the saved hidden-service backup and exit")
    p.add_argument("--site", action="append", default=[],
                   help="Multi-site: name=path (repeatable)")
    p.add_argument("--no-restart", action="store_true",
                   help="Disable PHP auto-restart on crash")
    p.add_argument("--max-restarts", type=int, default=DEFAULT_MAX_RESTARTS,
                   help=f"Max restarts per window (default: {DEFAULT_MAX_RESTARTS})")
    p.add_argument("--restart-window", type=int, default=DEFAULT_RESTART_WINDOW,
                   help=f"Restart window in seconds (default: {DEFAULT_RESTART_WINDOW})")
    p.add_argument("--version", action="version", version=f"HostOnion {VERSION}")
    return p


def build_sites(args, cfg_file: dict) -> list:
    sites = []
    for entry in args.site:
        if "=" not in entry:
            print(f"{C.RED}[!] Invalid --site format: {entry} (use name=path){C.RESET}")
            sys.exit(1)
        name, path = entry.split("=", 1)
        try:
            name = validate_site_name(name)
        except ValueError as exc:
            print(f"{C.RED}[!] {exc}{C.RESET}")
            sys.exit(1)
        sites.append(SiteConfig(name=name, folder=Path(path.strip())))

    if args.site_folder:
        if sites:
            print(f"{C.YELLOW}[!] Both --site and positional given; ignoring positional{C.RESET}")
        else:
            sites.append(SiteConfig(name="default", folder=Path(args.site_folder)))

    if not sites and "sites" in cfg_file:
        for name, info in cfg_file["sites"].items():
            sites.append(SiteConfig(
                name=validate_site_name(name),
                folder=Path(info["path"]),
                port=info.get("port"),
            ))

    if not sites:
        print(f"{C.RED}[!] No site specified. "
              f"Use: hostonion <folder> or --site name=path{C.RESET}")
        sys.exit(1)

    names = [s.name for s in sites]
    if len(names) != len(set(names)):
        print(f"{C.RED}[!] Duplicate site names detected{C.RESET}")
        sys.exit(1)

    return sites


def assign_ports(sites: list, cli_port: Optional[int]) -> None:
    used = set()
    for i, site in enumerate(sites):
        if site.port is not None:
            try:
                site.port = validate_port(int(site.port))
            except (TypeError, ValueError) as exc:
                raise ValueError(f"[{site.name}] {exc}") from exc
            if not is_port_free(site.port):
                print(f"{C.YELLOW}[!] Configured port {site.port} for "
                      f"[{site.name}] is busy; picking a free one{C.RESET}")
            elif site.port in used:
                print(f"{C.YELLOW}[!] Configured port {site.port} for "
                      f"[{site.name}] already used; picking a free one{C.RESET}")
            else:
                used.add(site.port)
                continue

        preferred = cli_port if (i == 0 and cli_port is not None) else DEFAULT_PORT
        validate_port(preferred)
        if preferred in used or not is_port_free(preferred):
            new_port = get_free_port()
            print(f"{C.YELLOW}[!] Port {preferred} busy → using {new_port}{C.RESET}")
            site.port = new_port
        else:
            site.port = preferred
        used.add(site.port)


def main():
    parser = build_argparser()
    args = parser.parse_args()

    cfg_file = load_config_file()
    setup_logger(verbose=args.verbose or cfg_file.get("verbose", False))

    banner()

    if args.status:
        show_status()
        return

    if args.restore:
        if not HIDDEN_SERVICE_BACKUP.exists():
            print(f"{C.RED}[!] No hidden-service backup found at {HIDDEN_SERVICE_BACKUP}{C.RESET}")
            return
        if HIDDEN_SERVICE_DIR.exists():
            shutil.rmtree(HIDDEN_SERVICE_DIR)
        shutil.copytree(HIDDEN_SERVICE_BACKUP, HIDDEN_SERVICE_DIR)
        harden_private_tree(HIDDEN_SERVICE_DIR)
        print(f"{C.GREEN}[+] Hidden-service backup restored.{C.RESET}")
        return

    check_dependencies()

    if args.max_restarts < 0 or args.restart_window <= 0:
        parser.error("--max-restarts must be >= 0 and --restart-window must be > 0")
    if args.port is not None:
        try:
            validate_port(args.port)
        except ValueError as exc:
            parser.error(str(exc))

    sites = build_sites(args, cfg_file)
    for site in sites:
        try:
            site.folder = safe_resolve_site(site.folder)
        except (FileNotFoundError, NotADirectoryError, PermissionError) as e:
            print(f"{C.RED}[!] {e}{C.RESET}")
            sys.exit(1)

    try:
        assign_ports(sites, args.port)
    except ValueError as exc:
        print(f"{C.RED}[!] {exc}{C.RESET}")
        sys.exit(1)

    try:
        reset_sites = {validate_site_name(name) for name in args.new_site}
    except ValueError as exc:
        print(f"{C.RED}[!] {exc}{C.RESET}")
        sys.exit(1)

    try:
        acquire_instance_lock()
        write_pid_file()
    except RuntimeError as exc:
        print(f"{C.RED}[!] {exc}{C.RESET}")
        sys.exit(1)

    Runtime.sites = sites
    install_signal_handlers()

    restart_enabled = not args.no_restart

    try:
        for site in sites:
            sup = PHPProcessSupervisor(
                site,
                verbose=args.verbose,
                max_restarts=args.max_restarts,
                restart_window=args.restart_window,
                enabled=restart_enabled,
            )
            if not sup.start():
                print(f"{C.RED}[!] Failed to start PHP for [{site.name}]{C.RESET}")
                raise RuntimeError(f"PHP start failed: {site.name}")
            site.supervisor = sup

            if not wait_for_http("127.0.0.1", site.port):
                log.warning(
                    f"PHP [{site.name}] did not respond to initial health check"
                )

        onions, tor_proc = start_tor_service(
            sites,
            reset=args.new or cfg_file.get("new", False),
            reset_sites=reset_sites,
            verbose=args.verbose,
            force=args.force,
        )
        Runtime.tor_proc = tor_proc

        print()
        print(f"{C.GREEN}{'─' * 55}{C.RESET}")
        for site in sites:
            onion = onions.get(site.name)
            if onion:
                url = f"http://{onion}"
                print(f"{C.GREEN}[+] {site.name:12} → {url}{C.RESET}")
                print(f"{C.DIM}    PHP port: {site.port}{C.RESET}")
                if args.copy or cfg_file.get("copy", False):
                    copy_to_clipboard(url)
            else:
                print(f"{C.RED}[!] {site.name}: onion failed{C.RESET}")
        print(f"{C.GREEN}{'─' * 55}{C.RESET}")

        restart_status = "enabled" if restart_enabled else "disabled"
        print(f"{C.DIM}[+] PHP auto-restart: {restart_status} "
              f"(max {args.max_restarts}/{args.restart_window}s){C.RESET}")
        print(f"{C.DIM}[+] Press CTRL+C to stop.{C.RESET}\n")

        log.info(f"All services running. Onions: {onions}")

        while not Runtime.shutdown_requested:
            if Runtime.tor_proc is not None and Runtime.tor_proc.poll() is not None:
                raise RuntimeError(
                    f"Tor exited unexpectedly (code={Runtime.tor_proc.returncode})"
                )
            dead_sites = [
                site.name for site in sites
                if site.supervisor and site.supervisor.proc is not None
                and site.supervisor.proc.poll() is not None
            ]
            if dead_sites and restart_enabled:
                log.warning("PHP supervisor stopped for: %s", ", ".join(dead_sites))
            time.sleep(0.5)

        cleanup()

    except KeyboardInterrupt:
        Runtime.shutdown_requested = True
        cleanup()
    except Exception as e:
        log.exception("Fatal error")
        print(f"{C.RED}[!] Fatal: {e}{C.RESET}")
        cleanup()
        sys.exit(1)


if __name__ == "__main__":
    main()
