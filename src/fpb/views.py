"""What `--json` prints about a profile. No proxy password ever goes in here.

Profile view:
  {"name", "state": "open"|"closed"|"broken", "pid": int|null,
   "proxy": "<URL, password as ***>"|"", "proxy_user": "<user name>"|"",
   "bypass": [...], "extensions": [{"path", "gecko_id", "name", "version", "error"}],
   "identity_digest", "dir", "error": ""}
A profile whose profile.json cannot be read is {"name", "state": "broken", "error"}.
Consumers must ignore fields they do not know; fields are only ever added.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from .extensions import ExtensionError, inspect_extension
from .store import Store


def extension_view(path: str) -> Dict[str, Any]:
    try:
        info = inspect_extension(path)
    except ExtensionError as exc:
        return {"path": path, "gecko_id": None, "name": None, "version": None, "error": str(exc)}
    return {"path": info.path, "gecko_id": info.gecko_id, "name": info.name, "version": info.version, "error": ""}


def profile_view(store: Store, name: str) -> Dict[str, Any]:
    try:
        p = store.load(name)
        proxy = p.proxy_obj()
    except Exception as exc:  # unreadable profile.json, or a stored proxy URL that no longer parses
        return {"name": name, "state": "broken", "error": f"{type(exc).__name__}: {exc}"[:300]}
    pid: Optional[int] = store.running_pid(name)
    return {
        "name": p.name,
        "state": "open" if pid else "closed",
        "pid": pid,
        "proxy": proxy.masked() if proxy else "",
        "proxy_user": (proxy.username or "") if proxy else "",
        "bypass": list(p.bypass),
        "extensions": [extension_view(path) for path in p.extensions],
        "identity_digest": p.identity_digest or "",
        "dir": str(store.dir(p.name)),
        "error": "",
    }
