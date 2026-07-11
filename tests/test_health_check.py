import argparse
import contextlib
import io
import json
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
        state = hc.default_state("primary-vps", "fallback-vps")
        state["nodes"]["primary"]["last_state"] = primary_state
        state["nodes"]["fallback"]["last_state"] = fallback_state
        state["active"]["last_switch_epoch"] = last_switch
        return state

    def test_primary_healthy_no_action(self):
        state = self._state(hc.STATE_UP, hc.STATE_UP)
        d = hc.evaluate(state, "primary", probe("p", True), probe("f", True), self.th, now=10_000.0)
        self.assertEqual(d.action, "none")
        self.assertEqual(d.reason, "healthy")

    def test_primary_down_switches_to_verified_fallback(self):
        state = self._state(hc.STATE_DOWN, hc.STATE_UP)
        d = hc.evaluate(state, "primary", probe("p", False), probe("f", True), self.th, now=10_000.0)
        self.assertEqual(d.action, "switch-to-fallback")
        self.assertEqual(d.target_role, "fallback")
        self.assertEqual(d.target_label, "fallback-vps")

    def test_primary_down_but_fallback_unverified_does_not_switch(self):
        # Fallback state is still UP, but it failed its ping THIS round.
        state = self._state(hc.STATE_DOWN, hc.STATE_UP)
        d = hc.evaluate(state, "primary", probe("p", False), probe("f", False), self.th, now=10_000.0)
        self.assertEqual(d.action, "none")
        self.assertEqual(d.reason, "fallback_unverified")

    def test_both_down(self):
        state = self._state(hc.STATE_DOWN, hc.STATE_DOWN)
        d = hc.evaluate(state, "primary", probe("p", False), probe("f", False), self.th, now=10_000.0)
        self.assertEqual(d.action, "none")
        self.assertEqual(d.reason, "both_down")

    def test_cooldown_blocks_switch(self):
        state = self._state(hc.STATE_DOWN, hc.STATE_UP, last_switch=10_000.0)
        d = hc.evaluate(state, "primary", probe("p", False), probe("f", True), self.th, now=10_000.0)
        self.assertEqual(d.action, "none")
        self.assertEqual(d.reason, "cooldown")

    def test_restore_primary_enabled_switches_back(self):
        state = self._state(hc.STATE_UP, hc.STATE_UP)
        d = hc.evaluate(state, "fallback", probe("p", True), probe("f", True), self.th, now=10_000.0)
        self.assertEqual(d.action, "switch-to-primary")
        self.assertEqual(d.event, "primary_recovered")

    def test_restore_primary_disabled_never_switches_back(self):
        th = hc.Thresholds(fail_threshold=3, ok_threshold=3, cooldown=60.0, restore_primary=False)
        state = self._state(hc.STATE_UP, hc.STATE_UP)
        d = hc.evaluate(state, "fallback", probe("p", True), probe("f", True), th, now=10_000.0)
        self.assertEqual(d.action, "none")
        self.assertEqual(d.reason, "restore_primary_disabled")
        self.assertEqual(d.event, "primary_recovered")

    def test_staying_on_fallback_while_primary_down(self):
        state = self._state(hc.STATE_DOWN, hc.STATE_UP)
        d = hc.evaluate(state, "fallback", probe("p", False), probe("f", True), self.th, now=10_000.0)
        self.assertEqual(d.action, "none")
        self.assertEqual(d.reason, "staying_on_fallback")

    def test_active_none_does_not_impose_exit_node(self):
        state = self._state(hc.STATE_UP, hc.STATE_UP)
        d = hc.evaluate(state, "none", probe("p", True), probe("f", True), self.th, now=10_000.0)
        self.assertEqual(d.action, "none")
        self.assertEqual(d.reason, "no_active_exit_node")

    def test_active_unknown_does_not_override_user_choice(self):
        state = self._state(hc.STATE_UP, hc.STATE_UP)
        d = hc.evaluate(state, "unknown", probe("p", True), probe("f", True), self.th, now=10_000.0)
        self.assertEqual(d.action, "none")
        self.assertEqual(d.reason, "unknown_active")

    def test_hysteresis_requires_threshold_failures(self):
        # One failure must NOT trip a DOWN/switch with fail_threshold=3.
        state = self._state(hc.STATE_UP, hc.STATE_UP)
        d = hc.evaluate(state, "primary", probe("p", False), probe("f", True), self.th, now=10_000.0)
        self.assertEqual(d.action, "none")
        self.assertEqual(d.reason, "healthy")
        self.assertEqual(state["nodes"]["primary"]["fail_count"], 1)

    def test_hysteresis_flips_exactly_at_threshold(self):
        state = self._state(hc.STATE_UP, hc.STATE_UP)
        state["nodes"]["primary"]["fail_count"] = 2  # one more failure reaches threshold 3
        d = hc.evaluate(state, "primary", probe("p", False), probe("f", True), self.th, now=10_000.0)
        self.assertEqual(state["nodes"]["primary"]["last_state"], hc.STATE_DOWN)
        self.assertEqual(d.action, "switch-to-fallback")

    def test_ensure_primary_selects_primary_when_none(self):
        th = hc.Thresholds(fail_threshold=1, ok_threshold=1, cooldown=0.0, ensure_primary=True)
        state = self._state(hc.STATE_UP, hc.STATE_UP)
        d = hc.evaluate(state, "none", probe("p", True), probe("f", True), th, now=10_000.0)
        self.assertEqual(d.action, "switch-to-primary")
        self.assertEqual(d.reason, "ensure_primary")

    def test_none_without_ensure_primary_does_nothing(self):
        th = hc.Thresholds(ensure_primary=False)
        state = self._state(hc.STATE_UP, hc.STATE_UP)
        d = hc.evaluate(state, "none", probe("p", True), probe("f", True), th, now=10_000.0)
        self.assertEqual(d.action, "none")
        self.assertEqual(d.reason, "no_active_exit_node")

    def test_ensure_primary_skips_when_primary_unreachable(self):
        th = hc.Thresholds(ensure_primary=True)
        state = self._state(hc.STATE_UP, hc.STATE_UP)
        d = hc.evaluate(state, "none", probe("p", False), probe("f", True), th, now=10_000.0)
        self.assertEqual(d.action, "none")
        self.assertEqual(d.reason, "no_active_exit_node")


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
        role = hc.live_active_role(SAMPLE_STATUS, "nodeP", ["100.64.0.1"], "nodeF", ["100.64.0.2"])
        self.assertEqual(role, "primary")

    def test_live_active_role_none(self):
        status = dict(SAMPLE_STATUS, ExitNodeStatus=None)
        role = hc.live_active_role(status, "nodeP", ["100.64.0.1"], "nodeF", ["100.64.0.2"])
        self.assertEqual(role, "none")

    def test_live_active_role_unknown(self):
        status = dict(SAMPLE_STATUS, ExitNodeStatus={"ID": "someoneelse", "TailscaleIPs": ["100.99.0.9/32"]})
        role = hc.live_active_role(status, "nodeP", ["100.64.0.1"], "nodeF", ["100.64.0.2"])
        self.assertEqual(role, "unknown")

    def test_live_active_role_matches_by_ip_only(self):
        status = dict(SAMPLE_STATUS, ExitNodeStatus={"TailscaleIPs": ["100.64.0.2/32"]})
        role = hc.live_active_role(status, "nodeP", ["100.64.0.1"], "nodeF", ["100.64.0.2"])
        self.assertEqual(role, "fallback")


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
        state = hc.load_state(path, "p", "f")
        self.assertEqual(state["schema_version"], hc.STATE_SCHEMA_VERSION)
        self.assertEqual(state["nodes"]["primary"]["configured_label"], "p")

    def test_same_label_preserves_counters(self):
        path = self.tmp / "failover-state.json"
        state = hc.default_state("p", "f")
        state["nodes"]["primary"]["fail_count"] = 2
        state["nodes"]["primary"]["last_state"] = hc.STATE_DOWN
        hc.save_state(path, state)
        reloaded = hc.load_state(path, "p", "f")
        self.assertEqual(reloaded["nodes"]["primary"]["fail_count"], 2)
        self.assertEqual(reloaded["nodes"]["primary"]["last_state"], hc.STATE_DOWN)

    def test_changed_label_resets_state(self):
        path = self.tmp / "failover-state.json"
        state = hc.default_state("old-primary", "f")
        state["nodes"]["primary"]["fail_count"] = 2
        state["nodes"]["primary"]["last_state"] = hc.STATE_DOWN
        hc.save_state(path, state)
        reloaded = hc.load_state(path, "new-primary", "f")
        self.assertEqual(reloaded["nodes"]["primary"]["configured_label"], "new-primary")
        self.assertEqual(reloaded["nodes"]["primary"]["fail_count"], 0)
        self.assertEqual(reloaded["nodes"]["primary"]["last_state"], hc.STATE_UNKNOWN)

    def test_corrupt_field_types_are_discarded(self):
        path = self.tmp / "failover-state.json"
        bad = hc.default_state("p", "f")
        bad["nodes"]["primary"]["fail_count"] = "x"
        bad["nodes"]["primary"]["last_state"] = "weird"
        bad["active"]["last_switch_epoch"] = "bad"
        path.write_text(json.dumps(bad), encoding="utf-8")
        reloaded = hc.load_state(path, "p", "f")
        self.assertEqual(reloaded["nodes"]["primary"]["fail_count"], 0)
        self.assertEqual(reloaded["nodes"]["primary"]["last_state"], hc.STATE_UNKNOWN)
        self.assertEqual(reloaded["active"]["last_switch_epoch"], 0.0)

    def test_state_lock_is_reentrant_across_calls(self):
        path = self.tmp / "failover-state.json"
        with hc.state_lock(path):
            hc.save_state(path, hc.default_state("p", "f"))
        with hc.state_lock(path):
            self.assertTrue(path.exists())
        self.assertTrue((self.tmp / "failover-state.lock").exists())


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
        stale = hc.default_state("primary-vps", "fallback-vps")
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
        stale = hc.default_state("primary-vps", "fallback-vps")
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

    def test_text_connectors_append_metrics_line(self):
        # Text mode is append-only: a new [metrics] line per connector; the
        # existing connector= line is not reworded/reordered.
        rc, out = run_cli(self._args())
        self.assertIn("connector=primary label=primary-vps", out)   # existing line intact
        self.assertIn("[metrics] connector=primary tx=", out)
        self.assertIn("[metrics] connector=fallback tx=", out)
        self.assertIn("path=", out)

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


class VersionTests(unittest.TestCase):
    def test_module_version_matches_version_file(self):
        self.assertEqual(hc.__version__, VERSION)


if __name__ == "__main__":
    unittest.main()
