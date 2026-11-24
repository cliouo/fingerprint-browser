"""fpb: a minimal Camoufox profile launcher."""

from __future__ import annotations

import argparse
import json
import sys
import warnings
from pathlib import Path
from typing import List, Optional

from . import __version__
from . import identity as ident
from .extensions import ExtensionError, bypass_candidates, inspect_extension, validate_extension
from .proxy import ProxyError, parse_bypass, parse_proxy, redact
from .store import Profile, Store, StoreError, now_iso, stop_pid, validate_name


class CliError(Exception):
    pass


def _out(line: str = "") -> None:
    print(line, flush=True)


def _validated_extensions(paths: List[str], allow_no_gecko_id: bool) -> List[str]:
    out = []
    for path in paths or []:
        info = validate_extension(path, allow_no_gecko_id=allow_no_gecko_id)
        if info.path not in out:
            out.append(info.path)
    return out


def _check_proxy(url: Optional[str]) -> Optional[str]:
    if url is None or url.lower() in ("", "none", "direct"):
        return None
    proxy = parse_proxy(url)
    if proxy.scheme == "socks5" and proxy.has_auth:
        # Playwright refuses it at launch ("Browser does not support socks5 proxy
        # authentication"); stable Camoufox (0.5.9) has no way around that.
        raise CliError(
            "SOCKS5 with a user name/password is not supported (the browser driver, Playwright, refuses it). "
            "Use the provider's HTTP endpoint (http://user:pass@host:port), or a SOCKS5 proxy without a password."
        )
    return proxy.url()


def _bypass_hint(p: Profile) -> None:
    """Point out the extensions' host permissions: one of them may be a backend that should skip the proxy."""
    if not p.proxy:
        return
    missing = []
    for path in p.extensions:
        try:
            info = inspect_extension(path)
        except ExtensionError:
            continue
        for host in bypass_candidates(info):
            if not any(host.endswith(entry) for entry in p.bypass) and host not in missing:
                missing.append(host)
    if missing:
        _out(f"note: the extensions may talk to {', '.join(missing)}; that traffic goes through the proxy.")
        _out("      If one of them is an extension's own backend (not a site you browse through the proxy), it can be")
        _out(f"      reached directly, seeing this machine's IP: fpb set-bypass {p.name} <host>")


def _identity_for_new(store: Store, profile: Profile) -> None:
    """Draw the identity, resolve it through Camoufox once, save digest + summary."""
    from .launcher import compute_options, require_browser

    require_browser()  # the preset pool and the UA version come from the installed build

    used = []
    for other in store.names():
        try:
            preset = store.load(other).identity.get("preset")
        except Exception:
            continue
        if preset:
            used.append(ident.preset_key(preset))
    last_error = None
    for _attempt in range(5):
        profile.identity = ident.new_identity(used)
        with warnings.catch_warnings():
            # geoip is off on purpose: this only resolves the identity, no launch
            warnings.filterwarnings("ignore", message=r"When using a proxy")
            config = ident.config_from_options(compute_options(store, profile, geoip=False))
        screen = profile.identity["preset"].get("screen") or {}
        if config.get("screen.width") == screen.get("width") and config.get("screen.height") == screen.get("height"):
            profile.identity_digest = ident.digest(config)
            profile.identity_summary = ident.summarize(config)
            return
        # Camoufox cropped the device's screen to this display: draw another
        # device, or the identity would change with the monitor.
        last_error = f"screen {screen.get('width')}x{screen.get('height')} was cropped by Camoufox"
        used.append(ident.preset_key(profile.identity["preset"]))
    raise CliError(f"could not draw an identity that fits this display ({last_error})")


def _print_summary(summary: dict, indent: str = "  ") -> None:
    for key, value in summary.items():
        _out(f"{indent}{key}: {value}")


# -- commands -----------------------------------------------------------------


