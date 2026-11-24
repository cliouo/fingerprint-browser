"""Profile store: one directory per profile under $FPB_HOME/profiles.

Nothing here imports Camoufox, so it is cheap to test.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import signal
import stat
import subprocess
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from .proxy import Proxy, parse_proxy

PROFILE_FILE = "profile.json"
RUN_FILE = "run.json"
USER_DATA = "user_data"
LOG_FILE = "open.log"
SCHEMA = 1

_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")


class StoreError(Exception):
    pass


def fpb_home() -> Path:
    env = os.environ.get("FPB_HOME")
    return Path(env).expanduser() if env else Path.home() / ".fpb"


def validate_name(name: str) -> str:
    if not _NAME.match(name or "") or name in (".", ".."):
        raise StoreError(
            f"invalid profile name {name!r}: use 1-64 letters, digits, '.', '_' or '-', starting with a letter or digit"
        )
    return name


@dataclass
class Profile:
    name: str
    created_at: str
    proxy: Optional[str] = None  # full URL incl. credentials; never print it, use proxy_masked()
    extensions: List[str] = field(default_factory=list)
    bypass: List[str] = field(default_factory=list)  # hosts that skip the proxy (e.g. the extension's backend)
    identity: Dict[str, Any] = field(default_factory=dict)  # {"os", "locale", "preset"}
    identity_digest: Optional[str] = None
    identity_summary: Dict[str, Any] = field(default_factory=dict)
    schema: int = SCHEMA

    def proxy_obj(self) -> Optional[Proxy]:
        return parse_proxy(self.proxy) if self.proxy else None

    def proxy_masked(self) -> str:
        p = self.proxy_obj()
        return p.masked() if p else "none (direct)"

    def secrets(self) -> List[str]:
        p = self.proxy_obj()
        return list(p.secrets()) if p else []


def _write_private(path: Path, data: str) -> None:
    """Atomically write `data` to `path` with mode 0600."""
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    os.replace(tmp, path)


def _private_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    os.chmod(path, 0o700)
    return path


class Store:
    def __init__(self, home: Optional[Path] = None):
        self.home = Path(home) if home else fpb_home()

    @property
    def profiles_dir(self) -> Path:
        return self.home / "profiles"

    def ensure(self) -> None:
        _private_dir(self.home)
        _private_dir(self.profiles_dir)

    def dir(self, name: str) -> Path:
        return self.profiles_dir / validate_name(name)

    def user_data_dir(self, name: str) -> Path:
        return self.dir(name) / USER_DATA

    def log_path(self, name: str) -> Path:
        return self.dir(name) / LOG_FILE

    def exists(self, name: str) -> bool:
        return (self.dir(name) / PROFILE_FILE).is_file()

    def names(self) -> List[str]:
        if not self.profiles_dir.is_dir():
            return []
        return sorted(p.name for p in self.profiles_dir.iterdir() if (p / PROFILE_FILE).is_file())

    def create(self, profile: Profile) -> Profile:
        self.ensure()
        d = self.dir(profile.name)
        if d.exists():
            raise StoreError(f"profile {profile.name!r} already exists")
        _private_dir(d)
        _private_dir(d / USER_DATA)
        self.save(profile)
        return profile

    def save(self, profile: Profile) -> None:
        d = self.dir(profile.name)
        if not d.is_dir():
            raise StoreError(f"profile {profile.name!r} does not exist")
        _write_private(d / PROFILE_FILE, json.dumps(asdict(profile), indent=2, sort_keys=True) + "\n")

    def load(self, name: str) -> Profile:
        path = self.dir(name) / PROFILE_FILE
        if not path.is_file():
            raise StoreError(f"no profile named {name!r} (see `fpb list`)")
        mode = stat.S_IMODE(path.stat().st_mode)
        if mode & 0o077:
            # Someone loosened it; the file holds proxy credentials.
            os.chmod(path, 0o600)
        data = json.loads(path.read_text(encoding="utf-8"))
        known = {f for f in Profile.__dataclass_fields__}
        return Profile(**{k: v for k, v in data.items() if k in known})

    def delete(self, name: str) -> None:
        d = self.dir(name)
        if not d.exists():
            raise StoreError(f"no profile named {name!r}")
        if self.running_pid(name):
            raise StoreError(f"profile {name!r} is open; close it first (`fpb close {name}`)")
        shutil.rmtree(d)

    # -- run state -------------------------------------------------------

    def write_run(self, name: str, info: Dict[str, Any]) -> None:
        _write_private(self.dir(name) / RUN_FILE, json.dumps(info) + "\n")

    def read_run(self, name: str) -> Optional[Dict[str, Any]]:
        path = self.dir(name) / RUN_FILE
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (FileNotFoundError, ValueError):
            return None

    def clear_run(self, name: str, pid: Optional[int] = None) -> None:
        info = self.read_run(name)
        if info is None:
            return
        if pid is not None and info.get("pid") != pid:
            return  # someone else's run file
        (self.dir(name) / RUN_FILE).unlink(missing_ok=True)

    def running_pid(self, name: str) -> Optional[int]:
        """The pid of the `fpb open` process holding this profile, if it is alive."""
        info = self.read_run(name)
        if not info:
            return None
        pid = info.get("pid")
        if isinstance(pid, int) and pid_alive(pid) and _looks_like_fpb(pid):
            return pid
        self.clear_run(name)  # stale: the process died without cleaning up
        return None


def pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _looks_like_fpb(pid: int) -> bool:
    """Guard against a recycled pid: the process must still be an fpb/python one."""
    try:
        out = subprocess.run(["ps", "-p", str(pid), "-o", "command="], capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return True  # cannot tell; trust the pid
    cmd = out.stdout.strip()
    return not cmd or "fpb" in cmd or "python" in cmd.lower()


def _wait_gone(pid: int, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not pid_alive(pid):
            return True
        time.sleep(0.2)
    return not pid_alive(pid)


def stop_pid(pid: int, timeout: float = 15.0) -> bool:
    """Stop an `fpb open` runner. Returns True once it has exited.

    First SIGTERM to the runner, which closes the browser cleanly. If it does
    not exit in time, SIGTERM then SIGKILL to its process group: a detached
    runner leads its own session, so the group is it, the Playwright driver
    and the browser, and nothing else.
    """
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return True
    if _wait_gone(pid, timeout):
        return True
    try:
        pgid = os.getpgid(pid)
    except ProcessLookupError:
        return True
    if pgid != pid:  # not a session leader (foreground run): only the runner
        os.kill(pid, signal.SIGKILL)
        return _wait_gone(pid, 5)
    for sig, wait in ((signal.SIGTERM, 10.0), (signal.SIGKILL, 5.0)):
        try:
            os.killpg(pgid, sig)
        except ProcessLookupError:
            return True
        if _wait_gone(pid, wait):
            return True
    return False


def now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")
