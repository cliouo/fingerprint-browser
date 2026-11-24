"""One end-to-end launch of an fpb profile, recording evidence.

  uv run python scripts/e2e/e2e.py <profile> --tag L1 --out DIR [--url URL] [--hold 45]

Opens the profile exactly as `fpb open` does (same launch code), then:
  1. loads https://example.com/ (or --url), screenshots it, and records what
     the page sees of the identity (DIR/<tag>-identity.json);
  2. records the profile's first extension's moz-extension id and version, and,
     with --hold N, keeps the window open N seconds (for GUI steps such as
     logging in or clicking in the extension's popup) and reports which calls
     reached the mock backend meanwhile (scripts/e2e/mock_backend.py: authed =
     the extension still has its login token; new logins = it logged in again)
     and which tabs Playwright tracked;
  3. closes the window.

Playwright cannot drive moz-extension:// pages (Juggler does not attach to the
extension process), so the login itself is done in the popup by hand / GUI
automation, and its survival is read from the mock backend's request log.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path

from fpb.launcher import run_open
from fpb.store import Store


PROBE = r"""
async () => {
  const gl = document.createElement('canvas').getContext('webgl');
  const dbg = gl && gl.getExtension('WEBGL_debug_renderer_info');
  // Scalars only: typed arrays cannot cross the isolated world's Xray boundary.
  const glParams = gl ? ['MAX_TEXTURE_SIZE', 'MAX_RENDERBUFFER_SIZE', 'MAX_VERTEX_ATTRIBS', 'MAX_VARYING_VECTORS',
      'MAX_COMBINED_TEXTURE_IMAGE_UNITS', 'MAX_FRAGMENT_UNIFORM_VECTORS', 'MAX_VERTEX_UNIFORM_VECTORS']
      .map(n => gl.getParameter(gl[n])) : null;
  const fontsToTest = ['Arial', 'Calibri', 'Cambria', 'Segoe UI', 'Segoe UI Variable Display', 'Bahnschrift',
      'Consolas', 'Candara', 'Gabriola', 'Ink Free', 'Leelawadee UI', 'Helvetica Neue', 'Menlo', 'Avenir',
      'Apple Color Emoji', 'PingFang SC', 'Ubuntu', 'DejaVu Sans'];
  const fonts = fontsToTest.filter(f => document.fonts.check(`12px "${f}"`));
  let voices = speechSynthesis.getVoices();
  for (let i = 0; i < 20 && voices.length === 0; i++) { await new Promise(r => setTimeout(r, 100)); voices = speechSynthesis.getVoices(); }
  const within = (p, ms) => Promise.race([p, new Promise(r => setTimeout(() => r('timeout'), ms))]);
  let devices = [];
  try {
    devices = await within(navigator.mediaDevices.enumerateDevices(), 10000);
    devices = devices === 'timeout' ? ['timeout'] : devices.map(d => d.kind);
  } catch (e) { devices = ['error']; }
  const c = document.createElement('canvas'); c.width = 200; c.height = 40;
  const ctx = c.getContext('2d'); ctx.font = '16px Arial'; ctx.fillText('fpb canvas probe', 4, 24);
  const canvasData = c.toDataURL();
  let h = 0; for (let i = 0; i < canvasData.length; i++) h = (h * 31 + canvasData.charCodeAt(i)) >>> 0;
  return {
    userAgent: navigator.userAgent, platform: navigator.platform, oscpu: navigator.oscpu,
    appVersion: navigator.appVersion, hardwareConcurrency: navigator.hardwareConcurrency,
    maxTouchPoints: navigator.maxTouchPoints, language: navigator.language, languages: navigator.languages,
    webdriver: navigator.webdriver,
    screen: {width: screen.width, height: screen.height, availWidth: screen.availWidth,
             availHeight: screen.availHeight, colorDepth: screen.colorDepth, pixelDepth: screen.pixelDepth},
    devicePixelRatio: window.devicePixelRatio,
    window: {innerWidth, innerHeight, outerWidth, outerHeight},
    timezone: Intl.DateTimeFormat().resolvedOptions().timeZone,
    intlLocale: Intl.DateTimeFormat().resolvedOptions().locale,
    tzOffsetMin: new Date().getTimezoneOffset(),
    webglVendor: dbg ? gl.getParameter(dbg.UNMASKED_VENDOR_WEBGL) : null,
    webglRenderer: dbg ? gl.getParameter(dbg.UNMASKED_RENDERER_WEBGL) : null,
    webglParams: glParams,
    webglExtensions: gl ? gl.getSupportedExtensions().length : null,
    fonts,
    voices: voices.map(v => v.name),
    mediaDevices: devices,
    canvasHash_expected_to_vary: h,
  };
}
"""


def extension_uuid(user_data: Path, gecko_id: str, timeout: float = 30.0) -> str:
    """The moz-extension:// host Firefox assigned to the gecko id (saved in prefs.js)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        prefs = user_data / "prefs.js"
        if prefs.exists():
            m = re.search(r'user_pref\("extensions\.webextensions\.uuids", "(.*)"\);', prefs.read_text())
            if m:
                uuids = json.loads(m.group(1).encode().decode("unicode_escape"))
                if gecko_id in uuids:
                    return uuids[gecko_id]
        time.sleep(0.5)
    raise RuntimeError("extension uuid not found in prefs.js")