def cmd_new(store: Store, args) -> int:
    name = validate_name(args.name)
    if store.exists(name):
        raise CliError(f"profile {name!r} already exists")
    profile = Profile(
        name=name,
        created_at=now_iso(),
        proxy=_check_proxy(args.proxy),
        extensions=_validated_extensions(args.extension, args.allow_no_gecko_id),
        bypass=parse_bypass(args.bypass),
    )
    if profile.bypass and not profile.proxy:
        raise CliError("--bypass only applies with --proxy")
    _identity_for_new(store, profile)
    store.create(profile)
    _out(f"created profile {name!r} in {store.dir(name)}")
    _out(f"  proxy: {profile.proxy_masked()}")
    if profile.bypass:
        _out(f"  bypass (direct, not via the proxy): {', '.join(profile.bypass)}")
    for path in profile.extensions:
        _out(f"  extension: {inspect_extension(path).label()}")
    _out("  identity (fixed for this profile):")
    _print_summary(profile.identity_summary, "    ")
    _bypass_hint(profile)
    _out(f"next: fpb check {name}   then   fpb open {name}")
    return 0


def cmd_open(store: Store, args) -> int:
    from .launcher import check_browser_for, install_redaction, open_detached, run_open

    if args.url and not args.url.lower().startswith(("http://", "https://")):
        # Playwright never sees moz-extension:// or about: pages finish loading.
        raise CliError("--url must be an http:// or https:// address")
    profile = store.load(args.name)
    install_redaction(profile)
    check_browser_for(profile)
    if args.detach:
        pid = open_detached(store, profile, url=args.url)
        _out(f"{profile.name}: window open in the background (pid {pid}); log {store.log_path(profile.name)}")
        _out(f"close it with the window's close button or: fpb close {profile.name}")
        return 0
    try:
        run_open(store, profile, url=args.url)
    except KeyboardInterrupt:
        store.clear_run(profile.name)
        return 130
    return 0


def cmd_close(store: Store, args) -> int:
    validate_name(args.name)
    pid = store.running_pid(args.name)
    if not pid:
        _out(f"{args.name} is not open")
        return 0
    if stop_pid(pid):
        _out(f"{args.name}: closed (pid {pid})")
        return 0
    raise CliError(f"{args.name}: pid {pid} did not exit")


def cmd_list(store: Store, args) -> int:
    names = store.names()
    if not names:
        _out(f"no profiles in {store.profiles_dir} (create one with `fpb new <name>`)")
        return 0
    rows = [("NAME", "STATE", "PROXY", "EXT", "SCREEN", "GPU")]
    for name in names:
        try:
            p = store.load(name)
        except Exception as exc:
            rows.append((name, "BROKEN", str(exc)[:40], "", "", ""))
            continue
        pid = store.running_pid(name)
        s = p.identity_summary or {}
        gpu = str(s.get("webGl:renderer", "?"))
        gpu = gpu.replace("ANGLE (", "").split(" Direct3D")[0][:40]
        rows.append((
            name,
            f"open:{pid}" if pid else "closed",
            p.proxy_masked(),
            str(len(p.extensions)),
            f"{s.get('screen.width', '?')}x{s.get('screen.height', '?')}",
            gpu,
        ))
    widths = [max(len(r[i]) for r in rows) for i in range(len(rows[0]))]
    for r in rows:
        _out("  ".join(c.ljust(w) for c, w in zip(r, widths)).rstrip())
    return 0


def cmd_show(store: Store, args) -> int:
    p = store.load(args.name)
    pid = store.running_pid(p.name)
    if args.json:
        data = {
            "name": p.name, "created_at": p.created_at, "proxy": p.proxy_masked(), "bypass": p.bypass,
            "extensions": p.extensions, "open_pid": pid, "dir": str(store.dir(p.name)),
            "identity_summary": p.identity_summary, "identity_digest": p.identity_digest,
        }
        _out(json.dumps(data, indent=2))
        return 0
    _out(f"name:       {p.name}")
    _out(f"created:    {p.created_at}")
    _out(f"state:      {'open (pid %d)' % pid if pid else 'closed'}")
    _out(f"proxy:      {p.proxy_masked()}")
    _out(f"bypass:     {', '.join(p.bypass) if p.bypass else 'none'}")
    _out(f"directory:  {store.dir(p.name)}")
    _out(f"launch:     os={p.identity.get('os')} locale={p.identity.get('locale')} geoip=True persistent_context=True")
    if p.extensions:
        for path in p.extensions:
            try:
                _out(f"extension:  {inspect_extension(path).label()}")
            except ExtensionError as exc:
                _out(f"extension:  MISSING/INVALID {exc}")
    else:
        _out("extension:  none")
    _out(f"identity:   digest {p.identity_digest}")
    _print_summary(p.identity_summary, "  ")
    return 0


