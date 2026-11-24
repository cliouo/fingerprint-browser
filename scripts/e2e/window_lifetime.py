"""Does `fpb open` keep the window while an extension opens and closes tabs? (Linux, under Xvfb)

  FPB_HOME=... python scripts/e2e/window_lifetime.py PROFILE [--legacy] [--watch 90]

PROFILE must load scripts/e2e/runner_ext (three rounds: an active
runner tab, a results POST to the mock on 127.0.0.1:8100, the tab removed).
Runs the profile in the foreground exactly like `fpb open` (run_open with
keep_open=True), logging every page Playwright tracks, then checks:

  1. all three results reached the mock,
  2. the runner is still alive after the rounds (the window was never closed),
  3. closing the window (Ctrl+Shift+W through xdotool) ends the runner.

--legacy swaps in fpb's earlier close check ("no Playwright page for 3 s =
closed", on every OS) to see whether it ends the browser early. Prints a JSON
summary; exits 1 if a check fails (only checks 1-3 without --legacy).
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.request

MOCK = "http://127.0.0.1:8100"


def legacy_wait_until_closed(context, stop, user_data_dir=None):
    """fpb's close check before this fix: empty page list for 3 s = closed, on every OS."""
    from playwright.sync_api import Error as PlaywrightError
    from playwright.sync_api import TimeoutError as PlaywrightTimeout

    closed = {"v": False}
    context.on("close", lambda *_: closed.__setitem__("v", True))
    empty_since = None
    while not closed["v"] and not stop["v"]:
        try:
            context.wait_for_event("close", timeout=1000)
            return
        except PlaywrightTimeout:
            pass
        except PlaywrightError:
            return
        if context.pages:
            empty_since = None
        else:
            empty_since = empty_since or time.monotonic()
            if time.monotonic() - empty_since > 3.0:
                print("[child] legacy check: no Playwright page for 3 s -> closing", flush=True)
                return


def child(profile_name: str, legacy: bool) -> int:
    from fpb import launcher
    from fpb.store import Store

    if legacy:
        launcher._wait_until_closed = legacy_wait_until_closed
    t0 = time.monotonic()

    def log(msg):
        print(f"[child] t+{time.monotonic() - t0:5.1f}s {msg}", flush=True)

    def on_ready(context):
        def opened(page):
            log(f"page opened {page.url[:60]} (tracked: {len(context.pages)})")
            page.on("close", lambda p: log(f"page closed (tracked: {len([x for x in context.pages if x is not p])})"))
        for page in context.pages:
            opened(page)
        context.on("page", opened)

    store = Store()
    launcher.run_open(store, store.load(profile_name), on_ready=on_ready, keep_open=True)
    log("run_open returned")
    return 0


def results() -> int:
    with urllib.request.urlopen(MOCK + "/__state", timeout=5) as resp:
        return len(json.loads(resp.read()).get("results", []))


def browser_windows() -> list:
    try:
        return subprocess.run(["xdotool", "search", "--onlyvisible", "--class", "camoufox"],
                              capture_output=True, text=True).stdout.split()
    except FileNotFoundError:  # no xdotool (macOS)
        return []


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("profile")
    ap.add_argument("--legacy", action="store_true")
    ap.add_argument("--watch", type=float, default=90)
    ap.add_argument("--child", action="store_true", help=argparse.SUPPRESS)
    args = ap.parse_args()
    if args.child:
        return child(args.profile, args.legacy)

    before = results()
    cmd = [sys.executable, __file__, args.profile, "--child"] + (["--legacy"] if args.legacy else [])
    proc = subprocess.Popen(cmd, env=dict(os.environ, PYTHONUNBUFFERED="1"))
    started = time.monotonic()
    died_at = None
    while time.monotonic() - started < args.watch:
        if proc.poll() is not None:
            died_at = round(time.monotonic() - started, 1)
            break
        time.sleep(1)
    summary = {
        "mode": "legacy" if args.legacy else "fix",
        "results_posted": results() - before,
        "runner_alive_after_watch": proc.poll() is None,
        "runner_exited_at_s": died_at,
        "browser_windows": len(browser_windows()),
    }
    if proc.poll() is None:
        wins = browser_windows()
        if wins:
            # No window manager under bare Xvfb: focus it directly, then type
            # Firefox's "close window" shortcut through XTEST.
            subprocess.run(["xdotool", "windowactivate", "--sync", wins[0]], capture_output=True, timeout=10)
            subprocess.run(["xdotool", "windowfocus", "--sync", wins[0]], capture_output=True, timeout=10)
            subprocess.run(["xdotool", "key", "ctrl+shift+w"], capture_output=True, timeout=10)
        t = time.monotonic()
        try:
            proc.wait(timeout=30)
            summary["closed_by_window_s"] = round(time.monotonic() - t, 1)
        except subprocess.TimeoutExpired:
            summary["closed_by_window_s"] = None
            proc.terminate()
            proc.wait(timeout=30)
    print(json.dumps(summary, indent=2), flush=True)
    if args.legacy:
        return 0
    ok = (summary["results_posted"] >= 3 and summary["runner_alive_after_watch"]
          and summary.get("closed_by_window_s") is not None)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
