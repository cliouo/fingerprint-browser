"""`--json` output of every command a program drives: shapes, error codes, exit codes, no secrets.

Shapes are checked against tests/fixtures/json_contract_v1.json (copied from
the interface contract, mvp-api.md v1.0 section 6.2).
"""

import base64
import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from fpb import cli, extctx, launcher
from fpb.store import Store

SECRET = "Sup3rS3cret"
FIXTURES = json.loads((Path(__file__).parent / "fixtures" / "json_contract_v1.json").read_text())
VIEW_KEYS = set(FIXTURES["profile_view"])
EXT_KEYS = set(FIXTURES["profile_view"]["extensions"][0]) | {"error"}  # "error": fpb addition
TOKEN = FIXTURES["injected_context"]["daemon_token"]


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("FPB_HOME", str(tmp_path / "fpbhome"))

    def fake_identity(store, profile):
        profile.identity = {"os": "windows", "locale": "en-US", "preset": {"screen": {"width": 1920, "height": 1080}}}
        profile.identity_digest = "d" * 32
        profile.identity_summary = {"screen.width": 1920, "screen.height": 1080}

    monkeypatch.setattr(cli, "_identity_for_new", fake_identity)
    return tmp_path / "fpbhome"


@pytest.fixture
def ext(tmp_path):
    d = tmp_path / "ext"
    d.mkdir()
    (d / "manifest.json").write_text(json.dumps({
        "name": "Example Extension", "version": "0.2.0",
        "browser_specific_settings": {"gecko": {"id": "example-ext@example.com"}},
        "host_permissions": ["https://api.example.com/*"],
        "options_ui": {"page": "src/options/options.html"}}))
    return d


@pytest.fixture
def ctx_file(tmp_path):
    f = tmp_path / "ctx.json"
    f.write_text(json.dumps(FIXTURES["injected_context"]))
    f.chmod(0o600)
    return f


def run(capsys, *argv):
    """(exit code, parsed stdout, stderr); stdout must be exactly one JSON object."""
    code = cli.main(list(argv))
    cap = capsys.readouterr()
    assert cap.out.count("\n") == 1 and cap.out.endswith("\n"), cap.out
    data = json.loads(cap.out)
    assert isinstance(data, dict)
    assert data["ok"] is (code == 0)
    if code:
        assert code == 1 and set(data) == {"ok", "error"} and set(data["error"]) == {"code", "message"}
    return code, data, cap.err


def new(capsys, ext, name="work1", *extra):
    code, data, err = run(capsys, "new", name, "--proxy", f"http://user1:{SECRET}@127.0.0.1:8080",
                          "--extension", str(ext), "--bypass", "127.0.0.1,localhost,api.example.com", "--json", *extra)
    assert code == 0, data
    return data


def assert_view(view, state="closed"):
    assert set(view) == VIEW_KEYS, set(view) ^ VIEW_KEYS
    assert view["state"] == state and view["error"] == ""
    for e in view["extensions"]:
        assert set(e) == EXT_KEYS


def fake_run(store, name, pid, **extra):
    store.write_run(name, {"pid": pid, "started_at": "", "ready": True, **extra})


# -- success shapes -------------------------------------------------------------------


def test_new_show_list_shapes_and_no_password(home, ext, capsys):
    data = new(capsys, ext)
    assert set(data) == {"ok", "profile"}
    view = data["profile"]
    assert_view(view)
    assert view["name"] == "work1" and view["pid"] is None
    assert view["proxy"] == "http://user1:***@127.0.0.1:8080" and view["proxy_user"] == "user1"
    assert view["bypass"] == ["127.0.0.1", "localhost", "api.example.com"]
    assert view["extensions"] == [{"path": str(ext.resolve()), "gecko_id": "example-ext@example.com",
                                   "name": "Example Extension", "version": "0.2.0", "error": ""}]
    assert view["identity_digest"] == "d" * 32 and view["dir"] == str(home / "profiles" / "work1")

    code, data, err = run(capsys, "show", "work1", "--json")
    assert code == 0 and set(data) == {"ok", "profile"}
    shown = data["profile"]
    assert VIEW_KEYS <= set(shown)
    # what `show --json` printed in fpb 0.1 is still there
    assert {"created_at", "open_pid", "identity_summary"} <= set(shown)

    code, data, err = run(capsys, "list", "--json")
    assert code == 0 and set(data) == {"ok", "profiles"} and len(data["profiles"]) == 1
    assert_view(data["profiles"][0])
    assert SECRET not in json.dumps(data) + err


