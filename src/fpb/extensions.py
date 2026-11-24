"""Extension directories: validation and summary.

Camoufox installs each directory as a temporary add-on on every launch.
Firefox keys extension storage by the add-on id, so an extension without a
fixed gecko id gets a fresh random id, and loses its storage (logins), on
every launch.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Tuple
from urllib.parse import urlsplit


class ExtensionError(ValueError):
    code = "invalid_extension"


@dataclass(frozen=True)
class ExtensionInfo:
    path: str
    name: str
    version: str
    gecko_id: Optional[str]
    hosts: Tuple[str, ...] = ()  # host names from host_permissions (and MV2 URL permissions)
    options_page: Optional[str] = None  # options_ui.page or options_page, relative to the extension root

    def label(self) -> str:
        gid = self.gecko_id or "NO GECKO ID"
        return f"{self.name} {self.version} [{gid}] {self.path}"


def gecko_id(manifest: dict) -> Optional[str]:
    for key in ("browser_specific_settings", "applications"):
        gecko = (manifest.get(key) or {}).get("gecko") or {}
        if gecko.get("id"):
            return str(gecko["id"])
    return None


def permission_hosts(manifest: dict) -> List[str]:
    out: List[str] = []
    for perm in list(manifest.get("host_permissions") or []) + list(manifest.get("permissions") or []):
        if not isinstance(perm, str) or "://" not in perm:
            continue
        host = (urlsplit(perm.replace("*://", "https://", 1)).hostname or "").lstrip("*.")
        if host and host not in out:
            out.append(host)
    return out


def options_page(manifest: dict) -> Optional[str]:
    """The extension's options page (path inside the extension), if it declares one."""
    for page in ((manifest.get("options_ui") or {}).get("page"), manifest.get("options_page")):
        if isinstance(page, str) and page.strip():
            return page.strip().lstrip("/").removeprefix("./") or None
    return None


# Never proxied by Firefox anyway, so never worth a bypass hint.
LOCAL_HOSTS = ("localhost", "127.0.0.1", "[::1]", "::1")


def bypass_candidates(info: "ExtensionInfo") -> List[str]:
    """Hosts from the extension's host permissions that are not local (one may be its own backend)."""
    return [h for h in info.hosts if h not in LOCAL_HOSTS]


def inspect_extension(path: str) -> ExtensionInfo:
    p = Path(path).expanduser()
    if p.suffix.lower() in (".xpi", ".zip", ".crx"):
        raise ExtensionError(f"{path}: pass the extracted directory (the one with manifest.json), not a {p.suffix} file")
    if not p.is_dir():
        raise ExtensionError(f"{path}: not a directory")
    manifest_path = p / "manifest.json"
    if not manifest_path.is_file():
        raise ExtensionError(f"{path}: no manifest.json (point at the built dist directory)")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except ValueError as exc:
        raise ExtensionError(f"{path}: manifest.json is not valid JSON ({exc})") from None
    return ExtensionInfo(
        path=str(p.resolve()),
        name=str(manifest.get("name") or p.name),
        version=str(manifest.get("version") or "?"),
        gecko_id=gecko_id(manifest),
        hosts=tuple(permission_hosts(manifest)),
        options_page=options_page(manifest),
    )


def validate_extension(path: str, allow_no_gecko_id: bool = False) -> ExtensionInfo:
    info = inspect_extension(path)
    if not info.gecko_id and not allow_no_gecko_id:
        raise ExtensionError(
            f"{path}: manifest has no browser_specific_settings.gecko.id, so Firefox would give it a new "
            "random id (and empty storage, i.e. no saved login) on every launch. Add a fixed gecko id, "
            "or pass --allow-no-gecko-id to load it anyway."
        )
    return info
