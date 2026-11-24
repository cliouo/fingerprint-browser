import json

import pytest

from fpb import cli
from fpb.store import Store

SECRET = "Sup3rS3cret"


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("FPB_HOME", str(tmp_path / "fpbhome"))

    def fake_identity(store, profile):
        profile.identity = {"os": "windows", "locale": "en-US", "preset": {"screen": {"width": 1920, "height": 1080}}}
        profile.identity_digest = "d" * 32
        profile.identity_summary = {"screen.width": 1920, "screen.height": 1080, "webGl:renderer": "ANGLE (NVIDIA, GTX 980 Direct3D11)"}

    monkeypatch.setattr(cli, "_identity_for_new", fake_identity)
    return tmp_path / "fpbhome"


@pytest.fixture
def extension(tmp_path):
    d = tmp_path / "dist"
    d.mkdir()
    (d / "manifest.json").write_text(json.dumps(
        {"name": "Ext", "version": "0.1.7", "browser_specific_settings": {"gecko": {"id": "e@x"}}}))
    return d


def run(capsys, *argv):
    code = cli.main(list(argv))
    out = capsys.readouterr()
    return code, out.out + out.err


def test_new_show_list_never_print_password(home, extension, capsys):
    code, out = run(capsys, "new", "work1", "--proxy", f"http://user1:{SECRET}@127.0.0.1:18080", "--extension", str(extension))
    assert code == 0, out
    assert SECRET not in out and "user1:***@127.0.0.1:18080" in out
    for argv in (["show", "work1"], ["show", "work1", "--json"], ["list"]):
        code, out = run(capsys, *argv)
        assert code == 0 and SECRET not in out, (argv, out)
    code, out = run(capsys, "list")
    assert "work1" in out and "closed" in out and "1920x1080" in out
    # but it is stored, with mode 600
    path = home / "profiles" / "work1" / "profile.json"
    assert SECRET in path.read_text()
    assert path.stat().st_mode & 0o777 == 0o600


def test_new_rejects_bad_input(home, extension, capsys):
    code, out = run(capsys, "new", "bad/name")
    assert code == 2 and "invalid profile name" in out
    code, out = run(capsys, "new", "p", "--proxy", "ftp://h:1")
    assert code == 2 and "unsupported proxy scheme" in out
    code, out = run(capsys, "new", "p", "--extension", str(extension.parent / "missing"))
    assert code == 2 and "not a directory" in out
    assert Store().names() == []
    run(capsys, "new", "p")
    code, out = run(capsys, "new", "p")
    assert code == 2 and "already exists" in out


def test_set_proxy_and_extensions(home, extension, capsys):
    run(capsys, "new", "p")
    code, out = run(capsys, "set-proxy", "p", f"https://u:{SECRET}@203.0.113.10:443")
    assert code == 0 and SECRET not in out
    assert Store().load("p").proxy == f"https://u:{SECRET}@203.0.113.10:443"
    # SOCKS5 with auth: Playwright refuses it, so refuse before saving
    code, out = run(capsys, "set-proxy", "p", f"socks5://u:{SECRET}@203.0.113.10:1080")
    assert code == 2 and "SOCKS5" in out and "HTTP endpoint" in out and SECRET not in out
    assert Store().load("p").proxy == f"https://u:{SECRET}@203.0.113.10:443"
    code, out = run(capsys, "new", "q", "--proxy", f"socks5://u:{SECRET}@203.0.113.10:1080")
    assert code == 2 and "SOCKS5" in out and SECRET not in out and "q" not in Store().names()
    code, out = run(capsys, "set-proxy", "p", "socks5://203.0.113.10:1080")
    assert code == 0
    run(capsys, "set-proxy", "p", "none")
    assert Store().load("p").proxy is None
    code, out = run(capsys, "set-extensions", "p", "--extension", str(extension))
    assert code == 0 and "Ext 0.1.7 [e@x]" in out
    assert Store().load("p").extensions == [str(extension.resolve())]
    run(capsys, "set-extensions", "p")
    assert Store().load("p").extensions == []


def test_delete_needs_yes(home, capsys):
    run(capsys, "new", "p")
    code, out = run(capsys, "delete", "p")
    assert code == 2 and "--yes" in out
    assert Store().names() == ["p"]
    code, _ = run(capsys, "delete", "p", "--yes")
    assert code == 0 and Store().names() == []


def test_close_when_not_open(home, capsys):
    run(capsys, "new", "p")
    code, out = run(capsys, "close", "p")
    assert code == 0 and "not open" in out



def test_open_without_paired_browser_asks_for_fetch(home, capsys, monkeypatch):
    from fpb import doctor

    run(capsys, "new", "p")
    monkeypatch.setattr(doctor, "find_browser", lambda: None)
    code, out = run(capsys, "open", "p")
    assert code == 1 and "camoufox fetch" in out


def test_accept_identity_rebases_digest_not_preset(home, capsys, monkeypatch):
    from fpb import launcher

    run(capsys, "new", "p")
    preset = Store().load("p").identity["preset"]
    config = {"screen.width": 1920, "screen.height": 1080, "webGl:supportedExtensions": ["EXT_clip_control"]}
    monkeypatch.setattr(launcher, "require_browser", lambda: None)
    monkeypatch.setattr(launcher, "compute_options", lambda *a, **k: {})
    monkeypatch.setattr(cli.ident, "config_from_options", lambda options: config)
    code, out = run(capsys, "accept-identity", "p")
    assert code == 0 and "now the reference" in out
    p = Store().load("p")
    assert p.identity["preset"] == preset and p.identity_digest == cli.ident.digest(config)
    code, out = run(capsys, "accept-identity", "p")
    assert code == 0 and "nothing to do" in out


def test_bypass_flags_and_hint(home, tmp_path, capsys):
    ext = tmp_path / "my-extension"
    ext.mkdir()
    (ext / "manifest.json").write_text(json.dumps({
        "name": "Ext", "version": "1", "browser_specific_settings": {"gecko": {"id": "example-ext@example.com"}},
        "host_permissions": ["https://shop.example.org/*", "https://api.example.com/*", "http://127.0.0.1/*"]}))
    code, out = run(capsys, "new", "p", "--proxy", f"http://u:{SECRET}@203.0.113.10:8080", "--extension", str(ext))
    note = out.split("note:")[1]
    assert code == 0 and "shop.example.org, api.example.com" in note and "127.0.0.1" not in note
    assert "fpb set-bypass p <host>" in out
    code, out = run(capsys, "set-bypass", "p", "api.example.com")
    assert code == 0 and Store().load("p").bypass == ["api.example.com"]
    code, out = run(capsys, "show", "p")
    assert "bypass:     api.example.com" in out and SECRET not in out
    code, out = run(capsys, "set-extensions", "p", "--extension", str(ext))
    assert code == 0 and "api.example.com" not in out.split("note:")[1]  # already bypassed
    code, out = run(capsys, "set-bypass", "p", "com")
    assert code == 2 and "too broad" in out and Store().load("p").bypass == ["api.example.com"]
    code, out = run(capsys, "set-bypass", "p", "none")
    assert code == 0 and Store().load("p").bypass == []
    code, out = run(capsys, "new", "q", "--bypass", "api.example.com")
    assert code == 2 and "only applies with --proxy" in out