def test_new_json_prints_no_hint_text(home, ext, capsys):
    code, data, err = run(capsys, "new", "p", "--proxy", f"http://u:{SECRET}@127.0.0.1:8080",
                          "--extension", str(ext), "--json")
    assert code == 0 and "note:" not in err and SECRET not in err


def test_no_proxy_view_and_list_empty(home, capsys):
    code, data, _ = run(capsys, "list", "--json")
    assert code == 0 and data == {"ok": True, "profiles": []}
    code, data, _ = run(capsys, "new", "p", "--json")
    view = data["profile"]
    assert view["proxy"] == "" and view["proxy_user"] == "" and view["bypass"] == [] and view["extensions"] == []


def test_list_marks_broken_and_open_profiles(home, ext, capsys):
    new(capsys, ext, "a")
    new(capsys, ext, "b")
    store = Store()
    (store.dir("b") / "profile.json").write_text("{not json")
    fake_run(store, "a", os.getpid())  # pytest is a python process: counts as an fpb runner
    code, data, _ = run(capsys, "list", "--json")
    a, b = data["profiles"]
    assert_view(a, state="open")
    assert a["pid"] == os.getpid()
    assert set(b) == set(FIXTURES["profile_view_broken"]) and b["state"] == "broken" and b["error"]
    assert err_code(capsys, "show", "b", "--json") == "profile_broken"


def test_missing_extension_dir_is_reported_in_view(home, ext, capsys):
    new(capsys, ext)
    (ext / "manifest.json").unlink()
    code, data, _ = run(capsys, "show", "work1", "--json")
    (e,) = data["profile"]["extensions"]
    assert e["gecko_id"] is None and e["version"] is None and "manifest.json" in e["error"]


def test_set_commands_return_the_view(home, ext, capsys):
    new(capsys, ext)
    code, data, _ = run(capsys, "set-proxy", "work1", f"http://user2:{SECRET}@127.0.0.1:8080", "--json")
    assert code == 0 and data["profile"]["proxy_user"] == "user2" and SECRET not in json.dumps(data)
    assert_view(data["profile"])
    code, data, _ = run(capsys, "set-bypass", "work1", "127.0.0.1,localhost", "--json")
    assert code == 0 and data["profile"]["bypass"] == ["127.0.0.1", "localhost"]
    code, data, _ = run(capsys, "set-bypass", "work1", "none", "--json")
    assert code == 0 and data["profile"]["bypass"] == []
    code, data, _ = run(capsys, "set-extensions", "work1", "--json")
    assert code == 0 and data["profile"]["extensions"] == []
    code, data, _ = run(capsys, "set-extensions", "work1", "--extension", str(ext), "--json")
    assert code == 0 and data["profile"]["extensions"][0]["version"] == "0.2.0"


def test_close_shapes(home, ext, capsys):
    new(capsys, ext)
    code, data, _ = run(capsys, "close", "work1", "--json")
    assert code == 0 and data == {"ok": True, "name": "work1", "was_open": False, "pid": None}
    # An orphaned python process (not our child, so it does not linger as a zombie once stopped).
    spawn = ("import subprocess, sys; "
             "p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'], "
             "stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL); print(p.pid)")
    pid = int(subprocess.run([sys.executable, "-c", spawn], capture_output=True, text=True, check=True).stdout)
    try:
        fake_run(Store(), "work1", pid)
        code, data, _ = run(capsys, "close", "work1", "--json")
        assert code == 0 and data == {"ok": True, "name": "work1", "was_open": True, "pid": pid}
        assert set(data) == set(FIXTURES["close"])
    finally:
        try:
            os.kill(pid, 9)
        except ProcessLookupError:
            pass


# -- error codes ----------------------------------------------------------------------


def err_code(capsys, *argv):
    code, data, err = run(capsys, *argv)
    assert code == 1, data
    assert SECRET not in json.dumps(data) + err
    return data["error"]["code"]


