#!/usr/bin/env python3
"""Health probing and failover decision engine for Tailscale AI Egress.

Standard-library only so it runs on a fresh VPS / client without extra packages.

Subcommands:
  probe          One-shot health probe of a single node (tailnet ping plus an
                 optional short-timeout HTTP egress check). This is the
                 on-demand "ping" function.
  verdict        Stateful failover decision for a primary/fallback exit-node
                 pair: reconcile live Tailscale state, probe both nodes, apply
                 hysteresis / cooldown via a pure evaluator, persist state under
                 an advisory lock, and print a machine-readable decision.
  record-switch  Record that the controller switched the active exit node (sets
                 the active role + switch timestamp used for cooldown).

This module NEVER mutates Tailscale. The calling controller runs `tailscale
set` only when `verdict` tells it to, then calls `record-switch` after a
verified switch.
"""

from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import fcntl
import json
import math
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator, Optional

# Kept in lock-step with the VERSION file (checked by tests/test_release_metadata.py).
__version__ = "1.1.1"

STATE_SCHEMA_VERSION = 1

DEFAULT_PING_TIMEOUT = 5.0
DEFAULT_HTTP_TIMEOUT = 5.0
DEFAULT_FAIL_THRESHOLD = 3
DEFAULT_OK_THRESHOLD = 3
DEFAULT_COOLDOWN = 60.0
DEFAULT_PROBE_TARGET = "https://ipinfo.io"
DEFAULT_STATUS_TIMEOUT = 15.0
DEFAULT_ROUTES = frozenset({"0.0.0.0/0", "::/0"})

# Upper bound for operator-supplied seconds values (probe timeouts / cooldown).
# Rejects absurd inputs (e.g. a 120-digit number) that would hang a probe or, in
# the shell wrapper, break `sleep` and spin the watcher.
MAX_TIMEOUT_SECONDS = 86400.0  # 1 day

STATE_UP = "UP"
STATE_DOWN = "DOWN"
STATE_UNKNOWN = "UNKNOWN"

_RTT_RE = re.compile(r"in\s+([0-9]+(?:\.[0-9]+)?)\s*ms")
_BARE_IP_RE = re.compile(r"\d{1,3}(?:\.\d{1,3}){3}")


def eprint(*parts: object) -> None:
    print(*parts, file=sys.stderr)


def now_epoch() -> float:
    return time.time()


def iso_now() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def tailscale_bin() -> str:
    return os.environ.get("TAILSCALE_BIN", "tailscale")


def env_float(name: str, default: float) -> float:
    """Read a finite float from the environment.

    Falls back to ``default`` when the variable is unset, unparseable, or
    non-finite (``nan`` / ``inf``). Never raises, so a malformed env var can
    never surface later as a ValueError/OverflowError traceback (e.g. when fed
    to ``subprocess.run(timeout=...)``)."""
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return default
    return value if math.isfinite(value) else default


# --------------------------------------------------------------------------- #
# Probing (stateless)
# --------------------------------------------------------------------------- #
@dataclass
class ProbeResult:
    label: str
    reachable: bool
    rtt_ms: Optional[float] = None
    egress_ok: Optional[bool] = None
    egress_ip: Optional[str] = None
    error: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "label": self.label,
            "reachable": self.reachable,
            "rtt_ms": self.rtt_ms,
            "egress_ok": self.egress_ok,
            "egress_ip": self.egress_ip,
            "error": self.error,
        }


def parse_rtt(text: str) -> Optional[float]:
    """Best-effort RTT extraction from `tailscale ping` output. Never raises."""
    match = _RTT_RE.search(text or "")
    if not match:
        return None
    try:
        return float(match.group(1))
    except ValueError:  # pragma: no cover - regex guarantees a number
        return None


def extract_ip(body: str) -> Optional[str]:
    """Pull an egress IP out of a probe response, best-effort. ipinfo.io returns
    JSON with an "ip" field; otherwise scan for the first bare IPv4."""
    try:
        data = json.loads(body)
        if isinstance(data, dict):
            ip = data.get("ip")
            if isinstance(ip, str) and ip:
                return ip
    except (json.JSONDecodeError, TypeError):
        pass
    match = _BARE_IP_RE.search(body or "")
    return match.group(0) if match else None


