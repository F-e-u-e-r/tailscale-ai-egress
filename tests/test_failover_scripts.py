import json
import os
import shlex
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts import health_check as hc

SCRIPT = ROOT / "failover-exit-node.sh"

# A small stateful fake `tailscale`: `ping` honours FAKE_UNREACHABLE, `status
# --json` reflects the exit node recorded by `set --exit-node=...` (FAKE_SET_NOOP
# makes `set` a no-op to exercise the apply-readback failure path).
FAKE_TAILSCALE = '''#!/usr/bin/env python3
import json
import os
import sys
import time

args = sys.argv[1:]
state_dir = os.environ.get("FAKE_STATE_DIR", ".")
active_path = os.path.join(state_dir, "active")
NODES = {"primary-vps": ("nodeP", "100.64.0.1"), "fallback-vps": ("nodeF", "100.64.0.2"),
         "fallback2-vps": ("nodeF2", "100.64.0.3")}


def read_active():
    try:
        with open(active_path) as handle:
            return handle.read().strip()
    except OSError:
        return ""


if args[:1] == ["ping"]:
    label = args[-1]
    down = [d for d in os.environ.get("FAKE_UNREACHABLE", "").split(",") if d]
    sys.exit(1 if label in down else 0)

if args[:1] == ["status"] and "--json" in args:
    fail = os.environ.get("FAKE_STATUS_FAIL", "")
    if fail == "nonzero":
        sys.exit(1)
    if fail == "invalid":
        print("{not valid json")
        sys.exit(0)
    if fail == "timeout":
        time.sleep(5)
        sys.exit(0)
    if fail == "empty":
        # Backend up but no usable Self yet -> exercises live_status_incomplete
        # (distinct from a malformed status with no BackendState at all).
        print('{"BackendState": "Running"}')
        sys.exit(0)
    active = read_active()
    offline = [d for d in os.environ.get("FAKE_OFFLINE", "").split(",") if d]
    noroutes = os.environ.get("FAKE_NOROUTES") == "1"
    backend = os.environ.get("FAKE_BACKEND", "Running")
    try:
        with open(os.path.join(state_dir, "backend")) as handle:
            override = handle.read().strip()
        if override:
            backend = override  # set when the backend "stops" after a tailscale set
    except OSError:
        pass
    peers = {}
    for lbl, (nid, ip) in NODES.items():
        peer = {"ID": nid, "HostName": lbl, "DNSName": lbl + ".example.ts.net.",
                "TailscaleIPs": [ip], "Online": lbl not in offline}
        if not noroutes and lbl == "primary-vps":
            peer["PrimaryRoutes"] = ["10.0.0.0/24"]
        peers[nid] = peer
    status = {
        "BackendState": backend,
        "Self": {"ID": "selfID", "HostName": "client", "TailscaleIPs": ["100.64.0.5"]},
        "Peer": peers,
        "ExitNodeStatus": None,
    }
    if active in NODES:
        nid, ip = NODES[active]
        status["ExitNodeStatus"] = {"ID": nid, "TailscaleIPs": [ip + "/32"], "Online": True}
    print(json.dumps(status))
    sys.exit(0)

if args[:1] == ["set"]:
    if os.environ.get("FAKE_SET_NOOP") == "1":
        sys.exit(0)
    for arg in args:
        if arg.startswith("--exit-node="):
            with open(active_path, "w") as handle:
                handle.write(arg.split("=", 1)[1])
    after = os.environ.get("FAKE_BACKEND_AFTER_SET")
    if after:
        with open(os.path.join(state_dir, "backend"), "w") as handle:
            handle.write(after)
    sys.exit(0)

sys.exit(0)
'''


