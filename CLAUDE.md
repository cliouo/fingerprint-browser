# CLAUDE.md

`fpb`: a small CLI that opens Camoufox (anti-detect Firefox) with one profile = proxy + fixed identity + persistent user_data_dir + extensions. Read `README.md` for behaviour and design decisions before changing them.

## Layout

- `src/fpb/proxy.py` — proxy URL parse/mask/redact, bypass list parsing (no Camoufox import)
- `src/fpb/store.py` — profile dirs under `$FPB_HOME` (default `~/.fpb`), `profile.json` (0600), run state, stop logic (no Camoufox import)
- `src/fpb/identity.py` — preset choice, identity digest/summary; Camoufox imported lazily
- `src/fpb/extensions.py` — extension dir validation (gecko id required), host permissions
- `src/fpb/launcher.py` — builds Camoufox launch options, runs the window (foreground / detached), close detection
- `src/fpb/extctx.py` — `open --ext-context`: context file checks, hand-off to the detached run, injection into an extension's options page
- `src/fpb/views.py` — the profile view `--json` prints
- `src/fpb/doctor.py` — environment checks, reversible macOS font fix (sfnt name parser)
- `src/fpb/cli.py` — argparse commands; `--json` wrapper (`_run_json`)
- `scripts/e2e/` — mock backend, local http/socks5 proxies with auth, single-launch driver (`e2e.py`), Camoufox-only SOCKS5 persistent-context repro, `runner_ext` (with an options page that takes `--ext-context`) + `window_lifetime.py` + `bypass_check.sh` + `ext_context_check.py` (Linux CI, `.github/workflows/linux-e2e.yml`)

## Commands

```sh
uv sync
uv run pytest -q                       # must pass; no browser needed
uv run fpb doctor
FPB_HOME=/tmp/fpbtest uv run fpb ...   # never test against your real ~/.fpb
```

## Invariants (do not break)

- Launch params: `os="windows"`, `locale="en-US"` (never let geoip choose the language), `geoip=True`, `persistent_context=True`, `fingerprint_preset=<saved dict>`, `no_viewport=True`, `allow_addon_new_tab=True` (else Camoufox rejects an extension's `chrome.tabs.create`). They live in `launcher.launch_kwargs`.
- The saved preset dict (including `fpb_salt`) must be passed back unchanged every launch: Camoufox hashes the whole dict into the identity salt; editing it changes the identity.
- Identity digest: launch settings (`addons`, `allowAddonNewtab`, proxy-derived geo/timezone/WebRTC) are excluded (`identity._VOLATILE_*`). After an expected change, `fpb accept-identity` re-baselines; never edit the saved preset.
- Window lifetime (`launcher._wait_until_closed`): never decide "closed" from Playwright's page list alone. Linux/Windows: only the context closing (the browser quits with its last window). macOS: closed only when the browser process has no window on screen (`macos_window_count`, Quartz; off-screen windows don't count, Firefox keeps a hidden one) and Playwright tracks no page.
- Proxy bypass (`Profile.bypass`, `fpb set-bypass`): passed as Playwright proxy `bypass` (the browser matches suffixes). No default; `new`/`set-extensions` only print a hint. The geoip lookup must keep using the proxy.
- Proxy passwords: only in `profile.json` (0600). Never print them; use `Profile.proxy_masked()` and `redact()` for any message that might contain them. Tests assert this.
- Camoufox is pinned to stable 0.5.9 (paired browser 158.0.1-beta.41). SOCKS5 with a password is refused at `new`/`set-proxy` (Playwright rejects it).
- `require_browser()` runs before anything calls `launch_options()`: Camoufox otherwise downloads its paired browser mid-launch and makes it active. To try another build use `HOME=<scratch>` or `e2e.py --executable-path`; never open a profile with a newer browser than fpb uses (Firefox downgrade protection then hangs the older one).
- Playwright auto-dismisses dialogs unless a listener exists; `run_open` registers a no-op context `dialog` listener so the user can answer them. Keep it.
- Don't `page.goto` non-http(s) URLs: navigation to `moz-extension://` never reports done and hangs past its timeout. Playwright cannot drive `moz-extension://` pages; end-to-end checks of an extension go through the mock backend's request log (and GUI automation for clicks). The one exception is `extctx.inject`: a short `goto(..., wait_until="commit")` whose timeout is expected (the page does open); keep it in a new page that is closed afterwards. Other goto errors (`NS_ERROR_NOT_AVAILABLE`: on a re-launch the UUID is known before the temporary add-on is installed again) are retried within `UUID_WAIT`.
- `--json`: stdout is exactly one JSON object (`{"ok": true, ...}` exit 0, or `{"ok": false, "error": {"code", "message"}}` exit 1); argparse errors stay exit 2. JSON fields and error codes are an interface other programs rely on: only add, never rename or remove. No password in any view (`proxy` masked, `proxy_user` separate).
- `--ext-context`: the context (and its base64url) never goes on a command line, into the environment or into any output; the detached run gets it through a 0600 hand-off file it deletes on read. `run.json` `ready` is written only after the injection attempt, so `open --detach` returns with the outcome.
- Don't touch the installed Camoufox browser files except through `fpb doctor --fix-fonts/--restore-fonts`; test those on a copy (`--fonts-dir/--backup-dir`, hidden flags).

## Testing with a real browser

Use `scripts/e2e/mock_backend.py` as the extension backend and `scripts/e2e/local_proxy.py` for proxies; never use real third-party proxy credentials or production backends in tests. Firefox's MV3 default CSP upgrades `http://` requests to non-local hosts to HTTPS: a test extension that talks to the plain-HTTP mock at a LAN address needs `content_security_policy.extension_pages = "script-src 'self'"` (as `runner_ext` has).
