import json
import os
import stat

import pytest

from fpb.store import Profile, Store, StoreError, validate_name


def mode(path):
    return stat.S_IMODE(os.stat(path).st_mode)


def make(store, name="p1", proxy="http://u:pw123@h:8080"):
    return store.create(Profile(name=name, created_at="2026-11-23T00:00:00", proxy=proxy,
                                identity={"os": "windows", "locale": "en-US", "preset": {"x": 1}}))


def test_create_load_roundtrip_and_permissions(tmp_path):
    store = Store(tmp_path / "home")
    make(store)
    assert store.names() == ["p1"]
    loaded = store.load("p1")
    assert loaded.proxy == "http://u:pw123@h:8080"
    assert loaded.identity["locale"] == "en-US"
    assert mode(store.home) == 0o700
    assert mode(store.dir("p1")) == 0o700
    assert mode(store.dir("p1") / "profile.json") == 0o600
    assert store.user_data_dir("p1").is_dir()


def test_duplicate_and_missing(tmp_path):
    store = Store(tmp_path)
    make(store)
    with pytest.raises(StoreError):
        make(store)
    with pytest.raises(StoreError):
        store.load("nope")


def test_load_tightens_loose_permissions(tmp_path):
    store = Store(tmp_path)
    make(store)
    path = store.dir("p1") / "profile.json"
    os.chmod(path, 0o644)
    store.load("p1")
    assert mode(path) == 0o600


def test_save_keeps_mode_and_is_atomic(tmp_path):
    store = Store(tmp_path)
    p = make(store)
    p.proxy = None
    store.save(p)
    path = store.dir("p1") / "profile.json"
    assert mode(path) == 0o600
    assert json.loads(path.read_text())["proxy"] is None
    assert not [f for f in os.listdir(store.dir("p1")) if f.endswith(".tmp")]


def test_masked_proxy_and_secrets(tmp_path):
    store = Store(tmp_path)
    p = make(store)
    assert p.proxy_masked() == "http://u:***@h:8080"
    assert p.secrets() == ["pw123"]
    assert make(store, "p2", proxy=None).proxy_masked() == "none (direct)"


def test_unknown_keys_ignored(tmp_path):
    store = Store(tmp_path)
    make(store)
    path = store.dir("p1") / "profile.json"
    data = json.loads(path.read_text())
    data["future_field"] = 1
    path.write_text(json.dumps(data))
    assert store.load("p1").name == "p1"


@pytest.mark.parametrize("name", ["a", "shop-1", "US_east.2", "x" * 64])
def test_valid_names(name):
    assert validate_name(name) == name


@pytest.mark.parametrize("name", ["", ".", "..", "-x", "a/b", "../x", "x" * 65, "has space", "ü"])
def test_invalid_names(name):
    with pytest.raises(StoreError):
        validate_name(name)


def test_run_state_and_delete(tmp_path):
    store = Store(tmp_path)
    make(store)
    assert store.running_pid("p1") is None
    store.write_run("p1", {"pid": os.getpid(), "ready": True})
    assert store.running_pid("p1") == os.getpid()
    with pytest.raises(StoreError):
        store.delete("p1")  # open profiles cannot be deleted
    store.clear_run("p1", pid=os.getpid() + 1)  # someone else's pid: kept
    assert store.running_pid("p1") == os.getpid()
    store.clear_run("p1", pid=os.getpid())
    assert store.running_pid("p1") is None
    store.delete("p1")
    assert store.names() == []


def test_stale_run_file_is_cleared(tmp_path):
    store = Store(tmp_path)
    make(store)
    store.write_run("p1", {"pid": 2**22 + 12345, "ready": True})  # no such process
    assert store.running_pid("p1") is None
    assert store.read_run("p1") is None


def test_fpb_home_env(tmp_path, monkeypatch):
    monkeypatch.setenv("FPB_HOME", str(tmp_path / "h"))
    assert Store().home == tmp_path / "h"