def test_error_codes(home, ext, capsys, tmp_path):
    for argv in (["show", "nope"], ["set-proxy", "nope", "none"], ["set-bypass", "nope", "none"],
                 ["set-extensions", "nope"], ["open", "nope", "--detach"], ["close", "nope"]):
        assert err_code(capsys, *argv, "--json") == "profile_not_found", argv
    assert err_code(capsys, "show", "bad/name", "--json") == "invalid_name"
    new(capsys, ext)
    assert err_code(capsys, "new", "work1", "--json") == "profile_exists"
    assert err_code(capsys, "new", "p", "--proxy", "ftp://h:1", "--json") == "invalid_proxy"
    assert err_code(capsys, "new", "p", "--proxy", f"socks5://u:{SECRET}@127.0.0.1:1080", "--json") == "invalid_proxy"
    assert err_code(capsys, "new", "p", "--extension", str(tmp_path / "missing"), "--json") == "invalid_extension"
    assert err_code(capsys, "new", "p", "--bypass", "com", "--proxy", "http://127.0.0.1:1", "--json") == "invalid_bypass"
    assert err_code(capsys, "new", "p", "--bypass", "api.example.com", "--json") == "invalid_bypass"
    assert err_code(capsys, "set-proxy", "work1", "http://", "--json") == "invalid_proxy"
    assert err_code(capsys, "set-bypass", "work1", "a/b", "--json") == "invalid_bypass"
    assert err_code(capsys, "set-extensions", "work1", "--extension", str(tmp_path), "--json") == "invalid_extension"
    run(capsys, "new", "direct", "--json")
    assert err_code(capsys, "set-bypass", "direct", "api.example.com", "--json") == "invalid_bypass"
    assert Store().names() == ["direct", "work1"]

    fake_run(Store(), "work1", os.getpid())
    assert err_code(capsys, "set-proxy", "work1", "none", "--json") == "profile_open"
    assert err_code(capsys, "set-bypass", "work1", "none", "--json") == "profile_open"
    assert err_code(capsys, "open", "work1", "--detach", "--json") == "already_open"


def test_new_browser_missing_and_identity_failed(tmp_path, monkeypatch, capsys):
    from fpb import doctor

    monkeypatch.setenv("FPB_HOME", str(tmp_path / "h"))
    monkeypatch.setattr(doctor, "find_browser", lambda: None)
    assert err_code(capsys, "new", "p", "--json") == "browser_missing"
    monkeypatch.setattr(launcher, "require_browser", lambda: None)

    def broken(*_a, **_k):
        raise launcher.LaunchError("could not prepare the launch: boom")

    monkeypatch.setattr(launcher, "compute_options", broken)
    assert err_code(capsys, "new", "p", "--json") == "identity_failed"
    assert Store().names() == []


def test_close_failed(home, ext, capsys, monkeypatch):
    new(capsys, ext)
    fake_run(Store(), "work1", os.getpid())
    monkeypatch.setattr(cli, "stop_pid", lambda pid: False)
    assert err_code(capsys, "close", "work1", "--json") == "close_failed"


def test_unexpected_exception_is_internal_and_redacted(home, ext, capsys, monkeypatch):
    new(capsys, ext)

    def boom(store, name):
        raise RuntimeError(f"leaked http://user1:{SECRET}@127.0.0.1:8080")

    monkeypatch.setattr(cli, "profile_view", boom)
    code, data, err = run(capsys, "show", "work1", "--json")
    assert code == 1 and data["error"]["code"] == "internal"
    assert "leaked" in data["error"]["message"] and SECRET not in data["error"]["message"]


def test_argparse_errors_keep_exit_2(home, capsys):
    for argv in (["show", "--json"], ["open", "p", "--json"], ["open", "p", "--detach", "--ext-context", "no-equals"]):
        with pytest.raises(SystemExit) as exc:
            cli.main(argv)
        assert exc.value.code == 2, argv
    capsys.readouterr()


def test_text_mode_unchanged_for_missing_profile_close(home, capsys):
    assert cli.main(["close", "nope"]) == 0
    assert "not open" in capsys.readouterr().out


# -- open --detach --json and --ext-context -------------------------------------------


class FakeChild:
    """Stands in for the background `fpb open`: records how it was started, plays its part."""

    def __init__(self, store, name, behaviour, calls):
        self.store, self.name, self.behaviour, self.calls = store, name, behaviour, calls
        self.pid = os.getpid()

    def __call__(self, cmd, **kwargs):
        self.calls["cmd"], self.calls["env"] = cmd, kwargs.get("env") or {}
        if "--ext-context-handoff" in cmd:
            path = Path(cmd[cmd.index("--ext-context-handoff") + 1])
            self.calls["handoff_mode"] = stat.S_IMODE(path.stat().st_mode)
            self.calls["ctx"] = extctx.take_handoff(self.store, self.name, str(path))
            self.calls["handoff_left"] = path.exists()
        if self.behaviour == "ready":
            outcome = None
            if "ctx" in self.calls:
                outcome = {"gecko_id": self.calls["ctx"].gecko_id, "injected": True, "error": ""}
            fake_run(self.store, self.name, self.pid, **({"ext_context": outcome} if outcome else {}))
        return self

    def poll(self):
        return 1 if self.behaviour == "exits" else None


