"""The fixed identity of a profile.

The identity is a Camoufox *fingerprint preset* (a recorded real device) plus
an ``fpb_salt`` key. Camoufox seeds every per-identity draw (fonts, voices,
WebGL parameters, media devices) from a hash of the whole preset dict, and
ignores keys it does not know when reading it, so:

* the same saved dict gives the same identity on every launch;
* two profiles that drew the same device still differ (different salt).

The pure helpers here (choose_preset, digest, summarize) do not import
Camoufox; the ones that do import it lazily.
"""

from __future__ import annotations

import hashlib
import json
import random
import re
import secrets
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

OS = "windows"
LOCALE = "en-US"
SALT_KEY = "fpb_salt"

# Config keys that follow the proxy exit (geoip) or the pinned locale, not the
# device (or are file paths). They may change between launches and are left out of the
# identity digest.
_VOLATILE_PREFIXES = ("geolocation:", "locale:", "webrtc:", "network:")
_VOLATILE_KEYS = {"timezone", "addons", "allowAddonNewtab"}  # extension paths and launch flags, not the device

# Software rasterizers (same list as camoufox.fingerprints._SOFTWARE_RENDERERS):
# a Windows box without a GPU driver reads as a VM / RDP session.
SOFTWARE_RENDERERS = ("llvmpipe", "Microsoft Basic Render Driver", "SwiftShader", "Generic Renderer")

_UA_VERSION = re.compile(r"(rv:|Firefox/)[0-9][0-9.]*")

SUMMARY_KEYS = (
    "navigator.userAgent",
    "navigator.platform",
    "navigator.oscpu",
    "navigator.hardwareConcurrency",
    "navigator.maxTouchPoints",
    "screen.width",
    "screen.height",
    "screen.availWidth",
    "screen.availHeight",
    "screen.colorDepth",
    "window.devicePixelRatio",
    "webGl:vendor",
    "webGl:renderer",
)


def preset_key(preset: Dict[str, Any]) -> str:
    """Identifies the recorded device a preset came from, salt excluded."""
    bare = {k: v for k, v in preset.items() if k != SALT_KEY}
    return hashlib.sha256(json.dumps(bare, sort_keys=True).encode()).hexdigest()[:16]


def has_hardware_gpu(preset: Dict[str, Any]) -> bool:
    renderer = (preset.get("webgl") or {}).get("unmaskedRenderer") or ""
    return bool(renderer) and not any(name in renderer for name in SOFTWARE_RENDERERS)


def screen_fits(preset: Dict[str, Any], max_w: Optional[int], max_h: Optional[int]) -> bool:
    screen = preset.get("screen") or {}
    w, h = screen.get("width"), screen.get("height")
    if not w or not h:
        return False
    return (max_w is None or w <= max_w) and (max_h is None or h <= max_h)


def screen_matches(preset: Dict[str, Any], disp_w: Optional[int], disp_h: Optional[int]) -> bool:
    screen = preset.get("screen") or {}
    return disp_w is not None and screen.get("width") == disp_w and screen.get("height") == disp_h


def candidate_presets(
    presets: Sequence[Dict[str, Any]],
    used_keys: Iterable[str] = (),
    max_w: Optional[int] = None,
    max_h: Optional[int] = None,
) -> List[Dict[str, Any]]:
    """Presets worth drawing from, best tier first.

    Best: a device no other profile uses whose screen is exactly this display's
    size, so any real window size (even maximized) is one the claimed screen can
    hold. Then a screen that at least fits (Camoufox would crop a bigger one,
    and the identity would then change with the monitor). Devices with a
    hardware WebGL GPU are preferred throughout. Never fails while any preset
    exists.
    """
    used = set(used_keys)
    usable = [p for p in presets if has_hardware_gpu(p)] or list(presets)
    fresh = [p for p in usable if preset_key(p) not in used]
    tiers = [
        [p for p in fresh if screen_matches(p, max_w, max_h)],
        [p for p in usable if screen_matches(p, max_w, max_h)],
        [p for p in fresh if screen_fits(p, max_w, max_h)],
        [p for p in usable if screen_fits(p, max_w, max_h)],
        list(usable),
    ]
    for tier in tiers:
        if tier:
            return tier
    return []


