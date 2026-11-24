"""Environment checks, and the reversible macOS font fix.

The font issue (macOS 27): Camoufox.app bundles copies of macOS's *hidden*
system fonts (family names starting with '.', e.g. '.SF NS', '.PingFang SC').
They are older than the host's copies, and the browser UI draws garbled text
with them. The fix moves those files out of Contents/Resources/fonts into
~/Library/Caches/camoufox/fonts-hidden-backup-<version>/, so the host's own
copies are used. `camoufox fetch` installs a fresh bundle, so it has to be
redone after every fetch. --restore-fonts moves them back.

Two hidden fonts stay in the bundle: '.LastResort' (Firefox's last-resort
glyph font) and '.Aqua Kana'. They were left in place by the manual fix this
automates, and the UI renders correctly with them, so moving them is untested.
"""

from __future__ import annotations

import os
import shutil
import struct
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Set

KEEP_FAMILIES = {".LastResort", ".Aqua Kana"}
FONT_SUFFIXES = {".ttf", ".ttc", ".otf"}


# -- minimal sfnt 'name' table reader ---------------------------------------


def _face_offsets(data: bytes) -> List[int]:
    if data[:4] == b"ttcf":
        (count,) = struct.unpack(">I", data[8:12])
        return list(struct.unpack(f">{count}I", data[12 : 12 + 4 * count]))
    return [0]


def family_names(path: Path) -> Set[str]:
    """Family names (name ids 1 and 16) of every face in a TTF/OTF/TTC file."""
    data = path.read_bytes()
    names: Set[str] = set()
    for off in _face_offsets(data):
        (num_tables,) = struct.unpack(">H", data[off + 4 : off + 6])
        for i in range(num_tables):
            rec = off + 12 + 16 * i
            tag, _checksum, table, _length = struct.unpack(">4sIII", data[rec : rec + 16])
            if tag != b"name":
                continue
            _fmt, count, strings = struct.unpack(">HHH", data[table : table + 6])
            for j in range(count):
                r = table + 6 + 12 * j
                pid, _eid, _lid, nid, length, soff = struct.unpack(">HHHHHH", data[r : r + 12])
                if nid not in (1, 16):
                    continue
                raw = data[table + strings + soff : table + strings + soff + length]
                names.add(raw.decode("utf-16-be", "replace") if pid in (0, 3) else raw.decode("mac_roman", "replace"))
    return names


def is_hidden_font(path: Path) -> bool:
    """A bundled copy of a hidden macOS system font that the fix moves out."""
    if path.suffix.lower() not in FONT_SUFFIXES:
        return False
    try:
        families = family_names(path)
    except (OSError, struct.error):
        return False
    hidden = {f for f in families if f.startswith(".")}
    return bool(hidden) and not (hidden & KEEP_FAMILIES)


# -- the fix -----------------------------------------------------------------


@dataclass
class FontState:
    fonts_dir: Path
    backup_dir: Path
    to_move: List[Path]
    backed_up: List[Path]

    @property
    def needs_fix(self) -> bool:
        return bool(self.to_move)


def font_state(fonts_dir: Path, backup_dir: Path) -> FontState:
    to_move = sorted(p for p in fonts_dir.iterdir() if p.is_file() and is_hidden_font(p)) if fonts_dir.is_dir() else []
    backed_up = sorted(p for p in backup_dir.iterdir() if p.is_file()) if backup_dir.is_dir() else []
    return FontState(fonts_dir, backup_dir, to_move, backed_up)


def fix_fonts(state: FontState) -> List[Path]:
    state.backup_dir.mkdir(parents=True, exist_ok=True)
    moved = []
    for src in state.to_move:
        dest = state.backup_dir / src.name
        if dest.exists():
            # An earlier fix already saved this file; the bundle got it back
            # (re-fetch). Keep the saved copy, drop the duplicate from the bundle.
            src.unlink()
        else:
            shutil.move(str(src), str(dest))
        moved.append(src)
    return moved


def restore_fonts(state: FontState) -> List[Path]:
    restored = []
    for src in state.backed_up:
        dest = state.fonts_dir / src.name
        if dest.exists():
            continue
        shutil.move(str(src), str(dest))
        restored.append(dest)
    if state.backup_dir.is_dir() and not any(state.backup_dir.iterdir()):
        state.backup_dir.rmdir()
    return restored


# -- locating the installed browser -------------------------------------------


@dataclass
class BrowserInstall:
    version: str
    install_dir: Path
    cache_dir: Path

    @property
    def app(self) -> Path:
        return self.install_dir / "Camoufox.app"

    @property
    def fonts_dir(self) -> Path:
        return self.app / "Contents" / "Resources" / "fonts"

    @property
    def backup_dir(self) -> Path:
        return self.cache_dir / f"fonts-hidden-backup-{self.version}"


def find_browser() -> Optional[BrowserInstall]:
    """The active Camoufox build, without downloading anything."""
    try:
        from camoufox.pkgman import INSTALL_DIR, camoufox_path, installed_verstr

        install_dir = Path(camoufox_path(download_if_missing=False))
        version = installed_verstr()
    except Exception:
        return None
    return BrowserInstall(version=version, install_dir=install_dir, cache_dir=Path(INSTALL_DIR))


def is_macos() -> bool:
    return sys.platform == "darwin"


def mode_ok(path: Path, want: int) -> bool:
    try:
        return (os.stat(path).st_mode & 0o777) & ~want == 0
    except FileNotFoundError:
        return True
