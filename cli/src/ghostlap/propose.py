"""`ghostlap feel propose` / `ghostlap feel apply`: verified fixes.

`analyze` predicts what a change will do. `propose` checks: it copies the project to
.ghostlap/work/<stamp>/project, applies the suggested values there, re-runs the probes and
repeats (a few passes) until the report is clean or nothing more can be changed. Every value
moves at most `cap`x per pass, so changes can't compound unseen (e.g. one fix raising gravity
2.3x while another multiplies falling gravity 2.9x on top).

The result (.ghostlap/proposals/<stamp>/proposal.json) lists the net changes, every metric's
before → after (measured, not predicted), regressions, prediction errors, and what still
needs code (patterns). Nothing in the real project changes until `ghostlap feel apply`.
"""

from __future__ import annotations

import json
import shutil
import time
from pathlib import Path

import yaml

from .analyze import _predict, analyze
from .kinds import TASTE, in_scope  # noqa: F401
from .edits import Edit, apply_edit, edits_from_suggestion, read_value
from .probes import run as prun
from .project import GhostlapError, Project
from .scan.run import scan

PROBLEM = ("warn", "error", "assert_failed")

def _work_copy(project: Project, stamp: str) -> Path:
    dst = project.state / "work" / stamp / "project"
    if dst.exists():
        shutil.rmtree(dst)
    shutil.copytree(project.root, dst, symlinks=True,
                    ignore=shutil.ignore_patterns(".ghostlap", ".git", ".svn", ".hg"))
    return dst


def _cap(e: Edit, cap: float) -> tuple[Edit, bool]:
    cur, new = e.original, e.value
    if isinstance(new, bool) or not isinstance(cur, (int, float)) or isinstance(cur, bool) or not cur:
        return e, False
    ratio = new / cur
    if ratio <= 0:  # sign flip: never do that automatically
        return Edit(e.kind, e.file, e.property, cur, cur, e.node), True
    clamped = min(max(ratio, 1.0 / cap), cap)
    if clamped == ratio:
        return e, False
    v = cur * clamped
    if isinstance(cur, int):
        v = int(round(v))
    return Edit(e.kind, e.file, e.property, v, cur, e.node), True


def _coupled_ratios(problems: list[dict], cap: float) -> dict[str, float]:
    """Jump velocity and gravity must move together: height ∝ v²/g. Capping them separately
    changes the height (gravity clipped, velocity not → higher jump). Instead raise both ratios
    to the same power k (rv^k, rg^k), shrinking k until both are within the cap. That moves the
    jump part of the way toward its target timing in this pass and keeps height drift small;
    the next pass re-measures and continues."""
    for f in problems:
        for s in f.get("suggestions", []):
            c = s.get("couple")
            if c and c.get("group") == "jump_arc":
                rv, rg = c["v_ratio"], c["g_ratio"]
                k = 1.0
                for _ in range(60):  # rv**k, rg**k keeps rv²/rg's direction; shrink k until both fit
                    if max(abs(_log(rv ** k)), abs(_log(rg ** k))) <= _log(cap) + 1e-9:
                        break
                    k *= 0.9
                return {"v": rv ** k, "g": rg ** k}
    return {}


def _log(x: float) -> float:
    import math
    return math.log(x) if x > 0 else 0.0


def _metric_table(rep: dict) -> dict[str, dict]:
    return {f["metric"]: {"value": f["value"], "unit": f["unit"], "severity": f["severity"], "range": f.get("range")}
            for f in rep["findings"]}


