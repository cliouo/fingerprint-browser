"""`fpb open --detach --json --ext-context` with a real browser.

  FPB_HOME=/tmp/fpbtest uv run python scripts/e2e/ext_context_check.py [--port 8100] [--profile ctx]

Starts the mock backend on --port, copies scripts/e2e/runner_ext pointed at
that port, creates a fresh profile with it, then twice (the first launch of a
new profile, and a re-open with a new context, as a caller that issues a new
credential per window would do):

  1. writes a context file (a JSON object with a random test token, mode 0600)
     and runs `fpb open PROFILE --detach --json --ext-context ID=FILE`;
  2. checks the JSON (ok, ext_context.injected) and that neither the token nor
     its base64url encoding is on any process's command line, in open.log,
     run.json, or left in a hand-off file;
  3. waits for the test extension's options page to report (through the mock)
     what it now holds in storage.local: the same object, and no hash left in
     its URL;
  4. `fpb close PROFILE --json`.

Prints a JSON summary; exits 1 if a check fails. Test only: the token is
random and means nothing.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import secrets
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
GECKO_ID = "fpb-runner-test@example.com"


def fpb(*argv: str) -> tuple:
    proc = subprocess.run([sys.executable, "-m", "fpb", *argv], capture_output=True, text=True, timeout=300)
    try:
        data = json.loads(proc.stdout)
    except ValueError:
        data = {"unparsed_stdout": proc.stdout[-2000:]}
    return proc.returncode, data, proc.stderr


def state(port: int) -> dict:
    with urllib.request.urlopen(f"http://127.0.0.1:{port}/__state", timeout=5) as resp:
        return json.loads(resp.read())


def one_round(args, workdir: Path, label: str) -> dict:
    token = "dct_" + secrets.token_urlsafe(32)[:43]
    ctx = {"v": 1, "service_url": "http://127.0.0.1:9000", "token": token, "channel": f"ch_{label}",
           "issued_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    ctx_file = workdir / f"ctx-{label}.json"
    fd = os.open(ctx_file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        json.dump(ctx, fh)
    payload = base64.urlsafe_b64encode(ctx_file.read_bytes()).decode().rstrip("=")
    reports_before = len(state(args.port).get("ext_contexts", []))

    t0 = time.monotonic()
    code, data, err = fpb("open", args.profile, "--detach", "--json", "--ext-context", f"{GECKO_ID}={ctx_file}")
    out = {"open_exit": code, "open_json": data, "open_s": round(time.monotonic() - t0, 1)}
    ps = subprocess.run(["ps", "-axww", "-o", "command="], capture_output=True, text=True).stdout
    ctx_file.unlink()  # what the caller does once `open` returned

    report = None
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline and report is None:
        reports = state(args.port).get("ext_contexts", [])
        report = reports[-1] if len(reports) > reports_before else None
        time.sleep(0.5)
    out["report"] = report

    _, listed, _ = fpb("list", "--json")
    view = next((p for p in listed.get("profiles", []) if p.get("name") == args.profile), {})
    out["state_while_open"] = view.get("state")

    home = Path(os.environ["FPB_HOME"]) / "profiles" / args.profile
    leaks = []
    if token in ps or payload in ps:
        leaks.append("process command line")
    for f in ("open.log", "run.json"):
        text = (home / f).read_text(errors="replace") if (home / f).exists() else ""
        if token in text or payload in text:
            leaks.append(f)
    if token in err or payload in err:
        leaks.append("fpb stderr")
    if list(home.glob(".ext_context.*")):
        leaks.append("hand-off file left behind")
    out["leaks"] = leaks

    code, closed, _ = fpb("close", args.profile, "--json")
    out["close_json"] = closed

    stored = (report or {}).get("stored") or {}
    out["checks"] = {
        "open ok, injected": code == 0 and data.get("ok") is True
        and (data.get("ext_context") or {}).get("injected") is True,
        "storage.local holds the context": {k: stored.get(k) for k in ctx} == ctx and "applied_at" in stored,
        "hash erased": report is not None and report.get("hash_after") == "" and "#" not in report.get("href", "#"),
        "open while running": out["state_while_open"] == "open",
        "no leak": not leaks,
        "closed": closed.get("ok") is True and closed.get("was_open") is True,
    }
    if stored.get("token"):  # random and meaningless, but no reason to print it whole
        stored["token"] = stored["token"][:8] + "..."
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8100)
    ap.add_argument("--profile", default="ctx")
    args = ap.parse_args()
    if not os.environ.get("FPB_HOME"):
        print("set FPB_HOME to a scratch directory (never test against your real ~/.fpb)", file=sys.stderr)
        return 2

    workdir = Path(tempfile.mkdtemp(prefix="fpb-extctx-"))
    ext = workdir / "runner_ext"
    shutil.copytree(HERE / "runner_ext", ext)
    for f in ("background.js", "options.js", "manifest.json"):
        p = ext / f
        p.write_text(p.read_text().replace("127.0.0.1:8100", f"127.0.0.1:{args.port}"))
    mock = subprocess.Popen([sys.executable, str(HERE / "mock_backend.py"), "--port", str(args.port)],
                            stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)
    try:
        time.sleep(1.5)
        if mock.poll() is not None:
            print(f"mock backend could not start on port {args.port}", file=sys.stderr)
            return 2
        code, created, err = fpb("new", args.profile, "--extension", str(ext), "--json")
        if code != 0:
            print(json.dumps(created), err, file=sys.stderr)
            return 1
        rounds = {"first launch": one_round(args, workdir, "1"), "re-open, new context": one_round(args, workdir, "2")}
    finally:
        mock.terminate()
        mock.wait(timeout=10)
        shutil.rmtree(workdir, ignore_errors=True)
    print(json.dumps(rounds, indent=2), flush=True)
    return 0 if all(all(r["checks"].values()) for r in rounds.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