def backend_state(base: str) -> dict:
    import urllib.request

    with urllib.request.urlopen(f"{base}/__state", timeout=5) as resp:
        return json.loads(resp.read())


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("profile")
    ap.add_argument("--tag", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--url", default="https://example.com/")
    ap.add_argument("--hold", type=float, default=0, help="seconds to keep the window open for the extension")
    ap.add_argument("--backend", default="http://127.0.0.1:8100")
    ap.add_argument("--no-extension", action="store_true")
    ap.add_argument("--ip-check", action="store_true", help="also record the exit IP the browser gets (api.ipify.org)")
    ap.add_argument("--no-site", action="store_true", help="leave the first tab blank (like `fpb open` without --url); with --hold")
    ap.add_argument("--until-results", action="store_true", help="end the hold once a results POST reaches the mock")
    ap.add_argument("--no-addon-new-tab", action="store_true", help="launch without allow_addon_new_tab (extensions cannot open tabs then)")
    ap.add_argument("--executable-path", help="run this Camoufox binary instead of the active install (testing a newer build)")
    args = ap.parse_args()
    import fpb.launcher as launcher

    if args.no_addon_new_tab:
        plain = launcher.launch_kwargs
        launcher.launch_kwargs = lambda *a, **k: {**plain(*a, **k), "allow_addon_new_tab": False}
    if args.executable_path:

        base = launcher.launch_kwargs
        launcher.launch_kwargs = lambda *a, **k: {**base(*a, **k), "executable_path": args.executable_path}
        launcher.require_browser = lambda: None  # the given binary, not the active install

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    store = Store()
    profile = store.load(args.profile)
    result: dict = {"profile": profile.name, "tag": args.tag}

    def hold(context, ext):
        """Keep the window open, record what Playwright tracks and what the extension sent."""
        uuid = ext.get("uuid")
        before = backend_state(args.backend)
        started = time.time()
        new_tabs = []
        context.on("page", lambda p: new_tabs.append(p))
        print(f"[e2e] holding {args.hold:.0f}s; extension uuid {uuid}", flush=True)
        deadline = time.monotonic() + args.hold
        timeline = []  # what Playwright tracks: fpb's close check used to rely on it
        while time.monotonic() < deadline:
            try:  # pumps Playwright's events, with or without a page
                context.wait_for_event("page", timeout=2000)
            except Exception:
                pass
            urls = [p.url[:60] for p in context.pages]
            if not timeline or timeline[-1][1] != urls:
                timeline.append((round(time.time() - started), urls))
                print(f"[e2e] t+{timeline[-1][0]}s pages={urls}", flush=True)
            if args.until_results and len(backend_state(args.backend).get("results", [])) > len(before.get("results", [])):
                time.sleep(3)
                break
        after = backend_state(args.backend)
        calls = [r for r in after["requests"] if r["t"] >= started]
        ext["pages_timeline"] = timeline
        ext["tabs_opened"] = [p.url for p in new_tabs if not p.is_closed()] + [
            "(closed)" for p in new_tabs if p.is_closed()]
        ext["backend"] = {
            "results_posted": [r for r in after.get("results", []) if r["t"] >= started],
            "new_logins": len(after["logins"]) - len(before["logins"]),
            "calls": [f"{r['m']} {r['p']} {'authed' if r['authed'] else 'UNAUTHED'}" for r in calls],
        }

    def extension_info():
        # The loaded build's id and version, read from the same build directory
        # the profile points at (Camoufox installs it from there each launch).
        manifest = json.loads((Path(profile.extensions[0]) / "manifest.json").read_text())
        gecko_id = (manifest.get("browser_specific_settings") or manifest.get("applications") or {}).get("gecko", {}).get("id")
        uuid = extension_uuid(store.user_data_dir(profile.name), gecko_id)
        return {"gecko_id": gecko_id, "uuid": uuid, "version_on_disk": manifest.get("version")}

    def ready(context):
        page = context.pages[0] if context.pages else context.new_page()
        if args.ip_check:
            page.goto("https://api.ipify.org/?format=json", wait_until="domcontentloaded", timeout=60_000)
            result["browser_exit_ip"] = json.loads(page.inner_text("body"))["ip"]
        if not args.no_site:
            t0 = time.monotonic()
            resp = page.goto(args.url, wait_until="domcontentloaded", timeout=90_000)
            page.wait_for_timeout(5000)
            result["page"] = {"url": page.url, "status": resp.status if resp else None, "title": page.title(),
                              "load_s": round(time.monotonic() - t0, 1)}
            body = page.inner_text("body")[:3000]
            result["page"]["blocked_page"] = bool(re.search(r"Access Denied|Pardon Our Interruption|unusual traffic", body))
            page.screenshot(path=str(out / f"{args.tag}-site.png"))
            result["identity"] = page.evaluate(PROBE)
            (out / f"{args.tag}-identity.json").write_text(json.dumps(result["identity"], indent=2))
        if args.no_extension:
            return
        ext = extension_info()
        result["extension"] = ext
        if args.hold:
            hold(context, ext)

    run_open(store, profile, on_ready=ready, keep_open=False)
    (out / f"{args.tag}-result.json").write_text(json.dumps(result, indent=2))
    summary = {k: v for k, v in result.items() if k != "identity"}
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