def propose(project: Project, profile: dict, godot: str | None = None, passes: int = 4, cap: float = 2.0,
            only: list[str] | None = None, scope: str | None = None) -> dict:
    scope = scope or profile.get("tune") or "forgiveness"
    profile = {**profile, "tune": scope}  # analyze in the copy flags taste metrics only when scope is "all"
    stamp = time.strftime("%Y%m%d-%H%M%S")
    work = _work_copy(project, stamp)
    wp = Project(work)
    wp.profile_path.write_text(yaml.safe_dump(profile, sort_keys=False))

    first: dict | None = None
    final: dict | None = None
    history: list[dict] = []
    net: dict[str, Edit] = {}
    pending_predictions: list[dict] = []
    prediction_checks: list[dict] = []
    manual: list[dict] = []

    for i in range(passes + 1):
        scan(wp, godot=godot)
        res = prun.run(wp, profile, godot=godot, only=only)
        rep = analyze(wp, res.run_dir, profile)
        table = _metric_table(rep)
        for pr in pending_predictions:  # check last pass's predictions against this measurement
            got = table.get(pr["metric"], {}).get("value")
            pr["measured"] = got
            pr["error"] = round(got - pr["predicted"], 4) if isinstance(got, (int, float)) and pr["predicted"] is not None else None
            prediction_checks.append(pr)
        pending_predictions = []
        if first is None:
            first = rep
        final = rep
        problems = [f for f in rep["findings"] if f["severity"] in PROBLEM and in_scope(f["metric"], scope)]
        if not problems or i == passes:
            break
        applied, seen = [], set()
        coupled = _coupled_ratios(problems, cap)
        for f in problems:
            for s in f.get("suggestions", []):
                e = edits_from_suggestion(s)
                if e is None or e.key in seen:
                    continue
                seen.add(e.key)
                role = (s.get("couple") or {}).get("role")
                if role in coupled and isinstance(e.original, (int, float)) and e.original:
                    ratio = coupled[role]
                    v = e.original * ratio
                    capped = abs(ratio - (s["couple"]["v_ratio"] if role == "v" else s["couple"]["g_ratio"])) > 1e-9
                    e = Edit(e.kind, e.file, e.property, int(round(v)) if isinstance(e.original, int) else v, e.original, e.node)
                else:
                    e, capped = _cap(e, cap)
                ok, msg = apply_edit(work, e)
                if not ok:
                    manual.append({"metric": f["metric"], "property": e.property, "file": e.file, "why": msg})
                    continue
                net.setdefault(e.key, Edit(e.kind, e.file, e.property, e.value, e.original, e.node))
                net[e.key].value = e.value
                pred = _predict({**s, "proposed": e.value}, e.value) if s.get("exponent") is not None else None
                if pred is not None:
                    pending_predictions.append({"pass": i + 1, "metric": f["metric"], "property": e.property,
                                                "from": f["value"], "predicted": round(pred, 4)})
                applied.append({"metric": f["metric"], "property": e.property, "file": e.file, "node": e.node,
                                "from": e.original, "to": e.value, "capped": capped})
        history.append({"pass": i + 1, "problems_before": len(problems), "edits": applied})
        if not applied:
            break

    assert first is not None and final is not None
    before, after = _metric_table(first), _metric_table(final)
    metrics = []
    for m in sorted(set(before) | set(after)):
        b, a = before.get(m, {}), after.get(m, {})
        metrics.append({"metric": m, "before": b.get("value"), "after": a.get("value"), "unit": a.get("unit") or b.get("unit"),
                        "severity_before": b.get("severity"), "severity_after": a.get("severity"), "range": a.get("range")})
    regressions = [x for x in metrics if x["severity_before"] in ("ok", "info") and x["severity_after"] in PROBLEM]
    remaining = [{"metric": f["metric"], "value": f["value"], "severity": f["severity"],
                  "needs": [s for s in f.get("suggestions", []) if s["kind"] in ("pattern", "hint")]}
                 for f in final["findings"] if f["severity"] in PROBLEM and in_scope(f["metric"], scope)]
    taste_flags = [{"metric": f["metric"], "value": f["value"], "range": f.get("range"), "feel": f.get("feel")}
                   for f in final["findings"] if f["severity"] == "style"]
    proposal = {
        "stamp": stamp,
        "project": str(project.root),
        "work_dir": str(work),
        "clean": not remaining,
        "scope": scope,
        "taste_left_alone": taste_flags,
        "passes": len(history),
        "cap_per_pass": cap,
        "changes": [{**e.to_dict(), "original": _original(project.root, e)} for e in net.values()],
        "metrics": metrics,
        "regressions": regressions,
        "remaining": remaining,
        "manual": manual,
        "prediction_checks": prediction_checks,
        "history": history,
        "counts_before": first["counts"],
        "counts_after": final["counts"],
        "uncalibrated": final.get("uncalibrated"),
        "applied": False,
    }
    out = project.state / "proposals" / stamp
    out.mkdir(parents=True, exist_ok=True)
    (out / "proposal.json").write_text(json.dumps(proposal, indent=1, default=str))
    (out / "proposal.md").write_text(render(proposal))
    (project.state / "proposals" / "latest").write_text(stamp)
    return proposal


def _original(root: Path, e: Edit):
    v = read_value(root, e)
    return v if v is not None else e.original


def load(project: Project, stamp: str | None = None) -> tuple[dict, Path]:
    base = project.state / "proposals"
    if stamp is None:
        ptr = base / "latest"
        if not ptr.is_file():
            raise GhostlapError("no proposal yet — run `ghostlap feel propose`")
        stamp = ptr.read_text().strip()
    path = base / stamp / "proposal.json"
    if not path.is_file():
        raise GhostlapError(f"proposal {stamp} not found")
    return json.loads(path.read_text()), path


