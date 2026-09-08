#!/usr/bin/env python3
"""Record v1.3.0 single-fallback evaluator decision sequences as golden fixtures.

Offline tool (NOT run in CI): extracts scripts/health_check.py from the
`v1.3.0` git tag, replays the scenario input sequences below through its pure
`evaluate`, and writes v130_golden.json containing inputs + observed outputs.
The committed JSON is the fixture; tests/test_health_check.py replays the
same inputs through the CURRENT engine (single-element fallback list) and
asserts equal decisions and counter transitions at every step.

Re-record: python3 tests/fixtures/record_v130_golden.py  (from the repo root,
with the v1.3.0 tag present).
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
OUT = Path(__file__).resolve().parent / "v130_golden.json"

PRIMARY = "node-a"
FALLBACK = "node-b"

# Each scenario: thresholds (+ restore/ensure), initial state tweaks, then
# steps. A step supplies the live-derived active_role, both probes'
# reachability, and the evaluation clock; optional "set_last_switch" mimics a
# record-switch between cycles (the controller's write, outside evaluate).
SCENARIOS = [
    {
        "name": "healthy_steady",
        "steps": [
            {"active_role": "primary", "p_reach": True, "f_reach": True, "now": 1000.0},
            {"active_role": "primary", "p_reach": True, "f_reach": True, "now": 1030.0},
            {"active_role": "primary", "p_reach": True, "f_reach": True, "now": 1060.0},
        ],
    },
    {
        "name": "hysteresis_to_switch",
        "steps": [
            {"active_role": "primary", "p_reach": True, "f_reach": True, "now": 1000.0},
            {"active_role": "primary", "p_reach": False, "f_reach": True, "now": 1030.0},
            {"active_role": "primary", "p_reach": False, "f_reach": True, "now": 1060.0},
            {"active_role": "primary", "p_reach": False, "f_reach": True, "now": 1090.0},
        ],
    },
    {
        "name": "fallback_unverified_then_both_down",
        "initial": {"p_state": "DOWN", "p_fail": 3},
        "steps": [
            {"active_role": "primary", "p_reach": False, "f_reach": False, "now": 1000.0},
            {"active_role": "primary", "p_reach": False, "f_reach": False, "now": 1030.0},
            {"active_role": "primary", "p_reach": False, "f_reach": False, "now": 1060.0},
        ],
    },
    {
        "name": "fallback_down_while_primary_unknown",
        "initial": {"f_state": "DOWN", "f_fail": 3},
        "steps": [
            {"active_role": "fallback", "p_reach": False, "f_reach": False, "now": 1000.0},
            {"active_role": "fallback", "p_reach": True, "f_reach": False, "now": 1030.0},
        ],
    },
    {
        "name": "cooldown_blocks_switch",
        "initial": {"p_state": "DOWN", "f_state": "UP", "p_fail": 3, "last_switch": 1080.0},
        "steps": [
            {"active_role": "primary", "p_reach": False, "f_reach": True, "now": 1100.0},
            {"active_role": "primary", "p_reach": False, "f_reach": True, "now": 1150.0},
        ],
    },
    {
        "name": "restore_primary",
        "initial": {"p_state": "DOWN", "f_state": "UP", "p_fail": 3, "last_switch": 1000.0},
        "steps": [
            {"active_role": "fallback", "p_reach": True, "f_reach": True, "now": 1100.0},
            {"active_role": "fallback", "p_reach": True, "f_reach": True, "now": 1130.0},
            {"active_role": "fallback", "p_reach": True, "f_reach": True, "now": 1160.0},
            {"active_role": "fallback", "p_reach": True, "f_reach": True, "now": 1190.0},
        ],
    },
    {
        "name": "restore_disabled",
        "thresholds": {"restore_primary": False},
        "initial": {"p_state": "UP", "f_state": "UP", "last_switch": 1000.0},
        "steps": [
            {"active_role": "fallback", "p_reach": True, "f_reach": True, "now": 2000.0},
            {"active_role": "fallback", "p_reach": True, "f_reach": False, "now": 2030.0},
        ],
    },
    {
        "name": "staying_on_fallback_and_fallback_down",
        "initial": {"p_state": "DOWN", "f_state": "UP", "p_fail": 3, "last_switch": 1000.0},
        "steps": [
            {"active_role": "fallback", "p_reach": False, "f_reach": True, "now": 2000.0},
            {"active_role": "fallback", "p_reach": False, "f_reach": False, "now": 2030.0},
            {"active_role": "fallback", "p_reach": False, "f_reach": False, "now": 2060.0},
            {"active_role": "fallback", "p_reach": False, "f_reach": False, "now": 2090.0},
            {"active_role": "fallback", "p_reach": True, "f_reach": False, "now": 2120.0},
        ],
    },
    {
        "name": "ensure_primary_from_none",
        "thresholds": {"ensure_primary": True},
        "steps": [
            {"active_role": "none", "p_reach": False, "f_reach": True, "now": 1000.0},
            {"active_role": "none", "p_reach": True, "f_reach": True, "now": 1030.0},
        ],
    },
    {
        "name": "none_without_ensure",
        "steps": [
            {"active_role": "none", "p_reach": True, "f_reach": True, "now": 1000.0},
        ],
    },
    {
        "name": "unknown_active_never_overridden",
        "steps": [
            {"active_role": "unknown", "p_reach": True, "f_reach": True, "now": 1000.0},
            {"active_role": "unknown", "p_reach": False, "f_reach": True, "now": 1030.0},
        ],
    },
]


def load_v130():
    src = subprocess.run(
        ["git", "-C", str(REPO), "show", "v1.3.0:scripts/health_check.py"],
        capture_output=True, text=True, check=True,
    ).stdout
    tmp = Path(tempfile.mkdtemp()) / "hc_v130.py"
    tmp.write_text(src, encoding="utf-8")
    spec = importlib.util.spec_from_file_location("hc_v130", tmp)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["hc_v130"] = mod  # dataclasses on py3.9 need the module registered
    spec.loader.exec_module(mod)
    return mod


def snapshot(node):
    return {
        "last_state": node["last_state"],
        "fail_count": node["fail_count"],
        "ok_count": node["ok_count"],
    }


def main() -> int:
    hc = load_v130()
    assert hc.STATE_SCHEMA_VERSION == 1, "expected the v1.3.0 engine"
    recorded = []
    for scen in SCENARIOS:
        th_kwargs = {"fail_threshold": 3, "ok_threshold": 3, "cooldown": 60.0,
                     "restore_primary": True, "ensure_primary": False}
        th_kwargs.update(scen.get("thresholds", {}))
        th = hc.Thresholds(**th_kwargs)
        state = hc.default_state(PRIMARY, FALLBACK)
        init = scen.get("initial", {})
        if "p_state" in init:
            state["nodes"]["primary"]["last_state"] = init["p_state"]
        if "f_state" in init:
            state["nodes"]["fallback"]["last_state"] = init["f_state"]
        if "p_fail" in init:
            state["nodes"]["primary"]["fail_count"] = init["p_fail"]
        if "f_fail" in init:
            state["nodes"]["fallback"]["fail_count"] = init["f_fail"]
        if "last_switch" in init:
            state["active"]["last_switch_epoch"] = init["last_switch"]
        steps_out = []
        for step in scen["steps"]:
            if "set_last_switch" in step:
                state["active"]["last_switch_epoch"] = step["set_last_switch"]
            d = hc.evaluate(
                state,
                step["active_role"],
                hc.ProbeResult(label=PRIMARY, reachable=step["p_reach"]),
                hc.ProbeResult(label=FALLBACK, reachable=step["f_reach"]),
                th,
                now=step["now"],
            )
            steps_out.append({
                "input": step,
                "decision": d.to_dict(),
                "post_primary": snapshot(state["nodes"]["primary"]),
                "post_fallback": snapshot(state["nodes"]["fallback"]),
            })
        recorded.append({
            "name": scen["name"],
            "thresholds": th_kwargs,
            "initial": init,
            "steps": steps_out,
        })
    OUT.write_text(json.dumps({
        "recorded_from": "v1.3.0",
        "primary_label": PRIMARY,
        "fallback_label": FALLBACK,
        "scenarios": recorded,
    }, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    total = sum(len(s["steps"]) for s in recorded)
    print(f"recorded {len(recorded)} scenarios / {total} steps -> {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
