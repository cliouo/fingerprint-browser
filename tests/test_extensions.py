import json

import pytest

from fpb.extensions import ExtensionError, inspect_extension, validate_extension


def ext(tmp_path, manifest, name="dist"):
    d = tmp_path / name
    d.mkdir()
    (d / "manifest.json").write_text(json.dumps(manifest))
    return d


def test_gecko_id_from_browser_specific_settings(tmp_path):
    d = ext(tmp_path, {"name": "X", "version": "1.2", "browser_specific_settings": {"gecko": {"id": "x@y"}}})
    info = validate_extension(str(d))
    assert (info.name, info.version, info.gecko_id) == ("X", "1.2", "x@y")
    assert info.path == str(d.resolve())


def test_legacy_applications_key(tmp_path):
    d = ext(tmp_path, {"name": "X", "version": "1", "applications": {"gecko": {"id": "old@y"}}})
    assert inspect_extension(str(d)).gecko_id == "old@y"


def test_missing_gecko_id_refused_unless_allowed(tmp_path):
    d = ext(tmp_path, {"name": "X", "version": "1"})
    with pytest.raises(ExtensionError, match="gecko"):
        validate_extension(str(d))
    assert validate_extension(str(d), allow_no_gecko_id=True).gecko_id is None


def test_bad_paths(tmp_path):
    with pytest.raises(ExtensionError, match="not a directory"):
        validate_extension(str(tmp_path / "missing"))
    (tmp_path / "e.xpi").write_text("")
    with pytest.raises(ExtensionError, match="extracted directory"):
        validate_extension(str(tmp_path / "e.xpi"))
    (tmp_path / "empty").mkdir()
    with pytest.raises(ExtensionError, match="no manifest.json"):
        validate_extension(str(tmp_path / "empty"))
    bad = tmp_path / "bad"
    bad.mkdir()
    (bad / "manifest.json").write_text("{nope")
    with pytest.raises(ExtensionError, match="not valid JSON"):
        validate_extension(str(bad))
