"""gamefeel command line.

    gamefeel scan                 scan the project into .gamefeel/graph.db (incremental)
    gamefeel detect               propose genre / player / actions / mechanics (writes a draft profile)
    gamefeel confirm              mark .gamefeel/profile.yaml as confirmed
    gamefeel graph profile        compact JSON summary for an agent
    gamefeel graph query NAME ..  canned graph queries (who-reads, exports-of, ...)
    gamefeel graph sql "..."      read-only SQL
    gamefeel probe list|validate|run
    gamefeel analyze              metrics + findings for the latest run
    gamefeel check                probe run + analyze
    gamefeel metrics | packs | pattern NAME | doctor | init
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

import yaml

from . import __version__
from .project import GamefeelError, Project, addon_dir, find_godot, find_project, godot_version

PATTERN_DIR = Path(__file__).resolve().parent / "patterns"


def _out(args, data, human=None) -> None:
    if getattr(args, "json", False) or human is None:
        print(json.dumps(data, indent=1, default=str))
    else:
        print(human)


def _project(args) -> Project:
    return find_project(getattr(args, "project", None))


# ------------------------------------------------------------------ commands

def cmd_scan(args) -> int:
    from .scan.run import scan

    p = _project(args)
    r = scan(p, godot=args.godot, text_only=args.text_only, full=args.full, excludes=args.exclude)
    c = r.counts
    human = (f"scan ({r.mode}) in {r.seconds}s: {c['changed']} changed, {c['rehashed']} rehashed, {c['unchanged']} unchanged, "
             f"{c['deleted']} deleted, {c['renamed']} renamed, {r.dependents} dependents; re-extracted {c['re_extracted']}"
             + (f"; upgraded {c['upgraded_to_engine']} text→engine" if c.get("upgraded_to_engine") else ""))
    for w in r.warnings:
        human += f"\n  warning: {w}"
    for f, errs in r.file_errors.items():
        human += f"\n  {f}: " + "; ".join(e.get("message", "") for e in errs[:2])
    if r.dangling_edges:
        human += f"\n  {r.dangling_edges} reference(s) point at files that no longer exist"
    _out(args, r.to_dict(), human)
    return 0


def _graph(p: Project):
    from .graph.store import Graph

    if not p.graph_db.exists():
        raise GamefeelError("no graph yet — run `gamefeel scan` first")
    return Graph(p.graph_db)


def cmd_detect(args) -> int:
    from .detect.detect import detect, draft_profile, read_profile, write_profile
    from .packs.loader import load_packs

    p = _project(args)
    det = detect(_graph(p), p.root)
    (p.state / "detected.json").write_text(json.dumps(det, indent=1, default=str))
    existing = read_profile(p.profile_path)
    wrote = False
    if args.write_profile or existing is None:
        if existing and existing.get("confirmed") and not args.force:
            pass
        else:
            write_profile(p.profile_path, draft_profile(det, load_packs(p.root)))
            wrote = True
    if args.json:
        _out(args, {**det, "profile_written": wrote, "profile_path": str(p.profile_path)})
        return 0
    print(_detect_human(det))
    print(f"\nprofile: {p.profile_path} ({'draft written — review it, then `gamefeel confirm`' if wrote else 'kept (already exists)'})")
    return 0


def _detect_human(det: dict) -> str:
    out = []
    pl = det.get("player")
    if pl:
        out.append(f"player: {pl['scene']} :: {pl['node']} ({pl['type']}), score {pl['score']}")
        for e in pl["evidence"]:
            out.append(f"   - {e}")
    else:
        out.append("player: not found (no CharacterBody/RigidBody reading movement input)")
    for c in det.get("player_candidates", []):
        out.append(f"   other candidate: {c['scene']} :: {c['node']} ({c['type']}), score {c['score']}")
    out.append("actions: " + ", ".join(f"{k}={v}" for k, v in det.get("roles", {}).items()))
    out.append("genres:")
    for gn in det.get("genres", []):
        out.append(f"   {gn['score']:.2f}  {gn['pack']}  ({gn['title']})")
        for m in gn["matched"][:4]:
            out.append(f"         + {m}")
        if gn["missing"]:
            out.append(f"         - missing: {', '.join(gn['missing'])}")
    out.append("mechanics: " + ", ".join(m["name"] for m in det.get("mechanics", [])))
    out.append("levels: " + ", ".join(lv["scene"] for lv in det.get("levels", [])))
    out.append("hazards: " + ", ".join(h["scene"] for h in det.get("hazards", [])))
    return "\n".join(out)


def cmd_confirm(args) -> int:
    from .detect.detect import read_profile, write_profile

    p = _project(args)
    prof = read_profile(p.profile_path)
    if prof is None:
        raise GamefeelError("no profile yet — run `gamefeel detect`")
    from .packs.loader import load_packs

    packs = load_packs(p.root)
    bad = [g for g in prof.get("genres") or [] if g not in packs or not packs[g].selectable]
    if bad or not prof.get("genres"):
        raise GamefeelError(f"profile.genres must list selectable packs; got {prof.get('genres')}. Available: "
                            f"{sorted(n for n, pk in packs.items() if pk.selectable)}")
    if not (prof.get("player") or {}).get("scene"):
        raise GamefeelError("profile.player.scene is empty")
    prof["confirmed"] = True
    write_profile(p.profile_path, prof)
    print(f"confirmed {p.profile_path}: genres={prof['genres']} player={prof['player']['scene']}")
    return 0


def cmd_graph(args) -> int:
    from .graph import queries

    p = _project(args)
    g = _graph(p)
    if args.graph_cmd == "profile":
        from .detect.detect import detect

        det = detect(g, p.root)
        compact = {
            "project": det["project"],
            "dims": det["dims"],
            "player": {k: det["player"][k] for k in ("scene", "node", "type", "evidence")} if det["player"] else None,
            "other_player_candidates": [f"{c['scene']}::{c['node']}" for c in det["player_candidates"]],
            "actions": det["roles"],
            "genres": [{"pack": x["pack"], "score": x["score"], "why": x["matched"][:3]} for x in det["genres"][:3]],
            "mechanics": [f"{m['name']}: {m['evidence']}" for m in det["mechanics"]],
            "levels": [lv["scene"] for lv in det["levels"]][:15],
            "hazards": [h["scene"] for h in det["hazards"]][:8],
            "camera": det["camera"],
            "hitboxes": det["hitboxes"],
            "counts": {k: len(g.nodes(k)) for k in ("Scene", "Script", "Function", "InputAction")},
        }
        print(json.dumps(compact, indent=1, default=str))
        return 0
    if args.graph_cmd == "sql":
        print(json.dumps(g.sql(args.query), indent=1, default=str))
        return 0
    if args.graph_cmd == "query":
        if args.name not in queries.QUERIES:
            raise GamefeelError("unknown query. Available:\n" + "\n".join(f"  {k} {v[1]}" for k, v in queries.QUERIES.items()))
        fn, _ = queries.QUERIES[args.name]
        print(json.dumps(fn(g, *args.args), indent=1, default=str))
        return 0
    return 2


def _profile(p: Project, force: bool) -> dict:
    from .detect.detect import read_profile

    prof = read_profile(p.profile_path)
    if prof is None:
        raise GamefeelError("no profile — run `gamefeel scan` and `gamefeel detect` first")
    if not prof.get("confirmed") and not force:
        raise GamefeelError("profile isn't confirmed yet — review .gamefeel/profile.yaml with the user, then `gamefeel confirm` (or pass --force)")
    return prof


def cmd_probe(args) -> int:
    from .probes import run as prun

    p = _project(args)
    if args.probe_cmd == "list":
        prof = _profile(p, force=True)
        sel = prun.select(p, prof)
        data = {"runnable": [{"probe": lp.id, "pack": lp.pack, "title": lp.spec.get("title"), "measure": lp.spec.get("measure")} for lp in sel.probes],
                "skipped": sel.skipped, "invalid": sel.invalid}
        human = "\n".join([f"  {d['probe']:18s} [{d['pack']}] {d['title']} -> {', '.join(d['measure'])}" for d in data["runnable"]]
                          + [f"  (skip) {s['probe']}: {s['reason']}" for s in sel.skipped]
                          + [f"  (invalid) {i['probe']}: {'; '.join(i['issues'])}" for i in sel.invalid])
        _out(args, data, "probes:\n" + human)
        return 0
    if args.probe_cmd == "validate":
        from .graph.store import Graph
        from .metrics import REGISTRY
        from .probes.schema import load_probe_file, validate
        from .packs.loader import load_packs, merged_vars

        prof = _profile(p, force=True)
        g = Graph(p.graph_db) if p.graph_db.exists() else None
        actions = prun.known_actions(g) if g else None
        pvars = merged_vars(load_packs(p.root), prof.get("genres") or [])
        ok = True
        results = []
        for f in args.files:
            lp = load_probe_file(Path(f))
            issues = [str(i) for i in lp.issues] or [str(i) for i in validate(prun.substitute_vars(lp.spec, pvars), set(REGISTRY), actions, prof.get("actions") or {})]
            results.append({"file": f, "probe": lp.id, "valid": not issues, "issues": issues})
            ok = ok and not issues
        _out(args, results, "\n".join(f"{'ok     ' if r['valid'] else 'INVALID'} {r['file']}" + "".join(f"\n   - {i}" for i in r["issues"]) for r in results))
        return 0 if ok else 1
    if args.probe_cmd == "run":
        prof = _profile(p, args.force)
        res = prun.run(p, prof, godot=args.godot, only=args.only, extra_files=[Path(f) for f in args.file or []])
        failed = [r for r in res.summary.get("runs", []) if r.get("error")]
        data = {"run_dir": str(res.run_dir), "seconds": res.seconds, "runs": len(res.summary.get("runs", [])),
                "failed_runs": failed[:10], "runner_errors": res.summary.get("errors", []),
                "skipped": res.selection.skipped, "invalid": res.selection.invalid}
        human = f"ran {data['runs']} runs from {len(res.plan['probes'])} probes in {res.seconds}s -> {res.run_dir}"
        grouped: dict[tuple, list] = {}
        for fr in failed:
            grouped.setdefault((fr["probe"], fr["error"]), []).append(fr["run"])
        for (probe, err), runs in grouped.items():
            human += f"\n  failed: {probe} ({len(runs)} run{'s' if len(runs) > 1 else ''}): {err}"
        for s in res.selection.skipped:
            human += f"\n  skipped {s['probe']}: {s['reason']}"
        for i in res.selection.invalid:
            human += f"\n  invalid {i['probe']}: {'; '.join(i['issues'])}"
        _out(args, data, human)
        return 0
    return 2


def cmd_analyze(args) -> int:
    from .analyze import analyze
    from .probes.run import latest_run

    p = _project(args)
    prof = _profile(p, force=True)
    run_dir = Path(args.run) if args.run else latest_run(p)
    if run_dir is None:
        raise GamefeelError("no probe run yet — `gamefeel probe run`")
    rep = analyze(p, run_dir, prof)
    if args.json:
        print(json.dumps(rep, indent=1, default=str))
    else:
        print((p.state / "report.md").read_text())
    return 0


def cmd_check(args) -> int:
    args.file = getattr(args, "file", None)
    rc = cmd_probe(argparse.Namespace(**{**vars(args), "probe_cmd": "run", "json": False}))
    if rc:
        return rc
    return cmd_analyze(argparse.Namespace(**{**vars(args), "run": None}))


def cmd_metrics(args) -> int:
    from .metrics import REGISTRY

    data = [{"metric": m.name, "unit": m.unit, "description": m.description} for m in sorted(REGISTRY.values(), key=lambda m: m.name)]
    _out(args, data, "\n".join(f"{d['metric']:28s} {d['unit']:7s} {d['description']}" for d in data))
    return 0


def cmd_packs(args) -> int:
    from .packs.loader import load_packs

    root = None
    try:
        root = _project(args).root
    except GamefeelError:
        pass
    packs = load_packs(root)
    data = [{"pack": pk.name, "title": pk.title, "selectable": pk.selectable, "includes": pk.includes,
             "probes": sorted(pk.probes), "styles": sorted(pk.profiles), "dir": str(pk.dir)} for pk in packs.values()]
    _out(args, data, "\n".join(f"{d['pack']:20s} {'' if d['selectable'] else '(included only) '}{d['title']}\n"
                               f"{'':20s} probes: {', '.join(d['probes'])}; styles: {', '.join(d['styles'])}" for d in data))
    return 0


def cmd_pattern(args) -> int:
    f = PATTERN_DIR / f"{args.name}.md"
    if not f.is_file():
        names = sorted(x.stem for x in PATTERN_DIR.glob("*.md"))
        raise GamefeelError(f"unknown pattern '{args.name}'. Available: {', '.join(names)}")
    print(f.read_text())
    return 0


def cmd_doctor(args) -> int:
    godot = find_godot(args.godot)
    checks = {"gamefeel": __version__, "python": sys.version.split()[0], "godot": godot,
              "godot_version": godot_version(godot) if godot else None, "addon_dir": None, "project": None}
    try:
        checks["addon_dir"] = str(addon_dir())
    except GamefeelError as e:
        checks["addon_dir"] = f"MISSING: {e}"
    try:
        p = _project(args)
        checks["project"] = str(p.root)
        checks["graph"] = p.graph_db.exists()
        prof = yaml.safe_load(p.profile_path.read_text()) if p.profile_path.exists() else None
        checks["profile"] = None if prof is None else ("confirmed" if prof.get("confirmed") else "draft")
    except GamefeelError as e:
        checks["project"] = str(e)
    if godot:
        major = (checks["godot_version"] or "0").split(".")[0]
        checks["godot_ok"] = major == "4"
    _out(args, checks, "\n".join(f"{k:14s} {v}" for k, v in checks.items()))
    return 0 if godot else 1


def cmd_init(args) -> int:
    p = _project(args)
    p.state  # creates .gamefeel/ with its .gitignore
    msg = [f"initialised {p.state}"]
    if args.install_addon:
        dst = p.root / "addons" / "gamefeel"
        if dst.exists():
            shutil.rmtree(dst)
        shutil.copytree(addon_dir(), dst, ignore=shutil.ignore_patterns("*.uid", "__pycache__"))
        msg.append(f"copied addon to {dst} (optional in v1: gamefeel runs its scripts from outside the project)")
    msg.append("next: gamefeel scan && gamefeel detect")
    print("\n".join(msg))
    return 0


# ------------------------------------------------------------------ parser

def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="gamefeel", description="Understand a Godot game and measure how it feels.")
    ap.add_argument("--version", action="version", version=f"gamefeel {__version__}")
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--project", help="Godot project directory (default: search upwards from cwd)")
    common.add_argument("--json", action="store_true", help="machine-readable output")
    common.add_argument("--godot", help="Godot binary (default: $GAMEFEEL_GODOT or `godot` on PATH)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("scan", parents=[common], help="scan the project into the graph")
    s.add_argument("--full", action="store_true", help="rebuild from scratch")
    s.add_argument("--text-only", action="store_true", help="don't use Godot (text fallback)")
    s.add_argument("--exclude", action="append", default=[], help="path to skip (repeatable)")
    s.set_defaults(fn=cmd_scan)

    s = sub.add_parser("detect", parents=[common], help="detect player, actions, genre, mechanics")
    s.add_argument("--write-profile", action="store_true", help="(re)write the draft profile")
    s.add_argument("--force", action="store_true", help="overwrite even a confirmed profile")
    s.set_defaults(fn=cmd_detect)

    s = sub.add_parser("confirm", parents=[common], help="mark the profile as confirmed")
    s.set_defaults(fn=cmd_confirm)

    s = sub.add_parser("graph", parents=[common], help="query the graph")
    gs = s.add_subparsers(dest="graph_cmd", required=True)
    gs.add_parser("profile", parents=[common])
    q = gs.add_parser("query", parents=[common])
    q.add_argument("name")
    q.add_argument("args", nargs="*")
    q2 = gs.add_parser("sql", parents=[common])
    q2.add_argument("query")
    s.set_defaults(fn=cmd_graph)

    s = sub.add_parser("probe", parents=[common], help="list / validate / run probes")
    ps = s.add_subparsers(dest="probe_cmd", required=True)
    ps.add_parser("list", parents=[common])
    v = ps.add_parser("validate", parents=[common])
    v.add_argument("files", nargs="+")
    r = ps.add_parser("run", parents=[common])
    r.add_argument("--only", nargs="+", help="probe ids to run")
    r.add_argument("--file", action="append", help="extra probe YAML file (repeatable)")
    r.add_argument("--force", action="store_true", help="run even if the profile isn't confirmed")
    s.set_defaults(fn=cmd_probe)

    s = sub.add_parser("analyze", parents=[common], help="analyze the latest (or --run) probe run")
    s.add_argument("--run", help="run directory")
    s.set_defaults(fn=cmd_analyze)

    s = sub.add_parser("check", parents=[common], help="probe run + analyze")
    s.add_argument("--only", nargs="+")
    s.add_argument("--file", action="append")
    s.add_argument("--force", action="store_true")
    s.set_defaults(fn=cmd_check)

    for name, fn, hlp in (("metrics", cmd_metrics, "list metrics"), ("packs", cmd_packs, "list genre packs")):
        s = sub.add_parser(name, parents=[common], help=hlp)
        s.set_defaults(fn=fn)
    s = sub.add_parser("pattern", parents=[common], help="print an implementation pattern (coyote_time, ...)")
    s.add_argument("name")
    s.set_defaults(fn=cmd_pattern)
    s = sub.add_parser("doctor", parents=[common], help="check the environment")
    s.set_defaults(fn=cmd_doctor)
    s = sub.add_parser("init", parents=[common], help="create .gamefeel/ (optionally copy the addon)")
    s.add_argument("--install-addon", action="store_true")
    s.set_defaults(fn=cmd_init)
    return ap


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.fn(args) or 0
    except GamefeelError as e:
        print(f"gamefeel: {e}", file=sys.stderr)
        return 1
