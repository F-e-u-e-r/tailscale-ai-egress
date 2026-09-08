import argparse
import contextlib
import io
import json
import math
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts import health_check as hc

VERSION = (ROOT / "VERSION").read_text(encoding="utf-8").strip()

SAMPLE_STATUS = {
    "BackendState": "Running",
    "Self": {
        "ID": "selfID",
        "HostName": "myclient",
        "DNSName": "myclient.example.ts.net.",
        "TailscaleIPs": ["100.64.0.5"],
    },
    "Peer": {
        "key1": {
            "ID": "nodeP",
            "HostName": "primary-vps",
            "DNSName": "primary-vps.example.ts.net.",
            "TailscaleIPs": ["100.64.0.1"],
        },
        "key2": {
            "ID": "nodeF",
            "HostName": "fallback-vps",
            "DNSName": "fallback-vps.example.ts.net.",
            "TailscaleIPs": ["100.64.0.2"],
        },
    },
    "ExitNodeStatus": {"ID": "nodeP", "TailscaleIPs": ["100.64.0.1/32"], "Online": True},
}

FAKE_TAILSCALE = """#!/usr/bin/env bash
set -u
if [ "${1:-}" = "ping" ]; then
  shift
  while [ "${1:-}" = "-c" ]; do shift 2; done
  label="${1:-}"
  if [ -n "${FAKE_UNREACHABLE:-}" ]; then
    IFS=',' read -ra downs <<< "$FAKE_UNREACHABLE"
    for d in "${downs[@]}"; do
      [ "$d" = "$label" ] && exit 1
    done
  fi
  echo "pong from $label (100.64.0.1) via DERP(sfo) in 12ms"
  exit 0
fi
exit 0
"""

FAKE_TAILSCALE_NO_C = """#!/usr/bin/env bash
set -u
if [ "${1:-}" = "ping" ]; then
  shift
  if [ "${1:-}" = "-c" ]; then
    echo "flag provided but not defined: -c" >&2
    exit 1
  fi
  echo "pong from ${1:-} in 5ms"
  exit 0
fi
exit 0
"""


def probe(label, reachable):
    return hc.ProbeResult(label=label, reachable=reachable)


def ev1(state, active_role, primary_probe, fallback_probe, th, now):
    """Single-element evaluate shim: the v1.3.0-shaped call sites drive the
    generalized evaluator with a one-slot bench (active fallback ⇒ index 0)."""
    index = 0 if active_role == "fallback" else None
    return hc.evaluate(state, active_role, index, primary_probe, [fallback_probe], th, now)


def live_role(status, p_id, p_ips, f_id, f_ips):
    """Single-pair derive_active shim returning just the role (the v1.3.0
    live_active_role surface these tests were written against)."""
    role, _index, _problem = hc.derive_active(
        status, (p_id, p_ips), [(f_id, f_ips)], "primary-vps", ["fallback-vps"], active_record=None
    )
    return role


def run_cli(argv):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = hc.main(argv)
    return rc, buf.getvalue()


class EvaluatorTests(unittest.TestCase):
    """Pure decision-matrix coverage (no subprocess)."""

    def setUp(self):
        self.th = hc.Thresholds(fail_threshold=3, ok_threshold=3, cooldown=60.0, restore_primary=True)

    def _state(self, primary_state, fallback_state, last_switch=0.0):
        state = hc.default_state("primary-vps", ["fallback-vps"])
        state["nodes"]["primary"]["last_state"] = primary_state
        state["nodes"]["fallbacks"][0]["last_state"] = fallback_state
        state["active"]["last_switch_epoch"] = last_switch
        return state

    def test_primary_healthy_no_action(self):
        state = self._state(hc.STATE_UP, hc.STATE_UP)
        d = ev1(state, "primary", probe("p", True), probe("f", True), self.th, now=10_000.0)
        self.assertEqual(d.action, "none")
        self.assertEqual(d.reason, "healthy")

    def test_primary_down_switches_to_verified_fallback(self):
        state = self._state(hc.STATE_DOWN, hc.STATE_UP)
        d = ev1(state, "primary", probe("p", False), probe("f", True), self.th, now=10_000.0)
        self.assertEqual(d.action, "switch-to-fallback")
        self.assertEqual(d.target_role, "fallback")
        self.assertEqual(d.target_label, "fallback-vps")

    def test_primary_down_but_fallback_unverified_does_not_switch(self):
        # Fallback state is still UP, but it failed its ping THIS round.
        state = self._state(hc.STATE_DOWN, hc.STATE_UP)
        d = ev1(state, "primary", probe("p", False), probe("f", False), self.th, now=10_000.0)
        self.assertEqual(d.action, "none")
        self.assertEqual(d.reason, "fallback_unverified")

    def test_both_down(self):
        state = self._state(hc.STATE_DOWN, hc.STATE_DOWN)
        d = ev1(state, "primary", probe("p", False), probe("f", False), self.th, now=10_000.0)
        self.assertEqual(d.action, "none")
        self.assertEqual(d.reason, "both_down")

    def test_cooldown_blocks_switch(self):
        state = self._state(hc.STATE_DOWN, hc.STATE_UP, last_switch=10_000.0)
        d = ev1(state, "primary", probe("p", False), probe("f", True), self.th, now=10_000.0)
        self.assertEqual(d.action, "none")
        self.assertEqual(d.reason, "cooldown")

    def test_restore_primary_enabled_switches_back(self):
        state = self._state(hc.STATE_UP, hc.STATE_UP)
        d = ev1(state, "fallback", probe("p", True), probe("f", True), self.th, now=10_000.0)
        self.assertEqual(d.action, "switch-to-primary")
        self.assertEqual(d.event, "primary_recovered")

    def test_restore_primary_disabled_never_switches_back(self):
        th = hc.Thresholds(fail_threshold=3, ok_threshold=3, cooldown=60.0, restore_primary=False)
        state = self._state(hc.STATE_UP, hc.STATE_UP)
        d = ev1(state, "fallback", probe("p", True), probe("f", True), th, now=10_000.0)
        self.assertEqual(d.action, "none")
        self.assertEqual(d.reason, "restore_primary_disabled")
        self.assertEqual(d.event, "primary_recovered")

    def test_staying_on_fallback_while_primary_down(self):
        state = self._state(hc.STATE_DOWN, hc.STATE_UP)
        d = ev1(state, "fallback", probe("p", False), probe("f", True), self.th, now=10_000.0)
        self.assertEqual(d.action, "none")
        self.assertEqual(d.reason, "staying_on_fallback")

    def test_active_none_does_not_impose_exit_node(self):
        state = self._state(hc.STATE_UP, hc.STATE_UP)
        d = ev1(state, "none", probe("p", True), probe("f", True), self.th, now=10_000.0)
        self.assertEqual(d.action, "none")
        self.assertEqual(d.reason, "no_active_exit_node")

    def test_active_unknown_does_not_override_user_choice(self):
        state = self._state(hc.STATE_UP, hc.STATE_UP)
        d = ev1(state, "unknown", probe("p", True), probe("f", True), self.th, now=10_000.0)
        self.assertEqual(d.action, "none")
        self.assertEqual(d.reason, "unknown_active")

    def test_hysteresis_requires_threshold_failures(self):
        # One failure must NOT trip a DOWN/switch with fail_threshold=3.
        state = self._state(hc.STATE_UP, hc.STATE_UP)
        d = ev1(state, "primary", probe("p", False), probe("f", True), self.th, now=10_000.0)
        self.assertEqual(d.action, "none")
        self.assertEqual(d.reason, "healthy")
        self.assertEqual(state["nodes"]["primary"]["fail_count"], 1)

    def test_hysteresis_flips_exactly_at_threshold(self):
        state = self._state(hc.STATE_UP, hc.STATE_UP)
        state["nodes"]["primary"]["fail_count"] = 2  # one more failure reaches threshold 3
        d = ev1(state, "primary", probe("p", False), probe("f", True), self.th, now=10_000.0)
        self.assertEqual(state["nodes"]["primary"]["last_state"], hc.STATE_DOWN)
        self.assertEqual(d.action, "switch-to-fallback")

    def test_ensure_primary_selects_primary_when_none(self):
        th = hc.Thresholds(fail_threshold=1, ok_threshold=1, cooldown=0.0, ensure_primary=True)
        state = self._state(hc.STATE_UP, hc.STATE_UP)
        d = ev1(state, "none", probe("p", True), probe("f", True), th, now=10_000.0)
        self.assertEqual(d.action, "switch-to-primary")
        self.assertEqual(d.reason, "ensure_primary")

    def test_ensure_primary_does_not_switch_on_unknown(self):
        # A malformed/untrusted ExitNodeStatus resolves to "unknown" (see
        # test_live_active_role_malformed_status_is_unknown_not_none), and "unknown"
        # must NOT authorize a switch even under --ensure-primary -- only a genuine
        # "none" does. Together they close the malformed-status fail-open (Blocker 1a).
        th = hc.Thresholds(fail_threshold=1, ok_threshold=1, cooldown=0.0, ensure_primary=True)
        state = self._state(hc.STATE_UP, hc.STATE_UP)
        d = ev1(state, "unknown", probe("p", True), probe("f", True), th, now=10_000.0)
        self.assertEqual(d.action, "none")
        self.assertEqual(d.reason, "unknown_active")

    def test_none_without_ensure_primary_does_nothing(self):
        th = hc.Thresholds(ensure_primary=False)
        state = self._state(hc.STATE_UP, hc.STATE_UP)
        d = ev1(state, "none", probe("p", True), probe("f", True), th, now=10_000.0)
        self.assertEqual(d.action, "none")
        self.assertEqual(d.reason, "no_active_exit_node")

    def test_ensure_primary_skips_when_primary_unreachable(self):
        th = hc.Thresholds(ensure_primary=True)
        state = self._state(hc.STATE_UP, hc.STATE_UP)
        d = ev1(state, "none", probe("p", False), probe("f", True), th, now=10_000.0)
        self.assertEqual(d.action, "none")
        self.assertEqual(d.reason, "no_active_exit_node")


class MultiFallbackEvaluatorTests(unittest.TestCase):
    """Pure decision-matrix coverage for the ORDERED multi-fallback bench
    (docs/design/multi-fallback.md). Slot labels are fb0/fb1/fb2; every walk
    must honor configuration order (order IS priority, no round-robin)."""

    LABELS = ["fb0", "fb1", "fb2"]

    def setUp(self):
        self.th = hc.Thresholds(fail_threshold=3, ok_threshold=3, cooldown=60.0, restore_primary=True)

    def _state(self, primary_state, fallback_states, last_switch=0.0):
        state = hc.default_state("primary-vps", list(self.LABELS))
        state["nodes"]["primary"]["last_state"] = primary_state
        for slot, value in zip(state["nodes"]["fallbacks"], fallback_states):
            slot["last_state"] = value
        state["active"]["last_switch_epoch"] = last_switch
        return state

    def _eval(self, state, role, index, p_reach, f_reach, th=None, now=10_000.0, unresolved=frozenset()):
        return hc.evaluate(
            state, role, index, probe("p", p_reach),
            [probe(label, reach) for label, reach in zip(self.LABELS, f_reach)],
            th or self.th, now, unresolved,
        )

    def test_e1_walk_order_first_reachable_wins(self):
        # slot0 unreachable, slot1+slot2 reachable -> the walk picks slot1, never slot2.
        state = self._state(hc.STATE_DOWN, [hc.STATE_UP] * 3)
        d = self._eval(state, "primary", None, False, [False, True, True])
        self.assertEqual((d.action, d.reason), ("switch-to-fallback", "primary_down"))
        self.assertEqual(d.target_index, 1)
        self.assertEqual(d.target_label, "fb1")

    def test_e2_walk_continues_past_unverified(self):
        # Declared priority bends only toward safety: unreachable fb0 is walked past.
        state = self._state(hc.STATE_DOWN, [hc.STATE_UP, hc.STATE_UNKNOWN, hc.STATE_UP])
        d = self._eval(state, "primary", None, False, [False, True, False])
        self.assertEqual(d.target_index, 1)

    def test_e3_all_fallbacks_down_only_when_every_slot_down(self):
        state = self._state(hc.STATE_DOWN, [hc.STATE_DOWN] * 3)
        for slot in state["nodes"]["fallbacks"]:
            slot["fail_count"] = 3
        d = self._eval(state, "primary", None, False, [False, False, False])
        self.assertEqual((d.action, d.reason), ("none", "all_fallbacks_down"))
        # E3 twin: ONE slot still UNKNOWN blocks the all-down classification.
        state = self._state(hc.STATE_DOWN, [hc.STATE_DOWN, hc.STATE_UNKNOWN, hc.STATE_DOWN])
        d = self._eval(state, "primary", None, False, [False, False, False])
        self.assertEqual((d.action, d.reason), ("none", "no_fallback_verified"))

    def test_e4_no_fallback_verified_mixed_states(self):
        state = self._state(hc.STATE_DOWN, [hc.STATE_UNKNOWN, hc.STATE_UP, hc.STATE_UNKNOWN])
        d = self._eval(state, "primary", None, False, [False, False, False])
        self.assertEqual((d.action, d.reason), ("none", "no_fallback_verified"))

    def test_e5_unknown_state_but_reachable_is_selectable(self):
        # The walk bar is reachability-this-round; a mid-hysteresis UNKNOWN (or
        # even DOWN-state) candidate with a passing ping is selectable — v1.3.0's
        # exact bar, generalized.
        state = self._state(hc.STATE_DOWN, [hc.STATE_UNKNOWN, hc.STATE_UP, hc.STATE_UP])
        d = self._eval(state, "primary", None, False, [True, True, True])
        self.assertEqual(d.target_index, 0)
        state = self._state(hc.STATE_DOWN, [hc.STATE_DOWN, hc.STATE_UP, hc.STATE_UP])
        state["nodes"]["fallbacks"][0]["fail_count"] = 3
        d = self._eval(state, "primary", None, False, [True, False, False])
        self.assertEqual(d.target_index, 0)  # DOWN-state + passing ping: selectable, as today

    def test_e6_fallback_down_next_fallback(self):
        # THE new capability: active fallback DOWN, primary not restorable ->
        # first verified other slot wins.
        state = self._state(hc.STATE_DOWN, [hc.STATE_DOWN, hc.STATE_UP, hc.STATE_UP])
        state["nodes"]["fallbacks"][0]["fail_count"] = 3
        d = self._eval(state, "fallback", 0, False, [False, True, True])
        self.assertEqual((d.action, d.reason), ("switch-to-fallback", "fallback_down_next_fallback"))
        self.assertEqual(d.target_index, 1)
        self.assertEqual(d.target_label, "fb1")

    def test_e7_restore_outranks_walk_same_cycle(self):
        # Primary restorable + active slot DOWN + a verified bench slot: the
        # restore row fires and the walk is NOT evaluated that cycle.
        state = self._state(hc.STATE_UP, [hc.STATE_DOWN, hc.STATE_UP, hc.STATE_UP])
        d = self._eval(state, "fallback", 0, True, [False, True, True])
        self.assertEqual((d.action, d.reason), ("switch-to-primary", "primary_recovered"))
        self.assertEqual(d.event, "primary_recovered")
        self.assertIsNone(d.target_index)

    def test_e8_restore_disabled_walk_still_runs(self):
        # RESTORE_PRIMARY=0 with a healthy primary and the ACTIVE slot DOWN:
        # restore is disabled, surviving is not — the walk runs.
        th = hc.Thresholds(fail_threshold=3, ok_threshold=3, cooldown=60.0, restore_primary=False)
        state = self._state(hc.STATE_UP, [hc.STATE_DOWN, hc.STATE_UP, hc.STATE_UP])
        d = self._eval(state, "fallback", 0, True, [False, True, True], th=th)
        self.assertEqual((d.action, d.reason), ("switch-to-fallback", "fallback_down_next_fallback"))
        self.assertEqual(d.target_index, 1)

    def test_e9_restore_disabled_healthy_fallback_unchanged(self):
        th = hc.Thresholds(fail_threshold=3, ok_threshold=3, cooldown=60.0, restore_primary=False)
        state = self._state(hc.STATE_UP, [hc.STATE_UP, hc.STATE_UP, hc.STATE_UP])
        d = self._eval(state, "fallback", 0, True, [True, True, True], th=th)
        self.assertEqual((d.action, d.reason), ("none", "restore_primary_disabled"))
        self.assertEqual(d.event, "primary_recovered")

    def test_e10_all_down_only_when_primary_and_every_slot_down(self):
        state = self._state(hc.STATE_DOWN, [hc.STATE_DOWN] * 3)
        d = self._eval(state, "fallback", 0, False, [False, False, False])
        self.assertEqual((d.action, d.reason), ("none", "all_down"))

    def test_e11_active_fallback_nothing_selectable_primary_up(self):
        # Primary UP but restore disabled: reason classifies the bench, not the primary.
        th = hc.Thresholds(fail_threshold=3, ok_threshold=3, cooldown=60.0, restore_primary=False)
        state = self._state(hc.STATE_UP, [hc.STATE_DOWN, hc.STATE_UNKNOWN, hc.STATE_UNKNOWN])
        d = self._eval(state, "fallback", 0, True, [False, False, False], th=th)
        self.assertEqual((d.action, d.reason), ("none", "no_fallback_verified"))

    def test_e12_cooldown_blocks_every_switch_kind(self):
        # walk from primary
        state = self._state(hc.STATE_DOWN, [hc.STATE_UP] * 3, last_switch=9_990.0)
        d = self._eval(state, "primary", None, False, [True, True, True])
        self.assertEqual((d.action, d.reason), ("none", "cooldown"))
        # f2f walk
        state = self._state(hc.STATE_DOWN, [hc.STATE_DOWN, hc.STATE_UP, hc.STATE_UP], last_switch=9_990.0)
        state["nodes"]["fallbacks"][0]["fail_count"] = 3
        d = self._eval(state, "fallback", 0, False, [False, True, True])
        self.assertEqual((d.action, d.reason), ("none", "cooldown"))
        # restore
        state = self._state(hc.STATE_UP, [hc.STATE_UP] * 3, last_switch=9_990.0)
        d = self._eval(state, "fallback", 0, True, [True, True, True])
        self.assertEqual((d.action, d.reason, d.event), ("none", "cooldown", "primary_recovered"))
        # delisted restore + delisted walk
        state = self._state(hc.STATE_UP, [hc.STATE_UP] * 3, last_switch=9_990.0)
        d = self._eval(state, "delisted", None, True, [True, True, True])
        self.assertEqual((d.action, d.reason), ("none", "cooldown"))
        th = hc.Thresholds(fail_threshold=3, ok_threshold=3, cooldown=60.0, restore_primary=False)
        state = self._state(hc.STATE_UP, [hc.STATE_UP] * 3, last_switch=9_990.0)
        d = self._eval(state, "delisted", None, True, [True, True, True], th=th)
        self.assertEqual((d.action, d.reason), ("none", "cooldown"))

    def test_e13_ensure_primary_never_selects_fallback(self):
        th = hc.Thresholds(fail_threshold=3, ok_threshold=3, cooldown=0.0, ensure_primary=True)
        state = self._state(hc.STATE_UP, [hc.STATE_UP] * 3)
        d = self._eval(state, "none", None, False, [True, True, True], th=th)
        self.assertEqual((d.action, d.reason), ("none", "no_active_exit_node"))

    def test_e14_delisted_restore_demands_strict_bar(self):
        # Restore needs reachable AND state UP (the primary_recovered bar).
        # A reachable mid-hysteresis UNKNOWN primary walks instead.
        state = self._state(hc.STATE_UNKNOWN, [hc.STATE_UP] * 3)
        d = self._eval(state, "delisted", None, True, [True, True, True])
        self.assertEqual((d.action, d.reason), ("switch-to-fallback", "delisted_next_fallback"))
        self.assertEqual(d.target_index, 0)
        # With the strict bar met, restore wins.
        state = self._state(hc.STATE_UP, [hc.STATE_UP] * 3)
        d = self._eval(state, "delisted", None, True, [True, True, True])
        self.assertEqual((d.action, d.reason), ("switch-to-primary", "delisted_restore_primary"))

    def test_e15_delisted_walk_from_the_top(self):
        th = hc.Thresholds(fail_threshold=3, ok_threshold=3, cooldown=60.0, restore_primary=False)
        state = self._state(hc.STATE_UP, [hc.STATE_UP] * 3)
        d = self._eval(state, "delisted", None, True, [False, True, True], th=th)
        self.assertEqual((d.action, d.reason), ("switch-to-fallback", "delisted_next_fallback"))
        self.assertEqual(d.target_index, 1)

    def test_e16_delisted_no_target(self):
        state = self._state(hc.STATE_DOWN, [hc.STATE_DOWN] * 3)
        d = self._eval(state, "delisted", None, False, [False, False, False])
        self.assertEqual((d.action, d.reason), ("none", "delisted_no_target"))

    def test_e18_walk_priority_pin_kills_round_robin(self):
        # Active slot 1 DOWN; slots 0 AND 2 both reachable. A j>i-only walk and
        # a round-robin-from-i+1 walk BOTH pick slot 2; configuration priority
        # demands slot 0.
        state = self._state(hc.STATE_DOWN, [hc.STATE_UP, hc.STATE_DOWN, hc.STATE_UP])
        state["nodes"]["fallbacks"][1]["fail_count"] = 3
        d = self._eval(state, "fallback", 1, False, [True, False, True])
        self.assertEqual((d.action, d.reason), ("switch-to-fallback", "fallback_down_next_fallback"))
        self.assertEqual(d.target_index, 0)
        self.assertEqual(d.target_label, "fb0")

    def test_e19_delisted_ignores_ensure_primary(self):
        # ensure-primary stays none-only: it neither selects a fallback nor
        # suppresses the delisted reasons.
        th = hc.Thresholds(fail_threshold=3, ok_threshold=3, cooldown=60.0,
                           restore_primary=True, ensure_primary=True)
        state = self._state(hc.STATE_UNKNOWN, [hc.STATE_UP] * 3)
        d = self._eval(state, "delisted", None, True, [True, True, True], th=th)
        self.assertEqual((d.action, d.reason), ("switch-to-fallback", "delisted_next_fallback"))
        state = self._state(hc.STATE_DOWN, [hc.STATE_DOWN] * 3)
        d = self._eval(state, "delisted", None, False, [False, False, False], th=th)
        self.assertEqual((d.action, d.reason), ("none", "delisted_no_target"))

    def test_e20_one_unknown_slot_blocks_all_down(self):
        # `all_down` fires ONLY when primary AND every bench slot are DOWN.
        state = self._state(hc.STATE_DOWN, [hc.STATE_DOWN, hc.STATE_UNKNOWN, hc.STATE_DOWN])
        state["nodes"]["fallbacks"][0]["fail_count"] = 3
        d = self._eval(state, "fallback", 0, False, [False, False, False])
        self.assertEqual((d.action, d.reason), ("none", "no_fallback_verified"))

    def test_e21_unresolved_slot_excluded_despite_passing_ping(self):
        # An unresolved candidate cannot be selected even when its ping passes:
        # identity unverifiable => unswitchable. The next candidate wins.
        state = self._state(hc.STATE_DOWN, [hc.STATE_UP] * 3)
        d = self._eval(state, "primary", None, False, [True, True, True], unresolved=frozenset({0}))
        self.assertEqual(d.target_index, 1)
        # ...in the f2f walk too.
        state = self._state(hc.STATE_DOWN, [hc.STATE_DOWN, hc.STATE_UP, hc.STATE_UP])
        state["nodes"]["fallbacks"][0]["fail_count"] = 3
        d = self._eval(state, "fallback", 0, False, [False, True, True], unresolved=frozenset({1}))
        self.assertEqual(d.target_index, 2)

    def test_e22_walk_excludes_the_active_slot_itself(self):
        # Active slot 0 is DOWN-state but ping-reachable this round: the walk
        # must not re-select it (a no-op switch readback would accept).
        state = self._state(hc.STATE_DOWN, [hc.STATE_DOWN, hc.STATE_UP, hc.STATE_UP])
        state["nodes"]["fallbacks"][0]["fail_count"] = 3
        d = self._eval(state, "fallback", 0, False, [True, True, True])
        self.assertEqual((d.action, d.reason), ("switch-to-fallback", "fallback_down_next_fallback"))
        self.assertEqual(d.target_index, 1)

    def test_decision_reports_fallback_states_and_scalar_slot0(self):
        # The legacy scalar pins slot 0 even when the ACTIVE fallback is slot 1.
        state = self._state(hc.STATE_DOWN, [hc.STATE_UP, hc.STATE_DOWN, hc.STATE_UNKNOWN])
        state["nodes"]["fallbacks"][1]["fail_count"] = 3
        d = self._eval(state, "fallback", 1, False, [True, False, True])
        self.assertEqual(d.fallback_state, hc.STATE_UP)  # slot0, not the active slot's DOWN
        self.assertEqual(d.fallback_states, [hc.STATE_UP, hc.STATE_DOWN, hc.STATE_UNKNOWN])
        self.assertEqual(d.to_dict()["fallback_state"], d.to_dict()["fallback_states"][0])

    def test_single_element_delisted_rows_still_fire(self):
        # The config-edit carve-out applies to single-element lists too.
        state = hc.default_state("primary-vps", ["only-fb"])
        state["nodes"]["primary"]["last_state"] = hc.STATE_UP
        d = hc.evaluate(state, "delisted", None, probe("p", True), [probe("only-fb", True)],
                        self.th, 10_000.0)
        self.assertEqual((d.action, d.reason), ("switch-to-primary", "delisted_restore_primary"))


