"""`fpb open --ext-context GECKO_ID=FILE`: pass an opaque context to an extension.

The context is a small JSON object (for example where a local service listens
and the credential to use there). fpb does not interpret it. Once the window
is up, fpb opens the extension's options page as

    moz-extension://<uuid>/<options page>#fpbctx=<base64url of FILE, no padding>

and closes that page a few seconds later. The options page is expected to read
the hash, erase it at once (``history.replaceState``) and keep the context in
its own storage. Whether it did is up to the extension to confirm; fpb only
reports whether the page was opened.

The context may hold a secret, so it never goes on a command line or into the
environment (``ps`` shows both): a detached run hands it to its background
process through a 0600 file in the profile directory, which that process reads
and deletes before it starts the browser. The encoded payload is redacted from
everything fpb prints.

Nothing here imports Camoufox or Playwright at module level.
"""

from __future__ import annotations

import base64
import json
import re
import secrets
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote

from .extensions import ExtensionError, inspect_extension
from .proxy import redact
from .store import Profile, Store, _write_private

HASH_KEY = "fpbctx"
MAX_BYTES = 64 * 1024  # it ends up in a URL
UUID_WAIT = 15.0  # a first launch writes the extension's UUID to prefs.js a little later
NAV_TIMEOUT_MS = 2000
PAGE_HOLD = 5.0  # seconds the options page stays open
HANDOFF_PREFIX = ".ext_context."


class ExtContextError(ValueError):
    code = "invalid_ext_context"


@dataclass(frozen=True)
class ExtContext:
    gecko_id: str
    options_page: str
    payload: str  # base64url of the file's bytes, no padding; treat as a secret

    def secrets(self) -> List[str]:
        return [self.payload]

    def url(self, uuid: str) -> str:
        return f"moz-extension://{uuid}/{quote(self.options_page, safe='/')}#{HASH_KEY}={self.payload}"


def parse_arg(text: str) -> Tuple[str, str]:
    """argparse type for GECKO_ID=FILE."""
    import argparse

    gecko_id, sep, path = (text or "").partition("=")
    if not sep or not gecko_id.strip() or not path.strip():
        raise argparse.ArgumentTypeError("expected GECKO_ID=FILE, e.g. example-ext@example.com=/path/ctx.json")
    return gecko_id.strip(), path.strip()


def encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def read_context_file(path: str) -> bytes:
    p = Path(path).expanduser()
    try:
        data = p.read_bytes()
    except OSError as exc:
        raise ExtContextError(f"cannot read the context file {path}: {exc.strerror or type(exc).__name__}") from None
    if not data.strip():
        raise ExtContextError(f"the context file {path} is empty")
    if len(data) > MAX_BYTES:
        raise ExtContextError(f"the context file {path} is {len(data)} bytes; the limit is {MAX_BYTES}")
    try:
        value = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        # The parser's message can quote the content; keep only the reason.
        raise ExtContextError(f"the context file {path} is not UTF-8 JSON") from None
    if not isinstance(value, dict):
        raise ExtContextError(f"the context file {path} must hold a JSON object")
    return data


def prepare(profile: Profile, gecko_id: str, path: str) -> ExtContext:
    """Everything that can be checked before the browser starts."""
    data = read_context_file(path)
    loaded = []
    for ext_path in profile.extensions:
        try:
            info = inspect_extension(ext_path)
        except ExtensionError:
            continue
        if info.gecko_id != gecko_id:
            if info.gecko_id:
                loaded.append(info.gecko_id)
            continue
        if not info.options_page:
            raise ExtContextError(
                f"extension {gecko_id} has no options page (options_ui.page or options_page in its manifest)")
        return ExtContext(gecko_id=gecko_id, options_page=info.options_page, payload=encode(data))
    raise ExtContextError(
        f"profile {profile.name!r} does not load an extension with gecko id {gecko_id!r} "
        f"(it loads: {', '.join(loaded) or 'none'})")


