"""Camoufox alone (no fpb code): a SOCKS5 proxy with a user/password. Upstream info only.

fpb is pinned to camoufox 0.5.9, which has no SOCKS5-auth support at all. This
needs camoufox >= 0.5.10b1 and browser >= 159.0-beta.42; run it in a scratch
HOME so the newer browser does not land in (or become active in) the real cache:

  HOME=/tmp/scratch uv run --no-project --with 'camoufox[geoip]==0.5.10b2' camoufox fetch
  HOME=/tmp/scratch PROXY_PASS=... uv run --no-project --with 'camoufox[geoip]==0.5.10b2' \
      python scripts/e2e/socks5_persistent_repro.py socks5://127.0.0.1:18091 USER

Start scripts/e2e/local_proxy.py socks5 first; its log shows, per connection,
whether the browser offered user/password auth. Three launches, each loading
https://api.ipify.org through the proxy:

  A. Camoufox(proxy=...)                          new_context()/new_page()
  B. Camoufox(proxy=..., persistent_context=True)  the persistent context
  C. B plus the per-context credentials init script (what fpb briefly did; removed)

With camoufox 0.5.10b2 + browser 159.0-beta.43, A and C load and B fails
(NS_ERROR_CONNECTION_REFUSED; the proxy logs "client offered methods [0]"):
Playwright also sets the proxy, without credentials, on the persistent
context, and Juggler prefers a context's proxy to the browser-level one that
got the CAMOU_PROXY_AUTH credentials.
"""

from __future__ import annotations

import os
import sys
import tempfile
import warnings

from camoufox.ip import proxy_auth_init_script, socks5_auth
from camoufox.sync_api import Camoufox

warnings.simplefilter("ignore")
URL = "https://api.ipify.org/?format=json"


def load(page) -> str:
    try:
        page.goto(URL, wait_until="domcontentloaded", timeout=30_000)
        return "OK " + page.inner_text("body")[:60]
    except Exception as exc:
        return "FAILED " + str(exc).splitlines()[0][:100]


def main() -> int:
    server, user = sys.argv[1], sys.argv[2]
    proxy = {"server": server, "username": user, "password": os.environ["PROXY_PASS"]}
    common = dict(os="windows", locale="en-US", geoip=True, headless=True, proxy=proxy)

    with Camoufox(**common) as browser:
        print("A. non-persistent, new_page():  ", load(browser.new_page()), flush=True)

    with tempfile.TemporaryDirectory() as d:
        with Camoufox(**common, persistent_context=True, user_data_dir=d) as context:
            print("B. persistent_context:          ", load(context.new_page()), flush=True)

    with tempfile.TemporaryDirectory() as d:
        with Camoufox(**common, persistent_context=True, user_data_dir=d) as context:
            _bare, auth = socks5_auth(proxy)
            context.add_init_script(script=proxy_auth_init_script(auth))
            print("C. persistent + init script:    ", load(context.new_page()), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
