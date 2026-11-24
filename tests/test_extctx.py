"""Passing a context to an extension's options page (no browser: Playwright is faked)."""

import argparse
import base64
import json

import pytest

from fpb import extctx
from fpb.extensions import options_page
from fpb.store import Profile, Store

UUID = "03a0d828-a7f0-4e0e-9993-0d810da83975"
PREFS = (
    '// Mozilla User Preferences\n'
    'user_pref("browser.startup.page", 1);\n'
    'user_pref("extensions.webextensions.uuids", "{\\"example-ext@example.com\\":\\"' + UUID + '\\",'
    '\\"other@example.org\\":\\"11111111-2222-3333-4444-555555555555\\"}");\n'
)


def test_parse_arg():
    assert extctx.parse_arg("example-ext@example.com=/tmp/a=b.json") == ("example-ext@example.com", "/tmp/a=b.json")
    for bad in ("x", "=file", "id=", ""):
        with pytest.raises(argparse.ArgumentTypeError):
            extctx.parse_arg(bad)


def test_options_page_from_manifest():
    assert options_page({"options_ui": {"page": "src/options/options.html"}}) == "src/options/options.html"
    assert options_page({"options_page": "/options.html"}) == "options.html"
    assert options_page({"options_ui": {"page": "./o.html"}, "options_page": "p.html"}) == "o.html"
    assert options_page({}) is None


def test_payload_and_url():
    raw = json.dumps({"v": 1, "k": "é?/+"}).encode()
    ctx = extctx.ExtContext("example-ext@example.com", "src/options/options.html", extctx.encode(raw))
    assert "=" not in ctx.payload and "+" not in ctx.payload and "/" not in ctx.payload
    assert base64.urlsafe_b64decode(ctx.payload + "=" * (-len(ctx.payload) % 4)) == raw
    assert ctx.url(UUID) == f"moz-extension://{UUID}/src/options/options.html#fpbctx={ctx.payload}"


def test_uuid_from_prefs(tmp_path):
    assert extctx.extension_uuids(PREFS)["example-ext@example.com"] == UUID
    assert extctx.extension_uuids("user_pref(\"x\", 1);") == {}
    (tmp_path / "prefs.js").write_text(PREFS)
    assert extctx.find_uuid(tmp_path, "example-ext@example.com", timeout=0) == UUID
    assert extctx.find_uuid(tmp_path, "missing@example.com", timeout=0) is None
    assert extctx.find_uuid(tmp_path / "nowhere", "example-ext@example.com", timeout=0) is None


def test_handoff_roundtrip_and_guard(tmp_path):
    store = Store(tmp_path / "home")
    store.create(Profile(name="p", created_at=""))
    ctx = extctx.ExtContext("example-ext@example.com", "o.html", "cGF5bG9hZA")
    path = extctx.write_handoff(store, "p", ctx)
    assert path.parent == store.dir("p") and (path.stat().st_mode & 0o777) == 0o600
    assert extctx.take_handoff(store, "p", str(path)) == ctx
    assert not path.exists()
    with pytest.raises(extctx.ExtContextError):
        extctx.take_handoff(store, "p", str(path))  # read once only
    outside = tmp_path / ".ext_context.x.json"
    outside.write_text("{}")
    with pytest.raises(extctx.ExtContextError):
        extctx.take_handoff(store, "p", str(outside))


class FakePage:
    def __init__(self, error=None, errors_first=()):
        self.error, self.errors_first, self.urls, self.closed = error, list(errors_first), [], False

    def goto(self, url, **kwargs):
        self.urls.append((url, kwargs))
        if self.errors_first:
            raise self.errors_first.pop(0)
        if self.error:
            raise self.error

    def close(self):
        self.closed = True


class FakeContext:
    def __init__(self, page):
        self.page = page

    def new_page(self):
        return self.page


@pytest.fixture
def user_data(tmp_path):
    (tmp_path / "prefs.js").write_text(PREFS)
    return tmp_path


CTX = extctx.ExtContext("example-ext@example.com", "src/options/options.html", "c2VjcmV0LWNvbnRleHQ")


def test_inject_timeout_means_navigation_issued(user_data):
    from playwright.sync_api import TimeoutError as PlaywrightTimeout

    page = FakePage(PlaywrightTimeout("Timeout 2000ms exceeded"))
    out = extctx.inject(FakeContext(page), user_data, CTX, uuid_wait=0, hold=0)
    assert out == {"gecko_id": "example-ext@example.com", "injected": True, "error": ""}
    (url, kwargs), = page.urls
    assert url == CTX.url(UUID) and kwargs["wait_until"] == "commit" and kwargs["timeout"] == extctx.NAV_TIMEOUT_MS
    assert page.closed


def test_inject_other_errors_fail_without_leaking_the_payload(user_data):
    page = FakePage(RuntimeError(f'NS_ERROR_FAILURE navigating to "{CTX.url(UUID)}"\nCall log: ...'))
    out = extctx.inject(FakeContext(page), user_data, CTX, uuid_wait=0, hold=0)
    assert out["injected"] is False and "NS_ERROR_FAILURE" in out["error"]
    assert CTX.payload not in out["error"] and "Call log" not in out["error"]
    assert page.closed


def test_inject_retries_until_the_addon_is_installed(user_data):
    """A re-launch knows the UUID at once, but the add-on's pages fail until it is installed again."""
    from playwright.sync_api import Error as PlaywrightError
    from playwright.sync_api import TimeoutError as PlaywrightTimeout

    not_yet = [PlaywrightError("Page.goto: NS_ERROR_NOT_AVAILABLE") for _ in range(3)]
    page = FakePage(PlaywrightTimeout("Timeout 2000ms exceeded"), errors_first=not_yet)
    out = extctx.inject(FakeContext(page), user_data, CTX, uuid_wait=5, hold=0, retry=0.01)
    assert out["injected"] is True and len(page.urls) == 4 and page.closed

    page = FakePage(PlaywrightError("Page.goto: NS_ERROR_NOT_AVAILABLE"))
    out = extctx.inject(FakeContext(page), user_data, CTX, uuid_wait=0.05, hold=0, retry=0.01)
    assert out["injected"] is False and "NS_ERROR_NOT_AVAILABLE" in out["error"] and len(page.urls) >= 2


def test_inject_without_uuid(tmp_path):
    page = FakePage()
    out = extctx.inject(FakeContext(page), tmp_path, CTX, uuid_wait=0, hold=0)
    assert out["injected"] is False and "prefs.js" in out["error"] and not page.urls