# -- handing the context to a detached run -------------------------------------


def write_handoff(store: Store, name: str, ctx: ExtContext) -> Path:
    path = store.dir(name) / f"{HANDOFF_PREFIX}{secrets.token_hex(8)}.json"
    _write_private(path, json.dumps({"gecko_id": ctx.gecko_id, "options_page": ctx.options_page, "payload": ctx.payload}))
    return path


def take_handoff(store: Store, name: str, path: str) -> ExtContext:
    """Read and delete the hand-off file written by write_handoff."""
    p = Path(path)
    if p.parent.resolve() != store.dir(name).resolve() or not p.name.startswith(HANDOFF_PREFIX):
        raise ExtContextError("the context hand-off file must be one fpb wrote into the profile directory")
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise ExtContextError("the context hand-off file is missing or unreadable") from None
    finally:
        p.unlink(missing_ok=True)
    return ExtContext(gecko_id=data["gecko_id"], options_page=data["options_page"], payload=data["payload"])


def remove_stale_handoffs(store: Store, name: str, older_than: float = 600.0) -> None:
    """Hand-off files a crashed run left behind."""
    now = time.time()
    for p in store.dir(name).glob(f"{HANDOFF_PREFIX}*.json"):
        try:
            if now - p.stat().st_mtime > older_than:
                p.unlink()
        except OSError:
            pass


# -- in the running browser -------------------------------------------------------

_UUIDS_PREF = re.compile(r'user_pref\("extensions\.webextensions\.uuids",\s*("(?:[^"\\]|\\.)*")\);')


def extension_uuids(prefs_js: str) -> Dict[str, str]:
    """gecko id -> moz-extension host, from prefs.js (a JSON object stored as a string pref)."""
    m = _UUIDS_PREF.search(prefs_js)
    if not m:
        return {}
    try:
        value = json.loads(json.loads(m.group(1)))
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


def find_uuid(user_data_dir: Path, gecko_id: str, timeout: float = UUID_WAIT) -> Optional[str]:
    deadline = time.monotonic() + timeout
    while True:
        try:
            uuid = extension_uuids((Path(user_data_dir) / "prefs.js").read_text(encoding="utf-8", errors="replace")).get(gecko_id)
        except OSError:
            uuid = None
        if uuid:
            return str(uuid)
        if time.monotonic() >= deadline:
            return None
        time.sleep(0.5)


def inject(context: Any, user_data_dir: Path, ctx: ExtContext, *, uuid_wait: float = UUID_WAIT,
           hold: float = PAGE_HOLD) -> Dict[str, Any]:
    """Open the options page with the context in its hash; never raises.

    Playwright does not see a moz-extension:// page commit or load, so
    page.goto() times out although the page opens. The timeout therefore
    means "navigation issued"; any other error means it was not.
    """
    result: Dict[str, Any] = {"gecko_id": ctx.gecko_id, "injected": False, "error": ""}
    uuid = find_uuid(user_data_dir, ctx.gecko_id, uuid_wait)
    if not uuid:
        result["error"] = f"no moz-extension UUID for {ctx.gecko_id} in prefs.js after {uuid_wait:.0f}s (extension not loaded?)"
        return result
    page = None
    try:
        from playwright.sync_api import TimeoutError as PlaywrightTimeout

        page = context.new_page()
        try:
            page.goto(ctx.url(uuid), wait_until="commit", timeout=NAV_TIMEOUT_MS)
        except PlaywrightTimeout:
            pass
        result["injected"] = True
        time.sleep(hold)
    except Exception as exc:
        result["injected"] = False
        first_line = (str(exc).strip().splitlines() or [""])[0]
        result["error"] = redact(f"{type(exc).__name__}: {first_line}", ctx.secrets())[:300]
    finally:
        if page is not None:
            try:
                page.close()
            except Exception:
                pass
    return result
