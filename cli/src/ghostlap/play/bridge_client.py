"""Talk to a running ghostlap play bridge (addons/ghostlap/play/bridge.gd).

The Godot process outlives each CLI call: `launch()` spawns it detached and waits for
<session>/bridge.json; later calls `attach()` to the same session and send one request each.
Requests and responses are single JSON lines over 127.0.0.1.
"""

from __future__ import annotations

import itertools
import json
import os
import shutil
import signal
import socket
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from ..project import GhostlapError, addon_dir

_ids = itertools.count(1)


class BridgeError(GhostlapError):
    pass


@dataclass
class Bridge:
    session_dir: Path
    port: int
    token: str
    pid: int
    info: dict

    # ---------------------------------------------------------------- lifecycle

    @classmethod
    def launch(cls, godot: str, project_dir: Path, scene: str, session_dir: Path, *, seed: int = 0,
               physics_tps: int = 60, display: str = "headless", profile: dict | None = None,
               timeout: float = 60.0, extra_args: list[str] | None = None) -> "Bridge":
        """display: "headless" (fastest, no screenshots), "window" (visible; desktop input is
        ignored), or "offscreen" (xvfb-run when available, else a window)."""
        session_dir = Path(session_dir).resolve()
        session_dir.mkdir(parents=True, exist_ok=True)
        for stale in ("bridge.json", "bridge_error.txt"):
            (session_dir / stale).unlink(missing_ok=True)
        if display not in ("headless", "window", "offscreen"):
            raise BridgeError(f"unknown display mode {display!r}")
        cmd = []
        if display == "offscreen" and shutil.which("xvfb-run"):
            cmd = ["xvfb-run", "-a", "-s", "-screen 0 1280x720x24"]
        cmd.append(godot)
        if display == "headless":
            cmd.append("--headless")
        cmd += ["--path", str(Path(project_dir).resolve()), "--fixed-fps", str(physics_tps),
                "--script", str(addon_dir() / "play" / "bridge.gd"), *(extra_args or []), "--",
                f"--session={session_dir}", f"--scene={scene}", f"--seed={seed}"]
        if profile is not None:
            (session_dir / "profile.json").write_text(json.dumps(profile, indent=1))
            cmd.append(f"--profile={session_dir / 'profile.json'}")
        log = open(session_dir / "godot.log", "w")
        proc = subprocess.Popen(cmd, stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                start_new_session=True)
        log.close()
        deadline = time.monotonic() + timeout
        bj = session_dir / "bridge.json"
        while time.monotonic() < deadline:
            if bj.is_file():
                return cls.attach(session_dir)
            if proc.poll() is not None or (session_dir / "bridge_error.txt").is_file():
                raise BridgeError(f"Godot exited before the bridge was ready:\n{_tail(session_dir / 'godot.log')}")
            if _bridge_broken(session_dir / "godot.log"):
                proc.kill()
                raise BridgeError(f"the ghostlap bridge failed to load:\n{_tail(session_dir / 'godot.log')}")
            time.sleep(0.05)
        proc.kill()
        raise BridgeError(f"bridge not ready after {timeout:.0f}s:\n{_tail(session_dir / 'godot.log')}")

    @classmethod
    def attach(cls, session_dir: Path) -> "Bridge":
        session_dir = Path(session_dir)
        try:
            info = json.loads((session_dir / "bridge.json").read_text())
        except (OSError, json.JSONDecodeError) as e:
            raise BridgeError(f"no running session in {session_dir} ({e})") from e
        return cls(session_dir, int(info["port"]), info["token"], int(info["pid"]), info)

    def alive(self) -> bool:
        try:
            os.kill(self.pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        # a zombie child still "exists"; treat it as dead
        try:
            stat = Path(f"/proc/{self.pid}/stat").read_text()
            return stat.split(") ", 1)[1][:1] != "Z"
        except (OSError, IndexError):
            return True

    def quit(self, timeout: float = 10.0) -> None:
        try:
            self.request("quit", timeout=timeout)
        except (BridgeError, OSError):
            pass
        deadline = time.monotonic() + timeout
        while self.alive() and time.monotonic() < deadline:
            time.sleep(0.05)
        if self.alive():
            self.kill()

    def kill(self) -> None:
        try:
            os.kill(self.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass

    # ---------------------------------------------------------------- requests

    def request(self, cmd: str, timeout: float | None = 30.0, **args) -> dict:
        req = {"id": next(_ids), "token": self.token, "cmd": cmd, "args": args}
        try:
            with socket.create_connection(("127.0.0.1", self.port), timeout=5) as s:
                s.settimeout(timeout)
                s.sendall((json.dumps(req) + "\n").encode())
                buf = b""
                while b"\n" not in buf:
                    chunk = s.recv(65536)
                    if not chunk:
                        raise BridgeError(f"bridge closed the connection during {cmd!r}"
                                          + ("" if self.alive() else " (Godot exited: crash?)"))
                    buf += chunk
        except socket.timeout as e:
            raise BridgeError(f"{cmd!r} got no reply within {timeout}s (hang?)") from e
        except ConnectionRefusedError as e:
            raise BridgeError(f"bridge not reachable on port {self.port}"
                              + ("" if self.alive() else " (Godot exited)")) from e
        resp = json.loads(buf.split(b"\n", 1)[0])
        if not resp.get("ok"):
            raise BridgeError(f"{cmd}: {resp.get('error')}")
        return resp

    def look(self, views: list[str] | None = None, shot: bool = False, **opts) -> dict:
        """opts: radius (player heights, for `near`), map_w / map_h (cells), shot_width (px)."""
        return self.request("look", views=views or [], shot=shot, **opts)["result"]

    def shot(self, width: int = 960) -> dict:
        return self.request("shot", width=width)["result"]

    def step(self, frames: int, inputs: list[dict] | None = None, timeout: float | None = None) -> dict:
        # generous default: headless games run far faster than real time
        t = timeout if timeout is not None else max(10.0, frames / 60.0 * 20)
        return self.request("step", timeout=t, frames=frames, inputs=inputs or [])["result"]

    def eval(self, expr: str):
        return self.request("eval", expr=expr)["result"]

    def hashes(self, start: int = 0) -> list[str]:
        return self.request("hashes", **{"from": start})["result"]


def _bridge_broken(log: Path) -> bool:
    """Our own scripts failed to compile (Godot keeps running with a dead main loop)."""
    try:
        text = log.read_text(errors="replace")
    except OSError:
        return False
    return "Failed to load script" in text and "ghostlap/play/" in text


def _tail(path: Path, n: int = 30) -> str:
    try:
        return "\n".join(path.read_text(errors="replace").splitlines()[-n:])
    except OSError:
        return "(no log)"