class FailoverControllerTests(unittest.TestCase):
    def setUp(self):
        self.base = Path(tempfile.mkdtemp())
        self.addCleanup(self._cleanup)
        self.fake_bin = self.base / "bin"
        self.fake_bin.mkdir()
        self.state_dir = self.base / "fakestate"
        self.state_dir.mkdir()
        self.gen_dir = self.base / "generated"
        self.gen_dir.mkdir()
        fake = self.fake_bin / "tailscale"
        fake.write_text(FAKE_TAILSCALE, encoding="utf-8")
        fake.chmod(0o755)
        self.state_file = self.gen_dir / "failover-state.json"
        self.lock_dir = self.gen_dir / "failover.lock.d"

    def _cleanup(self):
        for child in sorted(self.base.rglob("*"), reverse=True):
            if child.is_file() or child.is_symlink():
                child.unlink()
            else:
                child.rmdir()
        self.base.rmdir()

    def set_active(self, label):
        (self.state_dir / "active").write_text(label, encoding="utf-8")

    def read_active(self):
        path = self.state_dir / "active"
        return path.read_text(encoding="utf-8").strip() if path.exists() else ""

    def _controller_env(self, **overrides):
        env = os.environ.copy()
        env["PATH"] = f"{self.fake_bin}{os.pathsep}{env['PATH']}"
        env["FAKE_STATE_DIR"] = str(self.state_dir)
        env["GENERATED_DIR"] = str(self.gen_dir)
        env["AI_EGRESS_USE_SUDO"] = "0"
        env["PRIMARY_EXIT_NODE"] = "primary-vps"
        env["FALLBACK_EXIT_NODE"] = "fallback-vps"
        env["FAIL_THRESHOLD"] = "1"
        env["OK_THRESHOLD"] = "1"
        env["COOLDOWN"] = "0"
        env["READBACK_DELAY"] = "0"
        env.pop("TAILSCALE_BIN", None)
        for key, value in overrides.items():
            env[key] = value
        return env

    def run_controller(self, *args, **overrides):
        return subprocess.run(
            ["bash", str(SCRIPT), *args],
            env=self._controller_env(**overrides),
            capture_output=True,
            text=True,
            cwd=ROOT,
        )

    def read_state(self):
        return json.loads(self.state_file.read_text(encoding="utf-8"))

    # --- version / help -----------------------------------------------------
    def test_version_and_help(self):
        version = (ROOT / "VERSION").read_text(encoding="utf-8").strip()
        result = self.run_controller("--version")
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout.strip(), f"tailscale-ai-egress failover-exit-node.sh {version}")
        self.assertEqual(self.run_controller("--help").returncode, 0)

    # --- observe vs apply ---------------------------------------------------
    def test_observe_mode_never_calls_tailscale_set(self):
        self.set_active("primary-vps")
        result = self.run_controller("--once", FAKE_UNREACHABLE="primary-vps")
        self.assertEqual(result.returncode, 0)
        self.assertIn("switch-to-fallback", result.stdout)
        self.assertIn("[observe]", result.stdout)
        self.assertEqual(self.read_active(), "primary-vps")  # unchanged

    def test_apply_switches_to_verified_fallback(self):
        self.set_active("primary-vps")
        result = self.run_controller("--once", "--apply", FAKE_UNREACHABLE="primary-vps")
        self.assertEqual(result.returncode, 0)
        self.assertIn("exit node is now fallback-vps", result.stdout)
        self.assertEqual(self.read_active(), "fallback-vps")
        self.assertEqual(self.read_state()["active"]["role"], "fallback")

    def test_apply_dry_run_prints_command_without_switching(self):
        self.set_active("primary-vps")
        result = self.run_controller("--once", "--apply", "--dry-run", FAKE_UNREACHABLE="primary-vps")
        self.assertEqual(result.returncode, 0)
        self.assertIn("+ tailscale set --exit-node=fallback-vps", result.stdout)
        self.assertEqual(self.read_active(), "primary-vps")  # never mutated

    def test_notify_hook_fires_on_switch(self):
        self.set_active("primary-vps")
        notify_out = self.gen_dir / "notify.out"
        cmd = (f'printf "%s|%s|%s|%s|%s" "$FAILOVER_EVENT" "$FAILOVER_ROLE" '
               f'"$FAILOVER_LABEL" "$FAILOVER_REASON" "$FAILOVER_FALLBACK_INDEX" > "{notify_out}"')
        result = self.run_controller("--once", "--apply", FAKE_UNREACHABLE="primary-vps", FAILOVER_NOTIFY_CMD=cmd)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(self.read_active(), "fallback-vps")
        self.assertTrue(notify_out.exists(), "notify hook did not run on switch")
        event, role, label, reason, index = notify_out.read_text(encoding="utf-8").split("|")
        self.assertEqual(event, "switched")
        self.assertEqual(role, "fallback")
        self.assertEqual(label, "fallback-vps")
        self.assertEqual(reason, "primary_down")  # the exact v1.3.0 reason string
        self.assertEqual(index, "0")  # single-element fallback target -> index 0

    def test_notify_hook_primary_target_has_empty_index(self):
        # A primary restore's notify carries an EMPTY FAILOVER_FALLBACK_INDEX
        # (the additive variable is fallback-target-only) and the exact
        # v1.3.0 role/label/reason values.
        self.set_active("fallback-vps")
        notify_out = self.gen_dir / "notify.out"
        cmd = (f'printf "%s|%s|%s|%s|[%s]" "$FAILOVER_EVENT" "$FAILOVER_ROLE" '
               f'"$FAILOVER_LABEL" "$FAILOVER_REASON" "$FAILOVER_FALLBACK_INDEX" > "{notify_out}"')
        result = self.run_controller("--once", "--apply", FAILOVER_NOTIFY_CMD=cmd)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.read_active(), "primary-vps")
        event, role, label, reason, index = notify_out.read_text(encoding="utf-8").split("|")
        self.assertEqual(event, "switched")
        self.assertEqual(role, "primary")
        self.assertEqual(label, "primary-vps")
        self.assertEqual(reason, "primary_recovered")
        self.assertEqual(index, "[]")  # empty for primary targets

    def test_notify_hook_failure_does_not_change_outcome(self):
        # A failing hook (nonzero exit) must never change the controller result.
        self.set_active("primary-vps")
        result = self.run_controller("--once", "--apply", FAKE_UNREACHABLE="primary-vps", FAILOVER_NOTIFY_CMD="exit 7")
        self.assertEqual(result.returncode, 0)
        self.assertEqual(self.read_active(), "fallback-vps")

    def test_notify_hook_not_fired_on_dry_run(self):
        self.set_active("primary-vps")
        notify_out = self.gen_dir / "notify.out"
        cmd = f'echo fired > "{notify_out}"'
        result = self.run_controller("--once", "--apply", "--dry-run", FAKE_UNREACHABLE="primary-vps", FAILOVER_NOTIFY_CMD=cmd)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(self.read_active(), "primary-vps")  # dry-run never switches
        self.assertFalse(notify_out.exists(), "notify hook fired on a dry run")

    def test_notify_hook_reports_failed_event_on_readback_mismatch(self):
        # A failed switch (readback mismatch via FAKE_SET_NOOP) must notify
        # event=failed, so a regression that only notifies on success is caught.
        self.set_active("primary-vps")
        notify_out = self.gen_dir / "notify.out"
        cmd = f'printf "%s" "$FAILOVER_EVENT" > "{notify_out}"'
        result = self.run_controller(
            "--once", "--apply", FAKE_UNREACHABLE="primary-vps", FAKE_SET_NOOP="1", FAILOVER_NOTIFY_CMD=cmd
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertTrue(notify_out.exists(), "notify hook did not run on a failed switch")
        self.assertEqual(notify_out.read_text(encoding="utf-8"), "failed")

    def test_healthy_primary_no_switch(self):
        self.set_active("primary-vps")
        result = self.run_controller("--once", "--apply")
        self.assertIn("action=none", result.stdout)
        self.assertIn("reason=healthy", result.stdout)
        self.assertEqual(self.read_active(), "primary-vps")

    # --- reconciliation / safety -------------------------------------------
    def test_stale_state_reconciled_to_live(self):
        self.set_active("primary-vps")  # live = primary
        stale = hc.default_state("primary-vps", "fallback-vps")
        stale["active"]["role"] = "fallback"  # state lies
        self.state_file.write_text(json.dumps(stale), encoding="utf-8")
        result = self.run_controller("--once")
        self.assertIn("overrides stale state", result.stderr)
        self.assertIn("active=primary", result.stdout)
        self.assertEqual(self.read_state()["active"]["role"], "primary")

    def test_restore_primary_disabled_never_switches_back(self):
        self.set_active("fallback-vps")  # currently on fallback
        result = self.run_controller("--once", "--apply", RESTORE_PRIMARY="0")
        self.assertIn("restore_primary_disabled", result.stdout)
        self.assertIn("staying on fallback", result.stdout)
        self.assertEqual(self.read_active(), "fallback-vps")  # not switched back

    def test_apply_failed_when_readback_mismatches(self):
        self.set_active("primary-vps")
        result = self.run_controller("--once", "--apply", FAKE_UNREACHABLE="primary-vps", FAKE_SET_NOOP="1")
        self.assertIn("apply_failed", result.stderr)
        self.assertIn("readback does not show 'fallback-vps' as the live exit node", result.stderr)
        self.assertEqual(self.read_active(), "primary-vps")
        self.assertEqual(self.read_state()["active"]["role"], "primary")  # not recorded as switched

    def test_apply_failed_when_backend_stops_after_set(self):
        # Verdict sees a Running backend and proposes the switch; the `set`
        # succeeds but the backend then stops while the JSON still carries the
        # exit node. The readback must see active-role=unknown -> apply_failed, so
        # the switch is NOT recorded and no cooldown is started.
        self.set_active("primary-vps")
        result = self.run_controller(
            "--once", "--apply", FAKE_UNREACHABLE="primary-vps", FAKE_BACKEND_AFTER_SET="Stopped"
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("apply_failed", result.stderr)
        self.assertIn("readback does not show 'fallback-vps' as the live exit node", result.stderr)
        # Not recorded as switched: cooldown clock untouched.
        self.assertEqual(self.read_state()["active"]["role"], "primary")
        self.assertEqual(self.read_state()["active"]["last_switch_epoch"], 0.0)

    # --- locking ------------------------------------------------------------
    def test_stale_lock_is_removed(self):
        self.set_active("primary-vps")
        self.lock_dir.mkdir(parents=True)
        old = time.time() - 1000
        os.utime(self.lock_dir, (old, old))
        result = self.run_controller("--once")
        self.assertEqual(result.returncode, 0)
        self.assertIn("removing stale controller lock", result.stderr)
        self.assertFalse(self.lock_dir.exists())  # released after run

    def test_stale_lock_reclaim_leaves_no_stale_residue(self):
        # The stale-lock reclaim renames the directory to a private ".stale.$$"
        # name before deleting it (atomic, so racing controllers cannot both win
        # and delete a fresh lock). The temporary rename target must not survive.
        self.set_active("primary-vps")
        self.lock_dir.mkdir(parents=True)
        old = time.time() - 1000
        os.utime(self.lock_dir, (old, old))
        result = self.run_controller("--once")
        self.assertEqual(result.returncode, 0)
        self.assertIn("removing stale controller lock", result.stderr)
        residue = list(self.gen_dir.glob("failover.lock.d.stale.*"))
        self.assertEqual(residue, [], f"stale-rename residue left behind: {residue}")

    def test_acquire_lock_reclaims_stale_lock_via_atomic_rename(self):
        # The TOCTOU window between two controllers reclaiming the same stale lock
        # cannot be reproduced deterministically in a unit test, so guard the
        # mechanism: reclaim must go through `mv "$LOCK_DIR" ...` (atomic rename),
        # not a plain `rm -rf "$LOCK_DIR"`. Reverting to direct removal drops this
        # substring and fails the test.
        text = SCRIPT.read_text(encoding="utf-8")
        self.assertIn('mv "$LOCK_DIR"', text)

    def test_stale_lock_mv_failure_falls_through_to_bounded_wait(self):
        # Force the atomic rename to fail (shadow `mv` with one that always exits
        # 1) while the lock is stale. Reclaim must then fall through to the
        # bounded LOCK_WAIT path and exit, not spin: the pre-fix `mv ...; continue`
        # would loop without accounting and never terminate.
        self.set_active("primary-vps")
        self.lock_dir.mkdir(parents=True)
        old = time.time() - 1000
        os.utime(self.lock_dir, (old, old))  # stale by age (no pid file recorded)
        fake_mv = self.fake_bin / "mv"
        fake_mv.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
        fake_mv.chmod(0o755)
        try:
            result = subprocess.run(
                ["bash", str(SCRIPT), "--once"],
                env=self._controller_env(LOCK_WAIT="2"),
                capture_output=True,
                text=True,
                cwd=ROOT,
                timeout=30,
            )
        except subprocess.TimeoutExpired:
            self.fail("acquire_lock spun on mv failure instead of honoring LOCK_WAIT")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("could not acquire controller lock", result.stderr)

    def test_lock_released_after_normal_run(self):
        self.set_active("primary-vps")
        self.run_controller("--once")
        self.assertFalse(self.lock_dir.exists())

    # --- preflight ----------------------------------------------------------
    def test_missing_primary_config_fails(self):
        result = self.run_controller("--once", PRIMARY_EXIT_NODE="")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("PRIMARY_EXIT_NODE is not set", result.stderr)

    def test_env_file_inline_comments_are_stripped(self):
        env_file = self.gen_dir / "failover.env"
        env_file.write_text(
            "PRIMARY_EXIT_NODE=primary-vps\n"
            "FALLBACK_EXIT_NODE=fallback-vps\n"
            "FAIL_THRESHOLD=1   # consecutive failures before DOWN\n"
            "OK_THRESHOLD=1\n"
            "COOLDOWN=0\n"
            "READBACK_DELAY=0\n",
            encoding="utf-8",
        )
        self.set_active("primary-vps")
        result = self.run_controller(
            "--once",
            PRIMARY_EXIT_NODE="", FALLBACK_EXIT_NODE="",
            FAIL_THRESHOLD="", OK_THRESHOLD="", COOLDOWN="", READBACK_DELAY="",
        )
        self.assertEqual(result.returncode, 0)
        self.assertNotIn("invalid int", result.stderr)
        self.assertIn("action=none", result.stdout)

    def test_ensure_primary_selects_primary_from_none(self):
        (self.state_dir / "active").write_text("", encoding="utf-8")
        result = self.run_controller("--once", "--apply", "--ensure-primary")
        self.assertEqual(result.returncode, 0)
        self.assertEqual(self.read_active(), "primary-vps")
        self.assertIn("ensure_primary", result.stdout)

    def test_none_without_ensure_primary_does_not_switch(self):
        (self.state_dir / "active").write_text("", encoding="utf-8")
        result = self.run_controller("--once", "--apply")
        self.assertEqual(result.returncode, 0)
        self.assertIn("no_active_exit_node", result.stdout)
        self.assertEqual(self.read_active(), "")

    def test_restore_primary_switches_back_with_readback(self):
        self.set_active("fallback-vps")  # on fallback; primary healthy
        result = self.run_controller("--once", "--apply")  # RESTORE_PRIMARY defaults to 1
        self.assertEqual(result.returncode, 0)
        self.assertEqual(self.read_active(), "primary-vps")
        self.assertIn("exit node is now primary-vps", result.stdout)
        self.assertEqual(self.read_state()["active"]["role"], "primary")

    # --- locking / concurrency ----------------------------------------------
    def test_concurrent_controller_lock_blocks_second_run(self):
        self.set_active("primary-vps")
        self.lock_dir.mkdir(parents=True)  # a fresh lock held by "another run"
        result = self.run_controller("--once", LOCK_WAIT="2")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("could not acquire controller lock", result.stderr)
        self.assertTrue(self.lock_dir.exists())  # fresh lock is not stolen

    def test_watch_without_apply_never_mutates(self):
        self.set_active("primary-vps")
        env = os.environ.copy()
        env["PATH"] = f"{self.fake_bin}{os.pathsep}{env['PATH']}"
        env["FAKE_STATE_DIR"] = str(self.state_dir)
        env["GENERATED_DIR"] = str(self.gen_dir)
        env["AI_EGRESS_USE_SUDO"] = "0"
        env["PRIMARY_EXIT_NODE"] = "primary-vps"
        env["FALLBACK_EXIT_NODE"] = "fallback-vps"
        env["FAIL_THRESHOLD"] = "1"
        env["OK_THRESHOLD"] = "1"
        env["COOLDOWN"] = "0"
        env["READBACK_DELAY"] = "0"
        env["CHECK_INTERVAL"] = "1"
        env["FAKE_UNREACHABLE"] = "primary-vps"  # would switch if --apply were set
        env.pop("TAILSCALE_BIN", None)
        out = ""
        try:
            proc = subprocess.run(
                ["bash", str(SCRIPT), "--watch"],  # no --apply
                env=env, capture_output=True, text=True, cwd=ROOT, timeout=4,
            )
            out = proc.stdout
        except subprocess.TimeoutExpired as exc:
            out = exc.stdout or ""
            if isinstance(out, bytes):
                out = out.decode("utf-8", errors="replace")
        self.assertIn("[observe]", out)
        self.assertEqual(self.read_active(), "primary-vps")  # never switched

    def test_linux_sudo_branch_used_for_apply(self):
        self.set_active("primary-vps")
        (self.fake_bin / "uname").write_text("#!/usr/bin/env bash\necho Linux\n", encoding="utf-8")
        (self.fake_bin / "uname").chmod(0o755)
        marker = self.base / "sudo-was-used"
        (self.fake_bin / "sudo").write_text(
            f'#!/usr/bin/env bash\ntouch "{marker}"\nexec "$@"\n', encoding="utf-8"
        )
        (self.fake_bin / "sudo").chmod(0o755)
        result = self.run_controller(
            "--once", "--apply", FAKE_UNREACHABLE="primary-vps", AI_EGRESS_USE_SUDO="1"
        )
        self.assertEqual(result.returncode, 0)
        self.assertTrue(marker.exists(), "Linux apply should run tailscale via sudo")
        self.assertEqual(self.read_active(), "fallback-vps")

    # --- fail closed on status failure --------------------------------------
    def _assert_status_failure_fails_closed(self, mode, expected_reason="live_status_unavailable", **extra):
        (self.state_dir / "active").write_text("", encoding="utf-8")  # no exit node selected
        result = self.run_controller("--once", "--apply", "--ensure-primary", FAKE_STATUS_FAIL=mode, **extra)
        self.assertNotEqual(result.returncode, 0)  # --once must alert on a live-state problem
        self.assertIn(expected_reason, result.stdout)
        self.assertEqual(self.read_active(), "")  # no tailscale set despite --ensure-primary

    def test_status_failure_nonzero_fails_closed(self):
        self._assert_status_failure_fails_closed("nonzero")

    def test_status_failure_invalid_json_fails_closed(self):
        self._assert_status_failure_fails_closed("invalid")

    def test_status_failure_timeout_fails_closed(self):
        self._assert_status_failure_fails_closed("timeout", TAILSCALE_STATUS_TIMEOUT="1")

    def test_status_incomplete_empty_fails_closed(self):
        self._assert_status_failure_fails_closed("empty", expected_reason="live_status_incomplete")

    def test_backend_not_running_fails_closed(self):
        self._assert_status_failure_fails_closed("", expected_reason="backend_not_running", FAKE_BACKEND="Stopped")

    # --- config validation ---------------------------------------------------
    def test_rejects_negative_check_interval(self):
        result = self.run_controller("--once", CHECK_INTERVAL="-1")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("CHECK_INTERVAL must be a positive number", result.stderr)

    def test_rejects_zero_fail_threshold(self):
        result = self.run_controller("--once", FAIL_THRESHOLD="0")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("FAIL_THRESHOLD must be", result.stderr)

    def test_rejects_bare_dot_cooldown(self):
        # require_nonneg_number used to accept "." (then Python argparse blew up).
        self.set_active("primary-vps")
        result = self.run_controller("--once", COOLDOWN=".")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("COOLDOWN must be a non-negative number", result.stderr)

    def test_rejects_bare_dot_readback_delay(self):
        # "." used to slip through and make `sleep .` fail, then read back too soon.
        self.set_active("primary-vps")
        result = self.run_controller("--once", READBACK_DELAY=".")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("READBACK_DELAY must be a non-negative number", result.stderr)

    def test_rejects_non_numeric_cooldown(self):
        self.set_active("primary-vps")
        for bad in ("nan", "inf", "1e3", ".5", "5."):
            with self.subTest(bad=bad):
                result = self.run_controller("--once", COOLDOWN=bad)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("COOLDOWN must be a non-negative number", result.stderr)

    def test_rejects_oversized_lock_wait(self):
        # An oversized LOCK_WAIT used to pass validation, then the acquire loop's
        # integer comparison errored and could spin forever.
        self.set_active("primary-vps")
        result = self.run_controller("--once", LOCK_WAIT="999999999999999999999999")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("LOCK_WAIT must be a non-negative integer", result.stderr)

    def test_rejects_oversized_check_interval(self):
        # A huge-but-all-digits value used to pass the format check, then macOS
        # `sleep` rejected it and the watcher tight-looped.
        self.set_active("primary-vps")
        result = self.run_controller("--once", CHECK_INTERVAL="1" + "0" * 120)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("CHECK_INTERVAL must be at most", result.stderr)

    def test_rejects_oversized_cooldown(self):
        self.set_active("primary-vps")
        result = self.run_controller("--once", COOLDOWN="1" + "0" * 120)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("COOLDOWN must be at most", result.stderr)

    def test_rejects_oversized_readback_delay(self):
        self.set_active("primary-vps")
        result = self.run_controller("--once", READBACK_DELAY="1" + "0" * 120)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("READBACK_DELAY must be at most", result.stderr)

    def test_readback_sleep_failure_does_not_record_switch(self):
        # Defense in depth: if the readback settle-sleep fails, the switch must be
        # reported as failed and NOT recorded (no premature readback / false ok).
        self.set_active("primary-vps")
        fake_sleep = self.fake_bin / "sleep"
        fake_sleep.write_text("#!/usr/bin/env bash\nexit 1\n", encoding="utf-8")
        fake_sleep.chmod(0o755)
        self.addCleanup(lambda: fake_sleep.exists() and fake_sleep.unlink())
        result = self.run_controller(
            "--once", "--apply", FAKE_UNREACHABLE="primary-vps", READBACK_DELAY="1"
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("readback settle sleep", result.stderr)
        self.assertEqual(self.read_state()["active"]["role"], "primary")  # not recorded

    def test_watch_dies_when_interval_sleep_fails(self):
        # A failing interval sleep must die, not busy-loop.
        self.set_active("primary-vps")
        fake_sleep = self.fake_bin / "sleep"
        fake_sleep.write_text("#!/usr/bin/env bash\nexit 1\n", encoding="utf-8")
        fake_sleep.chmod(0o755)
        self.addCleanup(lambda: fake_sleep.exists() and fake_sleep.unlink())
        result = subprocess.run(
            ["bash", str(SCRIPT), "--watch"],
            env=self._controller_env(CHECK_INTERVAL="30"),
            capture_output=True, text=True, cwd=ROOT, timeout=10,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("refusing to busy-loop", result.stderr)

    def test_apply_reports_failure_when_record_switch_fails(self):
        # Switch applies and reads back OK, but persisting state fails -> the
        # controller must report non-zero and NOT claim a clean success.
        self.set_active("primary-vps")
        fake_py = self.fake_bin / "python3"
        fake_py.write_text(
            "#!/usr/bin/env bash\n"
            'for a in "$@"; do\n'
            '  if [ "$a" = "record-switch" ]; then\n'
            '    echo "simulated state persistence failure" >&2\n'
            '    exit 1\n'
            '  fi\n'
            'done\n'
            f'exec {shlex.quote(sys.executable)} "$@"\n',
            encoding="utf-8",
        )
        fake_py.chmod(0o755)
        self.addCleanup(lambda: fake_py.exists() and fake_py.unlink())
        result = self.run_controller("--once", "--apply", FAKE_UNREACHABLE="primary-vps")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("switch_state_persist_failed", result.stderr)
        self.assertNotIn("recorded switch", result.stdout)
        self.assertEqual(self.read_state()["active"]["role"], "primary")  # real record never ran

    # --- lock liveness -------------------------------------------------------
    def test_lock_with_live_pid_blocks(self):
        self.set_active("primary-vps")
        self.lock_dir.mkdir(parents=True)
        holder = subprocess.Popen(["sleep", "30"])
        self.addCleanup(lambda: (holder.kill(), holder.wait()))
        (self.lock_dir / "pid").write_text(str(holder.pid), encoding="utf-8")
        result = self.run_controller("--once", LOCK_WAIT="2")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("could not acquire", result.stderr)

    def test_lock_with_dead_pid_is_reclaimed(self):
        self.set_active("primary-vps")
        self.lock_dir.mkdir(parents=True)
        (self.lock_dir / "pid").write_text("2147483647", encoding="utf-8")  # not a live process
        result = self.run_controller("--once")
        self.assertEqual(result.returncode, 0)
        self.assertIn("removing stale controller lock", result.stderr)
        self.assertFalse(self.lock_dir.exists())

    def test_lock_with_reused_pid_is_reclaimed(self):
        # A live PID whose recorded start-time no longer matches (the original
        # holder was SIGKILLed and the PID was recycled by an unrelated process)
        # must be treated as stale, not as a live holder that blocks forever.
        self.set_active("primary-vps")
        self.lock_dir.mkdir(parents=True)
        holder = subprocess.Popen(["sleep", "30"])
        self.addCleanup(lambda: (holder.kill(), holder.wait()))
        (self.lock_dir / "pid").write_text(str(holder.pid), encoding="utf-8")
        (self.lock_dir / "start").write_text("STALE-START-TOKEN", encoding="utf-8")
        result = self.run_controller("--once")
        self.assertEqual(result.returncode, 0)
        self.assertIn("removing stale controller lock", result.stderr)
        self.assertFalse(self.lock_dir.exists())

    def test_concurrent_real_controllers_block(self):
        # Two real controller processes competing: #1 holds the lock while it
        # hangs inside `tailscale status`; #2 must observe a live holder whose
        # recorded start-time matches and refuse to steal the lock.
        self.set_active("primary-vps")
        holder = subprocess.Popen(
            ["bash", str(SCRIPT), "--once"],
            env=self._controller_env(FAKE_STATUS_FAIL="timeout"),
            cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        self.addCleanup(lambda: (holder.kill(), holder.wait()))
        pid_file = self.lock_dir / "pid"
        deadline = time.time() + 5
        while not pid_file.exists() and time.time() < deadline:
            time.sleep(0.05)
        self.assertTrue(pid_file.exists(), "controller #1 never acquired the lock")
        result = self.run_controller("--once", LOCK_WAIT="2")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("could not acquire", result.stderr)
        holder.wait(timeout=15)

    # --- connector monitor (monitor-connectors.sh) --------------------------
    def _monitor_env(self, **overrides):
        env = os.environ.copy()
        env["PATH"] = f"{self.fake_bin}{os.pathsep}{env['PATH']}"
        env["FAKE_STATE_DIR"] = str(self.state_dir)
        env["GENERATED_DIR"] = str(self.gen_dir)
        env["PRIMARY_CONNECTOR"] = "primary-vps"
        env["FALLBACK_CONNECTOR"] = "fallback-vps"
        env["PING_TIMEOUT"] = "5"
        env.pop("TAILSCALE_BIN", None)
        env.pop("TAILSCALE_API_KEY", None)
        for key, value in overrides.items():
            env[key] = value
        return env

    def run_monitor(self, *args, **overrides):
        return subprocess.run(
            ["bash", str(ROOT / "monitor-connectors.sh"), *args],
            env=self._monitor_env(**overrides),
            capture_output=True,
            text=True,
            cwd=ROOT,
        )

    def test_monitor_healthy_and_ordering_unavailable_without_token(self):
        result = self.run_monitor("--once")
        self.assertEqual(result.returncode, 0)
        self.assertIn("overall=healthy", result.stdout)
        self.assertIn("ordering=unavailable", result.stdout)

    def test_monitor_degraded_when_unreachable(self):
        result = self.run_monitor("--once", FAKE_UNREACHABLE="fallback-vps")
        self.assertEqual(result.returncode, 1)
        self.assertIn("overall=degraded", result.stdout)

    def test_monitor_degraded_when_offline(self):
        result = self.run_monitor("--once", FAKE_OFFLINE="primary-vps")
        self.assertEqual(result.returncode, 1)
        self.assertIn("overall=degraded", result.stdout)

    def test_monitor_degraded_when_no_routes(self):
        result = self.run_monitor("--once", FAKE_NOROUTES="1")
        self.assertEqual(result.returncode, 1)
        self.assertIn("routes_serving=none", result.stdout)
        self.assertIn("overall=degraded", result.stdout)

    def test_monitor_degraded_when_backend_not_running(self):
        result = self.run_monitor("--once", FAKE_BACKEND="Stopped")
        self.assertEqual(result.returncode, 1)
        self.assertIn("overall=degraded", result.stdout)

    # --- --prometheus-textfile (PR C) --------------------------------------
    def test_monitor_prometheus_textfile_writes_valid_file(self):
        dest = self.gen_dir / "conn.prom"
        result = self.run_monitor("--prometheus-textfile", str(dest))
        self.assertEqual(result.returncode, 0)
        self.assertIn("[prometheus] wrote", result.stdout)
        self.assertNotIn("overall=healthy", result.stdout)  # textfile-only: no normal report
        self.assertTrue(dest.exists())
        self.assertEqual(oct(dest.stat().st_mode & 0o777), "0o644")
        content = dest.read_text(encoding="utf-8")
        self.assertTrue(content.endswith("\n"))
        last = [line for line in content.split("\n") if line][-1]
        self.assertTrue(last.startswith("ai_egress_overall_healthy "))

    def test_monitor_prometheus_textfile_degraded_still_writes_exit0(self):
        dest = self.gen_dir / "deg.prom"
        result = self.run_monitor("--prometheus-textfile", str(dest), FAKE_UNREACHABLE="fallback-vps")
        self.assertEqual(result.returncode, 0)  # write succeeded; health is in the gauge
        self.assertIn("ai_egress_overall_healthy 0", dest.read_text(encoding="utf-8"))

    def test_monitor_prometheus_textfile_rejects_json_combo(self):
        result = self.run_monitor("--json", "--prometheus-textfile", str(self.gen_dir / "x.prom"))
        self.assertEqual(result.returncode, 2)
        self.assertIn("cannot be combined", result.stderr)

    def test_monitor_prometheus_textfile_requires_path(self):
        result = self.run_monitor("--prometheus-textfile")
        self.assertEqual(result.returncode, 2)
        self.assertIn("requires a <path>", result.stderr)

    def test_monitor_prometheus_textfile_rejects_non_prom(self):
        dest = self.gen_dir / "bad.txt"
        result = self.run_monitor("--prometheus-textfile", str(dest))
        self.assertNotEqual(result.returncode, 0)   # python write failure propagates
        self.assertIn("must end in .prom", result.stderr)
        self.assertFalse(dest.exists())

    def test_monitor_prometheus_textfile_rejects_empty_path(self):
        # An empty path (e.g. an unset automation variable) must be a usage error,
        # not silently fall through to the normal report / exit 0 without writing.
        result = self.run_monitor("--prometheus-textfile", "")
        self.assertEqual(result.returncode, 2)
        self.assertIn("non-empty path", result.stderr)

    def test_monitor_prometheus_watch_continues_on_write_failure(self):
        # A failed write must NOT abort --watch: the loop reaches the interval sleep
        # (forced to fail here to break the loop deterministically), proving the
        # write failure was tolerated (write_prometheus_textfile || true).
        fake_sleep = self.fake_bin / "sleep"
        fake_sleep.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
        fake_sleep.chmod(0o755)
        try:
            result = subprocess.run(
                ["bash", str(ROOT / "monitor-connectors.sh"), "--watch",
                 "--prometheus-textfile", str(self.gen_dir / "bad.txt")],  # write always fails
                env=self._monitor_env(), capture_output=True, text=True, cwd=ROOT, timeout=30,
            )
        finally:
            fake_sleep.unlink()
        self.assertIn("write failed", result.stderr)          # the write was attempted and failed
        self.assertIn("refusing to busy-loop", result.stderr)  # yet the loop reached the sleep

    def test_monitor_rejects_bad_ping_timeout(self):
        result = self.run_monitor("--once", PING_TIMEOUT="oops")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("PING_TIMEOUT must be a positive number", result.stderr)

    def test_monitor_rejects_oversized_check_interval(self):
        result = self.run_monitor("--once", CHECK_INTERVAL="1" + "0" * 120)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("CHECK_INTERVAL must be at most", result.stderr)

    def test_monitor_watch_dies_when_interval_sleep_fails(self):
        fake_sleep = self.fake_bin / "sleep"
        fake_sleep.write_text("#!/usr/bin/env bash\nexit 1\n", encoding="utf-8")
        fake_sleep.chmod(0o755)
        self.addCleanup(lambda: fake_sleep.exists() and fake_sleep.unlink())
        result = subprocess.run(
            ["bash", str(ROOT / "monitor-connectors.sh"), "--watch"],
            env=self._monitor_env(CHECK_INTERVAL="30"),
            capture_output=True, text=True, cwd=ROOT, timeout=10,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("refusing to busy-loop", result.stderr)

    def test_monitor_missing_config_fails(self):
        result = self.run_monitor("--once", PRIMARY_CONNECTOR="")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("PRIMARY_CONNECTOR is not set", result.stderr)


class MultiFallbackControllerTests(FailoverControllerTests):
    """Controller behavior over an ordered multi-fallback bench (H-series).

    Inherits the harness (fake tailscale grows a third node) and overrides the
    configured list; the inherited single-element tests do NOT rerun here."""

    FALLBACKS = "fallback-vps,fallback2-vps"

    # Neutralize the inherited single-element test methods (they already run in
    # the parent class); this subclass exists for its own multi-bench cases.
    def run(self, result=None):
        if self._testMethodName.startswith(("test_h", "test_multi")):
            return super().run(result)
        return None

    def _controller_env(self, **overrides):
        overrides.setdefault("FALLBACK_EXIT_NODE", self.FALLBACKS)
        return super()._controller_env(**overrides)

    def _install_notify_logger(self):
        log = self.base / "notify.log"
        return log, (
            'printf "%s %s %s idx=[%s]\\n" "$FAILOVER_EVENT" "$FAILOVER_ROLE" '
            f'"$FAILOVER_LABEL" "$FAILOVER_FALLBACK_INDEX" >> "{log}"'
        )

    def test_h1_f2f_switch_records_index_and_notifies(self):
        # Active fallback-vps goes down; primary stays down -> switch to
        # fallback2-vps with the fail-closed index+label pair recorded and
        # FAILOVER_FALLBACK_INDEX=1 in the notify environment.
        self.set_active("fallback-vps")
        log, notify_cmd = self._install_notify_logger()
        result = self.run_controller(
            "--once", "--apply",
            FAKE_UNREACHABLE="primary-vps,fallback-vps",
            FAILOVER_NOTIFY_CMD=notify_cmd,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("fallback_down_next_fallback", result.stdout)
        self.assertEqual(self.read_active(), "fallback2-vps")
        state = self.read_state()
        self.assertEqual(state["active"]["role"], "fallback")
        self.assertEqual(state["active"]["fallback_index"], 1)
        self.assertEqual(state["active"]["configured_label"], "fallback2-vps")
        self.assertGreater(state["active"]["last_switch_epoch"], 0.0)
        self.assertEqual(log.read_text(encoding="utf-8").strip(),
                         "switched fallback fallback2-vps idx=[1]")

    def test_h2_noop_f2f_switch_reads_back_failure_no_record(self):
        # FAKE_SET_NOOP: `tailscale set` silently does nothing. The identity
        # readback must fail (live is still fallback-vps, not the target), the
        # state must NOT be recorded, no cooldown starts, and the notify event
        # is `failed`. Under v1.3.0's role-class readback this exact case read
        # back as SUCCESS (both nodes are "fallback"-class) — the design's
        # motivating trap.
        self.set_active("fallback-vps")
        log, notify_cmd = self._install_notify_logger()
        result = self.run_controller(
            "--once", "--apply",
            FAKE_UNREACHABLE="primary-vps,fallback-vps",
            FAKE_SET_NOOP="1",
            FAILOVER_NOTIFY_CMD=notify_cmd,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("readback does not show 'fallback2-vps'", result.stderr)
        self.assertEqual(self.read_active(), "fallback-vps")  # unchanged
        state = self.read_state()
        self.assertNotEqual(state["active"].get("fallback_index"), 1)
        self.assertEqual(state["active"]["last_switch_epoch"], 0.0)  # no cooldown restart
        self.assertEqual(log.read_text(encoding="utf-8").strip(),
                         "failed fallback fallback2-vps idx=[1]")

    def _install_verdict_forger(self, **replacements):
        """PATH-shadowing python3 wrapper: intercepts `verdict` calls and
        rewrites selected k=v lines of the REAL engine's output (so the forged
        verdict is well-formed apart from the planted divergence); every other
        invocation (active-role, record-switch) passes through untouched."""
        real = shlex.quote(sys.executable)
        rewrites = "".join(
            f'    line = "{key}={value}" if line.startswith("{key}=") else line\n'
            for key, value in replacements.items()
        )
        fake_py = self.fake_bin / "python3"
        fake_py.write_text(
            "#!/usr/bin/env bash\n"
            'for a in "$@"; do\n'
            '  if [ "$a" = "verdict" ]; then\n'
            f'    out="$({real} "$@")" || exit $?\n'
            f'    exec {real} - "$out" <<\'PYEOF\'\n'
            "import sys\n"
            "for line in sys.argv[1].splitlines():\n"
            f"{rewrites}"
            "    print(line)\n"
            "PYEOF\n"
            "  fi\n"
            "done\n"
            f'exec {real} "$@"\n',
            encoding="utf-8",
        )
        fake_py.chmod(0o755)
        self.addCleanup(lambda: fake_py.exists() and fake_py.unlink())

    def test_h3_missing_or_malformed_target_index_skips_loudly(self):
        # A fallback target whose verdict lacks a usable target_index is a
        # FAILED VERDICT: loud skip, no `tailscale set`, no notify, non-zero.
        for planted in ("", "x1", "-1", "9", "08", "18446744073709551616"):
            with self.subTest(planted=planted):
                self.set_active("fallback-vps")
                log, notify_cmd = self._install_notify_logger()
                self._install_verdict_forger(target_index=planted)
                result = self.run_controller(
                    "--once", "--apply",
                    FAKE_UNREACHABLE="primary-vps,fallback-vps",
                    FAILOVER_NOTIFY_CMD=notify_cmd,
                )
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("invalid verdict", result.stderr)
                self.assertEqual(self.read_active(), "fallback-vps")  # no set
                self.assertFalse(log.exists(), "a failed verdict must not notify")

    def test_h3_h4_gate_applies_in_observe_mode_too(self):
        # A forged verdict is a FAILED VERDICT in observe mode as well: it must
        # exit non-zero with the invalid-verdict warning, never print a
        # valid-looking [observe] proposal.
        for planted in ({"target_index": "08"}, {"target_label": "fallback-vps"}):
            with self.subTest(planted=planted):
                self.set_active("fallback-vps")
                self._install_verdict_forger(**planted)
                result = self.run_controller(
                    "--once", FAKE_UNREACHABLE="primary-vps,fallback-vps",
                )
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("invalid verdict", result.stderr)
                self.assertNotIn("[observe] proposed", result.stdout)

    def test_h4_target_label_slot_mismatch_skips_loudly(self):
        # The verdict's target_label diverges from the configured slot at
        # target_index (forged/corrupt verdict): fail closed before any set.
        self.set_active("fallback-vps")
        self._install_verdict_forger(target_label="fallback-vps")  # slot 1 is fallback2-vps
        result = self.run_controller(
            "--once", "--apply", FAKE_UNREACHABLE="primary-vps,fallback-vps",
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("does not match configured slot 1", result.stderr)
        self.assertEqual(self.read_active(), "fallback-vps")

    def test_h5_primary_restore_uses_identity_readback(self):
        # Primary restore goes through the same --expect-label readback; a
        # no-op set therefore fails it (v1.3.0's role compare also failed here,
        # but only because the role differed — now the identity is checked).
        self.set_active("fallback-vps")
        result = self.run_controller("--once", "--apply", FAKE_SET_NOOP="1")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("readback does not show 'primary-vps'", result.stderr)
        self.assertEqual(self.read_state()["active"]["last_switch_epoch"], 0.0)

    def test_h6_single_element_passes_index_zero(self):
        # Single-element config through the SAME code path: index 0 recorded,
        # FAILOVER_FALLBACK_INDEX=0, legacy role/label values unchanged.
        self.set_active("primary-vps")
        log, notify_cmd = self._install_notify_logger()
        result = self.run_controller(
            "--once", "--apply",
            FAKE_UNREACHABLE="primary-vps",
            FALLBACK_EXIT_NODE="fallback-vps",
            FAILOVER_NOTIFY_CMD=notify_cmd,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.read_active(), "fallback-vps")
        state = self.read_state()
        self.assertEqual(state["active"]["fallback_index"], 0)
        self.assertEqual(log.read_text(encoding="utf-8").strip(),
                         "switched fallback fallback-vps idx=[0]")

    def test_h7_v1_state_on_disk_upgrades_preserving_history_and_clock(self):
        v1 = {
            "schema_version": 1,
            "active": {"role": "primary", "configured_label": "primary-vps", "node_id": "nodeP",
                       "tailscale_ips": ["100.64.0.1"], "last_switch_epoch": 333.0,
                       "last_switch_at": "2026-01-01T00:00:00Z"},
            "nodes": {
                "primary": {"configured_label": "primary-vps", "node_id": "nodeP",
                            "tailscale_ips": ["100.64.0.1"], "last_state": "UP",
                            "fail_count": 0, "ok_count": 3, "last_checked_at": "2026-01-01T00:00:00Z"},
                "fallback": {"configured_label": "fallback-vps", "node_id": "nodeF",
                             "tailscale_ips": ["100.64.0.2"], "last_state": "DOWN",
                             "fail_count": 4, "ok_count": 0, "last_checked_at": "2026-01-01T00:00:00Z"},
            },
        }
        self.state_file.write_text(json.dumps(v1), encoding="utf-8")
        self.set_active("primary-vps")
        result = self.run_controller("--once", COOLDOWN="60")
        self.assertEqual(result.returncode, 0, result.stderr)
        state = self.read_state()
        self.assertEqual(state["schema_version"], 2)
        self.assertIn("fallbacks", state["nodes"])
        self.assertNotIn("fallback", state["nodes"])
        # slot0 history survived the lift (one passing ping: fail 4 -> ok 1).
        self.assertEqual(state["nodes"]["fallbacks"][0]["configured_label"], "fallback-vps")
        self.assertEqual(state["nodes"]["fallbacks"][0]["ok_count"], 1)
        self.assertEqual(state["active"]["last_switch_epoch"], 333.0)  # clock survived

    def test_h8_delisted_recovery_end_to_end(self):
        # The sole fallback is replaced in config while Tailscale still sits on
        # the old node: the cycle classifies `delisted` and restores the
        # primary (v1.3.0 dead-ended on unknown_active here).
        self.set_active("fallback-vps")
        seed = self.run_controller("--once", FALLBACK_EXIT_NODE="fallback-vps")
        self.assertEqual(seed.returncode, 0, seed.stderr)
        result = self.run_controller(
            "--once", "--apply", FALLBACK_EXIT_NODE="fallback2-vps",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("active=delisted", result.stdout)
        self.assertIn("delisted_restore_primary", result.stdout)
        self.assertEqual(self.read_active(), "primary-vps")
        state = self.read_state()
        self.assertEqual(state["active"]["role"], "primary")

    def test_h9_observe_mode_proposes_f2f_without_mutating(self):
        self.set_active("fallback-vps")
        result = self.run_controller(
            "--once", FAKE_UNREACHABLE="primary-vps,fallback-vps",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("[observe] proposed: switch-to-fallback -> fallback2-vps", result.stdout)
        self.assertEqual(self.read_active(), "fallback-vps")  # untouched

    def test_multi_verdict_refusal_fails_cycle(self):
        # An invalid list (duplicate) is refused by the engine; the controller
        # reports a failed verdict and mutates nothing.
        self.set_active("primary-vps")
        result = self.run_controller(
            "--once", "--apply", FALLBACK_EXIT_NODE="fallback-vps,fallback-vps",
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("health engine verdict failed", result.stderr)
        self.assertEqual(self.read_active(), "primary-vps")


if __name__ == "__main__":
    unittest.main()
