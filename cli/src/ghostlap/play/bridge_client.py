"""Talk to a running ghostlap play bridge (addons/ghostlap/play/bridge.gd).

The Godot process outlives each CLI call: `launch()` spawns it detached and waits for
<session>/bridge.json; later calls `attach()` to the same session and send one request each.
Requests and responses are single JSON lines over 127.0.0.1.
"""

from __future__ import annotations

import itertools
import json
import os
import re
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


class BridgeHang(BridgeError):
    """A step made no progress for HANG_SECONDS: game code is stuck (an infinite loop?)."""

    def __init__(self, msg: str, frame: int | None):
        super().__init__(msg)
        self.frame = frame


# A stepping bridge writes its frame to <session>/progress every 250 ms. No change for HANG_SECONDS while
# Godot burns CPU is a hang (an infinite loop). In a rendered mode an idle Godot is usually waiting
# for the compositor (a hidden window or a locked screen renders slowly), so that only counts as a
# hang after IDLE_HANG_SECONDS.
HANG_SECONDS = float(os.environ.get("GHOSTLAP_HANG_SECONDS", "10"))
IDLE_HANG_SECONDS = float(os.environ.get("GHOSTLAP_IDLE_HANG_SECONDS", "120"))


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
               physics_tps: int | None = None, display: str = "headless", profile: dict | None = None,
               timeout: float = 60.0, extra_args: list[str] | None = None, shots: dict | None = None,
               hud: Path | None = None) -> "Bridge":
        """display: "headless" (fastest, no screenshots), "window" (visible; desktop input is
        ignored), or "offscreen" (xvfb-run when available, else a window). shots: capture
        settings from frame 0 on ({every, keep, on, width}, see shots.gd). hud: the caption
        HUD's JSON for a session video (hud.gd)."""
        session_dir = Path(session_dir).resolve()
        session_dir.mkdir(parents=True, exist_ok=True)
        if physics_tps is None:
            physics_tps = project_tps(project_dir)
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
        if shots is not None:
            cmd.append("--shots=" + json.dumps(shots, separators=(",", ":")))
        if hud is not None:
            cmd.append(f"--hud={Path(hud).resolve()}")
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

    def exited(self, within: float = 3.0) -> bool:
        """True if the process is gone or goes within `within` s (a crashing Godot closes its socket
        first, then spends a moment writing its backtrace)."""
        deadline = time.monotonic() + within
        while self.alive():
            if time.monotonic() > deadline:
                return False
            time.sleep(0.05)
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

    def request(self, cmd: str, timeout: float | None = 30.0, watch_progress: bool = False, **args) -> dict:
        """watch_progress (step/until): no overall timeout; instead a hang is HANG_SECONDS without
        the bridge's progress file changing."""
        req = {"id": next(_ids), "token": self.token, "cmd": cmd, "args": args}
        progress = self.session_dir / "progress"
        try:
            with socket.create_connection(("127.0.0.1", self.port), timeout=5) as s:
                s.settimeout(1.0 if watch_progress else timeout)
                s.sendall((json.dumps(req) + "\n").encode())
                buf = b""
                last, last_change, cpu0 = _read(progress), time.monotonic(), _cpu_seconds(self.pid)
                while b"\n" not in buf:
                    try:
                        chunk = s.recv(65536)
                    except socket.timeout:
                        if not watch_progress:
                            raise
                        now = _read(progress)
                        stalled = time.monotonic() - last_change
                        if now != last:
                            last, last_change, cpu0 = now, time.monotonic(), _cpu_seconds(self.pid)
                        elif stalled > HANG_SECONDS:
                            cpu = _cpu_seconds(self.pid)
                            # unknown CPU (no /proc) or headless: no progress is a hang
                            busy = cpu is None or cpu0 is None or (cpu - cpu0) > 0.5 * stalled \
                                or bool(self.info.get("headless"))
                            if busy or stalled > IDLE_HANG_SECONDS:
                                raise BridgeHang(f"{cmd!r} made no progress for {stalled:.0f}s "
                                                 f"(last progress at frame {last or '?'}"
                                                 + (", Godot busy" if busy else ", Godot idle: is the window hidden?")
                                                 + ")", int(last) if last.isdigit() else None) from None
                        if not self.alive():
                            raise BridgeError(f"Godot exited during {cmd!r} (crash?)") from None
                        continue
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

    def shot(self, width: int = -1, why: str = "requested") -> dict:
        """width -1: the session's shots.width."""
        return self.request("shot", width=width, why=why)["result"]

    def step(self, frames: int, inputs: list[dict] | None = None, timeout: float | None = None) -> dict:
        """Without a timeout, waits as long as frames keep advancing (see BridgeHang)."""
        if timeout is not None:
            return self.request("step", timeout=timeout, frames=frames, inputs=inputs or [])["result"]
        return self.request("step", watch_progress=True, frames=frames, inputs=inputs or [])["result"]

    def until(self, expr: str, max_frames: int, inputs: list[dict] | None = None) -> dict:
        return self.request("until", watch_progress=True, expr=expr, max=max_frames, inputs=inputs or [])["result"]

    def eval(self, expr: str):
        return self.request("eval", expr=expr)["result"]

    def hashes(self, start: int = 0) -> list[str]:
        return self.request("hashes", **{"from": start})["result"]

    def samples(self, start: int = 0) -> list[dict]:
        """The player per frame from `start` on: {pos, vel?, floor?}, {} on frames without one."""
        return self.request("samples", **{"from": start})["result"]


def project_tps(project_dir: Path) -> int:
    """The project's physics ticks per second. Sessions run --fixed-fps at this rate, so one
    frame is one physics tick (design §6.2)."""
    try:
        text = (Path(project_dir) / "project.godot").read_text(errors="replace")
    except OSError:
        return 60
    m = re.search(r"^\[physics\][^\[]*?^common/physics_ticks_per_second\s*=\s*(\d+)", text, re.M | re.S)
    return int(m.group(1)) if m else 60


def _cpu_seconds(pid: int) -> float | None:
    """User+system CPU time of a process (Linux /proc), or None."""
    try:
        fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
        return (int(fields[11]) + int(fields[12])) / os.sysconf("SC_CLK_TCK")
    except (OSError, IndexError, ValueError):
        return None


def _read(p: Path) -> str:
    try:
        return p.read_text()
    except OSError:
        return ""


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