def apply(project: Project, stamp: str | None = None, only: list[str] | None = None, force: bool = False) -> dict:
    proposal, path = load(project, stamp)
    if proposal.get("applied") and not force:
        raise GhostlapError(f"proposal {proposal['stamp']} was already applied (use --force to re-apply)")
    done, skipped = [], []
    for c in proposal["changes"]:
        if only and c["property"] not in only:
            continue
        e = Edit(c["kind"], c["file"], c["property"], c["value"], c["original"], c.get("node"))
        current = read_value(project.root, e)
        if current is not None and c["original"] is not None and current != c["original"] and not force:
            skipped.append({**c, "why": f"file changed since the proposal (now {current}, proposal expected {c['original']})"})
            continue
        ok, msg = apply_edit(project.root, e)
        (done if ok else skipped).append({**c, "why": msg})
    proposal["applied"] = True
    proposal["applied_changes"] = done
    path.write_text(json.dumps(proposal, indent=1, default=str))
    return {"applied": done, "skipped": skipped, "proposal": proposal["stamp"]}


def _fmt(v) -> str:
    if isinstance(v, float):
        return f"{v:.3g}"
    return "—" if v is None else str(v)


ICON = {"error": "❌", "assert_failed": "❌", "warn": "⚠️", "ok": "✅", "accepted": "☑️", "info": "ℹ️", "not_measured": "—", None: " "}


def render(p: dict) -> str:
    lines = [f"# ghostlap proposal {p['stamp']}", "",
             f"Verified in a scratch copy ({p['passes']} pass(es), each value changes at most {p['cap_per_pass']}x per pass). "
             "Your project is unchanged until `ghostlap feel apply`.", "",
             f"**Before:** {p['counts_before']}  →  **After (measured):** {p['counts_after']}", ""]
    if p.get("uncalibrated"):
        lines += ["> Ranges are uncalibrated placeholders. **Look at the clip** (`ghostlap feel clip`) before applying — "
                  "a clean report is not proof it feels right.", ""]
    lines += ["## Changes", "", "| file | property | from | to |", "|---|---|---|---|"]
    for c in p["changes"]:
        where = c["file"] + (f" :: {c['node']}" if c.get("node") else "")
        lines.append(f"| {where} | `{c['property']}` | {_fmt(c['original'])} | **{_fmt(c['value'])}** |")
    lines += ["", "## Metrics (measured)", "", "| metric | before | after | good range |", "|---|---|---|---|"]
    for m in p["metrics"]:
        if m["before"] == m["after"] and m["severity_before"] == m["severity_after"]:
            continue
        rng = f"{_fmt(m['range'][0])}–{_fmt(m['range'][1])}" if m.get("range") else ""
        lines.append(f"| `{m['metric']}` | {ICON.get(m['severity_before'], '')} {_fmt(m['before'])} | "
                     f"{ICON.get(m['severity_after'], '')} {_fmt(m['after'])} {m['unit'] or ''} | {rng} |")
    if p.get("taste_left_alone"):
        lines += ["", "## Motion character left alone (taste)", "",
                  f"Scope is `{p.get('scope')}`: these are outside the current style's ranges but describe the game's "
                  "personality, so they weren't changed. If the user wants a different feel, pick a style and run "
                  "`ghostlap feel propose --scope all` (or set `tune: all` in profile.yaml).", ""]
        lines += [f"- `{t['metric']}` = {_fmt(t['value'])}" + (f" ({t['feel']})" if t.get("feel") else "") for t in p["taste_left_alone"]]
    if p["regressions"]:
        lines += ["", "## ⚠️ Regressions", ""] + [f"- `{r['metric']}` was {r['severity_before']}, now {r['severity_after']} ({_fmt(r['after'])})" for r in p["regressions"]]
    if p["remaining"] or p["manual"]:
        lines += ["", "## Still needs work (code, not numbers)", ""]
        for r in p["remaining"]:
            need = "; ".join((s.get("pattern") and f"pattern `{s['pattern']}`") or s.get("why", "") for s in r["needs"]) or "no automatic fix"
            lines.append(f"- `{r['metric']}` = {_fmt(r['value'])}: {need}")
        for mnl in p["manual"]:
            lines.append(f"- `{mnl['property']}` ({mnl['file']}): {mnl['why']}")
    bad = [c for c in p["prediction_checks"] if c.get("error") is not None and c["predicted"] and abs(c["error"]) > 0.15 * abs(c["predicted"])]
    if bad:
        lines += ["", "## Predictions that were off by more than 15%", ""]
        lines += [f"- pass {c['pass']}: `{c['metric']}` via `{c['property']}` predicted {_fmt(c['predicted'])}, measured {_fmt(c['measured'])}" for c in bad]
    return "\n".join(lines) + "\n"
