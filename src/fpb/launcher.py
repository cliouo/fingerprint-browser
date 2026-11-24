"""Turning a profile into a running Camoufox window."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
import warnings
from typing import Any, Callable, Dict, List, Optional

from . import identity as ident
from .extensions import validate_extension
from .proxy import redact
from .store import Profile, Store, now_iso

READY_TIMEOUT = 180.0
EMPTY_WINDOW_GRACE = 3.0

# Camoufox turns canvas readback noise on by design whenever the identity
# claims another OS than the host (a Windows identity on a Mac), and warns on
# every launch. It is the intended setup here, so the warning is noise.
warnings.filterwarnings("ignore", message=r"Canvas and WebGL readback return random data")


class LaunchError(Exception):
    pass


def require_browser():
    """The installed Camoufox build this camoufox package will launch.

    Without this, Camoufox downloads its paired build (about 1.3 GB) in the
    middle of `new`/`check`/`open` and makes it the active one, without the
    macOS font fix. fpb asks for an explicit fetch instead.
    """
    from .doctor import find_browser

    install = find_browser()
    if install is None:
        raise LaunchError(
            "the Camoufox browser this camoufox package pairs with is not installed: "
            "run `uv run camoufox fetch`, then `fpb doctor --fix-fonts` (macOS)"
        )
    return install


def check_browser_for(profile: Profile) -> None:
    from . import doctor

    install = require_browser()
    if doctor.is_macos():
        try:
            if doctor.font_state(install.fonts_dir, install.backup_dir).needs_fix:
                print(f"[fpb] WARNING: browser {install.version} still bundles the hidden macOS fonts "
                      "(garbled UI text on macOS 27): run `fpb doctor --fix-fonts`", file=sys.stderr, flush=True)
        except OSError:
            pass


class RedactingStream:
    """Wraps stdout/stderr so a proxy password can never be printed."""

    def __init__(self, stream, secrets: List[str]):
        self._stream = stream
        self._secrets = [s for s in secrets if s]

    def write(self, text):
        return self._stream.write(redact(text, self._secrets) if self._secrets else text)

    def __getattr__(self, name):
        return getattr(self._stream, name)


def install_redaction(profile: Profile) -> None:
    secrets = profile.secrets()
    if not secrets:
        return
    if not isinstance(sys.stdout, RedactingStream):
        sys.stdout = RedactingStream(sys.stdout, secrets)
    if not isinstance(sys.stderr, RedactingStream):
        sys.stderr = RedactingStream(sys.stderr, secrets)


def launch_kwargs(store: Store, profile: Profile, *, geoip: Any = True, headless: bool = False) -> Dict[str, Any]:
    """The arguments fpb always passes to Camoufox for this profile."""
    identity = profile.identity or {}
    if not identity.get("preset"):
        raise LaunchError(f"profile {profile.name!r} has no saved identity")
    addons = [validate_extension(p, allow_no_gecko_id=True).path for p in profile.extensions]
    kwargs: Dict[str, Any] = {
        "os": identity.get("os", ident.OS),
        "locale": identity.get("locale", ident.LOCALE),  # never let geoip pick the language
        "geoip": geoip,
        "fingerprint_preset": identity["preset"],
        "addons": addons,
        "headless": headless,
        "user_data_dir": str(store.user_data_dir(profile.name)),
        # The page follows the real window instead of Playwright's fixed
        # 1280x720 viewport (which ignores resizing and can exceed the screen).
        "no_viewport": True,
        # Camoufox blocks tabs an add-on opens (chrome.tabs.create) unless this
        # is set, which silently breaks extensions that open a tab to work in.
        "allow_addon_new_tab": True,
    }
    proxy = profile.proxy_obj()
    if proxy:
        kwargs["proxy"] = proxy.to_playwright()
        if profile.bypass:
            # Only the browser's own requests to these hosts go direct; the
            # geoip lookup (exit IP, timezone, WebRTC IP) still uses the proxy.
            kwargs["proxy"]["bypass"] = ",".join(profile.bypass)
    return kwargs


def compute_options(store: Store, profile: Profile, *, geoip: Any = True, headless: bool = False) -> Dict[str, Any]:
    """Camoufox's launch_options() for the profile (does the geoip lookup)."""
    from camoufox.utils import launch_options

    require_browser()
    try:
        return launch_options(**launch_kwargs(store, profile, geoip=geoip, headless=headless))
    except LaunchError:
        raise
    except Exception as exc:  # proxy unreachable, geoip lookup failed, ...
        msg = redact(f"{type(exc).__name__}: {exc}", profile.secrets())
        raise LaunchError(f"could not prepare the launch: {msg}") from None