def cmd_delete(store: Store, args) -> int:
    validate_name(args.name)
    if not args.yes:
        raise CliError(f"this deletes {store.dir(args.name)} including logins and cookies; re-run with --yes")
    store.delete(args.name)
    _out(f"deleted {args.name}")
    return 0


def cmd_set_proxy(store: Store, args) -> int:
    p = store.load(args.name)
    if store.running_pid(p.name):
        raise CliError(f"{p.name} is open; close it first, the proxy applies at the next open")
    p.proxy = _check_proxy(args.url)
    store.save(p)
    _out(f"{p.name}: proxy set to {p.proxy_masked()}")
    return 0


def cmd_set_bypass(store: Store, args) -> int:
    p = store.load(args.name)
    if store.running_pid(p.name):
        raise CliError(f"{p.name} is open; close it first, the bypass list applies at the next open")
    values = [] if [v.strip().lower() for v in args.hosts] in (["none"], [""]) else args.hosts
    p.bypass = parse_bypass(values)
    if p.bypass and not p.proxy:
        raise CliError(f"{p.name} has no proxy; a bypass list only applies with one")
    store.save(p)
    _out(f"{p.name}: bypass {', '.join(p.bypass) if p.bypass else 'none'}; applies at the next open")
    if p.bypass:
        _out("  these hosts are reached directly and see this machine's IP; everything else, and geoip/timezone/WebRTC, still use the proxy")
    return 0


def cmd_set_extensions(store: Store, args) -> int:
    p = store.load(args.name)
    p.extensions = _validated_extensions(args.extension, args.allow_no_gecko_id)
    store.save(p)
    _out(f"{p.name}: {len(p.extensions)} extension(s); applies at the next open")
    for path in p.extensions:
        _out(f"  {inspect_extension(path).label()}")
    _bypass_hint(p)
    return 0


def cmd_accept_identity(store: Store, args) -> int:
    """Make the identity the installed Camoufox presents the reference for later warnings.

    The saved preset (the device) is not touched; only the digest and summary
    that `open`/`check` compare against are recomputed, e.g. after a Camoufox
    upgrade changed version-tied fields (WebGL extension lists, new keys).
    """
    from .launcher import compute_options, require_browser

    p = store.load(args.name)
    require_browser()
    with warnings.catch_warnings():
        warnings.filterwarnings("ignore", message=r"When using a proxy")
        config = ident.config_from_options(compute_options(store, p, geoip=False))
    new_digest = ident.digest(config)
    if new_digest == p.identity_digest:
        _out(f"{p.name}: identity already matches the reference; nothing to do")
        return 0
    new_summary = ident.summarize(config)
    for key, old, new in ident.summary_changes(p.identity_summary or {}, new_summary):
        _out(f"  {key}: {old!r} -> {new!r}")
    p.identity_digest, p.identity_summary = new_digest, new_summary
    store.save(p)
    _out(f"{p.name}: the current identity is now the reference (device preset unchanged)")
    return 0


