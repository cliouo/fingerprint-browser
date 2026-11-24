"""Proxy URL parsing, masking and redaction.

A profile stores its proxy as one URL string, e.g.
``http://user:pass@host:8080`` or ``socks5://host:1080``. Special characters
in the user name or password must be percent-encoded (``p%40ss`` for ``p@ss``).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional
from urllib.parse import quote, unquote, urlsplit

SCHEMES = ("http", "https", "socks5")


class ProxyError(ValueError):
    """The proxy URL is not usable. The message never contains the password."""

    code = "invalid_proxy"


@dataclass(frozen=True)
class Proxy:
    scheme: str
    host: str
    port: int
    username: Optional[str] = None
    password: Optional[str] = None

    @property
    def server(self) -> str:
        host = f"[{self.host}]" if ":" in self.host else self.host
        return f"{self.scheme}://{host}:{self.port}"

    @property
    def has_auth(self) -> bool:
        return bool(self.username)

    def url(self) -> str:
        """The full URL, credentials included. Only for storage and for clients."""
        if not self.has_auth:
            return self.server
        auth = quote(self.username or "", safe="")
        if self.password is not None:
            auth += ":" + quote(self.password, safe="")
        return self.server.replace("://", f"://{auth}@", 1)

    def masked(self) -> str:
        """For display and logs: the user name is kept, the password is not."""
        if not self.has_auth:
            return self.server
        auth = quote(self.username or "", safe="")
        if self.password is not None:
            auth += ":***"
        return self.server.replace("://", f"://{auth}@", 1)

    def to_playwright(self) -> Dict[str, str]:
        """The ``proxy=`` mapping Playwright and Camoufox take."""
        out = {"server": self.server}
        if self.has_auth:
            out["username"] = self.username or ""
            out["password"] = self.password or ""
        return out

    def secrets(self) -> Iterable[str]:
        """Strings that must never be printed, in every encoding they may appear in."""
        if self.password:
            yield self.password
            encoded = quote(self.password, safe="")
            if encoded != self.password:
                yield encoded


def parse_proxy(text: str) -> Proxy:
    text = (text or "").strip()
    if not text:
        raise ProxyError("empty proxy URL")
    if "://" not in text:
        raise ProxyError("proxy URL needs a scheme: http://, https:// or socks5://")
    try:
        parts = urlsplit(text)
        port = parts.port
    except ValueError:
        # urlsplit's own message can echo the URL; keep only the reason class.
        raise ProxyError("proxy URL is malformed (check the port and any special characters)") from None
    scheme = parts.scheme.lower()
    if scheme == "socks5h":
        scheme = "socks5"  # Firefox resolves names through SOCKS5 proxies anyway
    if scheme not in SCHEMES:
        raise ProxyError(f"unsupported proxy scheme {parts.scheme!r}; use one of {', '.join(SCHEMES)}")
    if not parts.hostname:
        raise ProxyError("proxy URL has no host")
    if port is None:
        raise ProxyError("proxy URL has no port")
    if not 0 < port < 65536:
        raise ProxyError("proxy port out of range")
    if parts.path not in ("", "/") or parts.query or parts.fragment:
        raise ProxyError("proxy URL must not have a path, query or fragment")
    username = unquote(parts.username) if parts.username else None
    password = unquote(parts.password) if parts.password is not None else None
    if password and not username:
        raise ProxyError("proxy URL has a password but no user name")
    return Proxy(scheme=scheme, host=parts.hostname, port=port, username=username, password=password)


def redact(text: str, secrets: Iterable[str]) -> str:
    for secret in sorted({s for s in secrets if s}, key=len, reverse=True):
        text = text.replace(secret, "***")
    return text


_HOST_RE = re.compile(r"\.?[a-z0-9](?:[a-z0-9-]*[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]*[a-z0-9])?)*")


def bypass_matches(entry: str, host: str) -> bool:
    """How the browser (Juggler) applies a bypass entry: a plain suffix match on the host name."""
    return host.lower().endswith(entry.lower())


def parse_bypass(values: Iterable[str]) -> List[str]:
    """Host names (or `.domain` suffixes) that skip the proxy, from comma-separated values.

    The browser matches them as suffixes of the request's host name (no ports,
    no paths, no wildcards beyond a leading `*.`/`.`). Bypassed hosts see this
    machine's real IP, so never list the sites you browse through the proxy.
    """
    out: List[str] = []
    for value in values:
        for raw in (value or "").split(","):
            host = raw.strip().lower()
            if not host:
                continue
            if "://" in host or "/" in host or ":" in host:
                raise ProxyError(f"bypass entry {raw.strip()!r}: give a host name only (no scheme, port or path)")
            if host.startswith("*."):
                host = host[1:]
            if not _HOST_RE.fullmatch(host):
                raise ProxyError(f"bypass entry {raw.strip()!r} is not a host name")
            if "." not in host.strip(".") and host != "localhost":
                raise ProxyError(f"bypass entry {raw.strip()!r} is too broad (it is matched as a suffix of every host name)")
            if host not in out:
                out.append(host)
    return out