@pytest.fixture
def detached(home, ext, capsys, monkeypatch):
    new(capsys, ext)
    monkeypatch.setattr(launcher, "check_browser_for", lambda profile: None)
    calls = {}

    def use(behaviour="ready"):
        monkeypatch.setattr(launcher.subprocess, "Popen", FakeChild(Store(), "work1", behaviour, calls))
        return calls

    return use


def test_open_detach_json_without_context(detached, capsys):
    detached()
    code, data, _ = run(capsys, "open", "work1", "--detach", "--json")
    assert code == 0 and set(data) == set(FIXTURES["open_detach"])
    assert data["name"] == "work1" and data["pid"] == os.getpid() and data["ext_context"] is None
    assert data["log"].endswith("open.log")


def test_open_detach_json_with_context_never_on_command_line(detached, capsys, ctx_file):
    calls = detached()
    code, data, err = run(capsys, "open", "work1", "--detach", "--json",
                          "--ext-context", f"example-ext@example.com={ctx_file}")
    assert code == 0, data
    assert data["ext_context"] == {"gecko_id": "example-ext@example.com", "injected": True, "error": ""}
    ctx = calls["ctx"]
    raw = ctx_file.read_bytes()
    assert ctx.options_page == "src/options/options.html"
    assert base64.urlsafe_b64decode(ctx.payload + "=" * (-len(ctx.payload) % 4)) == raw
    assert "=" not in ctx.payload
    # the context (and its encoding) is in neither the command line nor the environment
    exposed = " ".join(calls["cmd"]) + " ".join(f"{k}={v}" for k, v in calls["env"].items())
    assert TOKEN not in exposed and ctx.payload not in exposed and str(ctx_file) not in exposed
    assert calls["handoff_mode"] == 0o600 and calls["handoff_left"] is False
    assert not list(Store().dir("work1").glob(".ext_context.*"))
    assert TOKEN not in err and ctx.payload not in err


def test_open_detach_failures(detached, capsys, ctx_file, monkeypatch):
    detached("exits")
    assert err_code(capsys, "open", "work1", "--detach", "--json") == "launch_failed"
    detached("hangs")
    real = launcher.open_detached
    monkeypatch.setattr(launcher, "open_detached", lambda *a, **k: real(*a, **{**k, "timeout": 0.6}))
    assert err_code(capsys, "open", "work1", "--detach", "--json",
                    "--ext-context", f"example-ext@example.com={ctx_file}") == "launch_timeout"
    # the hand-off file does not outlive a failed open
    assert not list(Store().dir("work1").glob(".ext_context.*"))


def test_open_browser_missing(home, ext, capsys, monkeypatch):
    from fpb import doctor

    new(capsys, ext)
    monkeypatch.setattr(doctor, "find_browser", lambda: None)
    assert err_code(capsys, "open", "work1", "--detach", "--json") == "browser_missing"


def test_ext_context_problems_are_reported_before_launch(detached, capsys, tmp_path, ext):
    calls = detached()
    gid = "example-ext@example.com"
    bad = tmp_path / "bad.json"
    cases = {
        "missing": None,
        "empty": b"  \n",
        "not json": f"token={TOKEN}".encode(),
        "array": b"[1, 2]",
        "too big": json.dumps({"k": "x" * (extctx.MAX_BYTES + 1)}).encode(),
    }
    for label, content in cases.items():
        bad.unlink(missing_ok=True)
        if content is not None:
            bad.write_bytes(content)
        code, data, err = run(capsys, "open", "work1", "--detach", "--json", "--ext-context", f"{gid}={bad}")
        assert code == 1 and data["error"]["code"] == "invalid_ext_context", label
        assert TOKEN not in json.dumps(data) + err, label
    good = tmp_path / "good.json"
    good.write_text("{}")
    code, data, _ = run(capsys, "open", "work1", "--detach", "--json", "--ext-context", f"other@example.com={good}")
    assert data["error"]["code"] == "invalid_ext_context" and "example-ext@example.com" in data["error"]["message"]
    manifest = json.loads((ext / "manifest.json").read_text())
    del manifest["options_ui"]
    (ext / "manifest.json").write_text(json.dumps(manifest))
    code, data, _ = run(capsys, "open", "work1", "--detach", "--json", "--ext-context", f"{gid}={good}")
    assert data["error"]["code"] == "invalid_ext_context" and "options page" in data["error"]["message"]
    assert "cmd" not in calls  # nothing was started


def test_open_detach_text_mode_reports_injection(detached, capsys, ctx_file):
    detached()
    code = cli.main(["open", "work1", "--detach", "--ext-context", f"example-ext@example.com={ctx_file}"])
    out = capsys.readouterr().out
    assert code == 0 and "context passed to the options page of example-ext@example.com" in out