def cmd_check(store: Store, args) -> int:
    from camoufox.geolocation import get_geolocation
    from camoufox.ip import Proxy as CamoufoxProxy
    from camoufox.ip import public_ip

    from .launcher import LaunchError, compute_options, identity_report, install_redaction

    p = store.load(args.name)
    install_redaction(p)
    proxy = p.proxy_obj()
    _out(f"profile:   {p.name}")
    _out(f"proxy:     {p.proxy_masked()}")
    if p.bypass:
        _out(f"bypass:    {', '.join(p.bypass)} (direct; the exit IP below is the proxy's, used for timezone/geo/WebRTC)")
    try:
        ip = public_ip(CamoufoxProxy(**proxy.to_playwright()).as_string() if proxy else None)
    except Exception as exc:
        raise CliError(redact(f"exit IP lookup failed through {p.proxy_masked()}: {exc}", p.secrets())) from None
    geo = get_geolocation(ip)
    geo_locale = geo.locale.as_string if hasattr(geo.locale, "as_string") else str(geo.locale)
    try:
        config = ident.config_from_options(compute_options(store, p, geoip=ip))
    except LaunchError as exc:
        raise CliError(str(exc)) from None
    _out(f"exit IP:   {ip}")
    _out(f"geo:       {geo.latitude:.4f}, {geo.longitude:.4f} (region {geo.locale.region}; geoip would pick {geo_locale})")
    _out(f"timezone:  {config.get('timezone')}")
    _out(f"locale:    {config.get('locale:language')}-{config.get('locale:region')} (pinned; Accept-Language and Intl follow it)")
    _out(f"webrtc ip: {config.get('webrtc:ipv4') or config.get('webrtc:ipv6')}")
    _out("identity:")
    _print_summary(ident.summarize(config), "  ")
    report = identity_report(p, config)
    _out("identity matches the one saved at `fpb new`" if not report else "\n".join(report))
    return 0


def cmd_doctor(store: Store, args) -> int:
    from . import doctor

    problems = 0

    def line(status: str, text: str) -> None:
        nonlocal problems
        if status != "OK":
            problems += 1
        _out(f"[{status:4}] {text}")

    try:
        from importlib.metadata import version

        line("OK", f"camoufox package {version('camoufox')}, python {sys.version.split()[0]}")
    except Exception as exc:
        line("FAIL", f"camoufox package not importable: {exc}")

    install = doctor.find_browser()
    if install:
        line("OK", f"browser {install.version} at {install.install_dir}")
    else:
        line("FAIL", "Camoufox browser not installed: run `camoufox fetch` (or `uv run camoufox fetch`)")

    try:
        from camoufox.geolocation import get_mmdb_path

        mmdb = get_mmdb_path("ipv4")
        line("OK" if mmdb.exists() else "WARN", f"geoip database {'present' if mmdb.exists() else 'missing (downloaded on first open)'}: {mmdb}")
    except Exception as exc:
        line("WARN", f"geoip database check failed: {exc}")

    # fonts
    fonts_dir = Path(args.fonts_dir) if args.fonts_dir else (install.fonts_dir if install else None)
    backup_dir = Path(args.backup_dir) if args.backup_dir else (install.backup_dir if install else None)
    if fonts_dir and backup_dir and (doctor.is_macos() or args.fonts_dir):
        state = doctor.font_state(fonts_dir, backup_dir)
        if args.restore_fonts:
            restored = doctor.restore_fonts(state)
            line("OK", f"restored {len(restored)} font file(s) into {fonts_dir}")
        elif args.fix_fonts:
            moved = doctor.fix_fonts(state)
            line("OK", f"moved {len(moved)} hidden system font file(s) to {backup_dir}")
        elif state.needs_fix:
            line("WARN", f"{len(state.to_move)} bundled hidden system fonts (e.g. {state.to_move[0].name}) will draw garbled UI text on macOS 27; fix: fpb doctor --fix-fonts (undo: --restore-fonts)")
        else:
            line("OK", f"font fix in place ({len(state.backed_up)} file(s) in {backup_dir})")

    # profiles
    if store.profiles_dir.is_dir():
        if not doctor.mode_ok(store.home, 0o700) or not doctor.mode_ok(store.profiles_dir, 0o700):
            line("WARN", f"{store.home} should be mode 700")
        for name in store.names():
            try:
                p = store.load(name)  # also re-tightens profile.json to 600
            except Exception as exc:
                line("FAIL", f"profile {name}: unreadable ({exc})")
                continue
            for path in p.extensions:
                try:
                    info = inspect_extension(path)
                    if not info.gecko_id:
                        line("WARN", f"profile {name}: {info.name} has no gecko id; its storage resets every launch")
                except ExtensionError as exc:
                    line("FAIL", f"profile {name}: {exc}")
            px = p.proxy_obj() if p.proxy else None
            if px and px.scheme == "socks5" and px.has_auth:
                line("FAIL", f"profile {name}: SOCKS5 with a password is not supported; use an HTTP endpoint (fpb set-proxy)")
        line("OK", f"{len(store.names())} profile(s) in {store.profiles_dir}")
    return 0 if problems == 0 or args.fix_fonts or args.restore_fonts else 1