def identity_report(profile: Profile, config: Dict[str, Any]) -> List[str]:
    """Warnings if the identity Camoufox will present differs from the saved one."""
    if not profile.identity_digest:
        return []
    if ident.digest(config) == profile.identity_digest:
        return []
    changes = ident.summary_changes(profile.identity_summary or {}, ident.summarize(config))
    lines = ["WARNING: this launch's identity differs from the one saved at `fpb new`:"]
    for key, old, new in changes:
        lines.append(f"  {key}: {old!r} -> {new!r}")
    if not changes:
        lines.append("  (only fields outside the summary changed)")
    lines.append("  Usual causes: a Camoufox upgrade, or a smaller display than at `fpb new`.")
    lines.append(f"  If expected (e.g. after upgrading Camoufox), make it the reference: fpb accept-identity {profile.name}")
    return lines


def browser_pid(user_data_dir: str) -> Optional[int]:
    """The main browser process using this profile directory (Playwright passes `-profile DIR`)."""
    try:
        out = subprocess.run(["ps", "-axo", "pid=,command="], capture_output=True, text=True, timeout=5).stdout
    except Exception:
        return None
    needle = f"-profile {user_data_dir} "
    for line in out.splitlines():
        pid, _, command = line.strip().partition(" ")
        if needle in command and "-contentproc" not in command and "plugin-container" not in command:
            try:
                return int(pid)
            except ValueError:
                continue
    return None


def macos_window_count(pid: int) -> Optional[int]:
    """On-screen normal windows (layer 0) owned by `pid`, or None if unknown.

    Off-screen windows are not counted: Firefox keeps a hidden 500x500 window
    off screen for as long as it runs (browser 158).
    """
    try:
        import Quartz
    except Exception:
        return None
    infos = Quartz.CGWindowListCopyWindowInfo(Quartz.kCGWindowListOptionOnScreenOnly, Quartz.kCGNullWindowID) or []
    count = 0
    for w in infos:
        if w.get("kCGWindowOwnerPID") != pid or w.get("kCGWindowLayer") != 0:
            continue
        bounds = w.get("kCGWindowBounds") or {}
        if bounds.get("Width", 0) >= 100 and bounds.get("Height", 0) >= 100:
            count += 1
    return count


def _window_gone(context, pid: Optional[int]) -> bool:
    """macOS only: the browser keeps running with no window, so has the person closed it?

    Closed = no window on screen and no page Playwright tracks. A window on
    screen keeps the session whatever Playwright's page list says (tabs an
    extension opens and closes); a minimized window, or one on another Space,
    is off screen but still has its pages. Without the window list (no
    Quartz), only the page list is left.
    """
    if pid is not None:
        count = macos_window_count(pid)
        if count is not None:
            return count == 0 and not context.pages
    return not context.pages


def _wait_until_closed(context, stop: Dict[str, bool], user_data_dir: Optional[str] = None) -> None:
    """Block until the person closes the browser.

    Linux/Windows: Firefox quits when its last window closes, which closes the
    context; that is the only signal used (never a count of Playwright pages:
    on Linux an extension opening and closing its own tabs ended a window that was still open).
    macOS: Firefox stays running without windows, so the on-screen window
    list is checked together with the page list (_window_gone).
    """
    from playwright.sync_api import Error as PlaywrightError
    from playwright.sync_api import TimeoutError as PlaywrightTimeout

    closed = {"v": False}
    context.on("close", lambda *_: closed.__setitem__("v", True))
    mac = sys.platform == "darwin"
    pid = browser_pid(user_data_dir) if (mac and user_data_dir) else None
    empty_since: Optional[float] = None
    while not closed["v"] and not stop["v"]:
        try:
            context.wait_for_event("close", timeout=1000)
            return
        except PlaywrightTimeout:
            pass
        except PlaywrightError:
            return  # browser went away
        if not mac:
            continue
        if not _window_gone(context, pid):
            empty_since = None
        else:
            empty_since = empty_since or time.monotonic()
            if time.monotonic() - empty_since > EMPTY_WINDOW_GRACE:
                return