class HysteresisUnitTests(unittest.TestCase):
    def test_ok_count_recovers_to_up(self):
        th = hc.Thresholds(ok_threshold=2)
        node = hc.default_node_state("x")
        node["last_state"] = hc.STATE_DOWN
        hc._apply_hysteresis(node, True, th)
        self.assertEqual(node["last_state"], hc.STATE_DOWN)  # 1 ok, not enough
        hc._apply_hysteresis(node, True, th)
        self.assertEqual(node["last_state"], hc.STATE_UP)  # 2 oks -> UP
        self.assertEqual(node["fail_count"], 0)


class ParseTests(unittest.TestCase):
    def test_parse_rtt(self):
        self.assertEqual(hc.parse_rtt("pong from x (100.64.0.1) in 23.4 ms"), 23.4)
        self.assertIsNone(hc.parse_rtt("no timing here"))

    def test_extract_ip_from_json(self):
        self.assertEqual(hc.extract_ip('{"ip": "1.2.3.4"}'), "1.2.3.4")

    def test_extract_ip_from_text(self):
        self.assertEqual(hc.extract_ip("your address is 9.8.7.6 today"), "9.8.7.6")

    def test_extract_ip_none(self):
        self.assertIsNone(hc.extract_ip("no address"))


class IdentityTests(unittest.TestCase):
    def test_resolve_by_hostname(self):
        node_id, ips = hc.resolve_identity(SAMPLE_STATUS, "primary-vps")
        self.assertEqual(node_id, "nodeP")
        self.assertEqual(ips, ["100.64.0.1"])

    def test_resolve_by_magicdns(self):
        node_id, _ = hc.resolve_identity(SAMPLE_STATUS, "fallback-vps.example.ts.net")
        self.assertEqual(node_id, "nodeF")

    def test_resolve_by_ip(self):
        node_id, _ = hc.resolve_identity(SAMPLE_STATUS, "100.64.0.2")
        self.assertEqual(node_id, "nodeF")

    def test_resolve_ip_label_is_canonicalized(self):
        # GPT-5.6-sol r6 F1: an IP-valued label is canonicalized like the peer IPs, so an
        # expanded or uppercased IPv6 label still resolves (pre-fix: (None, []) because
        # the label kept its spelling while the peer IP was canonicalized).
        status = {"Peer": {"p": {"ID": "nodeP", "HostName": "p", "TailscaleIPs": ["fd7a:115c:a1e0::9"]}}}
        for label in ("fd7a:115c:a1e0::9", "fd7a:115c:a1e0:0:0:0:0:9", "FD7A:115C:A1E0::9"):
            with self.subTest(label=label):
                self.assertEqual(hc.resolve_identity(status, label), ("nodeP", ["fd7a:115c:a1e0::9"]))
        # a native IPv4 label and a v6-mapped peer IP are DIFFERENT addresses -> no match
        v4 = {"Peer": {"p": {"ID": "nodeP", "HostName": "p", "TailscaleIPs": ["100.64.0.1"]}}}
        self.assertEqual(hc.resolve_identity(v4, "::ffff:100.64.0.1"), (None, []))

    def test_resolve_not_found(self):
        self.assertEqual(hc.resolve_identity(SAMPLE_STATUS, "ghost"), (None, []))

    def test_resolve_ambiguous_hostname_is_unresolved(self):
        status = {"Peer": {
            "a": {"ID": "idA", "HostName": "dup", "TailscaleIPs": ["100.64.0.10"]},
            "b": {"ID": "idB", "HostName": "dup", "TailscaleIPs": ["100.64.0.11"]},
        }}
        self.assertEqual(hc.resolve_identity(status, "dup"), (None, []))

    def test_resolve_ambiguous_still_resolves_by_ip(self):
        status = {"Peer": {
            "a": {"ID": "idA", "HostName": "dup", "TailscaleIPs": ["100.64.0.10"]},
            "b": {"ID": "idB", "HostName": "dup", "TailscaleIPs": ["100.64.0.11"]},
        }}
        node_id, _ = hc.resolve_identity(status, "100.64.0.11")
        self.assertEqual(node_id, "idB")

    def test_resolve_by_node_id(self):
        status = {"Peer": {"k": {"ID": "nABC123", "HostName": "primary-vps", "TailscaleIPs": ["100.64.0.1"]}}}
        node_id, ips = hc.resolve_identity(status, "nABC123")
        self.assertEqual(node_id, "nABC123")
        self.assertEqual(ips, ["100.64.0.1"])

    def test_live_active_role_primary(self):
        role = live_role(SAMPLE_STATUS, "nodeP", ["100.64.0.1"], "nodeF", ["100.64.0.2"])
        self.assertEqual(role, "primary")

    def test_live_active_role_none(self):
        status = dict(SAMPLE_STATUS, ExitNodeStatus=None)
        role = live_role(status, "nodeP", ["100.64.0.1"], "nodeF", ["100.64.0.2"])
        self.assertEqual(role, "none")

    def test_live_active_role_unknown(self):
        status = dict(SAMPLE_STATUS, ExitNodeStatus={"ID": "someoneelse", "TailscaleIPs": ["100.99.0.9/32"]})
        role = live_role(status, "nodeP", ["100.64.0.1"], "nodeF", ["100.64.0.2"])
        self.assertEqual(role, "unknown")

    def test_live_active_role_matches_by_ip_only(self):
        status = dict(SAMPLE_STATUS, ExitNodeStatus={"TailscaleIPs": ["100.64.0.2/32"]})
        role = live_role(status, "nodeP", ["100.64.0.1"], "nodeF", ["100.64.0.2"])
        self.assertEqual(role, "fallback")

    def test_live_active_role_survives_malformed_exit_ips(self):
        # A malformed ExitNodeStatus.TailscaleIPs must fail closed on this GATING path:
        # never a TypeError (scalar) and never iterated as dict KEYS into a false role
        # attribution -- e.g. {"100.64.0.1": true} must NOT be read as owning 100.64.0.1.
        # Validation is WHOLE-FIELD fail-closed: a list mixing a real IP with any junk
        # element (e.g. ["100.64.0.1", 5]) is voided entirely, not partially kept, so it
        # cannot contribute even the valid address to a match.
        for bad in (7, "100.64.0.1", {"100.64.0.1": True}, ["100.64.0.1", 5]):
            with self.subTest(bad=bad):
                status = dict(SAMPLE_STATUS, ExitNodeStatus={"ID": "nodeX", "TailscaleIPs": bad})
                role = live_role(status, "nodeP", ["100.64.0.1"], "nodeF", ["100.64.0.2"])
                self.assertEqual(role, "unknown")

    def test_live_active_role_invalid_prefix_ip_no_false_match(self):
        # An IP string whose stripped head looks valid (`100.64.0.1/not-a-prefix`)
        # must be rejected wholesale, not `_norm_ip`-stripped into a false address
        # match on this gating path (GPT-5.6-sol Blocker 1b; pre-fix returned "primary").
        status = dict(SAMPLE_STATUS, ExitNodeStatus={"TailscaleIPs": ["100.64.0.1/not-a-prefix"]})
        role = live_role(status, "nodeP", ["100.64.0.1"], "nodeF", ["100.64.0.2"])
        self.assertEqual(role, "unknown")

    def test_live_active_role_malformed_status_is_unknown_not_none(self):
        # "none" is ACTIONABLE -- it authorizes switch-to-primary under --ensure-primary
        # -- so a present-but-malformed ExitNodeStatus (scalar / list / empty dict) must
        # fail closed to "unknown", never "none" (GPT-5.6-sol Blocker 1a). Absent/null
        # legitimately stays "none" (see test_live_active_role_none).
        for bad in (7, "exit", ["100.64.0.1"], {}):
            with self.subTest(bad=bad):
                status = dict(SAMPLE_STATUS, ExitNodeStatus=bad)
                role = live_role(status, "nodeP", ["100.64.0.1"], "nodeF", ["100.64.0.2"])
                self.assertEqual(role, "unknown")

    def test_live_active_role_no_false_match_from_malformed_candidate_ip(self):
        # GPT-5.6-sol Blocker A: a malformed PRIMARY peer IP must not normalize into
        # primary_ips and forge a match against a valid, different exit node. The primary
        # peer resolves by NAME only (no IP identity); the live exit node is nodeX.
        status = {"Peer": {"p": {"ID": "nodeP", "HostName": "primary-vps",
                                 "TailscaleIPs": ["100.64.0.1/not-a-prefix"]}},
                  "ExitNodeStatus": {"ID": "nodeX", "TailscaleIPs": ["100.64.0.1"]}}
        pid, pips = hc.resolve_identity(status, "primary-vps")
        self.assertEqual((pid, pips), ("nodeP", []))    # resolves by name; contributes NO ip identity
        role = live_role(status, pid, pips, "nodeF", ["100.64.0.2"])
        self.assertEqual(role, "unknown")               # pre-fix: "primary" (100.64.0.1 stripped in)

    def test_live_active_role_rejects_dotted_netmask_ip(self):
        # GPT-5.6-sol Blocker B: ipaddress accepts addr/dotted-mask, which Tailscale
        # never emits; it must not forge an address match on this gating path.
        status = dict(SAMPLE_STATUS, ExitNodeStatus={"TailscaleIPs": ["100.64.0.1/255.255.255.255"]})
        role = live_role(status, "nodeP", ["100.64.0.1"], "nodeF", ["100.64.0.2"])
        self.assertEqual(role, "unknown")               # pre-fix: "primary"

    def test_live_active_role_rejects_noncanonical_host_prefix(self):
        # GPT-5.6-sol r5 Blocker: a zero-padded (`/032`) or non-host (`/24`) suffix on a
        # host-identity token is not a real Tailscale form and must not forge a match.
        for bad_ip in ("100.64.0.1/032", "100.64.0.1/24"):
            with self.subTest(bad_ip=bad_ip):
                status = dict(SAMPLE_STATUS, ExitNodeStatus={"ID": "other", "TailscaleIPs": [bad_ip]})
                role = live_role(status, "nodeP", ["100.64.0.1"], "nodeF", ["100.64.0.2"])
                self.assertEqual(role, "unknown")       # pre-fix: "primary"

    def test_live_active_role_matches_equivalent_ipv6_spelling(self):
        # GPT-5.6-sol r5: identity keys are CANONICAL, so an expanded IPv6 exit address
        # still matches a peer configured with the compressed form (spelling-agnostic).
        status = dict(SAMPLE_STATUS,
                      ExitNodeStatus={"ID": "nodeQ", "TailscaleIPs": ["fd7a:115c:a1e0:0:0:0:0:9"]})
        role = live_role(status, "nodeP", ["100.64.0.1"], "nodeQ", ["fd7a:115c:a1e0::9"])
        self.assertEqual(role, "fallback")             # canonical match despite differing spelling


class StateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(self._cleanup)

    def _cleanup(self):
        for child in sorted(self.tmp.glob("*")):
            child.unlink()
        self.tmp.rmdir()

    def test_corrupt_state_falls_back_to_default(self):
        path = self.tmp / "failover-state.json"
        path.write_text("{not json", encoding="utf-8")
        state = hc.load_state(path, "p", ["f"])
        self.assertEqual(state["schema_version"], hc.STATE_SCHEMA_VERSION)
        self.assertEqual(state["nodes"]["primary"]["configured_label"], "p")

    def test_same_label_preserves_counters(self):
        path = self.tmp / "failover-state.json"
        state = hc.default_state("p", ["f"])
        state["nodes"]["primary"]["fail_count"] = 2
        state["nodes"]["primary"]["last_state"] = hc.STATE_DOWN
        hc.save_state(path, state)
        reloaded = hc.load_state(path, "p", ["f"])
        self.assertEqual(reloaded["nodes"]["primary"]["fail_count"], 2)
        self.assertEqual(reloaded["nodes"]["primary"]["last_state"], hc.STATE_DOWN)

    def test_changed_label_resets_state(self):
        path = self.tmp / "failover-state.json"
        state = hc.default_state("old-primary", ["f"])
        state["nodes"]["primary"]["fail_count"] = 2
        state["nodes"]["primary"]["last_state"] = hc.STATE_DOWN
        hc.save_state(path, state)
        reloaded = hc.load_state(path, "new-primary", ["f"])
        self.assertEqual(reloaded["nodes"]["primary"]["configured_label"], "new-primary")
        self.assertEqual(reloaded["nodes"]["primary"]["fail_count"], 0)
        self.assertEqual(reloaded["nodes"]["primary"]["last_state"], hc.STATE_UNKNOWN)

    def test_respelled_ip_label_preserves_state(self):
        # GPT-5.6-sol r7: an IP-valued configured label is compared by canonical address in
        # normalize_state, so re-spelling the SAME IP (expanded/uppercase IPv6 persisted by
        # an older run vs the compressed form now) must NOT reset health history and suppress
        # a due failover. A native IPv4 vs its v6-mapped form remain distinct (still resets).
        path = self.tmp / "failover-state.json"
        state = hc.default_state("FD7A:115C:A1E0:0:0:0:0:9", ["f"])
        state["nodes"]["primary"]["fail_count"] = 2
        state["nodes"]["primary"]["last_state"] = hc.STATE_DOWN
        hc.save_state(path, state)
        reloaded = hc.load_state(path, "fd7a:115c:a1e0::9", ["f"])
        self.assertEqual(reloaded["nodes"]["primary"]["fail_count"], 2)          # preserved
        self.assertEqual(reloaded["nodes"]["primary"]["last_state"], hc.STATE_DOWN)
        # a different address (native IPv4 vs v6-mapped) is still treated as a new node
        state2 = hc.default_state("100.64.0.1", ["f"])
        state2["nodes"]["primary"]["fail_count"] = 2
        hc.save_state(path, state2)
        reloaded2 = hc.load_state(path, "::ffff:100.64.0.1", ["f"])
        self.assertEqual(reloaded2["nodes"]["primary"]["fail_count"], 0)         # reset

    def test_corrupt_field_types_are_discarded(self):
        path = self.tmp / "failover-state.json"
        bad = hc.default_state("p", ["f"])
        bad["nodes"]["primary"]["fail_count"] = "x"
        bad["nodes"]["primary"]["last_state"] = "weird"
        bad["active"]["last_switch_epoch"] = "bad"
        path.write_text(json.dumps(bad), encoding="utf-8")
        reloaded = hc.load_state(path, "p", ["f"])
        self.assertEqual(reloaded["nodes"]["primary"]["fail_count"], 0)
        self.assertEqual(reloaded["nodes"]["primary"]["last_state"], hc.STATE_UNKNOWN)
        self.assertEqual(reloaded["active"]["last_switch_epoch"], 0.0)

    def test_state_lock_is_reentrant_across_calls(self):
        path = self.tmp / "failover-state.json"
        with hc.state_lock(path):
            hc.save_state(path, hc.default_state("p", ["f"]))
        with hc.state_lock(path):
            self.assertTrue(path.exists())
        self.assertTrue((self.tmp / "failover-state.lock").exists())


class V130GoldenEquivalenceTests(unittest.TestCase):
    """G1 — the single-element compatibility pin, executably.

    tests/fixtures/v130_golden.json was recorded from the REAL v1.3.0 evaluator
    (tests/fixtures/record_v130_golden.py extracts it from the git tag; the
    committed JSON is the fixture so CI needs no tags). Every step's COMPLETE
    eight-key legacy decision dict AND the post-step hysteresis counters must
    be equal under the new evaluator with a one-slot bench; the additive keys
    are asserted for consistency rather than excluded from scrutiny."""

    FIXTURE = Path(__file__).resolve().parent / "fixtures" / "v130_golden.json"
    LEGACY_KEYS = ("action", "reason", "active_role", "primary_state", "fallback_state",
                   "target_role", "target_label", "event")

    def test_g1_every_recorded_step_equivalent(self):
        data = json.loads(self.FIXTURE.read_text(encoding="utf-8"))
        primary, fallback = data["primary_label"], data["fallback_label"]
        self.assertEqual(data["recorded_from"], "v1.3.0")
        self.assertGreaterEqual(sum(len(s["steps"]) for s in data["scenarios"]), 30)
        for scen in data["scenarios"]:
            state = hc.default_state(primary, [fallback])
            init = scen["initial"]
            if "p_state" in init:
                state["nodes"]["primary"]["last_state"] = init["p_state"]
            if "f_state" in init:
                state["nodes"]["fallbacks"][0]["last_state"] = init["f_state"]
            if "p_fail" in init:
                state["nodes"]["primary"]["fail_count"] = init["p_fail"]
            if "f_fail" in init:
                state["nodes"]["fallbacks"][0]["fail_count"] = init["f_fail"]
            if "last_switch" in init:
                state["active"]["last_switch_epoch"] = init["last_switch"]
            th = hc.Thresholds(**scen["thresholds"])
            for step_number, step in enumerate(scen["steps"]):
                inp = step["input"]
                if "set_last_switch" in inp:
                    state["active"]["last_switch_epoch"] = inp["set_last_switch"]
                decision = ev1(state, inp["active_role"], probe(primary, inp["p_reach"]),
                               probe(fallback, inp["f_reach"]), th, inp["now"])
                got = decision.to_dict()
                with self.subTest(scenario=scen["name"], step=step_number):
                    for key in self.LEGACY_KEYS:
                        self.assertEqual(got[key], step["decision"][key],
                                         f"{key} diverged from the v1.3.0 recording")
                    for node_key, snap_key in (("primary", "post_primary"),):
                        for field_name, expected in step[snap_key].items():
                            self.assertEqual(state["nodes"][node_key][field_name], expected)
                    for field_name, expected in step["post_fallback"].items():
                        self.assertEqual(state["nodes"]["fallbacks"][0][field_name], expected)
                    # Additive keys stay consistent with the legacy ones.
                    self.assertEqual(got["fallback_states"], [got["fallback_state"]])
                    if got["target_role"] == "fallback":
                        self.assertEqual(got["target_index"], 0)
                    else:
                        self.assertIsNone(got["target_index"])