# -- argument parsing -----------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="fpb", description=__doc__)
    parser.add_argument("--version", action="version", version=f"fpb {__version__}")
    sub = parser.add_subparsers(dest="command", required=True, metavar="COMMAND")

    p = sub.add_parser("new", help="create a profile with a fixed identity")
    p.add_argument("name")
    p.add_argument("--proxy", help="http|https://[user:pass@]host:port or socks5://host:port (no SOCKS5 password; percent-encode special characters)")
    p.add_argument("--extension", action="append", default=[], metavar="DIR", help="extracted extension dir with manifest.json (repeatable)")
    p.add_argument("--bypass", action="append", default=[], metavar="HOSTS",
                   help="hosts reached directly, not via the proxy, e.g. the extension's backend (comma-separated, repeatable)")
    p.add_argument("--allow-no-gecko-id", action="store_true", help="accept extensions without a fixed gecko id")
    p.set_defaults(func=cmd_new)

    p = sub.add_parser("open", help="open the profile's window (blocks until it is closed)")
    p.add_argument("name")
    p.add_argument("--url", help="page to load first")
    p.add_argument("-d", "--detach", action="store_true", help="run in the background, survive the terminal closing")
    p.set_defaults(func=cmd_open)

    p = sub.add_parser("close", help="close a profile's window")
    p.add_argument("name")
    p.set_defaults(func=cmd_close)

    p = sub.add_parser("list", help="list profiles")
    p.set_defaults(func=cmd_list)

    p = sub.add_parser("show", help="show a profile (password masked)")
    p.add_argument("name")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_show)

    p = sub.add_parser("delete", help="delete a profile and its browser data")
    p.add_argument("name")
    p.add_argument("--yes", action="store_true")
    p.set_defaults(func=cmd_delete)

    p = sub.add_parser("set-proxy", help="change a profile's proxy ('none' for direct)")
    p.add_argument("name")
    p.add_argument("url")
    p.set_defaults(func=cmd_set_proxy)

    p = sub.add_parser("set-bypass", help="hosts that skip the proxy, e.g. the extension's backend (or 'none')")
    p.add_argument("name")
    p.add_argument("hosts", nargs="+", metavar="HOSTS", help="comma/space-separated host names or .domain suffixes, or 'none'")
    p.set_defaults(func=cmd_set_bypass)

    p = sub.add_parser("set-extensions", help="replace a profile's extension list")
    p.add_argument("name")
    p.add_argument("--extension", action="append", default=[], metavar="DIR")
    p.add_argument("--allow-no-gecko-id", action="store_true")
    p.set_defaults(func=cmd_set_extensions)

    p = sub.add_parser("accept-identity", help="after an expected change (Camoufox upgrade), stop warning about it")
    p.add_argument("name")
    p.set_defaults(func=cmd_accept_identity)

    p = sub.add_parser("check", help="exit IP, geo, timezone, locale and identity through the profile's proxy")
    p.add_argument("name")
    p.set_defaults(func=cmd_check)

    p = sub.add_parser("doctor", help="check the environment; fix the macOS font issue")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--fix-fonts", action="store_true", help="move bundled hidden system fonts to the backup dir")
    g.add_argument("--restore-fonts", action="store_true", help="undo --fix-fonts")
    p.add_argument("--fonts-dir", help=argparse.SUPPRESS)  # testing: operate on a copy
    p.add_argument("--backup-dir", help=argparse.SUPPRESS)
    p.set_defaults(func=cmd_doctor)
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    store = Store()
    try:
        return args.func(store, args)
    except (CliError, StoreError, ProxyError, ExtensionError) as exc:
        print(f"fpb: error: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:
        from .launcher import LaunchError

        if isinstance(exc, LaunchError):
            print(f"fpb: error: {exc}", file=sys.stderr)
            return 1
        raise


if __name__ == "__main__":
    sys.exit(main())
