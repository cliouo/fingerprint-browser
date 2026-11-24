from fpb.store import Profile


def test_launch_kwargs_fixed_params(tmp_path, monkeypatch):
    from fpb.launcher import launch_kwargs
    from fpb.store import Store

    monkeypatch.setenv("FPB_HOME", str(tmp_path))
    store = Store()
    p = Profile(name="p", created_at="", proxy="http://u:pw@127.0.0.1:8080",
                identity={"os": "windows", "locale": "en-US", "preset": {"fpb_salt": "x"}})
    kw = launch_kwargs(store, p)
    assert kw["os"] == "windows" and kw["locale"] == "en-US" and kw["geoip"] is True
    assert kw["fingerprint_preset"] == {"fpb_salt": "x"} and kw["no_viewport"] is True
    # extensions that open their own tabs (chrome.tabs.create) need it
    assert kw["allow_addon_new_tab"] is True
    assert kw["proxy"] == {"server": "http://127.0.0.1:8080", "username": "u", "password": "pw"}


def test_allow_addon_new_tab_is_not_part_of_the_identity():
    from fpb import identity as ident

    base = {"navigator.userAgent": "UA", "screen.width": 1920}
    assert ident.digest(base) == ident.digest({**base, "allowAddonNewtab": True})


class IdleContext:
    """A context that never closes; `pages` stays empty (tabs Playwright does not track)."""

    def __init__(self, stop, ticks):
        self.pages, self._stop, self._ticks, self.calls = [], stop, ticks, 0

    def on(self, *_):
        pass

    def wait_for_event(self, *_a, **_k):
        from playwright.sync_api import TimeoutError as PlaywrightTimeout

        self.calls += 1
        if self.calls >= self._ticks:
            self._stop["v"] = True
        raise PlaywrightTimeout("tick")


def test_linux_never_closes_on_empty_page_list(monkeypatch):
    from fpb import launcher

    monkeypatch.setattr(launcher.sys, "platform", "linux")
    monkeypatch.setattr(launcher, "EMPTY_WINDOW_GRACE", 0.0)
    stop = {"v": False}
    ctx = IdleContext(stop, ticks=5)
    launcher._wait_until_closed(ctx, stop, "/x/user_data")
    assert ctx.calls == 5  # ran until stopped, did not return on the empty page list


def test_macos_uses_window_list_not_page_list(monkeypatch):
    from fpb import launcher

    monkeypatch.setattr(launcher.sys, "platform", "darwin")
    monkeypatch.setattr(launcher, "EMPTY_WINDOW_GRACE", 0.0)
    monkeypatch.setattr(launcher, "browser_pid", lambda d: 4242)
    windows = {"n": 1}
    monkeypatch.setattr(launcher, "macos_window_count", lambda pid: windows["n"])
    stop = {"v": False}
    ctx = IdleContext(stop, ticks=4)
    launcher._wait_until_closed(ctx, stop, "/x/user_data")
    assert ctx.calls == 4  # window on screen: kept running despite no tracked pages

    windows["n"] = 0
    stop = {"v": False}
    ctx = IdleContext(stop, ticks=50)
    launcher._wait_until_closed(ctx, stop, "/x/user_data")
    assert ctx.calls < 50  # no window on screen and no page: the person closed it


def test_macos_minimized_window_is_not_closed(monkeypatch):
    from fpb import launcher

    monkeypatch.setattr(launcher, "macos_window_count", lambda pid: 0)  # minimized / other Space
    ctx = IdleContext({"v": False}, ticks=1)
    ctx.pages = ["page"]
    assert launcher._window_gone(ctx, 4242) is False
    ctx.pages = []
    assert launcher._window_gone(ctx, 4242) is True


def test_macos_falls_back_to_pages_without_window_list(monkeypatch):
    from fpb import launcher

    monkeypatch.setattr(launcher, "macos_window_count", lambda pid: None)
    ctx = IdleContext({"v": False}, ticks=1)
    assert launcher._window_gone(ctx, 4242) is True
    ctx.pages = ["page"]
    assert launcher._window_gone(ctx, 4242) is False
    assert launcher._window_gone(ctx, None) is False


def test_launch_kwargs_bypass(tmp_path, monkeypatch):
    from fpb.launcher import launch_kwargs
    from fpb.store import Store

    monkeypatch.setenv("FPB_HOME", str(tmp_path))
    p = Profile(name="p", created_at="", proxy="http://u:pw@127.0.0.1:8080", bypass=["api.example.com", ".x.test"],
                identity={"os": "windows", "locale": "en-US", "preset": {"fpb_salt": "s"}})
    kw = launch_kwargs(Store(), p)
    assert kw["proxy"] == {"server": "http://127.0.0.1:8080", "username": "u", "password": "pw",
                           "bypass": "api.example.com,.x.test"}
    p.bypass = []
    assert "bypass" not in launch_kwargs(Store(), p)["proxy"]