class FallbackListValidationTests(unittest.TestCase):
    def test_parse_splits_and_trims(self):
        self.assertEqual(hc.parse_fallback_list("a, b ,c"), ["a", "b", "c"])
        self.assertEqual(hc.parse_fallback_list("solo"), ["solo"])
        self.assertEqual(hc.parse_fallback_list(""), [""])

    def test_v1_empty_entries_refused(self):
        for raw in ("", "a,,b", " , ", "a,"):
            with self.subTest(raw=raw):
                error = hc.validate_candidate_labels("p", hc.parse_fallback_list(raw))
                self.assertIsNotNone(error)
                self.assertIn("empty", error)

    def test_v2_duplicates_refused_exact_and_canonical(self):
        self.assertIn("duplicate", hc.validate_candidate_labels("p", ["a", "b", "a"]) or "")
        # Canonical-IPv6: two spellings of ONE node refuse at config validation.
        error = hc.validate_candidate_labels("p", ["fd7a:115c:a1e0::9", "fd7a:115c:a1e0:0:0:0:0:9"])
        self.assertIsNotNone(error)
        self.assertIn("duplicate", error)

    def test_v3_fallback_equal_to_primary_refused(self):
        self.assertIn("PRIMARY", hc.validate_candidate_labels("node-a", ["node-a"]) or "")
        error = hc.validate_candidate_labels("fd7a:115c:a1e0::9", ["FD7A:115C:A1E0:0:0:0:0:9"])
        self.assertIsNotNone(error)

    def test_v4_case_different_hostnames_are_distinct(self):
        # Hostname/MagicDNS equivalence is exact-text, NOT case-insensitive.
        self.assertIsNone(hc.validate_candidate_labels("p", ["Node-B", "node-b"]))
        self.assertIsNone(hc.validate_candidate_labels("Node-A", ["node-a"]))

    def test_valid_lists_accepted(self):
        self.assertIsNone(hc.validate_candidate_labels("p", ["a"]))
        self.assertIsNone(hc.validate_candidate_labels("p", ["a", "b", "c"]))

    def test_v5_refusal_happens_before_any_probe_or_state_io(self):
        # Configuration refusal exits 2 with NO ping run and NO state file created.
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: [p.unlink() for p in tmp.glob("*")] and None or tmp.rmdir())
        calls = tmp / "calls.log"
        fake = tmp / "tailscale"
        fake.write_text(f"#!/usr/bin/env bash\necho \"$@\" >> {calls}\nexit 0\n", encoding="utf-8")
        fake.chmod(0o755)
        old = os.environ.get("TAILSCALE_BIN")
        os.environ["TAILSCALE_BIN"] = str(fake)
        self.addCleanup(lambda: os.environ.__setitem__("TAILSCALE_BIN", old) if old else os.environ.pop("TAILSCALE_BIN", None))
        state_file = tmp / "state.json"
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            rc, _out = run_cli([
                "verdict", "--state-file", str(state_file),
                "--primary", "p", "--fallback", "a,,b",
                "--fail-threshold", "1", "--ok-threshold", "1", "--cooldown", "0",
            ])
        self.assertEqual(rc, 2)
        self.assertIn("empty", buf.getvalue())
        self.assertFalse(calls.exists(), "no tailscale invocation may happen on a config refusal")
        self.assertFalse(state_file.exists(), "no state may be written on a config refusal")
        self.assertFalse((tmp / "state.lock").exists(), "no lock may be taken on a config refusal")


class MultiStateMigrationTests(unittest.TestCase):
    """State schema 2: v1 read-compat, positional history, index re-derivation,
    delisted retention, and the label-spelling invariant (S1-S9)."""

    LABELS = ["fb0", "fb1", "fb2"]

    def _v1_state(self, fallback_label="fb0", active_role="fallback"):
        return {
            "schema_version": 1,
            "active": {
                "role": active_role,
                "configured_label": fallback_label if active_role == "fallback" else None,
                "node_id": "nodeF",
                "tailscale_ips": ["100.64.0.2"],
                "last_switch_epoch": 111.0,
                "last_switch_at": "2026-01-01T00:00:00Z",
            },
            "nodes": {
                "primary": {"configured_label": "p", "node_id": "nodeP", "tailscale_ips": ["100.64.0.1"],
                            "last_state": hc.STATE_UP, "fail_count": 0, "ok_count": 3,
                            "last_checked_at": "2026-01-01T00:00:00Z"},
                "fallback": {"configured_label": fallback_label, "node_id": "nodeF", "tailscale_ips": ["100.64.0.2"],
                             "last_state": hc.STATE_DOWN, "fail_count": 5, "ok_count": 0,
                             "last_checked_at": "2026-01-01T00:00:00Z"},
            },
        }

    def test_s1_v1_read_compat_seeds_slot0_and_index(self):
        state = hc.normalize_state(self._v1_state(), "p", ["fb0", "fb1"])
        self.assertEqual(state["schema_version"], 2)
        self.assertEqual(state["nodes"]["fallbacks"][0]["fail_count"], 5)  # history kept
        self.assertEqual(state["nodes"]["fallbacks"][0]["last_state"], hc.STATE_DOWN)
        self.assertEqual(state["nodes"]["fallbacks"][1]["last_state"], hc.STATE_UNKNOWN)  # fresh
        self.assertEqual(state["active"]["fallback_index"], 0)  # v1 fallback -> slot 0
        self.assertEqual(state["active"]["last_switch_epoch"], 111.0)  # cooldown clock kept

    def test_s2_v1_label_mismatch_resets_history(self):
        state = hc.normalize_state(self._v1_state(fallback_label="other"), "p", ["fb0"])
        self.assertEqual(state["nodes"]["fallbacks"][0]["fail_count"], 0)
        self.assertEqual(state["nodes"]["fallbacks"][0]["last_state"], hc.STATE_UNKNOWN)
        # active label "other" matches no slot -> delisted retention (index null, label kept)
        self.assertIsNone(state["active"]["fallback_index"])
        self.assertEqual(state["active"]["configured_label"], "other")
        self.assertEqual(state["active"]["last_switch_epoch"], 111.0)

    def _v2_state(self):
        state = hc.default_state("p", list(self.LABELS))
        for i, slot in enumerate(state["nodes"]["fallbacks"]):
            slot["fail_count"] = i + 1
            slot["last_state"] = hc.STATE_DOWN
            slot["node_id"] = f"node{i}"
        state["active"].update({
            "role": "fallback", "configured_label": "fb1", "node_id": "node1",
            "tailscale_ips": ["100.64.0.11"], "fallback_index": 1,
            "last_switch_epoch": 222.0, "last_switch_at": "2026-02-02T00:00:00Z",
        })
        return json.loads(json.dumps(state))

    def test_s3_reorder_rebinds_active_index_and_resets_positional_history(self):
        stored = self._v2_state()
        # Reorder fb1 to the front: active label fb1 must REBIND to index 0;
        # per-slot history is positional, so every moved slot resets.
        state = hc.normalize_state(stored, "p", ["fb1", "fb0", "fb2"])
        self.assertEqual(state["active"]["fallback_index"], 0)
        self.assertEqual(state["active"]["configured_label"], "fb1")
        self.assertEqual(state["active"]["last_switch_epoch"], 222.0)  # clock survives the edit
        self.assertEqual(state["nodes"]["fallbacks"][0]["fail_count"], 0)  # fb1's slot: stored[0] was fb0 -> reset
        self.assertEqual(state["nodes"]["fallbacks"][2]["fail_count"], 3)  # fb2 stayed at slot 2 -> kept

    def test_s4_delisted_retains_label_identity_null_index(self):
        stored = self._v2_state()
        state = hc.normalize_state(stored, "p", ["fb0", "fb2"])  # fb1 delisted
        active = state["active"]
        self.assertEqual(active["role"], "fallback")
        self.assertEqual(active["configured_label"], "fb1")  # retained evidence
        self.assertEqual(active["node_id"], "node1")
        self.assertEqual(active["tailscale_ips"], ["100.64.0.11"])
        self.assertIsNone(active["fallback_index"])  # index nulled ONLY
        self.assertEqual(active["last_switch_epoch"], 222.0)  # cooldown survives

    def test_s5_unknown_schema_resets(self):
        # True == 1 and 2.0 == 2 in Python: the type-strict check must reset
        # those too, or a malformed file smuggles evidence past the reset.
        for version in (0, 3, "2", None, True, False, 1.0, 2.0):
            stored = self._v2_state()
            stored["schema_version"] = version
            state = hc.normalize_state(stored, "p", list(self.LABELS))
            self.assertEqual(state["active"]["role"], "unknown", f"version={version!r}")
            self.assertEqual(state["nodes"]["fallbacks"][0]["fail_count"], 0)
            self.assertEqual(state["active"]["last_switch_epoch"], 0.0)

    def test_s6_stored_index_never_trusted(self):
        stored = self._v2_state()
        stored["active"]["fallback_index"] = 999  # garbage on disk
        state = hc.normalize_state(stored, "p", list(self.LABELS))
        self.assertEqual(state["active"]["fallback_index"], 1)  # re-derived from the label
        stored["active"]["fallback_index"] = "one"
        state = hc.normalize_state(stored, "p", list(self.LABELS))
        self.assertEqual(state["active"]["fallback_index"], 1)
        # role != fallback -> index is always null whatever the file says
        stored2 = self._v2_state()
        stored2["active"]["role"] = "primary"
        stored2["active"]["fallback_index"] = 2
        state2 = hc.normalize_state(stored2, "p", list(self.LABELS))
        self.assertIsNone(state2["active"]["fallback_index"])

    def test_s7_v1_in_schema2_out(self):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: [p.unlink() for p in tmp.glob("*")] and None or tmp.rmdir())
        path = tmp / "state.json"
        path.write_text(json.dumps(self._v1_state()), encoding="utf-8")
        state = hc.load_state(path, "p", ["fb0"])
        hc.save_state(path, state)
        written = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(written["schema_version"], 2)
        self.assertIn("fallbacks", written["nodes"])
        self.assertNotIn("fallback", written["nodes"])
        self.assertEqual(written["nodes"]["fallbacks"][0]["fail_count"], 5)

    def test_s9_respell_keeps_history_and_refreshes_label(self):
        # Canonical-equivalent respell of a slot label: history is KEPT, but the
        # normalized record's configured_label — hence any later target_label —
        # is the NEW this-cycle spelling, never the stored one (the exact-text
        # cross-check soundness pin; mutant M18 copies the stored label instead).
        expanded, compressed = "fd7a:115c:a1e0:0:0:0:0:9", "fd7a:115c:a1e0::9"
        stored = hc.default_state("p", [expanded, "fb1"])
        stored["nodes"]["fallbacks"][0]["fail_count"] = 2
        stored["nodes"]["fallbacks"][0]["last_state"] = hc.STATE_DOWN
        stored = json.loads(json.dumps(stored))
        state = hc.normalize_state(stored, "p", [compressed, "fb1"])
        self.assertEqual(state["nodes"]["fallbacks"][0]["fail_count"], 2)  # history kept
        self.assertEqual(state["nodes"]["fallbacks"][0]["configured_label"], compressed)  # NEW spelling
        # ...and the evaluator's target_label carries the current spelling.
        state["nodes"]["primary"]["last_state"] = hc.STATE_DOWN
        d = hc.evaluate(state, "primary", None, probe("p", False),
                        [probe(compressed, True), probe("fb1", True)],
                        hc.Thresholds(cooldown=0.0), 10_000.0)
        self.assertEqual(d.target_label, compressed)


class DeriveActiveTests(unittest.TestCase):
    """derive_active: live matching, index derivation, delisted overlay with
    the coherence gate, unresolved-active fail-closed (L-series pure part)."""

    LABELS = ["fb0", "fb1", "fb2"]

    def _status(self, exit_id=None, exit_ips=None, exit_absent=False):
        status = {"BackendState": "Running", "Self": {"ID": "self"}}
        if not exit_absent:
            entry = {}
            if exit_id is not None:
                entry["ID"] = exit_id
            if exit_ips is not None:
                entry["TailscaleIPs"] = exit_ips
            status["ExitNodeStatus"] = entry or {"ID": exit_id}
        return status

    def _identities(self):
        primary = ("nodeP", ["100.64.0.1"])
        fallbacks = [("node0", ["100.64.0.10"]), ("node1", ["100.64.0.11"]), ("node2", ["100.64.0.12"])]
        return primary, fallbacks

    def _record(self, role="fallback", label="gone-fb", node_id="nodeGone", ips=None):
        return {
            "role": role, "configured_label": label, "node_id": node_id,
            "tailscale_ips": ips if ips is not None else ["100.64.0.99"],
        }

    def test_matches_fallback_slot_with_index(self):
        primary, fallbacks = self._identities()
        role, index, problem = hc.derive_active(
            self._status(exit_id="node2"), primary, fallbacks, "p", self.LABELS, None)
        self.assertEqual((role, index, problem), ("fallback", 2, None))

    def test_matches_primary_and_none(self):
        primary, fallbacks = self._identities()
        role, index, problem = hc.derive_active(
            self._status(exit_id="nodeP"), primary, fallbacks, "p", self.LABELS, None)
        self.assertEqual((role, index, problem), ("primary", None, None))
        status = {"BackendState": "Running", "ExitNodeStatus": None}
        self.assertEqual(hc.derive_active(status, primary, fallbacks, "p", self.LABELS, None),
                         ("none", None, None))

    def test_delisted_from_coherent_record(self):
        primary, fallbacks = self._identities()
        record = self._record()
        role, index, problem = hc.derive_active(
            self._status(exit_id="nodeGone"), primary, fallbacks, "p", self.LABELS, record)
        self.assertEqual((role, index, problem), ("delisted", None, None))
        # IP-set match works too (ID missing from the record).
        record = self._record(node_id=None, ips=["100.64.0.99"])
        role, _i, _p = hc.derive_active(
            self._status(exit_ips=["100.64.0.99/32"]), primary, fallbacks, "p", self.LABELS, record)
        self.assertEqual(role, "delisted")

    def test_l13_primary_role_record_can_be_delisted(self):
        # A PRIMARY-label swap while live sits on the old primary node: the
        # retained record (role=primary) is coherent evidence; preserve-as-is
        # semantics (D4) — no forced role rewrite.
        primary, fallbacks = self._identities()
        record = self._record(role="primary", label="old-primary", node_id="nodeOldP", ips=["100.64.0.98"])
        role, index, problem = hc.derive_active(
            self._status(exit_id="nodeOldP"), primary, fallbacks, "p", self.LABELS, record)
        self.assertEqual((role, index, problem), ("delisted", None, None))

    def test_l14_incoherent_records_are_not_delisted_evidence(self):
        # GPT finding 2: a type-valid state with role unknown/none (or missing
        # label/identity) must NOT turn a foreign node into "ours".
        primary, fallbacks = self._identities()
        for record in (
            self._record(role="unknown"),
            self._record(role="none"),
            self._record(label=None),
            self._record(label=""),
            self._record(node_id=None, ips=[]),
            None,
        ):
            with self.subTest(record=record):
                role, index, problem = hc.derive_active(
                    self._status(exit_id="nodeGone"), primary, fallbacks, "p", self.LABELS, record)
                self.assertEqual((role, index, problem), ("unknown", None, None))

    def test_l6_unresolved_active_fails_closed(self):
        # The retained label is STILL configured but that candidate did not
        # resolve this round: the ACTIVE node is unverifiable -> problem.
        primary, fallbacks = self._identities()
        fallbacks[1] = (None, [])  # fb1 unresolved this round
        record = self._record(label="fb1", node_id="node1", ips=["100.64.0.11"])
        role, index, problem = hc.derive_active(
            self._status(exit_id="node1"), primary, fallbacks, "p", self.LABELS, record)
        self.assertEqual(problem, "live_status_incomplete")

    def test_l7_label_repointed_is_foreign(self):
        # fb1 now resolves to a DIFFERENT node while live sits on the old one:
        # the label was re-pointed; the old node is foreign -> unknown, no override.
        primary, fallbacks = self._identities()
        fallbacks[1] = ("nodeNEW", ["100.64.0.21"])
        record = self._record(label="fb1", node_id="node1", ips=["100.64.0.11"])
        role, index, problem = hc.derive_active(
            self._status(exit_id="node1"), primary, fallbacks, "p", self.LABELS, record)
        self.assertEqual((role, index, problem), ("unknown", None, None))

    def test_l5_foreign_node_never_overridden(self):
        primary, fallbacks = self._identities()
        role, index, problem = hc.derive_active(
            self._status(exit_id="totally-foreign"), primary, fallbacks, "p", self.LABELS,
            self._record())
        self.assertEqual((role, index, problem), ("unknown", None, None))

    def test_l17_unmatched_live_with_unresolved_candidate_fails_closed(self):
        # GPT diff finding 1: with ANY bench candidate unresolved, an unmatched
        # live exit node cannot be proven foreign — it may BE that candidate
        # (a fresh state file offers no claiming record). Fail closed instead
        # of degrading into unknown_active.
        primary, fallbacks = self._identities()
        fallbacks[1] = (None, [])  # fb1 unresolved this round
        for record in (None, self._record()):  # no record, and a non-matching one
            with self.subTest(record=record is not None and "stale" or "fresh"):
                role, index, problem = hc.derive_active(
                    self._status(exit_id="mystery-node"), primary, fallbacks, "p", self.LABELS, record)
                self.assertEqual(problem, "live_status_incomplete")
        # The REPOINTED path needs the same guard (GPT confirm-round Major):
        # live matches the retained record, its label now resolves to a
        # different node, AND another bench slot is unresolved — the live node
        # may BE that unresolved candidate, so this too fails closed.
        primary, fallbacks = self._identities()
        fallbacks[1] = ("nodeNEW", ["100.64.0.21"])  # fb1 re-pointed
        fallbacks[2] = (None, [])  # fb2 unresolved
        record = self._record(label="fb1", node_id="node1", ips=["100.64.0.11"])
        role, index, problem = hc.derive_active(
            self._status(exit_id="node1"), primary, fallbacks, "p", self.LABELS, record)
        self.assertEqual(problem, "live_status_incomplete")
        # With EVERY candidate resolved, the same unmatched node is provably
        # foreign: unknown_active as always.
        primary, fallbacks = self._identities()
        role, index, problem = hc.derive_active(
            self._status(exit_id="mystery-node"), primary, fallbacks, "p", self.LABELS, None)
        self.assertEqual((role, index, problem), ("unknown", None, None))

    def test_malformed_exit_status_fails_closed(self):
        primary, fallbacks = self._identities()
        for exit_value in ({}, [], "nodeP", {"TailscaleIPs": ["not-an-ip"]}):
            with self.subTest(exit_value=exit_value):
                status = {"BackendState": "Running", "ExitNodeStatus": exit_value}
                role, _i, _p = hc.derive_active(status, primary, fallbacks, "p", self.LABELS, None)
                self.assertEqual(role, "unknown")


class AllPairsDistinctTests(unittest.TestCase):
    def test_l10_same_id_disjoint_ips_refused(self):
        # ID half of the mechanism alone must catch the alias.
        self.assertFalse(hc._all_pairs_distinct([
            ("nodeA", ["100.64.0.1"]), ("nodeA", ["100.64.0.2"]),
        ]))

    def test_l10_different_id_shared_ip_refused(self):
        # IP half of the mechanism alone must catch the alias.
        self.assertFalse(hc._all_pairs_distinct([
            ("nodeA", ["100.64.0.1"]), ("nodeB", ["100.64.0.1"]),
        ]))

    def test_l10_position_variants(self):
        distinct = [("a", ["1.1.1.1"]), ("b", ["2.2.2.2"]), ("c", ["3.3.3.3"]), ("d", ["4.4.4.4"])]
        self.assertTrue(hc._all_pairs_distinct(distinct))
        primary_vs_slot2 = list(distinct)
        primary_vs_slot2[2] = ("a", ["9.9.9.9"])  # aliases the primary by ID
        self.assertFalse(hc._all_pairs_distinct(primary_vs_slot2))
        slot1_vs_slot3 = list(distinct)
        slot1_vs_slot3[3] = ("z", ["2.2.2.2"])  # aliases slot 1 by IP
        self.assertFalse(hc._all_pairs_distinct(slot1_vs_slot3))

    def test_unresolved_entries_pass_trivially(self):
        self.assertTrue(hc._all_pairs_distinct([("a", ["1.1.1.1"]), (None, []), (None, [])]))


class ProbeSubprocessTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(self._cleanup)
        self.fake = self._write_fake("tailscale", FAKE_TAILSCALE)
        self._set_env("TAILSCALE_BIN", str(self.fake))

    def _cleanup(self):
        for child in sorted(self.tmp.rglob("*"), reverse=True):
            if child.is_file():
                child.unlink()
            else:
                child.rmdir()
        self.tmp.rmdir()

    def _set_env(self, key, value):
        old = os.environ.get(key)
        os.environ[key] = value
        self.addCleanup(lambda: os.environ.__setitem__(key, old) if old is not None else os.environ.pop(key, None))

    def _write_fake(self, name, content):
        path = self.tmp / name
        path.write_text(content, encoding="utf-8")
        path.chmod(path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
        return path

    def test_ping_reachable(self):
        result = hc.tailscale_ping("primary-vps", timeout=5.0)
        self.assertTrue(result.reachable)
        self.assertEqual(result.rtt_ms, 12.0)

    def test_ping_unreachable(self):
        self._set_env("FAKE_UNREACHABLE", "primary-vps")
        result = hc.tailscale_ping("primary-vps", timeout=5.0)
        self.assertFalse(result.reachable)

    def test_ping_falls_back_when_dash_c_unsupported(self):
        fake = self._write_fake("tailscale_no_c", FAKE_TAILSCALE_NO_C)
        self._set_env("TAILSCALE_BIN", str(fake))
        result = hc.tailscale_ping("primary-vps", timeout=5.0)
        self.assertTrue(result.reachable)
        self.assertEqual(result.rtt_ms, 5.0)

    def test_ping_missing_binary(self):
        self._set_env("TAILSCALE_BIN", str(self.tmp / "does-not-exist"))
        result = hc.tailscale_ping("primary-vps", timeout=5.0)
        self.assertFalse(result.reachable)
        self.assertIn("not found", result.error or "")

    def test_probe_cli_json(self):
        rc, out = run_cli(["probe", "--node", "primary-vps", "--json"])
        self.assertEqual(rc, 0)
        payload = json.loads(out)
        self.assertTrue(payload["probe"]["reachable"])

    def test_probe_cli_text_unreachable_returns_1(self):
        self._set_env("FAKE_UNREACHABLE", "primary-vps")
        rc, out = run_cli(["probe", "--node", "primary-vps"])
        self.assertEqual(rc, 1)
        self.assertIn("reachable=0", out)


class VerdictCliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(self._cleanup)
        fake = self.tmp / "tailscale"
        fake.write_text(FAKE_TAILSCALE, encoding="utf-8")
        fake.chmod(fake.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
        self._set_env("TAILSCALE_BIN", str(fake))
        self.status_file = self.tmp / "status.json"
        self.status_file.write_text(json.dumps(SAMPLE_STATUS), encoding="utf-8")
        self.state_file = self.tmp / "failover-state.json"

    def _cleanup(self):
        for child in sorted(self.tmp.rglob("*"), reverse=True):
            if child.is_file():
                child.unlink()
            else:
                child.rmdir()
        self.tmp.rmdir()

    def _set_env(self, key, value):
        old = os.environ.get(key)
        os.environ[key] = value
        self.addCleanup(lambda: os.environ.__setitem__(key, old) if old is not None else os.environ.pop(key, None))

    def _verdict(self, extra=None):
        argv = [
            "verdict",
            "--state-file", str(self.state_file),
            "--primary", "primary-vps",
            "--fallback", "fallback-vps",
            "--status-json-file", str(self.status_file),
            "--fail-threshold", "1",
            "--ok-threshold", "1",
            "--cooldown", "0",
            "--json",
        ]
        return run_cli(argv + (extra or []))

    def test_verdict_healthy_primary(self):
        rc, out = self._verdict()
        self.assertEqual(rc, 0)
        decision = json.loads(out)["decision"]
        self.assertEqual(decision["action"], "none")
        self.assertEqual(decision["reason"], "healthy")
        # State persisted with reconciled active role.
        state = json.loads(self.state_file.read_text(encoding="utf-8"))
        self.assertEqual(state["active"]["role"], "primary")
        self.assertEqual(state["nodes"]["primary"]["node_id"], "nodeP")
        # Active canonical identity is refreshed on every reconcile (not only on switch).
        self.assertEqual(state["active"]["node_id"], "nodeP")

    def test_verdict_primary_down_proposes_fallback(self):
        self._set_env("FAKE_UNREACHABLE", "primary-vps")
        rc, out = self._verdict()
        decision = json.loads(out)["decision"]
        self.assertEqual(decision["action"], "switch-to-fallback")
        self.assertEqual(decision["target_label"], "fallback-vps")

    def test_verdict_text_output_is_key_value(self):
        rc, out = run_cli([
            "verdict",
            "--state-file", str(self.state_file),
            "--primary", "primary-vps",
            "--fallback", "fallback-vps",
            "--status-json-file", str(self.status_file),
            "--fail-threshold", "1", "--ok-threshold", "1", "--cooldown", "0",
        ])
        self.assertEqual(rc, 0)
        self.assertIn("action=none", out)
        self.assertIn("active_role=primary", out)

    def test_record_switch_sets_cooldown_clock(self):
        self._verdict()  # establish state
        rc, out = run_cli([
            "record-switch",
            "--state-file", str(self.state_file),
            "--primary", "primary-vps",
            "--fallback", "fallback-vps",
            "--role", "fallback",
        ])
        self.assertEqual(rc, 0)
        state = json.loads(self.state_file.read_text(encoding="utf-8"))
        self.assertEqual(state["active"]["role"], "fallback")
        self.assertGreater(state["active"]["last_switch_epoch"], 0.0)
        self.assertEqual(state["active"]["configured_label"], "fallback-vps")

    def test_verdict_fails_closed_on_unavailable_status(self):
        bad = self.tmp / "bad-status.json"
        bad.write_text("{not valid json", encoding="utf-8")
        rc, out = run_cli([
            "verdict",
            "--state-file", str(self.state_file),
            "--primary", "primary-vps", "--fallback", "fallback-vps",
            "--status-json-file", str(bad),
            "--fail-threshold", "1", "--ok-threshold", "1", "--cooldown", "0",
            "--ensure-primary", "--json",
        ])
        self.assertEqual(rc, 0)
        decision = json.loads(out)["decision"]
        self.assertEqual(decision["action"], "none")
        self.assertEqual(decision["reason"], "live_status_unavailable")
        self.assertEqual(decision["active_role"], "unknown")
        # Fail closed: no state file is written when live status is unavailable.
        self.assertFalse(self.state_file.exists())

    def test_verdict_incomplete_on_empty_status(self):
        # Backend is up but the status carries no usable Self yet (a real transient
        # right after start): that is incomplete, distinct from backend-not-running.
        empty = self.tmp / "empty.json"
        empty.write_text(json.dumps({"BackendState": "Running"}), encoding="utf-8")
        rc, out = run_cli([
            "verdict", "--state-file", str(self.state_file),
            "--primary", "primary-vps", "--fallback", "fallback-vps",
            "--status-json-file", str(empty),
            "--fail-threshold", "1", "--ok-threshold", "1", "--cooldown", "0",
            "--ensure-primary", "--json",
        ])
        self.assertEqual(rc, 0)
        decision = json.loads(out)["decision"]
        self.assertEqual(decision["action"], "none")
        self.assertEqual(decision["reason"], "live_status_incomplete")
        self.assertFalse(self.state_file.exists())

    def test_verdict_candidates_not_distinct(self):
        # fallback label is the primary node's own Tailscale IP -> same node.
        rc, out = run_cli([
            "verdict", "--state-file", str(self.state_file),
            "--primary", "primary-vps", "--fallback", "100.64.0.1",
            "--status-json-file", str(self.status_file),
            "--fail-threshold", "1", "--ok-threshold", "1", "--cooldown", "0", "--json",
        ])
        decision = json.loads(out)["decision"]
        self.assertEqual(decision["action"], "none")
        self.assertEqual(decision["reason"], "candidates_not_distinct")

    def test_verdict_resets_state_on_node_id_change(self):
        stale = hc.default_state("primary-vps", ["fallback-vps"])
        stale["nodes"]["primary"]["node_id"] = "OLD-ID"
        stale["nodes"]["primary"]["fail_count"] = 5
        stale["nodes"]["primary"]["last_state"] = hc.STATE_DOWN
        self.state_file.write_text(json.dumps(stale), encoding="utf-8")
        os.environ["FAKE_UNREACHABLE"] = "primary-vps"
        self.addCleanup(lambda: os.environ.pop("FAKE_UNREACHABLE", None))
        self._verdict()  # primary-vps now resolves to nodeP (a different node_id)
        state = json.loads(self.state_file.read_text(encoding="utf-8"))
        self.assertEqual(state["nodes"]["primary"]["node_id"], "nodeP")
        # Old fail_count=5 was reset to 0 before this round, so one fresh failure => 1.
        self.assertEqual(state["nodes"]["primary"]["fail_count"], 1)

    def test_verdict_resets_state_on_ip_change_without_id(self):
        noid = self.tmp / "noid.json"
        noid.write_text(json.dumps({
            "BackendState": "Running",
            "Self": {"ID": "self", "HostName": "client", "TailscaleIPs": ["100.64.0.5"]},
            "Peer": {
                "k1": {"HostName": "primary-vps", "TailscaleIPs": ["100.64.0.1"]},  # no ID
                "k2": {"HostName": "fallback-vps", "TailscaleIPs": ["100.64.0.2"]},
            },
            "ExitNodeStatus": None,
        }), encoding="utf-8")
        stale = hc.default_state("primary-vps", ["fallback-vps"])
        stale["nodes"]["primary"]["node_id"] = None
        stale["nodes"]["primary"]["tailscale_ips"] = ["100.64.0.99"]  # old IP, now gone
        stale["nodes"]["primary"]["fail_count"] = 5
        stale["nodes"]["primary"]["last_state"] = hc.STATE_DOWN
        self.state_file.write_text(json.dumps(stale), encoding="utf-8")
        os.environ["FAKE_UNREACHABLE"] = "primary-vps"
        self.addCleanup(lambda: os.environ.pop("FAKE_UNREACHABLE", None))
        run_cli([
            "verdict", "--state-file", str(self.state_file),
            "--primary", "primary-vps", "--fallback", "fallback-vps",
            "--status-json-file", str(noid),
            "--fail-threshold", "1", "--ok-threshold", "1", "--cooldown", "0",
        ])
        state = json.loads(self.state_file.read_text(encoding="utf-8"))
        self.assertEqual(state["nodes"]["primary"]["tailscale_ips"], ["100.64.0.1"])
        self.assertEqual(state["nodes"]["primary"]["fail_count"], 1)  # reset to 0 then one fresh fail

    def test_verdict_preserves_history_across_ipv6_spelling_upgrade(self):
        # GPT-5.6-sol r6 F2: an older build persisted tailscale_ips as raw text (expanded
        # IPv6); this build canonicalizes resolved IPs. The SAME node in expanded vs
        # compressed spelling must NOT look like a node change -- otherwise health history
        # resets and a due failover is suppressed across the upgrade. Here the persisted
        # primary is DOWN-trending (fail_count 2, threshold 3); preserving history lets one
        # more failure trip the switch, whereas a spurious reset would restart the count.
        noid = self.tmp / "noid6.json"
        noid.write_text(json.dumps({
            "BackendState": "Running",
            "Self": {"ID": "self", "HostName": "client", "TailscaleIPs": ["100.64.0.5"]},
            "Peer": {
                "k1": {"HostName": "primary-vps", "TailscaleIPs": ["fd7a:115c:a1e0::9"]},  # no ID, compressed
                "k2": {"HostName": "fallback-vps", "TailscaleIPs": ["100.64.0.2"]},
            },
            "ExitNodeStatus": None,
        }), encoding="utf-8")
        stale = hc.default_state("primary-vps", ["fallback-vps"])
        stale["nodes"]["primary"]["node_id"] = None
        stale["nodes"]["primary"]["tailscale_ips"] = ["fd7a:115c:a1e0:0:0:0:0:9"]  # SAME node, expanded (old build)
        stale["nodes"]["primary"]["fail_count"] = 2
        stale["nodes"]["primary"]["last_state"] = hc.STATE_DOWN
        self.state_file.write_text(json.dumps(stale), encoding="utf-8")
        os.environ["FAKE_UNREACHABLE"] = "primary-vps"
        self.addCleanup(lambda: os.environ.pop("FAKE_UNREACHABLE", None))
        run_cli([
            "verdict", "--state-file", str(self.state_file),
            "--primary", "primary-vps", "--fallback", "fallback-vps",
            "--status-json-file", str(noid),
            "--fail-threshold", "3", "--ok-threshold", "1", "--cooldown", "0",
        ])
        state = json.loads(self.state_file.read_text(encoding="utf-8"))
        # history PRESERVED (not reset to 0): 2 -> 3 reaches the threshold. Pre-fix the
        # expanded-vs-compressed mismatch reset it, leaving fail_count == 1.
        self.assertEqual(state["nodes"]["primary"]["fail_count"], 3)
        self.assertEqual(state["nodes"]["primary"]["last_state"], hc.STATE_DOWN)

    def _verdict_with_backend(self, backend_value, *, present=True):
        """Run a verdict whose status has BackendState set to ``backend_value``
        (or removed when ``present`` is False) and return the decision dict."""
        data = json.loads(json.dumps(SAMPLE_STATUS))
        if present:
            data["BackendState"] = backend_value
        else:
            data.pop("BackendState", None)
        status = self.tmp / "backend-variant.json"
        status.write_text(json.dumps(data), encoding="utf-8")
        rc, out = run_cli([
            "verdict", "--state-file", str(self.state_file),
            "--primary", "primary-vps", "--fallback", "fallback-vps",
            "--status-json-file", str(status),
            "--fail-threshold", "1", "--ok-threshold", "1", "--cooldown", "0",
            "--ensure-primary", "--json",
        ])
        self.assertEqual(rc, 0)
        return json.loads(out)["decision"]

    def test_verdict_backend_not_running_fails_closed(self):
        decision = self._verdict_with_backend("Stopped")
        self.assertEqual(decision["action"], "none")
        self.assertEqual(decision["reason"], "backend_not_running")
        self.assertFalse(self.state_file.exists())

    def test_verdict_missing_backend_fails_closed(self):
        # A well-formed status always carries BackendState; absence is malformed
        # and must fail closed instead of being treated as Running.
        decision = self._verdict_with_backend(None, present=False)
        self.assertEqual(decision["reason"], "backend_not_running")
        self.assertFalse(self.state_file.exists())

    def test_verdict_null_backend_fails_closed(self):
        decision = self._verdict_with_backend(None)
        self.assertEqual(decision["reason"], "backend_not_running")
        self.assertFalse(self.state_file.exists())

    def test_verdict_nonstring_backend_fails_closed(self):
        decision = self._verdict_with_backend(1)
        self.assertEqual(decision["reason"], "backend_not_running")
        self.assertFalse(self.state_file.exists())

    def _active_role(self, status_data):
        status = self.tmp / "active-role-status.json"
        status.write_text(json.dumps(status_data), encoding="utf-8")
        rc, out = run_cli([
            "active-role", "--primary", "primary-vps", "--fallback", "fallback-vps",
            "--status-json-file", str(status),
        ])
        self.assertEqual(rc, 0)
        return out.strip()

    def test_active_role_primary_when_running(self):
        # SAMPLE_STATUS has BackendState=Running and ExitNodeStatus on the primary.
        self.assertEqual(self._active_role(SAMPLE_STATUS), "primary")

    def test_active_role_unknown_when_backend_stopped(self):
        # Backend stopped after a switch but the JSON still carries ExitNodeStatus:
        # the readback must report "unknown", not the stale role.
        stopped = json.loads(json.dumps(SAMPLE_STATUS))
        stopped["BackendState"] = "Stopped"
        self.assertEqual(self._active_role(stopped), "unknown")

    def test_active_role_unknown_when_backend_missing(self):
        missing = json.loads(json.dumps(SAMPLE_STATUS))
        missing.pop("BackendState", None)
        self.assertEqual(self._active_role(missing), "unknown")

    def test_active_role_unknown_when_status_unavailable(self):
        bad = self.tmp / "bad-active.json"
        bad.write_text("{not json", encoding="utf-8")
        rc, out = run_cli([
            "active-role", "--primary", "primary-vps", "--fallback", "fallback-vps",
            "--status-json-file", str(bad),
        ])
        self.assertEqual(rc, 0)
        self.assertEqual(out.strip(), "unknown")


CONN_STATUS = {
    "BackendState": "Running",
    "Self": {"ID": "selfID", "HostName": "client", "TailscaleIPs": ["100.64.0.5"]},
    "Peer": {
        "nodeP": {"ID": "nodeP", "HostName": "primary-vps", "DNSName": "primary-vps.example.ts.net.",
                  "TailscaleIPs": ["100.64.0.1"], "Online": True, "PrimaryRoutes": ["10.0.0.0/24"]},
        "nodeF": {"ID": "nodeF", "HostName": "fallback-vps", "DNSName": "fallback-vps.example.ts.net.",
                  "TailscaleIPs": ["100.64.0.2"], "Online": True},
    },
}
DEVICES_PRIMARY_OLDER = {"devices": [
    {"hostname": "primary-vps", "created": "2026-01-01T00:00:00Z"},
    {"hostname": "fallback-vps", "created": "2026-03-01T00:00:00Z"},
]}


def multi_status(active=None, drop_peers=(), repoint=None):
    """Status fixture with primary-vps + three bench peers fb-a/fb-b/fb-c.

    ``active``: label (or raw id) whose node is the live exit node; None -> no
    exit node. ``drop_peers``: labels removed from the Peer map (unresolved this
    round). ``repoint``: {label: new_id} to simulate a label re-pointed at a
    recreated node."""
    nodes = {
        "primary-vps": ("nodeP", "100.64.0.1"),
        "fb-a": ("nodeA", "100.64.0.10"),
        "fb-b": ("nodeB", "100.64.0.11"),
        "fb-c": ("nodeC", "100.64.0.12"),
    }
    peers = {}
    for label, (node_id, ip) in nodes.items():
        if label in drop_peers:
            continue
        if repoint and label in repoint:
            node_id = repoint[label]
        # Keyed by label (not node id) so a repointed entry can ALIAS another
        # node's id without clobbering that node's own peer entry.
        peers[f"key-{label}"] = {
            "ID": node_id, "HostName": label, "DNSName": f"{label}.example.ts.net.",
            "TailscaleIPs": [ip], "Online": True,
        }
    status = {
        "BackendState": "Running",
        "Self": {"ID": "selfID", "HostName": "client", "TailscaleIPs": ["100.64.0.5"]},
        "Peer": peers,
        "ExitNodeStatus": None,
    }
    if active is not None:
        node_id, ip = nodes.get(active, (active, "100.64.0.99"))
        if repoint and active in (repoint or {}):
            pass  # the OLD node stays live; callers pass raw ids for that case
        status["ExitNodeStatus"] = {"ID": node_id, "TailscaleIPs": [f"{ip}/32"], "Online": True}
    return status


class MultiVerdictCliTests(unittest.TestCase):
    """verdict/record-switch/active-role CLI over an ordered 3-slot bench."""

    FALLBACKS = "fb-a,fb-b,fb-c"

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(self._cleanup)
        fake = self.tmp / "tailscale"
        fake.write_text(FAKE_TAILSCALE, encoding="utf-8")
        fake.chmod(fake.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
        self._set_env("TAILSCALE_BIN", str(fake))
        self.status_file = self.tmp / "status.json"
        self.state_file = self.tmp / "failover-state.json"

    def _cleanup(self):
        for child in sorted(self.tmp.rglob("*"), reverse=True):
            child.unlink() if child.is_file() else child.rmdir()
        self.tmp.rmdir()

    def _set_env(self, key, value):
        old = os.environ.get(key)
        os.environ[key] = value
        self.addCleanup(lambda: os.environ.__setitem__(key, old) if old is not None else os.environ.pop(key, None))

    def _write_status(self, **kwargs):
        self.status_file.write_text(json.dumps(multi_status(**kwargs)), encoding="utf-8")

    def _verdict(self, extra=None, fallbacks=None, thresholds="1"):
        argv = [
            "verdict", "--state-file", str(self.state_file),
            "--primary", "primary-vps", "--fallback", fallbacks or self.FALLBACKS,
            "--status-json-file", str(self.status_file),
            "--fail-threshold", thresholds, "--ok-threshold", thresholds,
            "--cooldown", "0", "--json",
        ]
        return run_cli(argv + (extra or []))

    def read_state(self):
        return json.loads(self.state_file.read_text(encoding="utf-8"))

    def test_c1_verdict_json_shape_and_legacy_pins(self):
        # Active is slot 1 (fb-b); slot 0 must still own the legacy keys.
        self._write_status(active="fb-b")
        self._set_env("FAKE_UNREACHABLE", "fb-a")
        rc, out = self._verdict()
        self.assertEqual(rc, 0)
        payload = json.loads(out)
        self.assertEqual(payload["schema_version"], 1)  # REPORT schema, not state's 2
        self.assertEqual([p["label"] for p in payload["fallbacks"]], ["fb-a", "fb-b", "fb-c"])
        self.assertEqual(payload["fallback"], payload["fallbacks"][0])  # pinned to slot 0
        self.assertFalse(payload["fallbacks"][0]["reachable"])
        decision = payload["decision"]
        self.assertEqual(decision["active_role"], "fallback")
        self.assertEqual(decision["fallback_state"], decision["fallback_states"][0])
        self.assertEqual(len(decision["fallback_states"]), 3)
        self.assertIn("target_index", decision)

    def test_c1_f2f_target_fields_consistent(self):
        # Active fb-a goes DOWN; the verdict must emit a slot-1 target whose
        # label equals the configured slot text at that index.
        self._write_status(active="fb-a")
        self._set_env("FAKE_UNREACHABLE", "primary-vps,fb-a")
        rc, out = self._verdict()
        decision = json.loads(out)["decision"]
        self.assertEqual(decision["action"], "switch-to-fallback")
        self.assertEqual(decision["reason"], "fallback_down_next_fallback")
        self.assertEqual(decision["target_index"], 1)
        self.assertEqual(decision["target_label"], "fb-b")
        self.assertEqual(decision["target_label"], self.FALLBACKS.split(",")[1])

    def test_c2_text_target_index_only_for_fallback_targets(self):
        self._write_status(active="fb-a")
        self._set_env("FAKE_UNREACHABLE", "primary-vps,fb-a")
        rc, out = run_cli([
            "verdict", "--state-file", str(self.state_file),
            "--primary", "primary-vps", "--fallback", self.FALLBACKS,
            "--status-json-file", str(self.status_file),
            "--fail-threshold", "1", "--ok-threshold", "1", "--cooldown", "0",
        ])
        self.assertIn("target_index=1", out)
        self.assertNotIn("fallback_states", out)  # JSON-only additive
        # Primary target: no target_index line.
        os.environ.pop("FAKE_UNREACHABLE", None)
        rc, out = run_cli([
            "verdict", "--state-file", str(self.tmp / "s2.json"),
            "--primary", "primary-vps", "--fallback", self.FALLBACKS,
            "--status-json-file", str(self.status_file),
            "--fail-threshold", "1", "--ok-threshold", "1", "--cooldown", "0",
        ])
        self.assertNotIn("target_index=", out)

    def test_l1_manual_selection_persists_role_index_identity(self):
        self._write_status(active="fb-c")  # operator ran tailscale set themselves
        rc, _out = self._verdict()
        state = self.read_state()
        self.assertEqual(state["active"]["role"], "fallback")
        self.assertEqual(state["active"]["fallback_index"], 2)
        self.assertEqual(state["active"]["configured_label"], "fb-c")
        self.assertEqual(state["active"]["node_id"], "nodeC")

    def test_l2_reorder_converges_within_one_cycle(self):
        self._write_status(active="fb-b")
        self._verdict()
        self.assertEqual(self.read_state()["active"]["fallback_index"], 1)
        # Operator reorders the list; the next cycle rebinds by label.
        rc, _out = self._verdict(fallbacks="fb-b,fb-a,fb-c")
        state = self.read_state()
        self.assertEqual(state["active"]["fallback_index"], 0)
        self.assertEqual(state["active"]["configured_label"], "fb-b")

    def test_l8_unresolved_bench_candidate_still_pinged_and_excluded(self):
        # fb-b vanishes from status: its REAL ping still runs (design: every
        # configured node probed every cycle), it feeds hysteresis, and the walk
        # skips it — fb-c wins despite fb-b's ping PASSING.
        self._write_status(active="fb-a", drop_peers=("fb-b",))
        self._set_env("FAKE_UNREACHABLE", "primary-vps,fb-a")  # fb-b ping would PASS
        rc, out = self._verdict()
        payload = json.loads(out)
        decision = payload["decision"]
        self.assertEqual(decision["action"], "switch-to-fallback")
        self.assertEqual(decision["target_index"], 2)  # fb-b excluded though reachable
        self.assertEqual(decision["target_label"], "fb-c")
        self.assertTrue(payload["fallbacks"][1]["reachable"])  # real ping ran and passed
        state = self.read_state()
        self.assertEqual(state["nodes"]["fallbacks"][1]["ok_count"], 1)  # hysteresis fed

    def test_l11_l12_delisted_preserved_two_cycles_normal_persists_live(self):
        # Cycle 1: fb-b active, persisted normally (L12 live-derived).
        self._write_status(active="fb-b")
        self._verdict()
        state = self.read_state()
        self.assertEqual((state["active"]["role"], state["active"]["fallback_index"]), ("fallback", 1))
        # Cycles 2+3: fb-b delisted from config while still the live exit node.
        for _cycle in range(2):
            rc, out = self._verdict(fallbacks="fb-a,fb-c")
            decision = json.loads(out)["decision"]
            self.assertEqual(decision["active_role"], "delisted")
            state = self.read_state()
            self.assertEqual(state["active"]["role"], "fallback")  # never the string "delisted"
            self.assertEqual(state["active"]["configured_label"], "fb-b")  # evidence retained
            self.assertEqual(state["active"]["node_id"], "nodeB")
            self.assertIsNone(state["active"]["fallback_index"])
            self.assertIn(state["active"]["role"], ("primary", "fallback", "none", "unknown"))

    def test_l3_l4_delisted_recovery_both_arms(self):
        # Arm 1: primary healthy -> delisted_restore_primary (strict bar).
        self._write_status(active="fb-b")
        self._verdict()
        rc, out = self._verdict(fallbacks="fb-a,fb-c")
        decision = json.loads(out)["decision"]
        self.assertEqual((decision["action"], decision["reason"]),
                         ("switch-to-primary", "delisted_restore_primary"))
        # Arm 2: primary unreachable -> walk the bench from the top.
        self._set_env("FAKE_UNREACHABLE", "primary-vps")
        rc, out = self._verdict(fallbacks="fb-a,fb-c")
        decision = json.loads(out)["decision"]
        self.assertEqual((decision["action"], decision["reason"]),
                         ("switch-to-fallback", "delisted_next_fallback"))
        self.assertEqual(decision["target_index"], 0)
        self.assertEqual(decision["target_label"], "fb-a")

    def test_l17_unmatched_live_plus_unresolved_no_state_write(self):
        # Fresh state (no claiming record), fb-b unresolved, live exit is some
        # unmatched node: the cycle fails closed — live_status_incomplete, no
        # switch proposal, and NO state file is written.
        self._write_status(active="mystery-node", drop_peers=("fb-b",))
        rc, out = self._verdict()
        payload = json.loads(out)
        self.assertEqual(payload["decision"]["reason"], "live_status_incomplete")
        self.assertEqual(payload["decision"]["action"], "none")
        self.assertFalse(self.state_file.exists())

    def test_l17_preseeded_repointed_plus_unresolved_no_state_change(self):
        # Preseeded variant (GPT confirm-round Major): the state CLAIMS the
        # live node as fb-b; fb-b then re-points to a new node while fb-c is
        # unresolved. The live node cannot be proven foreign — fail closed,
        # nothing persisted.
        self._write_status(active="fb-b")
        self._verdict()  # seed: active fallback fb-b/nodeB
        before = self.read_state()
        self._write_status(active="nodeB", repoint={"fb-b": "nodeNEW"}, drop_peers=("fb-c",))
        rc, out = self._verdict()
        payload = json.loads(out)
        self.assertEqual(payload["decision"]["reason"], "live_status_incomplete")
        self.assertEqual(payload["decision"]["action"], "none")
        self.assertIsNone(payload["decision"]["target_index"])
        self.assertEqual(self.read_state(), before)  # nothing persisted

    def test_l16_unresolved_cycle_keeps_identity_baseline_for_reset(self):
        # GPT plan-confirm finding: an unresolved cycle must NOT blank the
        # slot's stored identity — it is the comparison baseline that lets the
        # identity-change reset fire when a DIFFERENT node later takes the
        # label. Sequence: (1) fb-b resolved and failing (accrues DOWN);
        # (2) fb-b unresolved — identity retained; (3) fb-b re-points to a NEW
        # node — history must RESET, not be inherited by the new node.
        # Thresholds of 3 make reset-vs-inherit observable: an inherited DOWN
        # would survive one fresh pass (ok 1 < 3), a reset lands on UNKNOWN.
        self._set_env("FAKE_UNREACHABLE", "fb-b")
        self._write_status(active="fb-a")
        for _cycle in range(3):
            self._verdict(thresholds="3")  # fb-b accrues DOWN, identity nodeB
        state = self.read_state()
        self.assertEqual(state["nodes"]["fallbacks"][1]["node_id"], "nodeB")
        self.assertEqual(state["nodes"]["fallbacks"][1]["last_state"], hc.STATE_DOWN)
        self._write_status(active="fb-a", drop_peers=("fb-b",))
        self._verdict(thresholds="3")  # unresolved cycle — identity baseline retained
        state = self.read_state()
        self.assertEqual(state["nodes"]["fallbacks"][1]["node_id"], "nodeB")
        os.environ.pop("FAKE_UNREACHABLE", None)
        self._write_status(active="fb-a", repoint={"fb-b": "nodeNEW"})
        self._verdict(thresholds="3")  # a different node holds the label now
        state = self.read_state()
        self.assertEqual(state["nodes"]["fallbacks"][1]["node_id"], "nodeNEW")
        self.assertEqual(state["nodes"]["fallbacks"][1]["fail_count"], 0)  # history reset
        self.assertEqual(state["nodes"]["fallbacks"][1]["ok_count"], 1)  # one fresh pass
        self.assertEqual(state["nodes"]["fallbacks"][1]["last_state"], hc.STATE_UNKNOWN)

    def test_l15_unresolved_primary_fails_closed_in_multi(self):
        self._write_status(active="fb-a", drop_peers=("primary-vps",))
        rc, out = self._verdict()
        decision = json.loads(out)["decision"]
        self.assertEqual(decision["reason"], "live_status_incomplete")
        self.assertEqual(decision["action"], "none")
        self.assertFalse(self.state_file.exists())  # fail-closed: no state write

    def test_l6_unresolved_active_fails_closed_cli(self):
        # fb-b is the recorded active; it vanishes from status while still the
        # live exit node -> unverifiable ACTIVE -> live_status_incomplete, no
        # switch proposal, no state mutation, no tailscale set target.
        self._write_status(active="fb-b")
        self._verdict()
        before = self.read_state()
        self._write_status(active="nodeB", drop_peers=("fb-b",))  # raw id keeps old node live
        rc, out = self._verdict()
        payload = json.loads(out)
        self.assertEqual(payload["decision"]["reason"], "live_status_incomplete")
        self.assertEqual(payload["decision"]["action"], "none")
        self.assertIsNone(payload["decision"]["target_index"])
        self.assertEqual(self.read_state(), before)  # nothing persisted

    def test_l9_single_element_unresolved_fallback_verbatim(self):
        self._write_status(active=None, drop_peers=("fb-b", "fb-c"))
        rc, out = self._verdict(fallbacks="fb-b")
        decision = json.loads(out)["decision"]
        self.assertEqual(decision["reason"], "live_status_incomplete")

    def test_l10_alias_pairs_refused_cli(self):
        # fb-c re-pointed to nodeA: two configured labels now alias one node.
        self._write_status(active=None, repoint={"fb-c": "nodeA"})
        rc, out = self._verdict()
        self.assertEqual(json.loads(out)["decision"]["reason"], "candidates_not_distinct")

    def test_c8_probe_json_report_schema_still_1(self):
        rc, out = run_cli(["probe", "--node", "primary-vps", "--json"])
        self.assertEqual(json.loads(out)["schema_version"], 1)


class RecordSwitchPairingTests(unittest.TestCase):
    """record-switch --fallback-index/--label fail-closed pairing (C3-C5)."""

    FALLBACKS = "fb-a,fb-b,fb-c"

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(self._cleanup)
        self.state_file = self.tmp / "state.json"

    def _cleanup(self):
        for child in sorted(self.tmp.rglob("*"), reverse=True):
            child.unlink() if child.is_file() else child.rmdir()
        self.tmp.rmdir()

    def _record(self, *extra, fallbacks=None):
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            rc, out = run_cli([
                "record-switch", "--state-file", str(self.state_file),
                "--primary", "primary-vps", "--fallback", fallbacks or self.FALLBACKS,
                *extra,
            ])
        return rc, out, buf.getvalue()

    def test_c3_multi_missing_index_refused(self):
        rc, _out, err = self._record("--role", "fallback", "--label", "fb-b")
        self.assertEqual(rc, 2)
        self.assertIn("--fallback-index is required", err)
        self.assertFalse(self.state_file.exists())

    def test_c3_multi_missing_label_refused(self):
        rc, _out, err = self._record("--role", "fallback", "--fallback-index", "1")
        self.assertEqual(rc, 2)
        self.assertIn("--label is required", err)
        self.assertFalse(self.state_file.exists())

    def test_c3_out_of_range_index_refused(self):
        for bad in ("3", "-1", "18446744073709551616"):
            with self.subTest(index=bad):
                rc, _out, err = self._record("--role", "fallback", "--fallback-index", bad, "--label", "fb-a")
                self.assertEqual(rc, 2)
                self.assertIn("out of range", err)
                self.assertFalse(self.state_file.exists())

    def test_c3_label_slot_mismatch_refused_nothing_written(self):
        # M10's discriminator: label names a REAL configured entry but not the
        # one at --fallback-index — the shell's and engine's views diverged.
        rc, _out, err = self._record("--role", "fallback", "--fallback-index", "0", "--label", "fb-b")
        self.assertEqual(rc, 2)
        self.assertIn("does not match configured fallback slot 0", err)
        self.assertFalse(self.state_file.exists())

    def test_c3_valid_pair_writes_index_and_identity(self):
        rc, out, _err = self._record("--role", "fallback", "--fallback-index", "1", "--label", "fb-b")
        self.assertEqual(rc, 0)
        state = json.loads(self.state_file.read_text(encoding="utf-8"))
        self.assertEqual(state["active"]["role"], "fallback")
        self.assertEqual(state["active"]["fallback_index"], 1)
        self.assertEqual(state["active"]["configured_label"], "fb-b")
        self.assertGreater(state["active"]["last_switch_epoch"], 0.0)

    def test_c3_canonical_ip_label_accepted(self):
        # --label may respell an IP-valued slot (same canonical address).
        rc, _out, _err = self._record(
            "--role", "fallback", "--fallback-index", "0", "--label", "FD7A:115C:A1E0:0:0:0:0:9",
            fallbacks="fd7a:115c:a1e0::9,fb-b",
        )
        self.assertEqual(rc, 0)
        state = json.loads(self.state_file.read_text(encoding="utf-8"))
        # The recorded label is the CONFIGURED slot spelling, not the caller's.
        self.assertEqual(state["active"]["configured_label"], "fd7a:115c:a1e0::9")

    def test_c4_single_element_defaults_index_zero(self):
        rc, _out, _err = self._record("--role", "fallback", fallbacks="only-fb")
        self.assertEqual(rc, 0)
        state = json.loads(self.state_file.read_text(encoding="utf-8"))
        self.assertEqual(state["active"]["fallback_index"], 0)
        # --label validated when given (wrong one refuses).
        rc, _out, err = self._record("--role", "fallback", "--label", "wrong", fallbacks="only-fb")
        self.assertEqual(rc, 2)

    def test_c5_primary_role_with_pairing_flags_refused(self):
        rc, _out, err = self._record("--role", "primary", "--fallback-index", "0")
        self.assertEqual(rc, 2)
        self.assertIn("only valid with --role fallback", err)
        rc, _out, err = self._record("--role", "none", "--label", "fb-a")
        self.assertEqual(rc, 2)
        self.assertFalse(self.state_file.exists())

    def test_c3_primary_record_clears_index(self):
        self._record("--role", "fallback", "--fallback-index", "1", "--label", "fb-b")
        rc, _out, _err = self._record("--role", "primary")
        self.assertEqual(rc, 0)
        state = json.loads(self.state_file.read_text(encoding="utf-8"))
        self.assertIsNone(state["active"]["fallback_index"])


class ActiveRoleExpectLabelTests(unittest.TestCase):
    """active-role --expect-label: identity-verified readback (C6)."""

    FALLBACKS = "fb-a,fb-b,fb-c"

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(self._cleanup)
        self.status_file = self.tmp / "status.json"

    def _cleanup(self):
        for child in sorted(self.tmp.rglob("*"), reverse=True):
            child.unlink() if child.is_file() else child.rmdir()
        self.tmp.rmdir()

    def _active_role(self, *extra, status_kwargs=None):
        self.status_file.write_text(json.dumps(multi_status(**(status_kwargs or {}))), encoding="utf-8")
        return run_cli([
            "active-role", "--primary", "primary-vps", "--fallback", self.FALLBACKS,
            "--status-json-file", str(self.status_file), *extra,
        ])

    def test_c6_match_exit_zero(self):
        rc, out = self._active_role("--expect-label", "fb-b", status_kwargs={"active": "fb-b"})
        self.assertEqual(rc, 0)
        self.assertEqual(out.strip(), "match=1")

    def test_c6_wrong_node_fails(self):
        # The no-op-switch shape: expected fb-c, live is still fb-b.
        rc, out = self._active_role("--expect-label", "fb-c", status_kwargs={"active": "fb-b"})
        self.assertEqual(rc, 1)
        self.assertEqual(out.strip(), "match=0")

    def test_c6_role_class_alone_never_matches(self):
        # Both are "fallback"-class nodes — identity, not role class, decides.
        rc, _out = self._active_role("--expect-label", "fb-a", status_kwargs={"active": "fb-c"})
        self.assertEqual(rc, 1)

    def test_c6_no_exit_node_fails(self):
        rc, _out = self._active_role("--expect-label", "fb-b", status_kwargs={"active": None})
        self.assertEqual(rc, 1)

    def test_c6_backend_not_running_fails(self):
        status = multi_status(active="fb-b")
        status["BackendState"] = "Stopped"
        self.status_file.write_text(json.dumps(status), encoding="utf-8")
        rc, out = run_cli([
            "active-role", "--primary", "primary-vps", "--fallback", self.FALLBACKS,
            "--status-json-file", str(self.status_file), "--expect-label", "fb-b",
        ])
        self.assertEqual(rc, 1)

    def test_c6_unresolved_expect_label_fails(self):
        rc, _out = self._active_role("--expect-label", "ghost", status_kwargs={"active": "fb-b"})
        self.assertEqual(rc, 1)

    def test_c6_canonical_ipv6_spelling_matches(self):
        status = multi_status(active=None)
        status["Peer"]["node6"] = {"ID": "node6", "HostName": "v6-node",
                                   "TailscaleIPs": ["fd7a:115c:a1e0::9"], "Online": True}
        status["ExitNodeStatus"] = {"ID": "node6", "TailscaleIPs": ["fd7a:115c:a1e0::9/128"], "Online": True}
        self.status_file.write_text(json.dumps(status), encoding="utf-8")
        rc, out = run_cli([
            "active-role", "--primary", "primary-vps", "--fallback", self.FALLBACKS,
            "--status-json-file", str(self.status_file),
            "--expect-label", "FD7A:115C:A1E0:0:0:0:0:9",
        ])
        self.assertEqual(rc, 0)

    def test_c6_invalid_list_refused_before_status(self):
        # The list validation wire point in active-role: exit 2, no status read.
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            rc, _out = run_cli([
                "active-role", "--primary", "p", "--fallback", "a,,b",
                "--status-json-file", str(self.tmp / "never-written.json"),
            ])
        self.assertEqual(rc, 2)
        self.assertIn("empty", buf.getvalue())

    def test_c6_flagless_mode_unchanged(self):
        rc, out = self._active_role(status_kwargs={"active": "fb-b"})
        self.assertEqual(rc, 0)
        self.assertEqual(out.strip(), "fallback")
        rc, out = self._active_role(status_kwargs={"active": None})
        self.assertEqual(out.strip(), "none")


class ConnectorsFallbackDefaultTests(unittest.TestCase):
    """C7: the connectors nested default never adopts one element of a list."""

    def _with_env(self, updates):
        for key, value in updates.items():
            old = os.environ.get(key)
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
            self.addCleanup(
                lambda k=key, o=old: os.environ.__setitem__(k, o) if o is not None else os.environ.pop(k, None)
            )

    def test_c7_unset_with_comma_list_resolves_unset(self):
        self._with_env({"FALLBACK_CONNECTOR": None, "FALLBACK_EXIT_NODE": "fb-a,fb-b"})
        self.assertEqual(hc.connectors_fallback_default(), "")

    def test_c7_unset_with_scalar_keeps_nested_default(self):
        self._with_env({"FALLBACK_CONNECTOR": None, "FALLBACK_EXIT_NODE": "fb-a"})
        self.assertEqual(hc.connectors_fallback_default(), "fb-a")

    def test_c7_set_but_empty_stays_empty(self):
        self._with_env({"FALLBACK_CONNECTOR": "", "FALLBACK_EXIT_NODE": "fb-a,fb-b"})
        self.assertEqual(hc.connectors_fallback_default(), "")

    def test_c7_set_wins_over_list(self):
        self._with_env({"FALLBACK_CONNECTOR": "conn-x", "FALLBACK_EXIT_NODE": "fb-a,fb-b"})
        self.assertEqual(hc.connectors_fallback_default(), "conn-x")

    def test_c7_neither_set_empty(self):
        self._with_env({"FALLBACK_CONNECTOR": None, "FALLBACK_EXIT_NODE": None})
        self.assertEqual(hc.connectors_fallback_default(), "")


class ConnectorOrderingUnitTests(unittest.TestCase):
    def test_primary_oldest(self):
        order, _ = hc.connector_ordering(DEVICES_PRIMARY_OLDER["devices"], "primary-vps", "fallback-vps")
        self.assertEqual(order, "primary_is_oldest")

    def test_fallback_oldest(self):
        devices = [
            {"hostname": "primary-vps", "created": "2026-05-01T00:00:00Z"},
            {"hostname": "fallback-vps", "created": "2026-02-01T00:00:00Z"},
        ]
        order, _ = hc.connector_ordering(devices, "primary-vps", "fallback-vps")
        self.assertEqual(order, "fallback_is_oldest")

    def test_unavailable_without_devices(self):
        order, reason = hc.connector_ordering(None, "primary-vps", "fallback-vps")
        self.assertEqual(order, "unavailable")
        self.assertEqual(reason, "no_api_token_or_source")

    def test_unavailable_when_device_missing(self):
        order, reason = hc.connector_ordering([{"hostname": "primary-vps", "created": "x"}], "primary-vps", "fallback-vps")
        self.assertEqual(order, "unavailable")
        self.assertEqual(reason, "device_created_not_found")

    def test_node_online_true(self):
        node_id, ips = hc.resolve_identity(CONN_STATUS, "primary-vps")
        self.assertTrue(hc.node_online(CONN_STATUS, node_id, ips))

    def test_node_online_unknown_when_absent(self):
        self.assertIsNone(hc.node_online(CONN_STATUS, "ghostID", []))

    def test_node_routes_excludes_default_routes(self):
        status = {"Peer": {"nodeP": {"ID": "nodeP", "HostName": "primary-vps",
                                     "TailscaleIPs": ["100.64.0.1"],
                                     "AllowedIPs": ["100.64.0.1/32", "0.0.0.0/0", "::/0"]}}}
        node_id, ips = hc.resolve_identity(status, "primary-vps")
        self.assertEqual(hc.node_routes(status, node_id, ips), [])

    def test_node_routes_keeps_real_routes(self):
        status = {"Peer": {"nodeP": {"ID": "nodeP", "HostName": "primary-vps",
                                     "TailscaleIPs": ["100.64.0.1"],
                                     "PrimaryRoutes": ["0.0.0.0/0", "10.0.0.0/24"]}}}
        node_id, ips = hc.resolve_identity(status, "primary-vps")
        self.assertEqual(hc.node_routes(status, node_id, ips), ["10.0.0.0/24"])

    def test_node_routes_empty_primary_routes_no_fallback(self):
        # PrimaryRoutes present but empty -> not serving; must NOT fall back to AllowedIPs.
        status = {"Peer": {"nodeP": {"ID": "nodeP", "HostName": "primary-vps",
                                     "TailscaleIPs": ["100.64.0.1"],
                                     "PrimaryRoutes": [], "AllowedIPs": ["10.0.0.0/24"]}}}
        node_id, ips = hc.resolve_identity(status, "primary-vps")
        self.assertEqual(hc.node_routes(status, node_id, ips), [])

    # --- hardening: malformed-status crash-safety + strict routes (task #7) --------
    def test_resolve_identity_survives_malformed_tailscaleips(self):
        # A non-list TailscaleIPs must not crash resolve_identity / _find_node.
        status = {"Peer": {"k": {"ID": "n1", "HostName": "prim", "TailscaleIPs": 7}}}
        self.assertEqual(hc.resolve_identity(status, "prim"), ("n1", []))   # by name, no crash
        self.assertIsNotNone(hc._find_node(status, "n1", []))
        self.assertEqual(hc.resolve_identity(status, "100.64.0.9"), (None, []))  # IP match, no crash

    def test_node_routes_strict(self):
        def routes(node_extra):
            status = {"Peer": {"nodeP": {"ID": "nodeP", "HostName": "primary-vps",
                                         "TailscaleIPs": ["100.64.0.1"], **node_extra}}}
            nid, ips = hc.resolve_identity(status, "primary-vps")
            return hc.node_routes(status, nid, ips)
        # malformed -> None (fail closed)
        self.assertIsNone(routes({"PrimaryRoutes": "10.0.0.0/24"}))          # non-list
        self.assertIsNone(routes({"PrimaryRoutes": ["not-a-cidr"]}))         # invalid element
        self.assertIsNone(routes({"PrimaryRoutes": ["10.0.0.0/24", ""]}))    # empty element
        self.assertIsNone(routes({"AllowedIPs": 7}))                         # non-list fallback
        # present-null / absent authoritative field -> [] (a Go nil slice marshals to
        # null); matches the pre-hardening behavior, NOT fail-closed None.
        self.assertEqual(routes({"PrimaryRoutes": None}), [])
        self.assertEqual(routes({}), [])                                     # no PrimaryRoutes, no AllowedIPs
        # AllowedIPs fallback with an UNTRUSTWORTHY TailscaleIPs -> fail closed: the
        # own-host exclusion is unreliable, so the node's own /32 must NOT be counted as
        # a route. "Untrustworthy" means non-list, a non-str/empty element, an element
        # that is not a valid IP/CIDR, OR an empty self set (nothing to exclude with).
        self.assertIsNone(routes({"TailscaleIPs": 7, "AllowedIPs": ["100.64.0.1/32"]}))
        self.assertIsNone(routes({"TailscaleIPs": ["bad", 5], "AllowedIPs": ["100.64.0.1/32"]}))
        self.assertIsNone(routes({"TailscaleIPs": ["not-an-ip"], "AllowedIPs": ["100.64.0.1/32"]}))
        self.assertIsNone(routes({"TailscaleIPs": ["100.64.0.1/nope"], "AllowedIPs": ["10.0.0.0/24"]}))
        self.assertIsNone(routes({"TailscaleIPs": [], "AllowedIPs": ["100.64.0.1/32"]}))
        # dotted netmask / host-mask forms are accepted by ipaddress but Tailscale never
        # emits them -> fail closed on both the route field and the self-IP set (Blocker B)
        self.assertIsNone(routes({"PrimaryRoutes": ["10.0.0.0/255.255.255.0"]}))
        self.assertIsNone(routes({"TailscaleIPs": ["100.64.0.1/255.0.0.0"], "AllowedIPs": ["10.0.0.0/24"]}))
        # a valid TailscaleIPs with a /prefix still excludes the own address
        self.assertEqual(routes({"TailscaleIPs": ["100.64.0.1/32"], "AllowedIPs": ["100.64.0.1/32", "10.0.0.0/24"]}),
                         ["10.0.0.0/24"])
        # valid -> strict list; non-canonical default and own host excluded
        self.assertEqual(routes({"PrimaryRoutes": ["0.0.0.1/0", "10.0.0.0/24"]}), ["10.0.0.0/24"])
        self.assertEqual(routes({"AllowedIPs": ["100.64.0.1/32", "10.0.0.0/24"]}), ["10.0.0.0/24"])
        # own address is excluded at ANY prefix, not only /32 -- preserves the prior
        # canonical behavior (a prefix-agnostic address-part match), so a valid status
        # with an aligned non-host prefix on the node's own address still yields [].
        self.assertEqual(routes({"AllowedIPs": ["100.64.0.1/32", "100.64.0.1/31"]}), [])
        # scoped IPv6 and over-long prefixes are rejected on the route field too: a
        # scoped `::%x/0` must not slip past the default-route exclusion, and a
        # thousands-of-digits prefix must fail closed, never raise (GPT-5.6-sol r4b).
        self.assertIsNone(routes({"PrimaryRoutes": ["::%forged/0"]}))
        self.assertIsNone(routes({"PrimaryRoutes": ["10.0.0.0/" + "9" * 5000]}))
        # own-host exclusion is by CANONICAL address, so an EXPANDED IPv6 spelling of the
        # node's own address is still excluded and not counted as a route (GPT-5.6-sol r5:
        # a text-only compare left it in -> false healthy).
        self.assertEqual(routes({"TailscaleIPs": ["fd7a:115c:a1e0::1"],
                                 "AllowedIPs": ["fd7a:115c:a1e0:0:0:0:0:1/128", "10.0.0.0/24"]}),
                         ["10.0.0.0/24"])

    def test_strict_ip_parser_table(self):
        # Direct table for the shared strict parser (GPT-5.6-sol r4b/r5). Accept only
        # real Tailscale forms and return the CANONICAL address; reject everything
        # ipaddress would over-accept. Canonicalization: an expanded IPv6 spelling and a
        # host-suffixed form collapse to the compressed address.
        for good, expected in (("100.64.0.1", "100.64.0.1"), ("100.64.0.1/32", "100.64.0.1"),
                               ("fd7a:115c:a1e0::1", "fd7a:115c:a1e0::1"),
                               ("fd7a:115c:a1e0::1/128", "fd7a:115c:a1e0::1"),
                               ("fd7a:115c:a1e0:0:0:0:0:1", "fd7a:115c:a1e0::1")):
            self.assertEqual(hc._strict_norm_ip(good), expected, msg=good)
        for bad in ("not-an-ip", "100.64.0.1/33", "100.64.0.1/nope", "100.64.0.1/255.255.255.255",
                    "100.64.0.1/24", "100.64.0.1/032", "fd7a:115c:a1e0::1/129", "fd7a:115c:a1e0::1%eth0",
                    "100.64.0.1/" + "9" * 5000, "", "100.64.0.1/-1"):
            self.assertIsNone(hc._strict_norm_ip(bad), msg=bad)
        # whole-field fail-closed: one bad element voids the set (never a partial keep)
        self.assertIsNone(hc._valid_ip_set(["100.64.0.1", "bad"]))
        self.assertEqual(hc._valid_ip_set(["100.64.0.1", "fd7a::1/128"]), {"100.64.0.1", "fd7a::1"})
        # compressed and expanded IPv6 spellings of the same address collapse to one key
        self.assertEqual(hc._valid_ip_set(["fd7a:115c:a1e0::1", "fd7a:115c:a1e0:0:0:0:0:1"]),
                         {"fd7a:115c:a1e0::1"})

    def test_find_node_prefers_valid_over_malformed_candidate(self):
        # A malformed candidate peer IP must not forge an IP match; _find_node must
        # select the peer whose VALIDATED IPs actually contain the wanted address, even
        # if a different peer's junk string normalizes to the same head (r4b Blocker A).
        status = {"Peer": {
            "bad": {"ID": "nodeBad", "HostName": "bad", "TailscaleIPs": ["100.64.0.1/not-a-prefix"]},
            "good": {"ID": "nodeGood", "HostName": "good", "TailscaleIPs": ["100.64.0.1"]},
        }}
        node = hc._find_node(status, None, ["100.64.0.1"])
        self.assertIsNotNone(node)
        self.assertEqual(node["ID"], "nodeGood")   # not nodeBad (its "100.64.0.1/not-a-prefix" is void)

    def test_fetch_devices_encodes_tailnet_in_url(self):
        # A tailnet name with reserved characters must be percent-encoded into a
        # single path segment, never allowed to rewrite the request path.
        captured = {}

        class _FakeResp:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def read(self):
                return b'{"devices": []}'

        def _fake_urlopen(req, timeout=None):
            captured["url"] = req.full_url
            return _FakeResp()

        env = {"TAILSCALE_API_KEY": "tskey-api-example", "TAILSCALE_TAILNET": "ex/ample#tn?x"}
        with mock.patch.dict(os.environ, env, clear=False):
            with mock.patch.object(hc.urllib.request, "urlopen", side_effect=_fake_urlopen):
                result = hc.fetch_devices_via_api()

        self.assertEqual(result, [])
        self.assertIn("/tailnet/ex%2Fample%23tn%3Fx/devices", captured["url"])
        self.assertNotIn("ex/ample", captured["url"])


class PeerMetricsTests(unittest.TestCase):
    NOW = hc.dt.datetime(2026, 7, 11, 12, 0, 0, tzinfo=hc.dt.timezone.utc)

    def _status(self, peer):
        p = dict(peer)
        p.setdefault("ID", "n1")
        return {
            "BackendState": "Running",
            "Self": {"ID": "self", "HostName": "self", "TailscaleIPs": ["100.64.0.9"]},
            "Peer": {"k": p},
        }

    def _metrics(self, peer, *, label="prim", now=None):
        return hc.peer_metrics(self._status(peer), True, label, now=now)

    def test_direct_peer_full_object(self):
        peer = {
            "HostName": "prim", "TailscaleIPs": ["100.64.0.1"], "Online": True, "Active": True,
            "TxBytes": 1234, "RxBytes": 5678, "LastHandshake": "2026-07-11T11:59:00+00:00",
            "Relay": "sin", "CurAddr": "1.2.3.4:41641",
        }
        m = self._metrics(peer, now=self.NOW)
        self.assertEqual(set(m), set(hc.PEER_METRIC_KEYS))  # fixed key set, no omissions
        self.assertEqual(m["connection_path"], "direct")
        self.assertEqual(m["tx_bytes_total"], 1234)
        self.assertEqual(m["rx_bytes_total"], 5678)
        self.assertIs(m["online"], True)
        self.assertIs(m["active"], True)
        self.assertEqual(m["relay"], "sin")
        self.assertEqual(m["cur_addr"], "1.2.3.4:41641")
        self.assertEqual(m["last_handshake"], "2026-07-11T11:59:00+00:00")
        self.assertEqual(m["last_handshake_age_seconds"], 60)

    def test_derp_when_curaddr_empty_and_online(self):
        m = self._metrics({"HostName": "d", "TailscaleIPs": ["100.64.0.2"], "Online": True, "CurAddr": "", "Relay": "sin"}, label="d")
        self.assertEqual(m["connection_path"], "derp")
        self.assertIsNone(m["cur_addr"])

    def test_unknown_when_offline_or_online_null(self):
        for peer in (
            {"HostName": "x", "TailscaleIPs": ["100.64.0.3"], "Online": False, "CurAddr": ""},
            {"HostName": "x", "TailscaleIPs": ["100.64.0.3"], "CurAddr": ""},            # Online absent
            {"HostName": "x", "TailscaleIPs": ["100.64.0.3"], "Online": None, "CurAddr": ""},  # Online null
        ):
            with self.subTest(online=peer.get("Online", "absent")):
                m = self._metrics(peer, label="x")
                # Empty CurAddr with online not exactly True (false/null/absent)
                # must yield unknown, never derp.
                self.assertEqual(m["connection_path"], "unknown")

    def test_relay_unset_preserved_raw(self):
        m = self._metrics({"HostName": "r", "TailscaleIPs": ["100.64.0.4"], "Online": True, "CurAddr": "1.2.3.4:5", "Relay": ""}, label="r")
        self.assertIsNone(m["relay"])            # empty Relay -> null, not ""
        self.assertEqual(m["cur_addr"], "1.2.3.4:5")
        self.assertEqual(m["connection_path"], "direct")

    def test_zero_value_handshake_is_null(self):
        m = self._metrics({"HostName": "z", "TailscaleIPs": ["100.64.0.5"], "Online": True, "CurAddr": "", "LastHandshake": "0001-01-01T00:00:00Z"}, label="z")
        self.assertIsNone(m["last_handshake"])
        self.assertIsNone(m["last_handshake_age_seconds"])

    def test_missing_fields_null_without_omitting_keys(self):
        m = self._metrics({"HostName": "m", "TailscaleIPs": ["100.64.0.6"], "Online": True, "CurAddr": ""}, label="m")
        self.assertEqual(set(m), set(hc.PEER_METRIC_KEYS))
        self.assertIsNone(m["tx_bytes_total"])
        self.assertIsNone(m["rx_bytes_total"])

    def test_transport_failure_is_null_filled(self):
        m = hc.peer_metrics({}, False, "anything")
        self.assertEqual(set(m), set(hc.PEER_METRIC_KEYS))
        self.assertTrue(all(v is None for v in m.values()))

    def test_peer_not_found_is_null_filled(self):
        m = self._metrics({"HostName": "prim", "TailscaleIPs": ["100.64.0.1"]}, label="does-not-exist")
        self.assertEqual(set(m), set(hc.PEER_METRIC_KEYS))
        self.assertTrue(all(v is None for v in m.values()))

    def test_cli_always_exits_zero_and_prints_object(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = Path(tmp) / "status.json"
            fixture.write_text(
                json.dumps(self._status({"HostName": "prim", "TailscaleIPs": ["100.64.0.1"], "Online": True, "CurAddr": "1.2.3.4:5", "TxBytes": 5})),
                encoding="utf-8",
            )
            for node in ("prim", "missing-peer"):
                with self.subTest(node=node):
                    result = subprocess.run(
                        [sys.executable, str(ROOT / "scripts/health_check.py"),
                         "peer-metrics", "--node", node, "--status-json-file", str(fixture)],
                        text=True, capture_output=True, cwd=ROOT,
                    )
                    self.assertEqual(result.returncode, 0)
                    obj = json.loads(result.stdout)
                    self.assertEqual(set(obj), set(hc.PEER_METRIC_KEYS))

    def test_naive_now_is_normalized(self):
        # A caller-supplied naive `now` must not raise against the aware handshake.
        peer = {"HostName": "p", "TailscaleIPs": ["100.64.0.1"], "Online": True, "CurAddr": "1.2.3.4:5",
                "LastHandshake": "2026-07-11T11:59:00+00:00"}
        m = self._metrics(peer, label="p", now=hc.dt.datetime(2026, 7, 11, 12, 0, 0))  # naive
        self.assertEqual(m["last_handshake_age_seconds"], 60)

    def test_handshake_parse_variants(self):
        now = hc.dt.datetime(2026, 7, 11, 12, 0, 0, tzinfo=hc.dt.timezone.utc)

        def age(hs):
            peer = {"HostName": "p", "TailscaleIPs": ["100.64.0.1"], "Online": True, "CurAddr": "1.2.3.4:5", "LastHandshake": hs}
            return self._metrics(peer, label="p", now=now)

        self.assertEqual(age("2026-07-11T11:59:00Z")["last_handshake_age_seconds"], 60)   # trailing Z
        self.assertEqual(age("2026-07-11T11:59:00")["last_handshake_age_seconds"], 60)    # naive input -> UTC
        # >6 fractional digits parse (trimmed to microseconds): 11:59:00.123456 -> age 59.
        self.assertEqual(age("2026-07-11T11:59:00.123456789Z")["last_handshake_age_seconds"], 59)
        # unparseable -> both handshake fields null.
        bad = age("not-a-timestamp")
        self.assertIsNone(bad["last_handshake"])
        self.assertIsNone(bad["last_handshake_age_seconds"])

    def test_byte_counters_ignore_bool_and_float(self):
        for tx in (True, False):  # bool is an int subclass; both must be rejected
            with self.subTest(tx=tx):
                m = self._metrics({"HostName": "p", "TailscaleIPs": ["100.64.0.1"], "Online": True, "CurAddr": "x", "TxBytes": tx}, label="p")
                self.assertIsNone(m["tx_bytes_total"])
        m = self._metrics({"HostName": "p", "TailscaleIPs": ["100.64.0.1"], "Online": True, "CurAddr": "x", "RxBytes": 1.5}, label="p")
        self.assertIsNone(m["rx_bytes_total"])   # float rejected

    def test_cli_survives_peer_metrics_exception(self):
        # The subcommand's "always exits 0" contract holds even if extraction raises.
        args = argparse.Namespace(node="prim", status_json_file=None, json=False, ping=False, ping_timeout=5.0)
        with mock.patch.object(hc, "get_status", return_value=({}, True)), \
             mock.patch.object(hc, "peer_metrics", side_effect=RuntimeError("boom")):
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = hc.cmd_peer_metrics(args)
        self.assertEqual(rc, 0)
        obj = json.loads(buf.getvalue())
        self.assertEqual(set(obj), set(hc.PEER_METRIC_KEYS))
        self.assertTrue(all(v is None for v in obj.values()))

    def test_online_non_bool_is_null_and_unknown_path(self):
        for online in (1, "true"):
            with self.subTest(online=online):
                m = self._metrics({"HostName": "p", "TailscaleIPs": ["100.64.0.1"], "Online": online, "CurAddr": ""}, label="p")
                self.assertIsNone(m["online"])                       # non-bool Online -> null
                self.assertEqual(m["connection_path"], "unknown")    # not exactly True -> not derp

    # --- latency_ms (PR A) -------------------------------------------------
    def _resolved(self, **extra):
        peer = {"HostName": "p", "TailscaleIPs": ["100.64.0.1"], "Online": True, "CurAddr": "1.2.3.4:5"}
        peer.update(extra)
        return self._status(peer)

    def test_latency_key_present_null_without_source(self):
        m = self._metrics({"HostName": "p", "TailscaleIPs": ["100.64.0.1"], "Online": True, "CurAddr": "x"}, label="p")
        self.assertEqual(set(m), set(hc.PEER_METRIC_KEYS))
        self.assertIsNone(m["latency_ms"])                            # no caller value / ping_fn

    def test_latency_from_caller_value_on_resolved(self):
        m = hc.peer_metrics(self._resolved(), True, "p", latency_ms=12.5)
        self.assertEqual(m["latency_ms"], 12.5)
        self.assertEqual(set(m), set(hc.PEER_METRIC_KEYS))

    def test_latency_from_ping_fn_only_when_resolved(self):
        calls = []

        def ping_fn():
            calls.append(1)
            return 7.0

        m = hc.peer_metrics(self._resolved(), True, "p", ping_fn=ping_fn)
        self.assertEqual(m["latency_ms"], 7.0)
        self.assertEqual(calls, [1])                                  # invoked exactly once

    def test_ping_fn_not_invoked_when_unresolved(self):
        calls = []

        def ping_fn():
            calls.append(1)
            return 7.0

        m = hc.peer_metrics(self._resolved(), True, "does-not-exist", ping_fn=ping_fn)
        self.assertTrue(all(v is None for v in m.values()))          # null-filled sentinel
        self.assertEqual(calls, [])                                  # never ping a phantom label

    def test_latency_dropped_on_null_fill_even_if_supplied(self):
        # Q6: null-filled objects stay all-null even when a latency is supplied,
        # because latency is a per-peer metric (no peer -> no peer latency).
        self.assertTrue(all(v is None for v in hc.peer_metrics({}, False, "x", latency_ms=12.3).values()))
        m = hc.peer_metrics(self._resolved(), True, "does-not-exist", latency_ms=12.3)
        self.assertEqual(set(m), set(hc.PEER_METRIC_KEYS))
        self.assertTrue(all(v is None for v in m.values()))

    def test_latency_validator(self):
        for bad in (True, False, float("nan"), float("inf"), -1.0, -5, "12", None, 10 ** 400):
            with self.subTest(bad=repr(bad)):
                self.assertIsNone(hc._latency_or_none(bad))
        self.assertEqual(hc._latency_or_none(0), 0.0)
        neg_zero = hc._latency_or_none(-0.0)                          # -0.0 normalized to +0.0
        self.assertEqual(neg_zero, 0.0)
        self.assertEqual(math.copysign(1.0, neg_zero), 1.0)          # actually checks the sign (== can't)
        self.assertEqual(hc._latency_or_none(12), 12.0)
        self.assertEqual(hc._latency_or_none(12.5), 12.5)

    def test_latency_validator_adversarial_float_subclass(self):
        # C7: normalization to a builtin float defeats a __lt__ lie, and a
        # __float__ that raises degrades to null instead of propagating.
        class LyingFloat(float):
            def __lt__(self, other):  # pragma: no cover - never consulted after normalize
                return False

        class RaisingFloat(float):
            def __float__(self):
                raise RuntimeError("adversarial conversion")

        self.assertIsNone(hc._latency_or_none(LyingFloat(-5.0)))     # normalized -5.0 < 0 -> None
        self.assertIsNone(hc._latency_or_none(RaisingFloat(7.0)))    # raise -> None, never propagates

    def test_raising_ping_fn_degrades_to_null_object(self):
        def ping_fn():
            raise RuntimeError("ping boom")

        m = hc._safe_peer_metrics(self._resolved(), True, "p", ping_fn=ping_fn)
        self.assertEqual(set(m), set(hc.PEER_METRIC_KEYS))
        self.assertTrue(all(v is None for v in m.values()))         # non-gating: raise -> null

    def test_malformed_tailscaleips_does_not_crash_resolution(self):
        # Hardening (C2): a non-list TailscaleIPs is treated as empty rather than
        # crashing resolution. The peer still resolves by HostName, so the object is
        # well-formed and the ping (reached only for a resolved peer) runs.
        calls = []

        def ping_fn():
            calls.append(1)
            return 5.0

        bad_status = {"BackendState": "Running", "Self": {"ID": "self"},
                      "Peer": {"k": {"ID": "n1", "HostName": "p", "TailscaleIPs": 7}}}
        m = hc._safe_peer_metrics(bad_status, True, "p", ping_fn=ping_fn)
        self.assertEqual(set(m), set(hc.PEER_METRIC_KEYS))          # no crash, well-formed
        self.assertEqual(m["latency_ms"], 5.0)                     # resolved by name -> ping ran
        self.assertEqual(calls, [1])
        # A malformed status whose label matches nothing still null-fills safely.
        m2 = hc._safe_peer_metrics(bad_status, True, "does-not-exist")
        self.assertTrue(all(v is None for v in m2.values()))

    def test_peer_metrics_cli_ping_flag(self):
        # peer-metrics --ping resolves first, then pings only the resolved peer.
        with tempfile.TemporaryDirectory() as tmp:
            fixture = Path(tmp) / "status.json"
            fixture.write_text(json.dumps(self._resolved()), encoding="utf-8")
            # resolved + --ping -> latency from mocked ping
            with mock.patch.object(hc, "tailscale_ping", return_value=hc.ProbeResult("p", True, rtt_ms=9.0)) as spy:
                args = argparse.Namespace(node="p", status_json_file=str(fixture), json=False, ping=True, ping_timeout=5.0)
                buf = io.StringIO()
                with contextlib.redirect_stdout(buf):
                    rc = hc.cmd_peer_metrics(args)
            self.assertEqual(rc, 0)
            self.assertEqual(json.loads(buf.getvalue())["latency_ms"], 9.0)
            self.assertEqual(spy.call_count, 1)
            # default (no --ping) -> no ping, latency null
            with mock.patch.object(hc, "tailscale_ping", return_value=hc.ProbeResult("p", True, rtt_ms=9.0)) as spy2:
                args = argparse.Namespace(node="p", status_json_file=str(fixture), json=False, ping=False, ping_timeout=5.0)
                buf = io.StringIO()
                with contextlib.redirect_stdout(buf):
                    hc.cmd_peer_metrics(args)
            self.assertIsNone(json.loads(buf.getvalue())["latency_ms"])
            self.assertEqual(spy2.call_count, 0)                    # default is side-effect-free
            # unresolved + --ping -> null object, ping NOT called (resolve-first)
            with mock.patch.object(hc, "tailscale_ping", return_value=hc.ProbeResult("x", True, rtt_ms=9.0)) as spy3:
                args = argparse.Namespace(node="missing", status_json_file=str(fixture), json=False, ping=True, ping_timeout=5.0)
                buf = io.StringIO()
                with contextlib.redirect_stdout(buf):
                    hc.cmd_peer_metrics(args)
            self.assertIsNone(json.loads(buf.getvalue())["latency_ms"])
            self.assertEqual(spy3.call_count, 0)

    def test_bad_ping_timeout_env_does_not_break_default_path(self):
        # An invalid PING_TIMEOUT in the environment must not break the default
        # (no --ping) path -- peer-metrics ignored PING_TIMEOUT entirely before the
        # --ping flag existed, so it must stay a clean exit-0 JSON print.
        with tempfile.TemporaryDirectory() as tmp:
            fixture = Path(tmp) / "status.json"
            fixture.write_text(json.dumps(self._resolved()), encoding="utf-8")
            with mock.patch.dict(os.environ, {"PING_TIMEOUT": "oops"}, clear=False):
                buf = io.StringIO()
                with contextlib.redirect_stdout(buf):
                    rc = hc.main(["peer-metrics", "--node", "p", "--status-json-file", str(fixture)])
                self.assertEqual(rc, 0)                              # not argparse-exit 2
                self.assertEqual(set(json.loads(buf.getvalue())), set(hc.PEER_METRIC_KEYS))
                # with --ping, a bad env value falls back to the default timeout.
                with mock.patch.object(hc, "tailscale_ping", return_value=hc.ProbeResult("p", True, rtt_ms=4.0)) as spy:
                    buf = io.StringIO()
                    with contextlib.redirect_stdout(buf):
                        rc = hc.main(["peer-metrics", "--node", "p", "--status-json-file", str(fixture), "--ping"])
                self.assertEqual(rc, 0)
                self.assertEqual(json.loads(buf.getvalue())["latency_ms"], 4.0)
                self.assertEqual(spy.call_args.args[1], hc.DEFAULT_PING_TIMEOUT)

    def test_invalid_utf8_status_file_is_unavailable_not_crash(self):
        # A status file that is not valid UTF-8 must degrade to the null object
        # (get_status fails closed), preserving peer-metrics' always-exits-0 contract.
        with tempfile.TemporaryDirectory() as tmp:
            fixture = Path(tmp) / "status.json"
            fixture.write_bytes(b"\xff\xfe not utf-8 \x80\x81")
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = hc.main(["peer-metrics", "--node", "p", "--status-json-file", str(fixture)])
            self.assertEqual(rc, 0)
            obj = json.loads(buf.getvalue())
            self.assertEqual(set(obj), set(hc.PEER_METRIC_KEYS))
            self.assertTrue(all(v is None for v in obj.values()))


class ConnectorsCliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(self._cleanup)
        fake = self.tmp / "tailscale"
        fake.write_text(FAKE_TAILSCALE, encoding="utf-8")
        fake.chmod(fake.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
        self._set_env("TAILSCALE_BIN", str(fake))
        self._unset_env("TAILSCALE_API_KEY")
        self.status_file = self.tmp / "status.json"
        self.status_file.write_text(json.dumps(CONN_STATUS), encoding="utf-8")
        self.devices_file = self.tmp / "devices.json"
        self.devices_file.write_text(json.dumps(DEVICES_PRIMARY_OLDER), encoding="utf-8")

    def _cleanup(self):
        for child in sorted(self.tmp.rglob("*"), reverse=True):
            if child.is_file():
                child.unlink()
            else:
                child.rmdir()
        self.tmp.rmdir()

    def _set_env(self, key, value):
        old = os.environ.get(key)
        os.environ[key] = value
        self.addCleanup(lambda: os.environ.__setitem__(key, old) if old is not None else os.environ.pop(key, None))

    def _unset_env(self, key):
        old = os.environ.pop(key, None)
        if old is not None:
            self.addCleanup(lambda: os.environ.__setitem__(key, old))

    def _args(self, *extra):
        return [
            "connectors",
            "--primary", "primary-vps",
            "--fallback", "fallback-vps",
            "--status-json-file", str(self.status_file),
            *extra,
        ]

    def test_healthy_with_primary_oldest(self):
        rc, out = run_cli(self._args("--devices-json-file", str(self.devices_file)))
        self.assertEqual(rc, 0)
        self.assertIn("overall=healthy", out)
        self.assertIn("ordering=primary_is_oldest", out)

    def test_json_connectors_include_metrics_object(self):
        # Additive: every connector row gains a `metrics` object with the full key
        # set; existing keys and the overall verdict are unchanged.
        rc, out = run_cli(self._args("--json"))
        report = json.loads(out)
        self.assertIn("overall", report)
        self.assertEqual(len(report["connectors"]), 2)
        for row in report["connectors"]:
            self.assertIn("online", row)          # existing key unchanged
            self.assertIn("metrics", row)         # new additive key
            self.assertEqual(set(row["metrics"]), set(hc.PEER_METRIC_KEYS))

    def test_json_key_sets_exact_no_internal_leak(self):
        # Guards additive-safety: internal resolution/identity data must NOT leak into
        # the JSON rows, so assert the EXACT key sets (not just presence).
        rc, out = run_cli(self._args("--json"))
        report = json.loads(out)
        self.assertEqual(
            set(report),
            {"schema_version", "connectors", "ordering", "ordering_reason", "routes_serving", "overall"},
        )
        for row in report["connectors"]:
            self.assertEqual(
                set(row), {"connector", "label", "online", "reachable", "rtt_ms", "routes", "metrics"}
            )

    def test_text_connectors_append_metrics_line(self):
        # Text mode is append-only: a new [metrics] line per connector; the
        # existing connector= line is not reworded/reordered.
        rc, out = run_cli(self._args())
        self.assertIn("connector=primary label=primary-vps", out)   # existing line intact
        self.assertIn("[metrics] connector=primary tx=", out)
        self.assertIn("[metrics] connector=fallback tx=", out)
        self.assertIn("path=", out)

    def test_metrics_line_appends_latency_byte_exact(self):
        # PR A: latency_ms is appended at the END of the existing [metrics] line
        # (no reword/reorder). assertEqual on the WHOLE line so a token appended
        # AFTER latency_ms (or any reorder) is caught (fake tailscale pings "in 12ms").
        rc, out = run_cli(self._args())
        lines = {
            line.split(" ", 2)[1].split("=", 1)[1]: line
            for line in out.splitlines() if line.startswith("[metrics] connector=")
        }
        self.assertEqual(
            lines["primary"], "[metrics] connector=primary tx=- rx=- path=derp handshake_age=- latency_ms=12.0"
        )
        self.assertEqual(
            lines["fallback"], "[metrics] connector=fallback tx=- rx=- path=derp handshake_age=- latency_ms=12.0"
        )

    def test_json_metrics_latency_from_reachability_ping(self):
        # latency_ms reuses the reachability ping (12ms) and is stamped on the
        # resolved connectors; the top-level rtt_ms is unchanged too.
        rc, out = run_cli(self._args("--json"))
        report = json.loads(out)
        for row in report["connectors"]:
            self.assertEqual(row["metrics"]["latency_ms"], 12.0)
            self.assertEqual(row["rtt_ms"], 12.0)                    # existing field intact

    def test_connectors_ping_exactly_once_per_connector(self):
        # No double-ping AND exactly one ping per connector (primary once, fallback
        # once) -- a total count of 2 alone would pass if primary were pinged twice.
        with mock.patch.object(hc, "tailscale_ping", wraps=hc.tailscale_ping) as spy:
            run_cli(self._args("--json"))
        pinged = sorted(call.args[0] for call in spy.call_args_list)
        self.assertEqual(pinged, ["fallback-vps", "primary-vps"])

    def test_connectors_survives_malformed_status(self):
        # C2 + hardening: a malformed status must degrade cleanly (no traceback) AND must
        # never manufacture a false-healthy verdict. The dangerous shape is an AllowedIPs
        # carrying the node's OWN /32 with a TailscaleIPs we cannot validate (non-list,
        # an invalid-IP string, or empty): the own-host exclusion is then untrustworthy,
        # so node_routes must fail closed (routes null) rather than count the own address
        # as an advertised route (GPT-5.6-sol Blocker 2 / Codex #1).
        peer_variants = [
            {"TailscaleIPs": 7},                                                 # non-list, no routes
            {"TailscaleIPs": 7, "AllowedIPs": ["100.64.0.1/32"]},                # non-list self
            {"TailscaleIPs": ["not-an-ip"], "AllowedIPs": ["100.64.0.1/32"]},    # invalid-IP self string
            {"TailscaleIPs": [], "AllowedIPs": ["100.64.0.1/32"]},               # empty self set
        ]
        for extra in peer_variants:
            with self.subTest(extra=extra):
                status = {"BackendState": "Running", "Self": {"ID": "self"},
                          "Peer": {"k": {"ID": "n1", "HostName": "primary-vps", **extra}}}
                self.status_file.write_text(json.dumps(status), encoding="utf-8")
                rc, out = run_cli(self._args("--json"))
                report = json.loads(out)
                self.assertEqual(len(report["connectors"]), 2)       # clean report, not a crash
                # the own /32 is never miscounted as an advertised route (mutation-sensitive:
                # pre-fix the malformed self-IP left the own /32 counted, i.e. routes == 1).
                self.assertNotIn(1, [c["routes"] for c in report["connectors"]])
                # (overall is degraded here too, but only weakly -- the fallback peer is
                # absent, so reachability already fails; the false-healthy CONTRACT with
                # both connectors reachable is locked by the next test, not this one.)

    def test_connectors_malformed_self_ip_is_not_false_healthy(self):
        # Non-tautological false-healthy lock (GPT-5.6-sol Minor): BOTH connectors are
        # online AND reachable, so reachability cannot drive the verdict -- only routes
        # do. The primary has an invalid TailscaleIPs alongside an own-/32 AllowedIPs;
        # pre-fix its own /32 was counted as a served route -> overall "healthy", exit 0.
        # Post-fix node_routes fails closed (routes null) -> nothing serving -> "degraded".
        status = {
            "BackendState": "Running",
            "Self": {"ID": "selfID", "HostName": "client", "TailscaleIPs": ["100.64.0.5"]},
            "Peer": {
                "nodeP": {"ID": "nodeP", "HostName": "primary-vps", "TailscaleIPs": ["not-an-ip"],
                          "Online": True, "AllowedIPs": ["100.64.0.1/32"]},
                "nodeF": {"ID": "nodeF", "HostName": "fallback-vps", "TailscaleIPs": ["100.64.0.2"],
                          "Online": True},
            },
        }
        self.status_file.write_text(json.dumps(status), encoding="utf-8")
        rc, out = run_cli(self._args("--json"))
        report = json.loads(out)
        primary = next(c for c in report["connectors"] if c["connector"] == "primary")
        self.assertIsNone(primary["routes"])                 # own /32 never counted (pre-fix: 1)
        self.assertEqual(report["routes_serving"], "none")
        self.assertEqual(report["overall"], "degraded")      # driven by routes, not reachability
        self.assertEqual(rc, 1)                              # exit reflects degraded

    def test_connectors_ambiguous_label_warns_once(self):
        # C4: an ambiguous label produces exactly ONE ambiguity warning (cmd_connectors
        # resolves once and threads the node into metrics extraction; before the fix
        # peer_metrics re-resolved and warned a second time).
        status = {"BackendState": "Running", "Self": {"ID": "self", "HostName": "self"},
                  "Peer": {"a": {"ID": "n1", "HostName": "dup", "TailscaleIPs": ["100.64.0.1"]},
                           "b": {"ID": "n2", "HostName": "dup", "TailscaleIPs": ["100.64.0.2"]}}}
        self.status_file.write_text(json.dumps(status), encoding="utf-8")
        buf_err = io.StringIO()
        with contextlib.redirect_stderr(buf_err):
            run_cli(["connectors", "--primary", "dup", "--fallback", "fallback-vps",
                     "--status-json-file", str(self.status_file)])
        self.assertEqual(buf_err.getvalue().count("is ambiguous"), 1)   # exactly one, not two

    def test_metrics_failure_is_non_gating(self):
        # A raising peer_metrics must NOT change the verdict/exit or existing
        # output; metrics degrade to the null-filled object (hard non-gating rule).
        with mock.patch.object(hc, "peer_metrics", side_effect=RuntimeError("boom")):
            rc, out = run_cli(self._args("--json", "--devices-json-file", str(self.devices_file)))
        report = json.loads(out)
        self.assertEqual(rc, 0)                        # verdict/exit unchanged
        self.assertEqual(report["overall"], "healthy")
        for row in report["connectors"]:
            self.assertEqual(set(row["metrics"]), set(hc.PEER_METRIC_KEYS))
            self.assertTrue(all(v is None for v in row["metrics"].values()))
        # Text mode shares the attach path: existing lines survive, metrics -> `-`.
        with mock.patch.object(hc, "peer_metrics", side_effect=RuntimeError("boom")):
            rc_text, out_text = run_cli(self._args("--devices-json-file", str(self.devices_file)))
        self.assertEqual(rc_text, 0)
        self.assertIn("connector=primary label=primary-vps", out_text)
        self.assertIn("[metrics] connector=primary tx=-", out_text)

    def test_json_schema_version_unchanged_by_metrics(self):
        rc, out = run_cli(self._args("--json"))
        self.assertEqual(json.loads(out)["schema_version"], hc.REPORT_SCHEMA_VERSION)

    def test_degraded_when_offline(self):
        status = json.loads(json.dumps(CONN_STATUS))
        status["Peer"]["nodeP"]["Online"] = False
        self.status_file.write_text(json.dumps(status), encoding="utf-8")
        rc, out = run_cli(self._args())
        self.assertEqual(rc, 1)
        self.assertIn("overall=degraded", out)

    def test_degraded_when_unreachable(self):
        self._set_env("FAKE_UNREACHABLE", "fallback-vps")
        rc, out = run_cli(self._args())
        self.assertEqual(rc, 1)
        self.assertIn("overall=degraded", out)

    def test_ordering_unavailable_without_token_or_devices(self):
        rc, out = run_cli(self._args())
        self.assertEqual(rc, 0)
        self.assertIn("ordering=unavailable", out)

    def test_json_output(self):
        rc, out = run_cli(self._args("--devices-json-file", str(self.devices_file), "--json"))
        payload = json.loads(out)
        self.assertEqual(payload["overall"], "healthy")
        self.assertEqual(len(payload["connectors"]), 2)
        self.assertEqual(payload["ordering"], "primary_is_oldest")
        self.assertEqual(payload["routes_serving"], "primary")

    def test_degraded_when_no_connector_serves_routes(self):
        status = json.loads(json.dumps(CONN_STATUS))
        status["Peer"]["nodeP"].pop("PrimaryRoutes", None)
        self.status_file.write_text(json.dumps(status), encoding="utf-8")
        rc, out = run_cli(self._args())
        self.assertEqual(rc, 1)
        self.assertIn("routes_serving=none", out)
        self.assertIn("overall=degraded", out)

    def test_require_routes_zero_ignores_missing_routes(self):
        status = json.loads(json.dumps(CONN_STATUS))
        status["Peer"]["nodeP"].pop("PrimaryRoutes", None)
        self.status_file.write_text(json.dumps(status), encoding="utf-8")
        rc, out = run_cli(self._args("--require-routes", "0"))
        self.assertEqual(rc, 0)
        self.assertIn("overall=healthy", out)

    def test_degraded_when_only_default_routes(self):
        status = json.loads(json.dumps(CONN_STATUS))
        status["Peer"]["nodeP"]["PrimaryRoutes"] = ["0.0.0.0/0", "::/0"]
        self.status_file.write_text(json.dumps(status), encoding="utf-8")
        rc, out = run_cli(self._args())
        self.assertEqual(rc, 1)
        self.assertIn("routes_serving=none", out)
        self.assertIn("overall=degraded", out)

    def test_degraded_when_backend_not_running(self):
        status = json.loads(json.dumps(CONN_STATUS))
        status["BackendState"] = "Stopped"
        self.status_file.write_text(json.dumps(status), encoding="utf-8")
        rc, out = run_cli(self._args("--devices-json-file", str(self.devices_file)))
        self.assertEqual(rc, 1)
        self.assertIn("overall=degraded", out)

    def test_degraded_when_backend_missing(self):
        # Missing BackendState is malformed -> fail closed (degraded), not healthy.
        status = json.loads(json.dumps(CONN_STATUS))
        status.pop("BackendState", None)
        self.status_file.write_text(json.dumps(status), encoding="utf-8")
        rc, out = run_cli(self._args("--devices-json-file", str(self.devices_file)))
        self.assertEqual(rc, 1)
        self.assertIn("overall=degraded", out)


class EnvNumberTests(unittest.TestCase):
    def setUp(self):
        self.addCleanup(lambda: os.environ.pop("HC_TEST_NUM", None))

    def _env_float(self, value):
        os.environ["HC_TEST_NUM"] = value
        return hc.env_float("HC_TEST_NUM", 15.0)

    def test_accepts_valid(self):
        self.assertEqual(self._env_float("3.5"), 3.5)
        self.assertEqual(self._env_float("0"), 0.0)

    def test_rejects_nan(self):
        self.assertEqual(self._env_float("nan"), 15.0)

    def test_rejects_inf(self):
        self.assertEqual(self._env_float("inf"), 15.0)
        self.assertEqual(self._env_float("-inf"), 15.0)

    def test_rejects_garbage(self):
        self.assertEqual(self._env_float("oops"), 15.0)

    def test_unset_returns_default(self):
        os.environ.pop("HC_TEST_NUM", None)
        self.assertEqual(hc.env_float("HC_TEST_NUM", 15.0), 15.0)


class NumericArgTypeTests(unittest.TestCase):
    def test_pos_float_accepts_positive(self):
        self.assertEqual(hc._pos_float("5"), 5.0)

    def test_pos_float_rejects_zero_dot_and_nonfinite(self):
        for bad in ("0", ".", "nan", "inf", "-inf", "-1", "oops"):
            with self.subTest(bad=bad), self.assertRaises(argparse.ArgumentTypeError):
                hc._pos_float(bad)

    def test_nonneg_float_accepts_zero(self):
        self.assertEqual(hc._nonneg_float("0"), 0.0)

    def test_nonneg_float_rejects_negative_and_nonfinite(self):
        for bad in ("-1", "nan", "inf"):
            with self.subTest(bad=bad), self.assertRaises(argparse.ArgumentTypeError):
                hc._nonneg_float(bad)

    def test_pos_int_accepts_one(self):
        self.assertEqual(hc._pos_int("1"), 1)

    def test_pos_int_rejects_zero_negative_and_floats(self):
        for bad in ("0", "-3", "3.5", "nan"):
            with self.subTest(bad=bad), self.assertRaises(argparse.ArgumentTypeError):
                hc._pos_int(bad)

    def test_finite_floats_accept_upper_bound(self):
        cap = str(int(hc.MAX_TIMEOUT_SECONDS))
        self.assertEqual(hc._pos_float(cap), hc.MAX_TIMEOUT_SECONDS)
        self.assertEqual(hc._nonneg_float(cap), hc.MAX_TIMEOUT_SECONDS)

    def test_finite_floats_reject_oversized(self):
        huge = "1" + "0" * 120  # 121-digit number: finite but absurd
        for fn in (hc._pos_float, hc._nonneg_float):
            with self.subTest(fn=fn.__name__), self.assertRaises(argparse.ArgumentTypeError):
                fn(huge)

    def test_bool01_accepts_0_and_1(self):
        self.assertEqual(hc._bool01("0"), 0)
        self.assertEqual(hc._bool01("1"), 1)

    def test_bool01_rejects_other(self):
        for bad in ("2", "-1", "", "x", "1.0"):
            with self.subTest(bad=bad), self.assertRaises(argparse.ArgumentTypeError):
                hc._bool01(bad)

    def test_verdict_cli_rejects_nonfinite_cooldown(self):
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf), self.assertRaises(SystemExit) as ctx:
            hc.build_parser().parse_args([
                "verdict", "--state-file", "/tmp/x", "--primary", "p", "--fallback", "f",
                "--cooldown", "inf",
            ])
        self.assertEqual(ctx.exception.code, 2)
        self.assertIn("cooldown", buf.getvalue())


class EnvDefaultValidationTests(unittest.TestCase):
    """Environment-variable defaults must be validated by argparse exactly like
    command-line values (regression: a numeric default bypassed ``type``, and
    ``choices`` never validates a string default at all)."""

    VERDICT = ["verdict", "--state-file", "/tmp/x", "--primary", "p", "--fallback", "f"]
    CONNECTORS = ["connectors", "--primary", "p", "--fallback", "f"]

    def _set(self, name, value):
        old = os.environ.get(name)
        os.environ[name] = value
        self.addCleanup(lambda: os.environ.__setitem__(name, old) if old is not None else os.environ.pop(name, None))

    def _expect_reject(self, name, value, argv):
        self._set(name, value)
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as ctx:
            hc.build_parser().parse_args(argv)
        self.assertEqual(ctx.exception.code, 2)

    def test_negative_ping_timeout_rejected(self):
        self._expect_reject("PING_TIMEOUT", "-1", self.VERDICT)

    def test_zero_http_timeout_rejected(self):
        self._expect_reject("PROBE_HTTP_TIMEOUT", "0", self.VERDICT)

    def test_zero_fail_threshold_rejected(self):
        self._expect_reject("FAIL_THRESHOLD", "0", self.VERDICT)

    def test_negative_ok_threshold_rejected(self):
        self._expect_reject("OK_THRESHOLD", "-2", self.VERDICT)

    def test_negative_cooldown_rejected(self):
        self._expect_reject("COOLDOWN", "-1", self.VERDICT)

    def test_bad_restore_primary_rejected(self):
        self._expect_reject("RESTORE_PRIMARY", "2", self.VERDICT)

    def test_bad_require_routes_rejected(self):
        self._expect_reject("REQUIRE_ROUTES", "2", self.CONNECTORS)

    def test_oversized_ping_timeout_rejected(self):
        self._expect_reject("PING_TIMEOUT", "1" + "0" * 120, self.VERDICT)

    def test_valid_env_defaults_are_accepted_and_typed(self):
        self._set("PING_TIMEOUT", "7")
        self._set("COOLDOWN", "0")
        self._set("FAIL_THRESHOLD", "2")
        self._set("RESTORE_PRIMARY", "0")
        ns = hc.build_parser().parse_args(self.VERDICT)
        self.assertEqual(ns.ping_timeout, 7.0)
        self.assertEqual(ns.cooldown, 0.0)
        self.assertEqual(ns.fail_threshold, 2)
        self.assertEqual(ns.restore_primary, 0)


class StatusTimeoutTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(self._cleanup)
        fake = self.tmp / "tailscale"
        fake.write_text("#!/usr/bin/env bash\necho '{\"BackendState\": \"Running\"}'\n", encoding="utf-8")
        fake.chmod(fake.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
        old = os.environ.get("TAILSCALE_BIN")
        os.environ["TAILSCALE_BIN"] = str(fake)
        self.addCleanup(lambda: os.environ.__setitem__("TAILSCALE_BIN", old) if old is not None else os.environ.pop("TAILSCALE_BIN", None))

    def _cleanup(self):
        for child in sorted(self.tmp.rglob("*"), reverse=True):
            child.unlink() if child.is_file() else child.rmdir()
        self.tmp.rmdir()

    def test_get_status_survives_malformed_or_oversized_timeout(self):
        # Regression: a malformed, non-positive, OR oversized-finite
        # TAILSCALE_STATUS_TIMEOUT must not raise a ValueError/OverflowError
        # inside subprocess.run(timeout=...). The fake binary responds instantly,
        # so a clamped timeout still yields a valid status (available=True);
        # if the clamp regressed, an oversized value would raise -> caught ->
        # available=False, failing this assertion.
        self.addCleanup(lambda: os.environ.pop("TAILSCALE_STATUS_TIMEOUT", None))
        for bad in ("inf", "nan", "0", "-1", "oops", "86401", "1000000000", "1e120", "1" + "0" * 120):
            with self.subTest(bad=bad):
                os.environ["TAILSCALE_STATUS_TIMEOUT"] = bad
                status, available = hc.get_status(None)
                self.assertTrue(available)
                self.assertEqual(status.get("BackendState"), "Running")

    def test_oversized_finite_timeout_is_clamped(self):
        # The effective timeout handed to subprocess.run stays within
        # (0, MAX_TIMEOUT_SECONDS] for huge finite env values (so a hung
        # `tailscale status` cannot hold the controller lock for a day).
        self.addCleanup(lambda: os.environ.pop("TAILSCALE_STATUS_TIMEOUT", None))
        captured = {}

        def fake_run(cmd, **kwargs):
            captured["timeout"] = kwargs.get("timeout")
            return subprocess.CompletedProcess(cmd, 0, '{"BackendState": "Running"}', "")

        for value in ("86401", "1000000000", "1e120", "1" + "0" * 120):
            with self.subTest(value=value):
                os.environ["TAILSCALE_STATUS_TIMEOUT"] = value
                with mock.patch.object(hc.subprocess, "run", side_effect=fake_run):
                    status, available = hc.get_status(None)
                self.assertTrue(available)
                self.assertIsNotNone(captured["timeout"])
                self.assertGreater(captured["timeout"], 0)
                self.assertLessEqual(captured["timeout"], hc.MAX_TIMEOUT_SECONDS)

    def test_valid_timeout_passed_through_unchanged(self):
        # Main-flow guarantee: an in-bound value (including the 86400 boundary) is
        # used verbatim -- the clamp must not mangle legitimate configuration.
        self.addCleanup(lambda: os.environ.pop("TAILSCALE_STATUS_TIMEOUT", None))
        captured = {}

        def fake_run(cmd, **kwargs):
            captured["timeout"] = kwargs.get("timeout")
            return subprocess.CompletedProcess(cmd, 0, '{"BackendState": "Running"}', "")

        for value, expected in (("30", 30.0), (str(int(hc.MAX_TIMEOUT_SECONDS)), hc.MAX_TIMEOUT_SECONDS)):
            with self.subTest(value=value):
                os.environ["TAILSCALE_STATUS_TIMEOUT"] = value
                with mock.patch.object(hc.subprocess, "run", side_effect=fake_run):
                    hc.get_status(None)
                self.assertEqual(captured["timeout"], expected)


def _parse_prom(text):
    """Strict-enough Prometheus text parser used to validate the emitter. Enforces:
    trailing newline; HELP/TYPE at most once per family; samples only after their
    family's TYPE and contiguously (no family reappearance, no orphan metadata);
    every sample value numeric. Returns {name: [(labels_str, value_str), ...]}."""
    assert text.endswith("\n"), "document must end with a trailing newline"
    families: dict = {}
    help_seen: set = set()
    type_seen: dict = {}
    order: list = []
    current = None
    for line in text.split("\n"):
        if not line:
            continue
        if line.startswith("# HELP "):
            name = line[len("# HELP "):].split(" ", 1)[0]
            assert name not in help_seen, f"duplicate HELP for {name}"
            help_seen.add(name)
            continue
        if line.startswith("# TYPE "):
            _, _, name, kind = line.split(" ", 3)
            assert name not in type_seen, f"duplicate TYPE for {name}"
            assert name not in order, f"family {name} reappears"
            assert kind in ("gauge", "counter"), f"unexpected TYPE {kind!r} for {name}"
            type_seen[name] = kind
            order.append(name)
            families[name] = []
            current = name
            continue
        assert not line.startswith("#"), f"unexpected comment line: {line!r}"
        if "{" in line:
            name = line[: line.index("{")]
            labels = line[line.index("{"): line.rindex("}") + 1]
            value = line[line.rindex("}") + 1:].strip()
        else:
            name, value = line.split(" ", 1)
            labels, value = "", value.strip()
        assert name in type_seen, f"sample for {name} before its TYPE"
        assert name == current, f"sample {name} not contiguous with its family (current {current})"
        num = float(value)  # numeric-only (raises otherwise)
        assert math.isfinite(num), f"non-finite sample value {value!r} for {name}"
        families[name].append((labels, value))
    assert help_seen == set(type_seen), f"every family needs one HELP and one TYPE; mismatch: {help_seen ^ set(type_seen)}"
    return families, type_seen


class PrometheusTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self):
        for child in sorted(self.tmp.rglob("*"), reverse=True):
            child.rmdir() if child.is_dir() else child.unlink()
        self.tmp.rmdir()

    def _status(self, primary_extra=None, fallback_extra=None):
        p = {"ID": "nodeP", "HostName": "primary-vps", "TailscaleIPs": ["100.64.0.1"], "Online": True,
             "CurAddr": "1.2.3.4:41641", "TxBytes": 100, "RxBytes": 200, "PrimaryRoutes": ["10.0.0.0/24"]}
        f = {"ID": "nodeF", "HostName": "fallback-vps", "TailscaleIPs": ["100.64.0.2"], "Online": True}
        if primary_extra:
            p.update(primary_extra)
        if fallback_extra:
            f.update(fallback_extra)
        return {"BackendState": "Running", "Self": {"ID": "self", "HostName": "c", "TailscaleIPs": ["100.64.0.9"]},
                "Peer": {"nodeP": p, "nodeF": f}}

    def _emit(self, status, *, primary="primary-vps", fallback="fallback-vps", rtt=12.0, require_routes="1"):
        fixture = self.tmp / "status.json"
        fixture.write_text(json.dumps(status), encoding="utf-8")
        with mock.patch.object(hc, "tailscale_ping", return_value=hc.ProbeResult("x", True, rtt_ms=rtt)):
            return run_cli([
                "connectors", "--primary", primary, "--fallback", fallback,
                "--status-json-file", str(fixture), "--require-routes", require_routes, "--prometheus",
            ])

    def test_valid_document_and_sentinel_last(self):
        rc, out = self._emit(self._status())
        fams, types = _parse_prom(out)  # raises on any structural violation
        self.assertEqual(len(fams["ai_egress_overall_healthy"]), 1)   # exactly one sentinel
        last = [line for line in out.split("\n") if line][-1]
        self.assertRegex(last, r"^ai_egress_overall_healthy [01]$")   # sentinel is EXACTLY the last line
        self.assertEqual(types["ai_egress_connector_tx_bytes_total"], "counter")
        self.assertEqual(types["ai_egress_connector_rx_bytes_total"], "counter")
        self.assertEqual(types["ai_egress_connector_online"], "gauge")
        self.assertEqual(rc, 0)                                       # healthy -> 0

    def test_non_finite_rtt_omitted(self):
        for bad in (float("nan"), float("inf"), -1.0):
            with self.subTest(rtt=bad):
                _, out = self._emit(self._status(), rtt=bad)
                fams, _ = _parse_prom(out)   # would raise if a non-finite value slipped in
                self.assertEqual(fams.get("ai_egress_connector_latency_ms", []), [])

    def test_empty_rows_document_is_just_sentinel(self):
        # All samples omitted / no connectors -> the document is still valid and
        # ends with exactly the sentinel (the write-completeness guarantee).
        doc = hc._prometheus_document([], True)
        self.assertEqual(
            doc,
            "# HELP ai_egress_overall_healthy Connector-pair health: 1 healthy, 0 degraded.\n"
            "# TYPE ai_egress_overall_healthy gauge\n"
            "ai_egress_overall_healthy 1\n",
        )
        _parse_prom(doc)  # still structurally valid

    def test_route_noncanonical_default_gauge_and_health_agree(self):
        # After the hardening, node_routes is strict for BOTH the gauge and the health
        # verdict: "0.0.0.1/0" canonicalizes to the default route -> 0 non-default
        # routes -> gauge 0 AND (neither connector serves) overall_healthy 0. The
        # earlier PR-C strict-gauge-vs-lenient-health divergence is gone.
        _, out = self._emit(self._status(primary_extra={"PrimaryRoutes": ["0.0.0.1/0"]}))
        fams, _ = _parse_prom(out)
        prim = [val for lbl, val in fams["ai_egress_connector_routes"] if "primary" in lbl]
        self.assertEqual(prim, ["0"])                                   # strict gauge -> 0
        self.assertEqual(fams["ai_egress_overall_healthy"][0][1], "0")  # health now strict too -> degraded

    def test_route_malformed_self_like_entry_omits_gauge(self):
        # An AllowedIPs entry that resembles a self address but is malformed must
        # omit the gauge (not be silently skipped as "self"). Remove PrimaryRoutes
        # entirely so the AllowedIPs fallback branch is actually taken.
        status = self._status()
        del status["Peer"]["nodeP"]["PrimaryRoutes"]
        status["Peer"]["nodeP"]["AllowedIPs"] = ["100.64.0.1/not-a-prefix"]
        _, out = self._emit(status)
        fams, _ = _parse_prom(out)
        prim = [lbl for lbl, _ in fams.get("ai_egress_connector_routes", []) if "primary" in lbl]
        self.assertEqual(prim, [])

    def test_write_rejects_incomplete_document(self):
        dest = self.tmp / "keep.prom"
        dest.write_text("OLD\n", encoding="utf-8")
        for bad in ("no trailing newline", "partial\n", "ai_egress_overall_healthy 1\ntrailing 2\n",
                    "ai_egress_overall_healthy 1\nai_egress_overall_healthy 0\n"):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    hc._write_textfile_atomic(str(dest), bad)
        self.assertEqual(dest.read_text(encoding="utf-8"), "OLD\n")   # never clobbered

    def test_output_without_prometheus_rejected(self):
        fixture = self.tmp / "s.json"
        fixture.write_text(json.dumps(self._status()), encoding="utf-8")
        dest = self.tmp / "o.prom"
        with mock.patch.object(hc, "tailscale_ping", return_value=hc.ProbeResult("x", True, rtt_ms=1.0)):
            rc = hc.main(["connectors", "--primary", "primary-vps", "--fallback", "fallback-vps",
                          "--status-json-file", str(fixture), "--output", str(dest)])
        self.assertEqual(rc, 2)
        self.assertFalse(dest.exists())

    def test_empty_output_errors_not_silent_stdout(self):
        fixture = self.tmp / "s.json"
        fixture.write_text(json.dumps(self._status()), encoding="utf-8")
        with mock.patch.object(hc, "tailscale_ping", return_value=hc.ProbeResult("x", True, rtt_ms=1.0)):
            rc = hc.main(["connectors", "--primary", "primary-vps", "--fallback", "fallback-vps",
                          "--status-json-file", str(fixture), "--prometheus", "--output", ""])
        self.assertEqual(rc, 1)   # empty path -> write error, never a silent stdout fall-back

    def test_write_rejects_disguised_or_duplicate_sentinel(self):
        # A timestamped or labeled sentinel line (both valid Prometheus grammar) must
        # not slip past the completeness boundary as an invisible extra sample, and
        # rejection must happen BEFORE any temp file is created.
        dest = self.tmp / "keep.prom"
        dest.write_text("OLD\n", encoding="utf-8")
        for bad in (
            "ai_egress_overall_healthy 0 123\nai_egress_overall_healthy 1\n",   # timestamped + bare
            'ai_egress_overall_healthy{connector="x"} 1\n',                     # labeled, not bare
            "ai_egress_overall_healthy 0 1700000000000\n",                     # single timestamped
            "  ai_egress_overall_healthy 0\nai_egress_overall_healthy 1\n",     # leading whitespace + bare
            "ai_egress_overall_healthy\t0\nai_egress_overall_healthy 1\n",      # tab separator + bare
        ):
            with self.subTest(bad=repr(bad)):
                with mock.patch.object(hc.tempfile, "mkstemp") as mkstemp:
                    with self.assertRaises(ValueError):
                        hc._write_textfile_atomic(str(dest), bad)
                    mkstemp.assert_not_called()
        self.assertEqual(dest.read_text(encoding="utf-8"), "OLD\n")

    def test_parser_rejects_non_gauge_counter_type(self):
        # A family declared with a type other than gauge/counter must be rejected even
        # with no sample line. Use a clean sample-less fixture and assert the SPECIFIC
        # message, so the test fails if the TYPE-branch guard is removed (a trailing
        # sample would otherwise trip "sample before its TYPE" regardless of the guard).
        with self.assertRaisesRegex(AssertionError, "unexpected TYPE"):
            _parse_prom("# HELP x d\n# TYPE x histogram\n")

    def test_huge_counter_omitted_not_infinite(self):
        # A float64-unrepresentable TxBytes (adversarial status) must be omitted, not
        # emitted as a sample that scrapes to +Inf.
        _, out = self._emit(self._status(primary_extra={"TxBytes": 10 ** 400}))
        fams, _ = _parse_prom(out)  # would raise on a non-finite sample if one slipped in
        prim = [lbl for lbl, _ in fams.get("ai_egress_connector_tx_bytes_total", []) if "primary" in lbl]
        self.assertEqual(prim, [])

    def test_null_and_negative_counter_omitted(self):
        # primary negative TxBytes + fallback missing TxBytes -> both tx samples omitted
        # (never a negative or fabricated counter).
        _, out = self._emit(self._status(primary_extra={"TxBytes": -5}))
        fams, _ = _parse_prom(out)
        self.assertEqual(fams.get("ai_egress_connector_tx_bytes_total", []), [])

    def test_malformed_routes_omitted(self):
        for bad in ([""], ["not-a-cidr"], "10.0.0.0/24", [123], {"x": 1}):
            with self.subTest(bad=bad):
                _, out = self._emit(self._status(primary_extra={"PrimaryRoutes": bad}))
                fams, _ = _parse_prom(out)
                prim = [lbl for lbl, _ in fams.get("ai_egress_connector_routes", []) if "primary" in lbl]
                self.assertEqual(prim, [], f"malformed routes {bad!r} must omit the gauge")

    def test_valid_routes_counted(self):
        _, out = self._emit(self._status(primary_extra={"PrimaryRoutes": ["10.0.0.0/24", "192.168.0.0/16", "0.0.0.0/0"]}))
        fams, _ = _parse_prom(out)
        prim = [val for lbl, val in fams["ai_egress_connector_routes"] if "primary" in lbl]
        self.assertEqual(prim, ["2"])  # two non-default routes; default 0.0.0.0/0 excluded

    def test_latency_from_rtt_even_when_unresolved(self):
        # An unresolvable connector label still gets its probe RTT as latency (from
        # row.rtt_ms), but peer-derived gauges (online) are omitted -- F-01.
        _, out = self._emit(self._status(), primary="ghost-primary")
        fams, _ = _parse_prom(out)
        lat = [lbl for lbl, _ in fams.get("ai_egress_connector_latency_ms", []) if "ghost-primary" in lbl]
        self.assertEqual(len(lat), 1)
        online = [lbl for lbl, _ in fams.get("ai_egress_connector_online", []) if "ghost-primary" in lbl]
        self.assertEqual(online, [])

    def test_label_escaping(self):
        _, out = self._emit(self._status(), primary='has"q\\b')
        self.assertIn(r'label="has\"q\\b"', out)

    def test_degraded_stdout_exit_1(self):
        # Neither connector serves routes -> degraded -> stdout mode exits 1.
        _, out = self._emit(self._status(primary_extra={"PrimaryRoutes": []}))
        # (still a valid document; only the exit code and the sentinel value change)
        fams, _ = _parse_prom(out)
        self.assertEqual(fams["ai_egress_overall_healthy"][0][1], "0")
        rc, _ = self._emit(self._status(primary_extra={"PrimaryRoutes": []}))
        self.assertEqual(rc, 1)

    # --- atomic writer -----------------------------------------------------
    def test_write_sets_mode_0644_and_trailing_newline(self):
        dest = self.tmp / "m.prom"
        hc._write_textfile_atomic(str(dest), "ai_egress_overall_healthy 1\n")
        self.assertEqual(oct(dest.stat().st_mode & 0o777), "0o644")
        self.assertTrue(dest.read_text(encoding="utf-8").endswith("\n"))

    def test_write_rejects_non_prom_missing_parent_and_dir(self):
        good = "ai_egress_overall_healthy 1\n"   # passes the completeness check
        with self.assertRaises(ValueError):
            hc._write_textfile_atomic(str(self.tmp / "x.txt"), good)        # not .prom
        with self.assertRaises(ValueError):
            hc._write_textfile_atomic(str(self.tmp / "no" / "x.prom"), good)   # missing parent
        adir = self.tmp / "d.prom"
        adir.mkdir()
        with self.assertRaises((IsADirectoryError, OSError)):
            hc._write_textfile_atomic(str(adir), good)                     # dest is a directory

    def test_write_atomic_keeps_old_file_on_failure(self):
        dest = self.tmp / "keep.prom"
        dest.write_text("OLD\n", encoding="utf-8")
        os.chmod(dest, 0o600)
        with mock.patch.object(hc.os, "replace", side_effect=OSError("boom")):
            with self.assertRaises(OSError):
                hc._write_textfile_atomic(str(dest), "ai_egress_overall_healthy 1\n")
        self.assertEqual(dest.read_text(encoding="utf-8"), "OLD\n")         # bytes unchanged
        self.assertEqual(oct(dest.stat().st_mode & 0o777), "0o600")        # mode unchanged
        self.assertEqual(list(self.tmp.glob(".ai-egress-prom.*")), [])     # temp cleaned up

    def test_cmd_output_exit_0_on_write_even_if_degraded(self):
        status = self._status(primary_extra={"PrimaryRoutes": []})          # degraded
        fixture = self.tmp / "s.json"
        fixture.write_text(json.dumps(status), encoding="utf-8")
        dest = self.tmp / "o.prom"
        with mock.patch.object(hc, "tailscale_ping", return_value=hc.ProbeResult("x", True, rtt_ms=5.0)):
            rc = hc.main([
                "connectors", "--primary", "primary-vps", "--fallback", "fallback-vps",
                "--status-json-file", str(fixture), "--prometheus", "--output", str(dest),
            ])
        self.assertEqual(rc, 0)                                             # write ok -> 0 even degraded
        self.assertEqual([line for line in dest.read_text().split("\n") if line][-1], "ai_egress_overall_healthy 0")

    def test_json_and_prometheus_mutually_exclusive(self):
        with self.assertRaises(SystemExit):  # argparse error, either order
            hc.build_parser().parse_args(["connectors", "--json", "--prometheus"])
        with self.assertRaises(SystemExit):
            hc.build_parser().parse_args(["connectors", "--prometheus", "--json"])


class VersionTests(unittest.TestCase):
    def test_module_version_matches_version_file(self):
        self.assertEqual(hc.__version__, VERSION)


if __name__ == "__main__":
    unittest.main()
