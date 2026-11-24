import struct

from fpb import doctor


def sfnt(*families: str) -> bytes:
    """A minimal TrueType file whose only table is 'name' with the given families (id 1)."""
    strings = b""
    records = b""
    for fam in families:
        raw = fam.encode("utf-16-be")
        records += struct.pack(">HHHHHH", 3, 1, 0x409, 1, len(raw), len(strings))
        strings += raw
    name = struct.pack(">HHH", 0, len(families), 6 + len(records)) + records + strings
    header = struct.pack(">IHHHH", 0x00010000, 1, 16, 0, 0)
    offset = 12 + 16
    return header + struct.pack(">4sIII", b"name", 0, offset, len(name)) + name


def ttc(*faces: bytes) -> bytes:
    """Wrap single-face fonts into a collection, fixing up their table offsets."""
    head_len = 12 + 4 * len(faces)
    out, offsets, pos = b"", [], head_len
    for face in faces:
        tag, chk, toff, tlen = struct.unpack(">4sIII", face[12:28])
        shifted = face[:12] + struct.pack(">4sIII", tag, chk, toff + pos, tlen) + face[28:]
        offsets.append(pos)
        out += shifted
        pos += len(shifted)
    return b"ttcf" + struct.pack(">HHI", 1, 0, len(faces)) + struct.pack(f">{len(faces)}I", *offsets) + out


def test_family_names_ttf_and_ttc(tmp_path):
    f = tmp_path / "a.ttf"
    f.write_bytes(sfnt(".SF NS", "Font Sistem"))
    assert doctor.family_names(f) == {".SF NS", "Font Sistem"}
    c = tmp_path / "b.ttc"
    c.write_bytes(ttc(sfnt(".PingFang SC"), sfnt(".PingFang TC")))
    assert doctor.family_names(c) == {".PingFang SC", ".PingFang TC"}


def test_hidden_font_rule(tmp_path):
    cases = {
        "42__SFNS.ttf": (sfnt(".SF NS"), True),
        "PingFang.ttc": (ttc(sfnt(".PingFang SC"), sfnt(".PingFang HK")), True),
        "0__arial.ttf": (sfnt("Arial"), False),
        "23__LastResort.otf": (sfnt(".LastResort"), False),  # kept on purpose
        "44__AquaKana.ttc": (sfnt(".Aqua Kana", ".Aqua Kana Bold"), False),  # kept on purpose
        "openadd-x.t1": (b"not a font", False),
        "broken.ttf": (b"\x00\x01", False),
    }
    for name, (data, _) in cases.items():
        (tmp_path / name).write_bytes(data)
    for name, (_, hidden) in cases.items():
        assert doctor.is_hidden_font(tmp_path / name) is hidden, name


def test_fix_and_restore_round_trip(tmp_path):
    fonts = tmp_path / "Camoufox.app" / "fonts"
    fonts.mkdir(parents=True)
    backup = tmp_path / "fonts-hidden-backup-1.0"
    (fonts / "42__SFNS.ttf").write_bytes(sfnt(".SF NS"))
    (fonts / "PingFang.ttc").write_bytes(ttc(sfnt(".PingFang SC")))
    (fonts / "0__arial.ttf").write_bytes(sfnt("Arial"))
    (fonts / "23__LastResort.otf").write_bytes(sfnt(".LastResort"))
    before = sorted(p.name for p in fonts.iterdir())

    state = doctor.font_state(fonts, backup)
    assert state.needs_fix and [p.name for p in state.to_move] == ["42__SFNS.ttf", "PingFang.ttc"]
    moved = doctor.fix_fonts(state)
    assert len(moved) == 2
    assert sorted(p.name for p in fonts.iterdir()) == ["0__arial.ttf", "23__LastResort.otf"]
    assert sorted(p.name for p in backup.iterdir()) == ["42__SFNS.ttf", "PingFang.ttc"]

    again = doctor.font_state(fonts, backup)
    assert not again.needs_fix and len(again.backed_up) == 2
    assert doctor.fix_fonts(again) == []  # idempotent

    restored = doctor.restore_fonts(doctor.font_state(fonts, backup))
    assert len(restored) == 2
    assert sorted(p.name for p in fonts.iterdir()) == before
    assert not backup.exists()


def test_fix_after_refetch_keeps_existing_backup(tmp_path):
    fonts = tmp_path / "fonts"
    fonts.mkdir()
    backup = tmp_path / "backup"
    backup.mkdir()
    (backup / "42__SFNS.ttf").write_bytes(sfnt(".SF NS"))
    (fonts / "42__SFNS.ttf").write_bytes(sfnt(".SF NS"))  # bundle got it back
    doctor.fix_fonts(doctor.font_state(fonts, backup))
    assert not (fonts / "42__SFNS.ttf").exists()
    assert (backup / "42__SFNS.ttf").exists()
