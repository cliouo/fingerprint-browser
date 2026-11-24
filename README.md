# fpb — a small fingerprint-browser launcher on Camoufox

`fpb` is a command-line launcher for [Camoufox](https://github.com/daijro/camoufox) (an anti-detect build of Firefox). One profile = one proxy + one fixed browser identity + one persistent Firefox profile directory + the extensions to load. It aims at being small and reliable, not feature-rich.

## Install

Needs [uv](https://docs.astral.sh/uv/). Developed on macOS; Linux is covered by CI (Xvfb).

```sh
git clone <this repository> fpb && cd fpb
uv sync                         # .venv with camoufox[geoip] 0.5.9 (stable)
uv run camoufox fetch           # the browser 0.5.9 pairs with: 158.0.1-beta.41
uv run fpb doctor --fix-fonts   # macOS 27 only: font fix for that build (see below); then checks geoip db and profiles
```

fpb stays on stable Camoufox: camoufox 0.5.9 with browser 158.0.1-beta.41. It never downloads the browser itself: if that build is missing, `new`, `check` and `open` stop and say to run `uv run camoufox fetch` (Camoufox alone would download it in the middle of a launch).

Run commands with `uv run fpb ...` from the repository, or put `fpb` on your PATH with `uv tool install --editable .`.

## Use

```sh
# 1. create a profile: proxy + an unpacked extension (the directory with manifest.json)
fpb new work1 --proxy 'http://user:pass@203.0.113.10:8000' --extension ~/my-extension/dist

# 2. what will sites see? exit IP, geo, timezone, locale, identity — through the proxy
fpb check work1

# 3. open the window (blocks until you close it), or in the background
fpb open work1 --url https://example.com/
fpb open work1 --detach          # survives closing the terminal; `fpb close work1` ends it

# manage
fpb list
fpb show work1                   # password shown as ***
fpb set-proxy work1 'http://user:pass@203.0.113.20:8000'   # or 'socks5://203.0.113.20:1080' (no password), or 'none'
fpb set-extensions work1 --extension ~/my-extension/dist
fpb set-bypass work1 api.example.com   # hosts reached directly, not via the proxy ('none' clears)
fpb accept-identity work1        # after an expected identity change (e.g. a Camoufox upgrade)
fpb delete work1 --yes           # removes logins/cookies too
```

### Proxies

Percent-encode special characters in proxy user names and passwords (`p@ss` → `p%40ss`). Supported: `http://` and `https://` with or without user/password, `socks5://` **without** user/password.

SOCKS5 with a password is refused by `fpb new`/`set-proxy`: Playwright, which starts the browser, rejects it ("Browser does not support socks5 proxy authentication"), and stable Camoufox has no way around that; use the provider's HTTP endpoint. (Upstream: camoufox 0.5.10b1+ with browser 159.0-beta.42+ supports it, but not for a persistent profile; `scripts/e2e/socks5_persistent_repro.py` shows the gap.)

A profile with `--proxy` connects to that proxy directly; one without uses the system's network settings.

### Proxy bypass

An extension's own backend often should not go through the profile's proxy: uploads through a remote proxy can be slow, and with a password-protected proxy an extension's background requests can fail outright (407), because they have no page that answers the proxy's password challenge until some page has done so.

```sh
fpb new work1 --proxy 'http://user:pass@203.0.113.10:8000' --extension ~/my-extension/dist --bypass api.example.com
fpb set-bypass work1 api.example.com      # or later; 'none' clears it
```

- Entries are host names or `.domain` suffixes, comma-separated; the browser matches them as suffixes of the request's host (no ports or paths). Overly broad entries (`com`, `.ai`) are refused.
- Only the browser's requests to those hosts go direct, and those hosts see this machine's IP, so never list the sites you browse through the proxy. Everything else, and the geoip lookup (exit IP, timezone, geolocation, WebRTC IP), still uses the proxy; `fpb check` shows the bypass next to the proxy's exit IP.
- Nothing is bypassed by default. `fpb new`/`set-extensions` list the hosts in an extension's `host_permissions` as a hint.

## What is fixed per launch

| | |
|---|---|
| `os="windows"` | a Windows identity |
| `locale="en-US"` | pinned; geoip alone may pick a regional variant (e.g. `es-US` for a US IP) |
| `geoip=True` | timezone, geolocation and WebRTC IP follow the proxy's exit IP; if the exit IP cannot be looked up, the launch stops instead of running with a wrong timezone |
| `fingerprint_preset=<saved>` | the identity drawn at `fpb new` (a recorded real Windows device + a per-profile salt), identical on every launch |
| `persistent_context=True`, `user_data_dir=<profile>/user_data` | logins, cookies and extension storage persist |
| `no_viewport=True` | the page follows the real window instead of Playwright's fixed 1280×720 viewport |
| `addons=[<dirs>]` | installed as temporary add-ons each launch; storage persists because it is keyed by the gecko id |
| `allow_addon_new_tab=True` | lets an extension open tabs; Camoufox rejects `chrome.tabs.create` otherwise |

The identity is a Camoufox `fingerprint_preset` (a recorded real device) chosen at `fpb new` and saved with a random per-profile salt: Camoufox hashes the whole preset into the identity's per-profile draws (fonts, voices, media devices, WebGL parameters), so the saved dict gives the same identity on every launch, and two profiles on the same device still differ. `new` prefers devices whose screen matches your display and skips software GPUs.

Canvas/WebGL readback is randomized by Camoufox when the claimed OS differs from the host (otherwise the pixels would show the host's rendering), so a canvas hash differs between reads; everything else is identical across launches.

`fpb open` compares the identity Camoufox is about to present with the one saved at `fpb new` and warns, listing the fields, if they differ (a Camoufox upgrade or a smaller monitor can cause that). Launch settings (extension paths, `allow_addon_new_tab`, proxy-derived timezone/geo/WebRTC) are not part of the comparison. After an expected change, `fpb accept-identity NAME` makes the current identity the reference (the saved device preset is kept).

## Window lifetime

- `fpb open NAME` runs in the foreground until the window is closed (closing the last window, Cmd+Q, or Ctrl+C); the profile is then closed cleanly. Closing the terminal kills it.
- `fpb open NAME --detach` starts the browser in its own session (setsid) with output in `<profile>/open.log`, waits until the window is up, and returns. Closing the terminal, or the time limit of whatever started it, does not touch the window. End it by closing the window or with `fpb close NAME`.
- One window per profile: a second `fpb open` of an open profile is refused.
- How a closed window is noticed: on Linux (and Windows) the browser quits with its last window, which ends `fpb open`. On macOS the browser keeps running without windows, so fpb treats the window as closed when the browser has no window on screen and no open tab (a minimized window, or one on another Space, keeps its tabs). Tabs an extension opens and closes never end the session.
- Don't open a profile with a newer Camoufox browser than fpb uses: Firefox's downgrade protection then makes the older browser hang on it.

## Extensions

Point `--extension` at the unpacked build directory (the one with `manifest.json`). The extension needs a fixed `browser_specific_settings.gecko.id` (e.g. `example-ext@example.com`): without one Firefox gives it a new id, and empty storage (no login), on every launch, so `fpb` refuses it unless you pass `--allow-no-gecko-id`.

To update an extension, rebuild it in place; the next `fpb open` loads the new version and its storage (e.g. a login) is kept.

Playwright, which fpb uses to start the browser, dismisses `alert/confirm/prompt` dialogs by default; fpb turns that off so dialogs (an extension's login prompt, say) stay for you to answer.

## Secrets

Proxy credentials live only in `~/.fpb/profiles/<name>/profile.json` (directory 700, file 600; outside the repository). `show`, `list`, `check` and `open.log` print the password as `***`, and fpb's output streams redact it. Override the location with `FPB_HOME`.

## macOS 27 font issue

On macOS 27, Camoufox's bundled copies of macOS's hidden system fonts (`.SF NS`, `.PingFang SC`, `.Hiragino Kaku Gothic Interface`, … — 46 files in 158.0.1-beta.41) are older than the host's, and the browser UI draws garbled text. The fix moves them out of `Camoufox.app/Contents/Resources/fonts` into `~/Library/Caches/camoufox/fonts-hidden-backup-<version>/` so the host's copies are used:

```sh
fpb doctor                  # reports whether the fix is needed
fpb doctor --fix-fonts      # move them out (idempotent)
fpb doctor --restore-fonts  # undo
```

`camoufox fetch` installs a fresh bundle, so run `fpb doctor --fix-fonts` again after every fetch; `fpb open` warns while the active build still has them. `.LastResort` and `.Aqua Kana` stay in the bundle (the UI renders correctly with them).

## Development

```sh
uv run pytest               # unit tests: proxy parsing, profile store, identity, extensions, doctor, CLI, close detection
```

### How it was tested

Besides the unit tests (which need no browser), fpb was checked with a real browser:

- identity: the same profile probed on two launches gives the same UA, screen, WebGL vendor/renderer/parameters, fonts, voices, media devices, cores and languages; only the canvas hash differs (by design, see above);
- proxies: an authenticated HTTP proxy from `scripts/e2e/local_proxy.py`; the browser's exit IP equals the proxy's; a wrong password stops the launch before the browser starts, and the password never appears in any output;
- extensions: an extension's login (stored in its storage) survives a restart and an in-place update of the extension, checked against `scripts/e2e/mock_backend.py`; an extension that opens its own tabs works (`allow_addon_new_tab`);
- window lifetime: `--detach` survives killing the launching process group; closing the window ends `fpb open` on macOS and Linux; a minimized window does not;
- proxy bypass: with `set-bypass`, the backend calls never reach the proxy while other traffic and the geoip lookup still do.

The Linux part runs in CI (`.github/workflows/linux-e2e.yml`, Xvfb): `scripts/e2e/window_lifetime.py` and `scripts/e2e/bypass_check.sh` with the test extension in `scripts/e2e/runner_ext`. `scripts/e2e/e2e.py` drives a single launch for manual checks (identity probe, screenshot, holding the window for GUI steps).

## License

MIT, see [LICENSE](LICENSE). Camoufox and Playwright are separate projects under their own licenses.
