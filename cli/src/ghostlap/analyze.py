"""`ghostlap feel analyze`: probe recordings -> metrics -> findings vs reference ranges -> report.

Pipeline: compute metrics → resolve ranges (pack profiles, style, overrides) → classify
(ok / warn / error, with direction and a feel word) → suggest (graph-linked knobs) →
group root causes (findings sharing a knob) → render report.json + report.md.

No LLM here: the analysis is deterministic so it can be tested and diffed. The agent reads
report.json afterwards.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from .graph.store import Graph
from .metrics import REGISTRY, load_probe_data
from .packs.loader import load_packs, load_ranges, merged_feel
from .project import Project
from .kinds import in_scope
from .suggest import suggest

SEVERITY_ORDER = {"error": 0, "warn": 1, "assert_failed": 0, "accepted": 2, "ok": 3, "style": 4, "info": 5, "not_measured": 6}


def classify(value: Any, rng: dict | None) -> tuple[str, str | None]:
    if value is None:
        return "not_measured", None
    if not rng or not rng.get("good"):
        return "info", None
    lo, hi = rng["good"]
    if lo <= value <= hi:
        return "ok", None
    direction = "high" if value > hi else "low"
    warn = rng.get("warn")
    if warn and warn[0] <= value <= warn[1]:
        return "warn", direction
    return "error", direction


def analyze(project: Project, run_dir: Path, profile: dict) -> dict:
    plan = json.loads((run_dir / "plan.json").read_text())
    summary = json.loads((run_dir / "summary.json").read_text()) if (run_dir / "summary.json").exists() else {}
    packs = load_packs(project.root)
    genres = plan.get("genres") or profile.get("genres") or []
    style = profile.get("style") or plan.get("style")
    ranges = load_ranges(packs, genres, style)
    for metric, override in ((profile.get("ranges") or {}).items()):
        ranges[metric] = {**ranges.get(metric, {}), **override, "pack": "profile override"}
    feel = merged_feel(packs, genres)
    accepted = profile.get("accepted") or {}
    fps = float(plan.get("physics_fps", 60))

    measured: dict[str, dict] = {}
    probe_status = []
    for cp in plan["probes"]:
        pd = load_probe_data(run_dir, cp)
        run_errors = [r.header.get("error") for r in pd.runs if r.header.get("error")]
        script_errors = []
        for r in pd.runs + ([pd.cal] if pd.cal else []):
            for e in r.header.get("script_errors") or []:
                msg = f"{e.get('file')}:{e.get('line')} {e.get('message')}"
                if msg not in script_errors:
                    script_errors.append(msg)
        probe_status.append({"probe": cp["id"], "pack": cp.get("pack"), "runs": len(pd.runs), "ok_runs": len(pd.ok_runs()),
                             "errors": sorted(set(run_errors)), "script_errors": script_errors[:5]})
        for m in cp.get("measure", []):
            mdef = REGISTRY.get(m)
            if mdef is None:
                continue
            try:
                mv = mdef.fn(pd)
            except Exception as e:  # a metric bug must not hide the rest of the report
                measured.setdefault(m, {"metric": m, "value": None, "unit": mdef.unit, "note": f"metric error: {e!r}", "probe": cp["id"]})
                continue
            entry = {"metric": m, "value": mv.value, "unit": mv.unit, "spread": mv.spread, "details": mv.details,
                     "evidence": {**mv.evidence, "dir": str(pd.dir)}, "note": mv.note, "probe": cp["id"],
                     "asserts": (cp.get("assert") or {}).get(m)}
            prev = measured.get(m)
            if prev is None or (prev["value"] is None and mv.value is not None):
                measured[m] = entry

    findings = []
    for m, e in measured.items():
        rng = ranges.get(m)
        sev, direction = classify(e["value"], rng)
        f = {**e, "range": rng.get("good") if rng else None, "warn_range": rng.get("warn") if rng else None,
             "calibrated": rng.get("calibrated") if rng else None, "range_source": rng.get("pack") if rng else None,
             "severity": sev, "direction": direction,
             "feel": (feel.get(m) or {}).get(direction) if direction else None,
             "description": REGISTRY[m].description}
        a = e.get("asserts")
        if a and e["value"] is not None and (("min" in a and e["value"] < a["min"]) or ("max" in a and e["value"] > a["max"])):
            f["severity"] = "assert_failed"
            f["feel"] = f["feel"] or "probe assertion failed"
        if f["severity"] in ("warn", "error") and not in_scope(m, profile.get("tune")):
            # motion character: describe it against the style, don't call it a problem
            f["style_note"] = f"{f['severity']} against the '{style}' style ({f.get('feel') or direction})"
            f["original_severity"] = f["severity"]
            f["severity"] = "style"
        if m in accepted and f["severity"] in ("warn", "error", "assert_failed"):
            f["accepted_reason"] = accepted[m]
            f["original_severity"] = f["severity"]
            f["severity"] = "accepted"
        if rng and rng.get("conflict"):
            f["range_conflict"] = rng["conflict"]
        findings.append(f)

    g = Graph(project.graph_db)
    for f in findings:
        if f["severity"] in ("warn", "error", "assert_failed"):
            f["suggestions"] = suggest(g, profile, f, measured, ranges, fps)
        else:
            f["suggestions"] = []
    _group_root_causes(findings)
    findings.sort(key=lambda f: (SEVERITY_ORDER.get(f["severity"], 9), f.get("root_cause") or "~", f["metric"]))

    counts: dict[str, int] = {}
    for f in findings:
        counts[f["severity"]] = counts.get(f["severity"], 0) + 1
    report = {
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "run_dir": str(run_dir),
        "project": str(project.root),
        "genres": genres,
        "style": style,
        "player": profile.get("player"),
        "counts": counts,
        "uncalibrated": any(f.get("calibrated") is False for f in findings),
        "findings": findings,
        "probes": probe_status,
        "runner_errors": summary.get("errors", []),
        "skipped_probes": json.loads((run_dir / "selection.json").read_text()).get("skipped", []) if (run_dir / "selection.json").exists() else [],
    }
    (run_dir / "report.json").write_text(json.dumps(report, indent=1, default=str))
    md = render_markdown(report)
    (run_dir / "report.md").write_text(md)
    (project.state / "report.md").write_text(md)
    (project.state / "report.json").write_text(json.dumps(report, indent=1, default=str))
    return report


def _group_root_causes(findings: list[dict]) -> None:
    """Findings that share a knob get one root cause and, where the models allow, one value."""
    by_knob: dict[str, list[tuple[dict, dict]]] = {}
    for f in findings:
        for s in f.get("suggestions", []):
            key = s.get("id") or s.get("setting") or (s.get("pattern") and f"pattern:{s['pattern']}")
            if key:
                by_knob.setdefault(key, []).append((f, s))
    n = 0
    for key, pairs in by_knob.items():
        metrics = {f["metric"] for f, _ in pairs}
        if len(metrics) < 2:
            continue
        rc = next((f.get("root_cause") for f, _ in pairs if f.get("root_cause")), None)
        if rc is None:
            n += 1
            rc = f"rc{n}"
        for f, _ in pairs:
            f["root_cause"] = rc
        _combine(pairs)


def _predict(s: dict, new: float) -> float | None:
    """metric ∝ knob^exponent."""
    cur = s.get("current")
    mv = s.get("metric_value")
    exp = s.get("exponent")
    if exp is None and s.get("model") in ("inverse", "proportional"):
        exp = -1.0 if s["model"] == "inverse" else 1.0
    if not isinstance(cur, (int, float)) or not cur or mv is None or exp is None or not new or (new / cur) <= 0:
        return None
    return mv * (new / cur) ** exp


def _combine(pairs: list[tuple[dict, dict]]) -> None:
    modelled = [(f, s) for f, s in pairs if (s.get("exponent") is not None or s.get("model"))
                and isinstance(s.get("proposed"), (int, float))]
    if len(modelled) < 2:
        return
    import math

    best = None
    for _, cand in modelled:
        value = cand["proposed"]
        in_range = 0
        for f, s in modelled:
            pred = _predict(s, value)
            rng = f.get("range")
            if pred is not None and rng and rng[0] <= pred <= rng[1]:
                in_range += 1
        change = abs(math.log(abs(value / cand["current"]))) if cand.get("current") else 0
        score = (in_range, -change)
        if best is None or score > best[0]:
            best = (score, value)
    value = best[1]
    for f, s in modelled:
        pred = _predict(s, value)
        s["proposed"] = value
        s["combined"] = True
        if pred is not None:
            s["predicted_effect"] = f"{f['metric']} {f['value']:.3g} → {pred:.3g} (shared value for this root cause)"


def _fmt(v: Any) -> str:
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return str(v)
    if isinstance(v, int):
        return str(v)
    if abs(v) >= 100:
        return f"{v:.0f}"
    if abs(v) >= 1:
        return f"{v:.2f}".rstrip("0").rstrip(".")
    return f"{v:.3g}"


ICON = {"error": "❌", "assert_failed": "❌", "warn": "⚠️", "ok": "✅", "accepted": "☑️", "style": "🎨", "info": "ℹ️", "not_measured": "—"}


def render_markdown(r: dict) -> str:
    lines = [f"# ghostlap report", "",
             f"- **Genres:** {', '.join(r['genres']) or '—'} (style: {r.get('style') or 'default'})",
             f"- **Player:** `{(r.get('player') or {}).get('scene')}` :: `{(r.get('player') or {}).get('node')}`",
             f"- **Summary:** " + ", ".join(f"{ICON.get(k, '')} {v} {k}" for k, v in sorted(r["counts"].items(), key=lambda kv: SEVERITY_ORDER.get(kv[0], 9))),
             f"- **Run:** `{r['run_dir']}`", ""]
    if r.get("uncalibrated"):
        lines += ["> ⚠️ Reference ranges are **uncalibrated placeholders** (see docs/calibration.md). Treat findings as "
                  "\"worth a look\", not verdicts.", ""]
    problems = [f for f in r["findings"] if f["severity"] in ("error", "warn", "assert_failed")]
    if problems:
        lines += ["## Findings", ""]
        seen_rc: set[str] = set()
        for f in problems:
            rc = f.get("root_cause")
            if rc and rc not in seen_rc:
                seen_rc.add(rc)
                members = [x["metric"] for x in r["findings"] if x.get("root_cause") == rc]
                lines += [f"**Root cause {rc}** — one change affects: {', '.join(members)}", ""]
            rng = f"{_fmt(f['range'][0])}–{_fmt(f['range'][1])}" if f.get("range") else "—"
            feel = f" — *{f['feel']}*" if f.get("feel") else ""
            lines.append(f"### {ICON[f['severity']]} `{f['metric']}` = {_fmt(f['value'])} {f['unit']} (good: {rng}){feel}")
            lines.append(f"{f['description']}  ")
            ev = f.get("evidence") or {}
            lines.append(f"Evidence: probe `{f['probe']}`" + (f", run `{ev['run']}`" if ev.get("run") else "") +
                         (f", frames {ev['frames']}" if ev.get("frames") else ""))
            if f.get("note"):
                lines.append(f"Note: {f['note']}")
            for s in f.get("suggestions", []):
                lines.append("- " + _fmt_sugg(s))
            lines.append("")
    styled = [f for f in r["findings"] if f["severity"] == "style"]
    if styled:
        lines += ["## Motion character (style, not problems)", "",
                  "These describe how the game moves. They're outside this style's reference range, which is a matter "
                  "of taste: momentum-heavy movement is a valid choice. They're only changed if you want a different feel "
                  "(`tune: all` in profile.yaml, or `ghostlap feel propose --scope all`).", "",
                  "| metric | value | style reference | reads as |", "|---|---|---|---|"]
        for f in styled:
            rng = f"{_fmt(f['range'][0])}–{_fmt(f['range'][1])}" if f.get("range") else ""
            lines.append(f"| `{f['metric']}` | {_fmt(f['value'])} {f['unit']} | {rng} | {f.get('feel') or ''} |")
        lines.append("")
    rest = [f for f in r["findings"] if f["severity"] not in ("error", "warn", "assert_failed", "style")]
    if rest:
        lines += ["## All metrics", "", "| | metric | value | good range | probe | note |", "|---|---|---|---|---|---|"]
        for f in rest + problems:
            rng = f"{_fmt(f['range'][0])}–{_fmt(f['range'][1])}" if f.get("range") else ""
            note = f.get("accepted_reason") or f.get("note") or ""
            lines.append(f"| {ICON[f['severity']]} | `{f['metric']}` | {_fmt(f['value'])} {f['unit'] if f['value'] is not None else ''} | {rng} | {f['probe']} | {note} |")
        lines.append("")
    if problems:
        lines += ["## Next", "",
                  "The suggestions above are **predictions** from simple models. Verify them before changing anything:",
                  "`ghostlap feel propose` applies them to a scratch copy, re-measures until clean, and reports what actually "
                  "changed; `ghostlap feel clip` shows it next to the current game. A clean report is not proof it feels right — "
                  "look at the clip, and record what still feels off with `ghostlap feel feedback \"...\"`.", ""]
    issues = [p for p in r["probes"] if p["errors"] or p["script_errors"] or p["ok_runs"] < p["runs"]]
    if issues or r.get("runner_errors") or r.get("skipped_probes"):
        lines += ["## Probe health", ""]
        for p in issues:
            lines.append(f"- `{p['probe']}`: {p['ok_runs']}/{p['runs']} runs ok. " + "; ".join(p["errors"] + p["script_errors"]))
        for e in r.get("runner_errors", []):
            lines.append(f"- runner: {e.get('message')}")
        for s in r.get("skipped_probes", []):
            lines.append(f"- skipped `{s['probe']}`: {s['reason']}")
        lines.append("")
    return "\n".join(lines)


def _fmt_sugg(s: dict) -> str:
    k = s["kind"]
    if k == "property":
        loc = f"{s.get('file')}:{s.get('line')}" if s.get("line") else s.get("file")
        eff = f" → {s['predicted_effect']}" if s.get("predicted_effect") else ""
        return f"Set `{s['property']}` {_fmt(s.get('current'))} → **{_fmt(s.get('proposed'))}** ({loc}). {s['why']}{eff}"
    if k == "node_property":
        return f"Set `{s['property']}` on node `{s.get('node')}` ({s.get('file')}) {_fmt(s.get('current'))} → **{_fmt(s.get('proposed'))}**. {s['why']}"
    if k == "setting":
        return f"Project setting `{s['setting']}` {_fmt(s.get('current'))} → **{_fmt(s.get('proposed'))}**. {s['why']}"
    if k == "pattern":
        return f"Add the **{s['pattern']}** pattern (`ghostlap feel pattern {s['pattern']}`): {s['why']}"
    extra = f" (`{s['query']}`)" if s.get("query") else ""
    return f"{s['why']}{extra}"