def tailscale_ping(label: str, timeout: float) -> ProbeResult:
    """Bounded tailnet reachability probe.

    Reachability is decided by the process exit code (or a visible "pong"); RTT
    is metadata only and never affects the verdict. Wall-clock time is bounded
    by ``timeout`` so the watcher cannot stall regardless of CLI flag support.
    """
    binary = tailscale_bin()
    # Prefer a single packet when supported; fall back if the flag is unknown so
    # we do not depend on flags that differ across Tailscale CLI versions.
    for args in (["ping", "-c", "1", label], ["ping", label]):
        try:
            proc = subprocess.run(
                [binary, *args],
                capture_output=True,
                text=True,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            return ProbeResult(label=label, reachable=False, error="ping timeout")
        except FileNotFoundError:
            return ProbeResult(label=label, reachable=False, error="tailscale binary not found")
        except OSError as exc:  # pragma: no cover - defensive
            return ProbeResult(label=label, reachable=False, error=f"ping error: {exc}")
        combined = f"{proc.stdout or ''}\n{proc.stderr or ''}"
        if "flag provided but not defined" in combined or "unknown flag" in combined:
            continue  # retry without the -c flag
        # Reachability is decided by the process exit code only. RTT and any
        # "pong" text are metadata and must never flip the verdict.
        reachable = proc.returncode == 0
        return ProbeResult(label=label, reachable=reachable, rtt_ms=parse_rtt(proc.stdout or ""))
    return ProbeResult(label=label, reachable=False, error="tailscale ping unsupported")


def http_egress(url: str, timeout: float) -> tuple[Optional[bool], Optional[str], Optional[str]]:
    """Short-timeout HTTP GET through whatever exit node is *currently active*.

    Returns ``(ok, egress_ip, error)``. This can only ever describe the active
    path; it must never be treated as proof that an inactive candidate node has
    working egress.
    """
    req = urllib.request.Request(
        url,
        method="GET",
        headers={"User-Agent": f"tailscale-ai-egress-health/{__version__}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - operator-supplied URL
            body = resp.read(4096).decode("utf-8", errors="replace")
            ok = 200 <= getattr(resp, "status", 0) < 400
            return ok, extract_ip(body), None
    except urllib.error.HTTPError as exc:
        return False, None, f"http {exc.code}"
    except (urllib.error.URLError, TimeoutError, ValueError, OSError) as exc:
        return None, None, f"egress error: {exc}"


def probe_node(
    label: str,
    *,
    ping_timeout: float,
    egress: bool,
    egress_url: str,
    http_timeout: float,
) -> ProbeResult:
    result = tailscale_ping(label, ping_timeout)
    if egress:
        ok, ip, err = http_egress(egress_url, http_timeout)
        result.egress_ok = ok
        result.egress_ip = ip
        if err and not result.error:
            result.error = err
    return result


# --------------------------------------------------------------------------- #
# State
# --------------------------------------------------------------------------- #
def default_node_state(label: str) -> dict[str, Any]:
    return {
        "configured_label": label,
        "node_id": None,
        "tailscale_ips": [],
        "last_state": STATE_UNKNOWN,
        "fail_count": 0,
        "ok_count": 0,
        "last_checked_at": None,
    }


def default_state(primary_label: str, fallback_label: str) -> dict[str, Any]:
    return {
        "schema_version": STATE_SCHEMA_VERSION,
        "active": {
            "role": "unknown",
            "configured_label": None,
            "node_id": None,
            "tailscale_ips": [],
            "last_switch_epoch": 0.0,
            "last_switch_at": None,
        },
        "nodes": {
            "primary": default_node_state(primary_label),
            "fallback": default_node_state(fallback_label),
        },
    }


def _as_int(value: Any, default: int = 0) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else default


def _as_float(value: Any, default: float = 0.0) -> float:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    return default


def _as_str_or_none(value: Any) -> Optional[str]:
    return value if isinstance(value, str) else None


def _as_str_list(value: Any) -> list[str]:
    return [item for item in value if isinstance(item, str)] if isinstance(value, list) else []


def normalize_state(raw: Any, primary_label: str, fallback_label: str) -> dict[str, Any]:
    """Return a well-formed, type-validated state dict. Corrupt input (wrong
    shape, bad field types, or old schema) is defensively discarded. Configured
    labels always win; if a role's stored label differs from the configured one,
    that role's health history is reset (it is a different node)."""
    base = default_state(primary_label, fallback_label)
    if not isinstance(raw, dict) or raw.get("schema_version") != STATE_SCHEMA_VERSION:
        return base

    raw_nodes = raw.get("nodes")
    if isinstance(raw_nodes, dict):
        for role, label in (("primary", primary_label), ("fallback", fallback_label)):
            stored = raw_nodes.get(role)
            if not isinstance(stored, dict):
                continue
            # Reset health history when the configured label changed (new node).
            if stored.get("configured_label") != label:
                continue
            target = base["nodes"][role]
            last_state = stored.get("last_state")
            target["last_state"] = last_state if last_state in (STATE_UP, STATE_DOWN, STATE_UNKNOWN) else STATE_UNKNOWN
            target["fail_count"] = _as_int(stored.get("fail_count"))
            target["ok_count"] = _as_int(stored.get("ok_count"))
            target["last_checked_at"] = _as_str_or_none(stored.get("last_checked_at"))
            target["node_id"] = _as_str_or_none(stored.get("node_id"))
            target["tailscale_ips"] = _as_str_list(stored.get("tailscale_ips"))

    raw_active = raw.get("active")
    if isinstance(raw_active, dict):
        active = base["active"]
        role_value = raw_active.get("role")
        active["role"] = role_value if role_value in ("primary", "fallback", "none", "unknown") else "unknown"
        active["configured_label"] = _as_str_or_none(raw_active.get("configured_label"))
        active["node_id"] = _as_str_or_none(raw_active.get("node_id"))
        active["tailscale_ips"] = _as_str_list(raw_active.get("tailscale_ips"))
        active["last_switch_epoch"] = _as_float(raw_active.get("last_switch_epoch"))
        active["last_switch_at"] = _as_str_or_none(raw_active.get("last_switch_at"))
    return base


def load_state(path: Path, primary_label: str, fallback_label: str) -> dict[str, Any]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        raw = None
    return normalize_state(raw, primary_label, fallback_label)


def save_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(path)


@contextlib.contextmanager
def state_lock(path: Path) -> Iterator[None]:
    """Advisory lock on ``<state>.lock`` (defense-in-depth; the controller also
    holds an outer process-level lock around the whole apply cycle)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.parent / (path.stem + ".lock")
    fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


# --------------------------------------------------------------------------- #
# Live-state reconciliation (canonical identity)
# --------------------------------------------------------------------------- #
def _norm_ip(value: str) -> str:
    return value.split("/")[0].strip().lower()


def resolve_identity(status: dict[str, Any], label: str) -> tuple[Optional[str], list[str]]:
    """Resolve a configured label (hostname / MagicDNS / Tailscale IP) to a
    canonical ``(node_id, tailscale_ips)`` via the status Peer map. Defensive:
    returns ``(None, [])`` if not found or the JSON shape is unexpected."""
    nodes: list[dict[str, Any]] = []
    self_node = status.get("Self")
    if isinstance(self_node, dict):
        nodes.append(self_node)
    peers = status.get("Peer")
    if isinstance(peers, dict):
        nodes.extend(peer for peer in peers.values() if isinstance(peer, dict))

    wanted = label.strip().lower().rstrip(".")
    matches: list[tuple[Optional[str], list[str]]] = []
    seen_ids: set[str] = set()
    for node in nodes:
        names: list[str] = []
        for key in ("HostName", "DNSName", "Name"):
            value = node.get(key)
            if isinstance(value, str) and value:
                normalized = value.strip().lower().rstrip(".")
                names.append(normalized)
                names.append(normalized.split(".")[0])
        ips = [_norm_ip(ip) for ip in (node.get("TailscaleIPs") or []) if isinstance(ip, str)]
        raw_id = node.get("ID") or node.get("StableID")
        node_id = str(raw_id) if raw_id else None
        if wanted in names or wanted in ips or (node_id is not None and wanted == node_id.lower()):
            if node_id is not None and node_id in seen_ids:
                continue
            if node_id is not None:
                seen_ids.add(node_id)
            matches.append((node_id, ips))
    if len(matches) > 1:
        eprint(
            f"warning: node label '{label}' is ambiguous (matches multiple peers); "
            "use a MagicDNS FQDN, Tailscale IP, or node ID. Treating as unresolved."
        )
        return (None, [])
    return matches[0] if matches else (None, [])


def live_active_role(
    status: dict[str, Any],
    primary_id: Optional[str],
    primary_ips: list[str],
    fallback_id: Optional[str],
    fallback_ips: list[str],
) -> str:
    """Map the live ExitNodeStatus to one of primary / fallback / none / unknown.

    "none" means no exit node is selected; "unknown" means some *other* exit
    node is selected (the controller must not override the user's choice).
    """
    exit_status = status.get("ExitNodeStatus")
    if not isinstance(exit_status, dict) or not exit_status:
        return "none"
    exit_id = exit_status.get("ID")
    exit_id = str(exit_id) if exit_id else None
    exit_ips = {_norm_ip(ip) for ip in (exit_status.get("TailscaleIPs") or []) if isinstance(ip, str)}

    def matches(node_id: Optional[str], ips: list[str]) -> bool:
        if node_id and exit_id and node_id == exit_id:
            return True
        return bool(ips) and bool(exit_ips) and bool(set(ips) & exit_ips)

    if matches(primary_id, primary_ips):
        return "primary"
    if matches(fallback_id, fallback_ips):
        return "fallback"
    return "unknown"


def get_status(status_file: Optional[str]) -> tuple[dict[str, Any], bool]:
    """Load `tailscale status --json` (or a file for testing).

    Returns ``(status, available)``. ``available`` is False when the status
    could not be obtained or parsed (command failure, non-zero exit, timeout,
    invalid JSON, or wrong shape). It is True for a valid object even if it is
    empty or has no ExitNodeStatus -- that is a legitimately observed state, not
    a failure. Callers MUST fail closed (no mutation) when ``available`` is
    False, rather than treating it as "no exit node selected"."""
    if status_file:
        try:
            data = json.loads(Path(status_file).read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            eprint(f"warning: could not read status file: {exc}")
            return {}, False
        return (data, True) if isinstance(data, dict) else ({}, False)
    binary = tailscale_bin()
    # Clamp to the same (0, MAX_TIMEOUT_SECONDS] bound as the other timeouts.
    # env_float already rejects nan/inf; a 0/negative value (immediate timeout),
    # or an oversized finite value -- which would otherwise hold the controller
    # lock for over a day on a hung status, or overflow subprocess.run for a huge
    # number like 1e120 -- falls back to the safe default.
    timeout = env_float("TAILSCALE_STATUS_TIMEOUT", DEFAULT_STATUS_TIMEOUT)
    if not (0 < timeout <= MAX_TIMEOUT_SECONDS):
        timeout = DEFAULT_STATUS_TIMEOUT
    try:
        proc = subprocess.run([binary, "status", "--json"], capture_output=True, text=True, timeout=timeout)
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError, ValueError, OverflowError) as exc:
        # ValueError/OverflowError are defensive: the clamp above keeps the timeout
        # in range, but never let a bad timeout surface as an unhandled traceback.
        eprint(f"warning: tailscale status failed: {exc}")
        return {}, False
    if proc.returncode != 0:
        eprint("warning: tailscale status returned non-zero")
        return {}, False
    try:
        data = json.loads(proc.stdout or "")
    except json.JSONDecodeError as exc:
        eprint(f"warning: tailscale status JSON parse failed: {exc}")
        return {}, False
    return (data, True) if isinstance(data, dict) else ({}, False)


# --------------------------------------------------------------------------- #
# Pure transition evaluator
# --------------------------------------------------------------------------- #
@dataclass
class Thresholds:
    fail_threshold: int = DEFAULT_FAIL_THRESHOLD
    ok_threshold: int = DEFAULT_OK_THRESHOLD
    cooldown: float = DEFAULT_COOLDOWN
    restore_primary: bool = True
    ensure_primary: bool = False


@dataclass
class Decision:
    action: str  # none | switch-to-fallback | switch-to-primary
    reason: str
    active_role: str
    primary_state: str
    fallback_state: str
    target_role: Optional[str] = None
    target_label: Optional[str] = None
    event: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "action": self.action,
            "reason": self.reason,
            "active_role": self.active_role,
            "primary_state": self.primary_state,
            "fallback_state": self.fallback_state,
            "target_role": self.target_role or "",
            "target_label": self.target_label or "",
            "event": self.event or "",
        }

    def to_lines(self) -> str:
        return "\n".join(f"{key}={value}" for key, value in self.to_dict().items())


def _apply_hysteresis(node: dict[str, Any], reachable: bool, th: Thresholds) -> None:
    if reachable:
        node["ok_count"] = int(node.get("ok_count", 0)) + 1
        node["fail_count"] = 0
        if node.get("last_state") != STATE_UP and node["ok_count"] >= th.ok_threshold:
            node["last_state"] = STATE_UP
    else:
        node["fail_count"] = int(node.get("fail_count", 0)) + 1
        node["ok_count"] = 0
        if node.get("last_state") != STATE_DOWN and node["fail_count"] >= th.fail_threshold:
            node["last_state"] = STATE_DOWN
    node["last_checked_at"] = iso_now()


def evaluate(
    state: dict[str, Any],
    active_role: str,
    primary_probe: ProbeResult,
    fallback_probe: ProbeResult,
    th: Thresholds,
    now: float,
) -> Decision:
    """Pure decision function (mutates only the hysteresis counters in ``state``).

    A switch is only ever proposed when the *target* node passed its tailnet
    ping THIS round, so we never fail over to an unverified node.
    """
    nodes = state["nodes"]
    _apply_hysteresis(nodes["primary"], primary_probe.reachable, th)
    _apply_hysteresis(nodes["fallback"], fallback_probe.reachable, th)
    p_state = str(nodes["primary"]["last_state"])
    f_state = str(nodes["fallback"]["last_state"])
    last_switch = float(state["active"].get("last_switch_epoch") or 0.0)
    in_cooldown = (now - last_switch) < th.cooldown

    def make(action: str, reason: str, target_role: Optional[str] = None, event: Optional[str] = None) -> Decision:
        label = str(nodes[target_role]["configured_label"]) if target_role else None
        return Decision(
            action=action,
            reason=reason,
            active_role=active_role,
            primary_state=p_state,
            fallback_state=f_state,
            target_role=target_role,
            target_label=label,
            event=event,
        )

    if active_role == "primary":
        if p_state != STATE_DOWN:
            return make("none", "healthy")
        if not fallback_probe.reachable:
            return make("none", "both_down" if f_state == STATE_DOWN else "fallback_unverified")
        if in_cooldown:
            return make("none", "cooldown")
        return make("switch-to-fallback", "primary_down", target_role="fallback")

    if active_role == "fallback":
        if primary_probe.reachable and p_state == STATE_UP:
            if not th.restore_primary:
                return make("none", "restore_primary_disabled", event="primary_recovered")
            if in_cooldown:
                return make("none", "cooldown", event="primary_recovered")
            return make("switch-to-primary", "primary_recovered", target_role="primary", event="primary_recovered")
        if f_state == STATE_DOWN:
            return make("none", "both_down" if p_state == STATE_DOWN else "fallback_down")
        return make("none", "staying_on_fallback")

    if active_role == "none":
        # No exit node selected. By default do not impose one (observe-first /
        # least surprise); with ensure_primary, select the primary once it is
        # reachable this round.
        if th.ensure_primary and primary_probe.reachable:
            if in_cooldown:
                return make("none", "cooldown")
            return make("switch-to-primary", "ensure_primary", target_role="primary")
        return make("none", "no_active_exit_node")

    # A different exit node is selected; never override the user's choice.
    return make("none", "unknown_active")


# --------------------------------------------------------------------------- #
# CLI handlers
# --------------------------------------------------------------------------- #
def cmd_probe(args: argparse.Namespace) -> int:
    result = probe_node(
        args.node,
        ping_timeout=args.ping_timeout,
        egress=args.egress,
        egress_url=args.egress_url,
        http_timeout=args.http_timeout,
    )
    if args.json:
        print(json.dumps({"schema_version": STATE_SCHEMA_VERSION, "probe": result.to_dict()}, sort_keys=True))
    else:
        print(f"node={result.label} reachable={int(result.reachable)} rtt_ms={result.rtt_ms}")
        if args.egress:
            print(f"egress_ok={result.egress_ok} egress_ip={result.egress_ip}")
        if result.error:
            print(f"error={result.error}")
    return 0 if result.reachable else 1


def _candidates_distinct(p_id: Optional[str], p_ips: list[str], f_id: Optional[str], f_ips: list[str]) -> bool:
    if p_id and f_id and p_id == f_id:
        return False
    return not (set(p_ips) & set(f_ips))


def _live_state_problem(
    status: dict[str, Any], available: bool, primary_label: str, fallback_label: str
) -> tuple[Optional[str], Optional[str], list[str], Optional[str], list[str]]:
    """Return ``(problem, primary_id, primary_ips, fallback_id, fallback_ips)``.

    ``problem`` is non-None when the live state cannot support a failover
    decision: status unreadable (``live_status_unavailable``); readable but with
    no usable Self or unresolvable candidates (``live_status_incomplete``); or
    both candidates resolving to the same node (``candidates_not_distinct``).
    Callers MUST fail closed in these cases -- including under --ensure-primary."""
    if not available:
        return "live_status_unavailable", None, [], None, []
    # `tailscale status --json` returns 0 and valid JSON even when the backend is
    # Stopped / NeedsLogin / Starting, so the exit code is not enough: require an
    # explicit Running backend. ipnstate.Status.BackendState is always a string in
    # a well-formed status, so a missing / null / non-string value is malformed --
    # fail closed rather than assume the backend is up.
    if status.get("BackendState") != "Running":
        return "backend_not_running", None, [], None, []
    self_node = status.get("Self")
    if not isinstance(self_node, dict) or not self_node:
        return "live_status_incomplete", None, [], None, []
    p_id, p_ips = resolve_identity(status, primary_label)
    f_id, f_ips = resolve_identity(status, fallback_label)
    if (not p_id and not p_ips) or (not f_id and not f_ips):
        return "live_status_incomplete", p_id, p_ips, f_id, f_ips
    if not _candidates_distinct(p_id, p_ips, f_id, f_ips):
        return "candidates_not_distinct", p_id, p_ips, f_id, f_ips
    return None, p_id, p_ips, f_id, f_ips


def _set_active_identity(
    state: dict[str, Any],
    active_role: str,
    primary_id: Optional[str],
    primary_ips: list[str],
    fallback_id: Optional[str],
    fallback_ips: list[str],
    primary_label: str,
    fallback_label: str,
) -> None:
    """Keep active.* canonical identity fresh on every reconcile (not only on a
    controller switch), so the persisted state never shows stale identity."""
    active = state["active"]
    active["role"] = active_role
    if active_role == "primary":
        active["configured_label"] = primary_label
        active["node_id"] = primary_id
        active["tailscale_ips"] = primary_ips
    elif active_role == "fallback":
        active["configured_label"] = fallback_label
        active["node_id"] = fallback_id
        active["tailscale_ips"] = fallback_ips
    else:
        active["configured_label"] = None
        active["node_id"] = None
        active["tailscale_ips"] = []


def cmd_verdict(args: argparse.Namespace) -> int:
    state_path = Path(args.state_file)
    th = Thresholds(
        fail_threshold=args.fail_threshold,
        ok_threshold=args.ok_threshold,
        cooldown=args.cooldown,
        restore_primary=bool(args.restore_primary),
        ensure_primary=bool(args.ensure_primary),
    )
    with state_lock(state_path):
        state = load_state(state_path, args.primary, args.fallback)
        status, available = get_status(args.status_json_file)
        problem, primary_id, primary_ips, fallback_id, fallback_ips = _live_state_problem(
            status, available, args.primary, args.fallback
        )

        if problem is not None:
            # Fail closed: do not infer the active node from the (possibly stale)
            # state file, and do not mutate -- not even with --ensure-primary.
            decision = Decision(
                action="none",
                reason=problem,
                active_role="unknown",
                primary_state=str(state["nodes"]["primary"]["last_state"]),
                fallback_state=str(state["nodes"]["fallback"]["last_state"]),
            )
            primary_probe = ProbeResult(label=args.primary, reachable=False, error=problem)
            fallback_probe = ProbeResult(label=args.fallback, reachable=False, error=problem)
        else:
            for role, resolved_id, resolved_ips in (
                ("primary", primary_id, primary_ips),
                ("fallback", fallback_id, fallback_ips),
            ):
                node = state["nodes"][role]
                stored_id = node.get("node_id")
                stored_ips = node.get("tailscale_ips") or []
                if stored_id and resolved_id:
                    identity_changed = stored_id != resolved_id
                else:
                    # No reliable ID on at least one side: fall back to IP sets and
                    # treat disjoint non-empty sets as a different node.
                    identity_changed = bool(stored_ips) and bool(resolved_ips) and not (set(stored_ips) & set(resolved_ips))
                if identity_changed:
                    # Same label now points at a different node (recreated / new IP):
                    # reset its health history instead of inheriting old state.
                    node["last_state"] = STATE_UNKNOWN
                    node["fail_count"] = 0
                    node["ok_count"] = 0
                    node["last_checked_at"] = None
                node["node_id"] = resolved_id
                node["tailscale_ips"] = resolved_ips

            active_role = live_active_role(status, primary_id, primary_ips, fallback_id, fallback_ips)
            prev_role = state["active"].get("role")
            if prev_role != active_role:
                eprint(f"warning: live active exit-node role '{active_role}' overrides stale state '{prev_role}'")
            _set_active_identity(
                state, active_role, primary_id, primary_ips, fallback_id, fallback_ips, args.primary, args.fallback
            )

            primary_probe = probe_node(
                args.primary,
                ping_timeout=args.ping_timeout,
                egress=args.egress and active_role == "primary",
                egress_url=args.egress_url,
                http_timeout=args.http_timeout,
            )
            fallback_probe = probe_node(
                args.fallback,
                ping_timeout=args.ping_timeout,
                egress=args.egress and active_role == "fallback",
                egress_url=args.egress_url,
                http_timeout=args.http_timeout,
            )

            decision = evaluate(state, active_role, primary_probe, fallback_probe, th, now_epoch())
            save_state(state_path, state)

    if args.json:
        payload = {
            "schema_version": STATE_SCHEMA_VERSION,
            "decision": decision.to_dict(),
            "primary": primary_probe.to_dict(),
            "fallback": fallback_probe.to_dict(),
        }
        print(json.dumps(payload, sort_keys=True))
    else:
        print(decision.to_lines())
    return 0


def cmd_record_switch(args: argparse.Namespace) -> int:
    state_path = Path(args.state_file)
    with state_lock(state_path):
        state = load_state(state_path, args.primary, args.fallback)
        role = args.role
        active = state["active"]
        active["role"] = role
        active["last_switch_epoch"] = now_epoch()
        active["last_switch_at"] = iso_now()
        if role in ("primary", "fallback"):
            node = state["nodes"][role]
            active["configured_label"] = node.get("configured_label")
            active["node_id"] = node.get("node_id")
            active["tailscale_ips"] = node.get("tailscale_ips")
        else:
            active["configured_label"] = None
            active["node_id"] = None
            active["tailscale_ips"] = []
        save_state(state_path, state)
    print(f"recorded active role={role}")
    return 0


def cmd_active_role(args: argparse.Namespace) -> int:
    """Print the live active exit-node role. Used by the controller for the
    post-switch readback (and handy for diagnostics)."""
    status, available = get_status(args.status_json_file)
    # Fail closed for the controller's post-switch readback: an unreadable status,
    # or a backend that is not Running, must not be reported as a concrete active
    # role. The JSON can still carry stale ExitNodeStatus after the backend stops,
    # which would otherwise let the controller record a "successful" switch and
    # start its cooldown against a node whose egress is actually down.
    if not available or status.get("BackendState") != "Running":
        print("unknown")
        return 0
    primary_id, primary_ips = resolve_identity(status, args.primary)
    fallback_id, fallback_ips = resolve_identity(status, args.fallback)
    print(live_active_role(status, primary_id, primary_ips, fallback_id, fallback_ips))
    return 0


# --------------------------------------------------------------------------- #
# Connector native-HA monitoring (read-only; never switches anything)
# --------------------------------------------------------------------------- #
def _find_node(status: dict[str, Any], node_id: Optional[str], ips: list[str]) -> Optional[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    self_node = status.get("Self")
    if isinstance(self_node, dict):
        candidates.append(self_node)
    peers = status.get("Peer")
    if isinstance(peers, dict):
        candidates.extend(peer for peer in peers.values() if isinstance(peer, dict))
    wanted_ips = {_norm_ip(ip) for ip in ips}
    for node in candidates:
        raw_id = node.get("ID") or node.get("StableID")
        nid = str(raw_id) if raw_id else None
        node_ips = {_norm_ip(ip) for ip in (node.get("TailscaleIPs") or []) if isinstance(ip, str)}
        if (node_id and nid == node_id) or (wanted_ips and wanted_ips & node_ips):
            return node
    return None


def node_online(status: dict[str, Any], node_id: Optional[str], ips: list[str]) -> Optional[bool]:
    node = _find_node(status, node_id, ips)
    if node is None:
        return None
    value = node.get("Online")
    return bool(value) if isinstance(value, bool) else None


def node_routes(status: dict[str, Any], node_id: Optional[str], ips: list[str]) -> Optional[list[str]]:
    """App-connector / subnet routes a connector currently advertises, best-effort
    from the client's status view. Returns None if the node is absent. Prefers
    PrimaryRoutes; falls back to AllowedIPs minus the node's own host addresses."""
    node = _find_node(status, node_id, ips)
    if node is None:
        return None
    # If PrimaryRoutes is present at all it is authoritative: an empty list means
    # this node currently advertises no app-connector/subnet routes. Only fall
    # back to AllowedIPs when the field is absent entirely. Default (exit-node)
    # routes are excluded throughout; this remains a best-effort signal that a
    # non-default route is advertised, not proof that AI app routes resolve.
    if "PrimaryRoutes" in node:
        primary_routes = node.get("PrimaryRoutes")
        if not isinstance(primary_routes, list):
            return []
        return [route for route in primary_routes if isinstance(route, str) and route.strip() not in DEFAULT_ROUTES]
    self_ips = {_norm_ip(ip) for ip in (node.get("TailscaleIPs") or []) if isinstance(ip, str)}
    extra: list[str] = []
    for entry in node.get("AllowedIPs") or []:
        if not isinstance(entry, str):
            continue
        if entry.strip() in DEFAULT_ROUTES or _norm_ip(entry) in self_ips:
            continue
        extra.append(entry)
    return extra


# --------------------------------------------------------------------------- #
# Per-peer metrics: read-only counters + liveness. See
# docs/design/metrics-collection.md. Single normative shape: a FIXED key set,
# keys never omitted; every value null on transport/resolution failure.
# --------------------------------------------------------------------------- #
PEER_METRIC_KEYS = (
    "tx_bytes_total",
    "rx_bytes_total",
    "online",
    "active",
    "last_handshake",
    "last_handshake_age_seconds",
    "relay",
    "cur_addr",
    "connection_path",
)


def _null_metrics() -> dict[str, Any]:
    return {key: None for key in PEER_METRIC_KEYS}


def _int_or_none(value: Any) -> Optional[int]:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _str_or_none(value: Any) -> Optional[str]:
    return value if isinstance(value, str) and value else None


def _bool_or_none(value: Any) -> Optional[bool]:
    return value if isinstance(value, bool) else None


def _fmt_metric(value: Any) -> str:
    """Render a metric value for text output; null renders as ``-``."""
    return "-" if value is None else str(value)


def _parse_handshake(value: Any) -> Optional[dt.datetime]:
    """Parse a Tailscale RFC3339 LastHandshake into an aware UTC datetime, or None
    for the Go zero-value (0001-01-01...), absent, or unparseable timestamp."""
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text or text.startswith("0001-01-01"):
        return None
    # Only a trailing 'Z' is the UTC designator; don't touch any other 'Z'.
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    # Python 3.9 fromisoformat accepts at most microsecond precision; trim extra.
    text = re.sub(r"(\.\d{6})\d+", r"\1", text)
    try:
        parsed = dt.datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed


def _connection_path(cur_addr: Optional[str], online: Optional[bool]) -> str:
    """direct if a current direct address is present; else derp when the peer is
    online; else unknown. Observed on this host with Tailscale 1.98.2: Relay was
    set for every observed peer (the home/preferred DERP region), so CurAddr is
    the direct-path discriminator, not Relay. Best-effort: consumers needing
    certainty can use the raw cur_addr / relay fields."""
    if cur_addr:
        return "direct"
    if online is True:
        return "derp"
    return "unknown"


def peer_metrics(
    status: dict[str, Any],
    available: bool,
    label: str,
    *,
    now: Optional[dt.datetime] = None,
) -> dict[str, Any]:
    """Read-only per-peer counters + liveness for a connector label. Returns the
    fixed-key metrics object; ALL values null on transport failure (status
    unavailable) or resolution failure (peer not found). On a resolved peer,
    individual missing / zero-value fields are null but keys are never omitted.

    tx/rx are cumulative byte counters since tailscaled started (reset on restart),
    NOT session or billing usage."""
    if not available:
        return _null_metrics()
    node_id, ips = resolve_identity(status, label)
    node = _find_node(status, node_id, ips)
    if node is None:
        return _null_metrics()
    obj = _null_metrics()
    obj["tx_bytes_total"] = _int_or_none(node.get("TxBytes"))
    obj["rx_bytes_total"] = _int_or_none(node.get("RxBytes"))
    obj["online"] = _bool_or_none(node.get("Online"))
    obj["active"] = _bool_or_none(node.get("Active"))
    raw_handshake = node.get("LastHandshake")
    handshake = _parse_handshake(raw_handshake)
    if handshake is not None:
        obj["last_handshake"] = raw_handshake.strip() if isinstance(raw_handshake, str) else None
        current = now if now is not None else dt.datetime.now(dt.timezone.utc)
        if current.tzinfo is None:  # normalize a caller-supplied naive now to UTC
            current = current.replace(tzinfo=dt.timezone.utc)
        obj["last_handshake_age_seconds"] = max(0, int((current - handshake).total_seconds()))
    obj["relay"] = _str_or_none(node.get("Relay"))
    obj["cur_addr"] = _str_or_none(node.get("CurAddr"))
    obj["connection_path"] = _connection_path(obj["cur_addr"], obj["online"])
    return obj


def _safe_peer_metrics(status: dict[str, Any], available: bool, label: str) -> dict[str, Any]:
    """Non-gating wrapper: metrics extraction must NEVER abort the monitor report
    (the hard rule in docs/design/metrics-collection.md), so any unexpected error
    degrades to the null-filled object instead of propagating."""
    try:
        return peer_metrics(status, available, label)
    except Exception:
        return _null_metrics()


def fetch_devices_via_api() -> Optional[list[Any]]:  # pragma: no cover - network path
    token = os.environ.get("TAILSCALE_API_KEY")
    if not token:
        return None
    tailnet = os.environ.get("TAILSCALE_TAILNET", "-")
    # Encode the tailnet in the path segment so reserved characters (/, #, ?) in
    # the operator-supplied name cannot rewrite the request path; matches
    # policy_tool.tailnet_path().
    quoted_tailnet = urllib.parse.quote(tailnet, safe="")
    url = f"https://api.tailscale.com/api/v2/tailnet/{quoted_tailnet}/devices"
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    try:
        with urllib.request.urlopen(req, timeout=DEFAULT_HTTP_TIMEOUT) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, json.JSONDecodeError, ValueError, OSError):
        return None
    return data.get("devices") if isinstance(data, dict) else None


def load_devices(devices_file: Optional[str]) -> Optional[list[Any]]:
    """Device list for ordering: from a file (testing), else the API if a token
    is configured, else None. Absence of a source is NOT an error."""
    if devices_file:
        try:
            data = json.loads(Path(devices_file).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        if isinstance(data, dict):
            devices = data.get("devices")
            return devices if isinstance(devices, list) else None
        return data if isinstance(data, list) else None
    if os.environ.get("TAILSCALE_API_KEY"):
        return fetch_devices_via_api()
    return None


def _device_created(devices: list[Any], label: str) -> Optional[str]:
    wanted = label.strip().lower().rstrip(".")
    for device in devices:
        if not isinstance(device, dict):
            continue
        names: set[str] = set()
        for key in ("hostname", "name"):
            value = device.get(key)
            if isinstance(value, str) and value:
                normalized = value.strip().lower().rstrip(".")
                names.add(normalized)
                names.add(normalized.split(".")[0])
        if wanted in names:
            created = device.get("created")
            return created if isinstance(created, str) else None
    return None


def connector_ordering(devices: Optional[list[Any]], primary_label: str, fallback_label: str) -> tuple[str, str]:
    """Best-effort 'which connector is the oldest (= native primary)' check.
    Always degrades gracefully; never raises and never requires a token."""
    if devices is None:
        return "unavailable", "no_api_token_or_source"
    primary_created = _device_created(devices, primary_label)
    fallback_created = _device_created(devices, fallback_label)
    if not primary_created or not fallback_created:
        return "unavailable", "device_created_not_found"
    if primary_created <= fallback_created:
        return "primary_is_oldest", f"primary created {primary_created} <= fallback {fallback_created}"
    return "fallback_is_oldest", f"fallback created {fallback_created} < primary {primary_created}"


def _fmt_online(value: Optional[bool]) -> str:
    if value is True:
        return "1"
    if value is False:
        return "0"
    return "unknown"


def cmd_connectors(args: argparse.Namespace) -> int:
    status, available = get_status(args.status_json_file)
    # A well-formed status always carries a string BackendState; treat anything
    # other than "Running" (including a missing / null / non-string value) as down.
    backend_down = status.get("BackendState") != "Running"
    rows: list[dict[str, Any]] = []
    reachable_ok = True
    serving = "none"
    for role, label in (("primary", args.primary), ("fallback", args.fallback)):
        node_id, ips = resolve_identity(status, label)
        online = node_online(status, node_id, ips)
        result = tailscale_ping(label, args.ping_timeout)
        routes = node_routes(status, node_id, ips)
        route_count = len(routes) if routes is not None else None
        if not (online and result.reachable):
            reachable_ok = False
        if route_count and serving == "none":
            serving = role
        rows.append({
            "connector": role,
            "label": label,
            "online": online,
            "reachable": result.reachable,
            "rtt_ms": result.rtt_ms,
            "routes": route_count,
            # Additive per-peer counters + liveness (read-only). Never gates the
            # monitor's health verdict below; null-filled (and exception-isolated)
            # if unavailable.
            "metrics": _safe_peer_metrics(status, available, label),
        })
    # In oldest-first HA exactly one connector serves routes at a time, so the
    # pair is route-healthy when at least one advertises app-connector/subnet
    # routes; "none" means routing is broken even if both nodes are pingable.
    routes_ok = serving != "none"
    overall_healthy = available and not backend_down and reachable_ok and (not args.require_routes or routes_ok)
    order, order_reason = connector_ordering(load_devices(args.devices_json_file), args.primary, args.fallback)
    overall = "healthy" if overall_healthy else "degraded"
    if args.json:
        print(json.dumps({
            "schema_version": STATE_SCHEMA_VERSION,
            "connectors": rows,
            "ordering": order,
            "ordering_reason": order_reason,
            "routes_serving": serving,
            "overall": overall,
        }, sort_keys=True))
    else:
        for row in rows:
            route_text = row["routes"] if row["routes"] is not None else "unknown"
            print(
                f"connector={row['connector']} label={row['label']} "
                f"online={_fmt_online(row['online'])} reachable={int(row['reachable'])} "
                f"rtt_ms={row['rtt_ms']} routes={route_text}"
            )
            m = row["metrics"]
            print(
                f"[metrics] connector={row['connector']} "
                f"tx={_fmt_metric(m['tx_bytes_total'])} rx={_fmt_metric(m['rx_bytes_total'])} "
                f"path={_fmt_metric(m['connection_path'])} "
                f"handshake_age={_fmt_metric(m['last_handshake_age_seconds'])}"
            )
        print(f"ordering={order} reason={order_reason}")
        print(f"routes_serving={serving}")
        print(f"overall={overall}")
    return 0 if overall_healthy else 1


def cmd_peer_metrics(args: argparse.Namespace) -> int:
    """Print the read-only metrics object for one connector label as JSON. Always
    exits 0 when it can print the object (including the null-filled object on
    transport/resolution failure); non-zero only for usage/arg errors. This is the
    single source of truth for the metrics object shape used by the monitor."""
    status, available = get_status(args.status_json_file)
    print(json.dumps(peer_metrics(status, available, args.node), sort_keys=True))
    return 0


# --------------------------------------------------------------------------- #
# Argument parsing
# --------------------------------------------------------------------------- #
def _finite_float(value: str, *, allow_zero: bool, label: str) -> float:
    """argparse type: a finite (non-nan/inf) float within the required range and
    not exceeding ``MAX_TIMEOUT_SECONDS`` (so an absurd value cannot hang a
    probe). Applied to env-var defaults too, since argparse runs ``type`` on a
    string default but does NOT check ``choices`` on it."""
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        raise argparse.ArgumentTypeError(f"invalid number: {value!r}")
    too_small = parsed < 0 or (parsed == 0 and not allow_zero)
    if not math.isfinite(parsed) or too_small or parsed > MAX_TIMEOUT_SECONDS:
        raise argparse.ArgumentTypeError(f"must be {label} (got {value!r})")
    return parsed


def _pos_float(value: str) -> float:
    return _finite_float(value, allow_zero=False, label=f"a finite number in (0, {int(MAX_TIMEOUT_SECONDS)}]")


def _nonneg_float(value: str) -> float:
    return _finite_float(value, allow_zero=True, label=f"a finite number in [0, {int(MAX_TIMEOUT_SECONDS)}]")


def _pos_int(value: str) -> int:
    """argparse type: an integer >= 1 (rejects 0, negatives, and non-integers)."""
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        raise argparse.ArgumentTypeError(f"invalid integer: {value!r}")
    if parsed < 1:
        raise argparse.ArgumentTypeError(f"must be an integer >= 1 (got {value!r})")
    return parsed


def _bool01(value: str) -> int:
    """argparse type: exactly 0 or 1. Used instead of relying on ``choices``
    because argparse does not validate a string env-var default against
    ``choices`` (it only applies ``type``)."""
    if value not in ("0", "1"):
        raise argparse.ArgumentTypeError(f"must be 0 or 1 (got {value!r})")
    return int(value)


def _add_common_probe_args(parser: argparse.ArgumentParser) -> None:
    # Defaults are kept as raw strings so argparse runs the `type` validator on a
    # value taken from the environment, not just on command-line input.
    parser.add_argument("--ping-timeout", type=_pos_float, default=os.environ.get("PING_TIMEOUT", str(DEFAULT_PING_TIMEOUT)))
    parser.add_argument("--http-timeout", type=_pos_float, default=os.environ.get("PROBE_HTTP_TIMEOUT", str(DEFAULT_HTTP_TIMEOUT)))
    parser.add_argument("--egress-url", default=os.environ.get("PROBE_TARGET", DEFAULT_PROBE_TARGET))


def _add_pair_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--state-file", required=True)
    parser.add_argument("--primary", default=os.environ.get("PRIMARY_EXIT_NODE", ""))
    parser.add_argument("--fallback", default=os.environ.get("FALLBACK_EXIT_NODE", ""))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--version",
        action="version",
        version=f"tailscale-ai-egress health_check.py {__version__}",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    probe = sub.add_parser("probe", help="One-shot health probe of a single node (the on-demand ping).")
    probe.add_argument("--node", required=True, help="Node label: hostname, MagicDNS name, or Tailscale IP.")
    probe.add_argument("--egress", action="store_true", help="Also HTTP-test egress of the CURRENTLY ACTIVE exit node.")
    probe.add_argument("--json", action="store_true")
    _add_common_probe_args(probe)
    probe.set_defaults(func=cmd_probe)

    verdict = sub.add_parser("verdict", help="Stateful failover decision for the primary/fallback pair.")
    _add_pair_args(verdict)
    verdict.add_argument("--status-json-file", help="Read status JSON from a file instead of running tailscale (testing).")
    verdict.add_argument("--fail-threshold", type=_pos_int, default=os.environ.get("FAIL_THRESHOLD", str(DEFAULT_FAIL_THRESHOLD)))
    verdict.add_argument("--ok-threshold", type=_pos_int, default=os.environ.get("OK_THRESHOLD", str(DEFAULT_OK_THRESHOLD)))
    verdict.add_argument("--cooldown", type=_nonneg_float, default=os.environ.get("COOLDOWN", str(DEFAULT_COOLDOWN)))
    verdict.add_argument("--restore-primary", type=_bool01, default=os.environ.get("RESTORE_PRIMARY", "1"))
    verdict.add_argument(
        "--ensure-primary",
        action="store_true",
        help="When no exit node is selected, select the primary once it is reachable.",
    )
    verdict.add_argument("--egress", action="store_true")
    verdict.add_argument("--json", action="store_true")
    _add_common_probe_args(verdict)
    verdict.set_defaults(func=cmd_verdict)

    record = sub.add_parser("record-switch", help="Record a verified exit-node switch (sets active role + cooldown clock).")
    _add_pair_args(record)
    record.add_argument("--role", required=True, choices=("primary", "fallback", "none"))
    record.set_defaults(func=cmd_record_switch)

    active = sub.add_parser("active-role", help="Print the live active exit-node role (primary/fallback/none/unknown).")
    active.add_argument("--primary", default=os.environ.get("PRIMARY_EXIT_NODE", ""))
    active.add_argument("--fallback", default=os.environ.get("FALLBACK_EXIT_NODE", ""))
    active.add_argument("--status-json-file")
    active.set_defaults(func=cmd_active_role)

    connectors = sub.add_parser("connectors", help="Report health/ordering of the primary+fallback App Connector pair (read-only).")
    connectors.add_argument("--primary", default=os.environ.get("PRIMARY_CONNECTOR", os.environ.get("PRIMARY_EXIT_NODE", "")))
    connectors.add_argument("--fallback", default=os.environ.get("FALLBACK_CONNECTOR", os.environ.get("FALLBACK_EXIT_NODE", "")))
    connectors.add_argument("--status-json-file")
    connectors.add_argument("--devices-json-file")
    connectors.add_argument("--ping-timeout", type=_pos_float, default=os.environ.get("PING_TIMEOUT", str(DEFAULT_PING_TIMEOUT)))
    connectors.add_argument(
        "--require-routes",
        type=_bool01,
        default=os.environ.get("REQUIRE_ROUTES", "1"),
        help="Degrade when neither connector advertises routes (default 1). Set 0 if routes are not client-visible.",
    )
    connectors.add_argument("--json", action="store_true")
    connectors.set_defaults(func=cmd_connectors)

    peer = sub.add_parser(
        "peer-metrics",
        help="Read-only per-peer counters + liveness for one connector label (JSON; always exits 0).",
    )
    peer.add_argument("--node", required=True)
    peer.add_argument("--status-json-file")
    peer.add_argument("--json", action="store_true", help="Accepted for consistency; output is always JSON.")
    peer.set_defaults(func=cmd_peer_metrics)

    return parser


def main(argv: Optional[list[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