def run_open(store: Store, profile: Profile, url: Optional[str] = None,
             on_ready: Optional[Callable[[Any], None]] = None, keep_open: bool = True) -> None:
    """Open the profile's window and block until it is closed.

    on_ready(context) runs once the browser is up (tests and scripts use it);
    with keep_open=False the window is closed after on_ready returns.
    """
    from camoufox.sync_api import NewBrowser
    from playwright.sync_api import sync_playwright

    name = profile.name
    pid = store.running_pid(name)
    if pid:
        raise LaunchError(f"profile {name!r} is already open (pid {pid}); `fpb close {name}` to close it")

    stop = {"v": False}

    def _stop(signum, _frame):
        stop["v"] = True

    previous = {s: signal.signal(s, _stop) for s in (signal.SIGTERM, signal.SIGHUP)}
    try:
        print(f"[fpb] {name}: preparing (proxy {profile.proxy_masked()}, geoip lookup through it)...", flush=True)
        options = compute_options(store, profile)
        config = ident.config_from_options(options)
        for line in identity_report(profile, config):
            print(line, file=sys.stderr, flush=True)
        print(
            f"[fpb] {name}: exit IP {config.get('webrtc:ipv4') or config.get('webrtc:ipv6') or '?'}, "
            f"timezone {config.get('timezone', '?')}, locale "
            f"{config.get('locale:language', '?')}-{config.get('locale:region', '?')}",
            flush=True,
        )
        with sync_playwright() as pw:
            try:
                context = NewBrowser(pw, from_options=options, persistent_context=True)
            except Exception as exc:
                raise LaunchError(redact(f"browser failed to start: {exc}", profile.secrets())) from None
            # Playwright dismisses alert/confirm/prompt by itself unless someone
            # listens for them, which breaks every dialog for the person using
            # the window (e.g. an extension's login prompt()). A listener
            # that does nothing leaves them to the user.
            context.on("dialog", lambda dialog: None)
            store.write_run(name, {"pid": os.getpid(), "started_at": now_iso(), "ready": True})
            print(f"[fpb] {name}: window open (pid {os.getpid()}). Close the window to end.", flush=True)
            try:
                page = context.pages[0] if context.pages else context.new_page()
                if url:
                    try:
                        page.goto(url, wait_until="commit", timeout=60_000)
                    except Exception as exc:
                        print(f"[fpb] {name}: could not load {url}: {exc}", file=sys.stderr, flush=True)
                if on_ready:
                    on_ready(context)
                if keep_open:
                    _wait_until_closed(context, stop, str(store.user_data_dir(name)))
            finally:
                try:
                    context.close()
                except Exception:
                    pass
                store.clear_run(name, os.getpid())
                print(f"[fpb] {name}: closed.", flush=True)
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)


def open_detached(store: Store, profile: Profile, url: Optional[str] = None, timeout: float = READY_TIMEOUT) -> int:
    """Start `fpb open` in its own session and return its pid once the window is up."""
    name = profile.name
    pid = store.running_pid(name)
    if pid:
        raise LaunchError(f"profile {name!r} is already open (pid {pid})")
    log_path = store.log_path(name)
    fd = os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    os.fchmod(fd, 0o600)
    cmd = [sys.executable, "-m", "fpb", "open", name]
    if url:
        cmd += ["--url", url]
    env = dict(os.environ, FPB_HOME=str(store.home), PYTHONUNBUFFERED="1")
    with os.fdopen(fd, "a") as log:
        log.write(f"\n=== {now_iso()} fpb open --detach ===\n")
        log.flush()
        child = subprocess.Popen(
            cmd, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
            start_new_session=True, close_fds=True, env=env,
        )
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        info = store.read_run(name)
        if info and info.get("pid") == child.pid and info.get("ready"):
            return child.pid
        if child.poll() is not None:
            tail = log_path.read_text(encoding="utf-8", errors="replace").splitlines()[-15:]
            raise LaunchError("fpb open exited before the window came up:\n" + "\n".join(tail))
        time.sleep(0.5)
    raise LaunchError(f"window did not come up within {timeout:.0f}s; see {log_path}")
