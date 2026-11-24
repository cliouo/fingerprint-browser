import json
import random

from fpb import identity as ident


def preset(w, h, renderer="ANGLE (NVIDIA, NVIDIA GeForce GTX 980 Direct3D11 vs_5_0 ps_5_0), or similar", cores=8):
    return {
        "navigator": {"userAgent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:150.0) Gecko/20100101 Firefox/150.0",
                      "platform": "Win32", "hardwareConcurrency": cores},
        "screen": {"width": w, "height": h},
        "webgl": {"unmaskedVendor": "Google Inc.", "unmaskedRenderer": renderer},
    }


POOL = [
    preset(1920, 1080, cores=4),
    preset(1920, 1080, cores=8),
    preset(1366, 768),
    preset(2560, 1440),
    preset(1920, 1080, renderer="ANGLE (Microsoft, Microsoft Basic Render Driver Direct3D11 vs_5_0 ps_5_0), or similar"),
]


def test_prefers_exact_display_match_and_skips_software_gpu():
    pool = ident.candidate_presets(POOL, [], 1920, 1080)
    assert [p["navigator"]["hardwareConcurrency"] for p in pool] == [4, 8]


def test_prefers_unused_devices():
    used = [ident.preset_key(POOL[0])]
    pool = ident.candidate_presets(POOL, used, 1920, 1080)
    assert pool == [POOL[1]]


def test_falls_back_when_all_used_then_to_fitting():
    used = [ident.preset_key(p) for p in POOL]
    assert ident.candidate_presets(POOL, used, 1920, 1080) == POOL[:2]
    # a display no preset matches exactly: anything that fits
    assert ident.candidate_presets(POOL, [], 2000, 1100) == POOL[:3]
    # nothing fits: still returns something rather than failing
    assert ident.candidate_presets(POOL, [], 800, 600) == POOL[:4]


def test_choose_adds_salt_and_copies():
    a = ident.choose_preset(POOL, [], 1920, 1080, rng=random.Random(1))
    b = ident.choose_preset(POOL, [], 1920, 1080, rng=random.Random(1))
    assert ident.SALT_KEY in a and a[ident.SALT_KEY] != b[ident.SALT_KEY]
    assert ident.SALT_KEY not in POOL[0] and ident.SALT_KEY not in POOL[1]
    # same device either way: the salt is not part of the device key
    assert ident.preset_key(a) == ident.preset_key(b)


def config(**over):
    base = {
        "navigator.userAgent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:158.0) Gecko/20100101 Firefox/158.0",
        "navigator.platform": "Win32",
        "screen.width": 1920,
        "screen.height": 1080,
        "webGl:renderer": "ANGLE (NVIDIA ...)",
        "fonts": ["Arial", "Calibri"],
        "voices": [{"name": "Microsoft David"}],
        "timezone": "America/New_York",
        "geolocation:latitude": 40.7,
        "locale:language": "en",
        "webrtc:ipv4": "1.2.3.4",
        "network:ipv4Only": True,
        "addons": ["/a/dist"],
    }
    base.update(over)
    return base


def test_digest_ignores_geo_locale_paths_and_browser_version():
    d = ident.digest(config())
    assert ident.digest(config(**{"timezone": "Europe/Paris", "geolocation:latitude": 1.0, "webrtc:ipv4": "5.6.7.8",
                                  "addons": ["/b/dist"], "network:ipv4Only": False})) == d
    upgraded = config(**{"navigator.userAgent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:160.0) Gecko/20100101 Firefox/160.0"})
    assert ident.digest(upgraded) == d


def test_digest_sees_device_changes():
    d = ident.digest(config())
    assert ident.digest(config(**{"screen.width": 1536})) != d
    assert ident.digest(config(fonts=["Arial"])) != d
    assert ident.digest(config(**{"webGl:renderer": "other"})) != d


def test_summary_and_changes():
    s1 = ident.summarize(config())
    assert s1["screen.width"] == 1920 and s1["fonts"].startswith("2 fonts")
    s2 = ident.summarize(config(**{"screen.width": 1536}))
    assert ident.summary_changes(s1, s2) == [("screen.width", 1920, 1536)]
    upgraded = ident.summarize(config(**{"navigator.userAgent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:160.0) Gecko/20100101 Firefox/160.0"}))
    assert ident.summary_changes(s1, upgraded) == []


def test_config_from_options_joins_chunks_in_numeric_order():
    blob = json.dumps({"a": 1, "b": "x" * 90})
    chunks = [blob[i:i + 10] for i in range(0, len(blob), 10)]
    env = {f"CAMOU_CONFIG_{i + 1}": c for i, c in enumerate(chunks)}
    env["OTHER"] = "1"
    assert len(chunks) >= 10  # exercises _10 sorting after _9
    assert ident.config_from_options({"env": env}) == {"a": 1, "b": "x" * 90}
    assert ident.config_from_options({"env": {}}) == {}