def choose_preset(
    presets: Sequence[Dict[str, Any]],
    used_keys: Iterable[str] = (),
    max_w: Optional[int] = None,
    max_h: Optional[int] = None,
    rng: Optional[random.Random] = None,
) -> Dict[str, Any]:
    pool = candidate_presets(presets, used_keys, max_w, max_h)
    if not pool:
        raise ValueError("no fingerprint presets available")
    rng = rng or random.SystemRandom()
    chosen = json.loads(json.dumps(rng.choice(pool)))  # deep copy
    chosen[SALT_KEY] = secrets.token_hex(8)
    return chosen


def config_from_options(options: Dict[str, Any]) -> Dict[str, Any]:
    """The CAMOU_CONFIG Camoufox passes to the browser, from launch_options()."""
    env = options.get("env") or {}
    keys = sorted((k for k in env if k.startswith("CAMOU_CONFIG_")), key=lambda k: int(k.rsplit("_", 1)[1]))
    if not keys:
        return {}
    return json.loads("".join(str(env[k]) for k in keys))


def _stable_items(config: Dict[str, Any]) -> Dict[str, Any]:
    out = {}
    for key, value in config.items():
        if key in _VOLATILE_KEYS or key.startswith(_VOLATILE_PREFIXES):
            continue
        if key == "navigator.userAgent" and isinstance(value, str):
            value = _UA_VERSION.sub(r"\1", value)  # a browser upgrade is not a new device
        out[key] = value
    return out


def _hash(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str).encode()).hexdigest()


def digest(config: Dict[str, Any]) -> str:
    return _hash(_stable_items(config))[:32]


def summarize(config: Dict[str, Any]) -> Dict[str, Any]:
    summary = {k: config.get(k) for k in SUMMARY_KEYS if config.get(k) is not None}
    fonts = config.get("fonts") or []
    voices = config.get("voices") or []
    summary["fonts"] = f"{len(fonts)} fonts, sha {_hash(fonts)[:12]}"
    summary["voices"] = f"{len(voices)} voices, sha {_hash(voices)[:12]}"
    summary["other"] = f"sha {_hash({k: v for k, v in _stable_items(config).items() if k not in SUMMARY_KEYS and k not in ('fonts', 'voices')})[:12]}"
    return summary


def summary_changes(old: Dict[str, Any], new: Dict[str, Any]) -> List[Tuple[str, Any, Any]]:
    changes = []
    for key in sorted(set(old) | set(new)):
        a, b = old.get(key), new.get(key)
        if key == "navigator.userAgent" and isinstance(a, str) and isinstance(b, str):
            if _UA_VERSION.sub(r"\1", a) == _UA_VERSION.sub(r"\1", b):
                continue
        if a != b:
            changes.append((key, a, b))
    return changes


# -- Camoufox-backed helpers -----------------------------------------------


def firefox_major() -> str:
    from camoufox.utils import resolve_verstr

    return resolve_verstr(None).split(".", 1)[0]


def windows_presets() -> List[Dict[str, Any]]:
    from camoufox.fingerprints import load_presets

    data = load_presets(firefox_major()) or {}
    return list((data.get("presets") or {}).get(OS) or [])


def display_bounds() -> Tuple[Optional[int], Optional[int]]:
    from camoufox.utils import get_screen_cons

    cons = get_screen_cons(False)
    return (cons.max_width, cons.max_height) if cons else (None, None)


def new_identity(used_keys: Iterable[str] = ()) -> Dict[str, Any]:
    max_w, max_h = display_bounds()
    preset = choose_preset(windows_presets(), used_keys, max_w, max_h)
    return {"os": OS, "locale": LOCALE, "preset": preset}
