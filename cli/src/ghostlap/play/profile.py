"""The bridge's view of .ghostlap/profile.yaml: what observe.gd needs, as plain JSON."""

from __future__ import annotations

from ..graph.store import Graph
from ..probes.run import TRACK_PROP_RE, _player_props
from ..project import Project

DEFAULT_VIEWS = ["ui", "state"]
SPATIAL_VIEWS = ["player", "near", "map", "ui"]


def bridge_profile(project: Project, profile: dict | None) -> dict:
    profile = profile or {}
    player = profile.get("player") or {}
    track: list[str] = list(profile.get("track") or [])
    if player.get("scene") and project.graph_db.is_file():
        g = Graph(project.graph_db)
        track = sorted({p["name"] for p in _player_props(g, player) if TRACK_PROP_RE.match(p["name"])} | set(track))
    return {
        "player": {"scene": player.get("scene"), "node": player.get("node") or "."} if player.get("scene") else {},
        "dims": profile.get("dims") or "2d",
        "hazard": profile.get("hazard"),
        "track_props": track,
        # profiles written before `views` existed: spatial games get the spatial set
        "views": profile.get("views") or (SPATIAL_VIEWS if profile.get("genres") and player.get("scene") else DEFAULT_VIEWS),
        "state_nodes": profile.get("state_nodes") or [],
    }
