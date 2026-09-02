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
import ipaddress
import json
import math
import os
import re
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterator, Optional

# Kept in lock-step with the VERSION file (checked by tests/test_release_metadata.py).
__version__ = "1.3.0"

STATE_SCHEMA_VERSION = 1

DEFAULT_PING_TIMEOUT = 5.0
DEFAULT_HTTP_TIMEOUT = 5.0
DEFAULT_FAIL_THRESHOLD = 3
DEFAULT_OK_THRESHOLD = 3
DEFAULT_COOLDOWN = 60.0
DEFAULT_PROBE_TARGET = "https://ipinfo.io"
DEFAULT_STATUS_TIMEOUT = 15.0

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
            # Reset health history when the configured label changed (new node). IP-valued
            # labels are compared by canonical address, so re-spelling the same IP (expanded
            # vs compressed IPv6, case) across runs is not mistaken for a node change (which
            # would drop history and suppress a due failover across an upgrade).
            if not _labels_equivalent(stored.get("configured_label"), label):
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
def _safe_ip_token(text: str) -> bool:
    """Reject an IP / CIDR token that ``ipaddress`` accepts but Tailscale never emits
    and that could forge a match or crash:

    - an IPv6 zone id (``fd7a::1%eth0``): ``ipaddress`` accepts ``%scope`` and a scoped
      form is never equal to the same address unscoped, so it could forge an identity
      match or (as ``::%x/0``) slip past the default-route exclusion;
    - a slash suffix that is not a SHORT decimal prefix: a dotted netmask / host-mask
      (``/255.255.255.0``), or an over-long digit run (``/999...`` thousands of digits)
      that would make ``int()`` raise on Python >= 3.11 (the 4300-digit limit) -- a
      crash on the very path this hardening protects.

    A well-formed token still needs family/range validation by the caller."""
    if "%" in text:
        return False
    _, slash, prefix = text.partition("/")
    if slash and not (prefix.isascii() and prefix.isdigit() and len(prefix) <= 3):
        return False
    if slash and len(prefix) > 1 and prefix.startswith("0"):
        return False  # non-canonical leading-zero prefix (e.g. /032) -> Tailscale never emits
    return True


def _canon_addr(text: str) -> Optional[str]:
    """Canonical address string of a token's address part (before any ``/``), so two
    spellings of the same address -- compressed vs expanded IPv6, mixed case -- compare
    equal. Returns None if the address part is not a valid IP."""
    try:
        return str(ipaddress.ip_address(text.partition("/")[0].strip()))
    except ValueError:
        return None


def _labels_equivalent(a: Any, b: Any) -> bool:
    """True if two configured labels denote the same target: exact text, or -- when both
    are IP-valued -- the same canonical address, so re-spelling an IP label (expanded vs
    compressed IPv6, case) is not mistaken for a different node. Hostnames and node IDs
    keep the exact-text comparison (a native IPv4 and its v6-mapped form stay distinct)."""
    if a == b:
        return True
    if not isinstance(a, str) or not isinstance(b, str):
        return False
    ca = _canon_addr(a.strip().lower())
    return ca is not None and ca == _canon_addr(b.strip().lower())


def _strict_norm_ip(value: str) -> Optional[str]:
    """Validate a Tailscale HOST-IP string and return its CANONICAL address (so equal
    addresses spelled differently match), else None.

    Accepts a bare address, or ``addr/N`` where N is exactly the host prefix for the
    family (``/32`` or ``/128``) -- a TailscaleIP is a single host address, so any other
    or non-canonical suffix (``/24``, ``/032``, a dotted netmask), an IPv6 zone id, or an
    over-long prefix is rejected (see ``_safe_ip_token``) rather than truncated or
    coerced into a false identity match. Returns the ``ipaddress``-canonical form, not a
    raw text strip, so ``fd7a::1`` and ``fd7a:0:0:0:0:0:0:1`` collapse to one key."""
    text = value.strip()
    if not _safe_ip_token(text):
        return None
    addr, slash, prefix = text.partition("/")
    try:
        ip = ipaddress.ip_address(addr)
    except ValueError:
        return None
    if slash and prefix != str(ip.max_prefixlen):
        return None  # a host-identity token's only valid suffix is its own /32 or /128
    return str(ip)


def _valid_ip_set(raw: Any) -> Optional[set[str]]:
    """Parse a status IP list into a set of CANONICAL address strings (see
    ``_strict_norm_ip``) for identity/gating comparisons, or None if the field is not a
    list or ANY element is not a str or not a strictly-valid IP -- so a malformed
    element fails the WHOLE field closed rather than slipping through."""
    if not isinstance(raw, list):
        return None
    out: set[str] = set()
    for item in raw:
        if not isinstance(item, str):
            return None
        norm = _strict_norm_ip(item)
        if norm is None:
            return None
        out.add(norm)
    return out


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
    # Canonicalize an IP-valued label the same way the peer IPs are, so an expanded or
    # otherwise non-canonical IPv6 label still matches the peer's canonical address
    # (else this side would be asymmetric and a valid IP label would fail to resolve).
    wanted_ip = _canon_addr(wanted)
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
        # Validated IPs only: a malformed element (e.g. "100.64.0.1/not-a-prefix" or a
        # dotted netmask) must not normalize into a peer's IP identity, or it could
        # forge a live-role / connector match. The peer can still resolve by name/ID.
        ips = sorted(_valid_ip_set(node.get("TailscaleIPs")) or set())
        raw_id = node.get("ID") or node.get("StableID")
        node_id = str(raw_id) if raw_id else None
        if (wanted in names or (wanted_ip is not None and wanted_ip in ips)
                or (node_id is not None and wanted == node_id.lower())):
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
    if exit_status is None:
        return "none"  # field absent/null -> genuinely no exit node selected
    if not isinstance(exit_status, dict) or not exit_status:
        # present but malformed (scalar/list) or empty -> untrustworthy. "none" is
        # actionable (it authorizes switch-to-primary under --ensure-primary), so a
        # status we cannot trust must fail closed to "unknown", never "none".
        return "unknown"
    exit_id = exit_status.get("ID")
    exit_id = str(exit_id) if exit_id else None
    # Validated IPs only: a malformed element (e.g. "100.64.0.1/not-a-prefix") must
    # not normalize into a false address match on this gating path.
    exit_ips = _valid_ip_set(exit_status.get("TailscaleIPs")) or set()
    if exit_id is None and not exit_ips:
        return "unknown"  # a present dict with no trustworthy identity -> fail closed

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
        except (json.JSONDecodeError, UnicodeDecodeError, OSError) as exc:
            # UnicodeDecodeError: a status file that is not valid UTF-8 must degrade
            # to "unavailable" (like a JSON error), never surface as a traceback --
            # callers such as peer-metrics rely on get_status failing closed.
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
                # Canonicalize the persisted set (an older build stored it as raw text)
                # the same way resolved IPs are canonicalized, so an expanded-vs-compressed
                # IPv6 spelling across an upgrade is not mistaken for a node change (which
                # would reset health history and suppress a due failover). A malformed
                # persisted set canonicalizes to empty -> no spurious reset.
                stored_ips = _valid_ip_set(node.get("tailscale_ips")) or set()
                if stored_id and resolved_id:
                    identity_changed = stored_id != resolved_id
                else:
                    # No reliable ID on at least one side: fall back to IP sets and
                    # treat disjoint non-empty sets as a different node.
                    identity_changed = bool(stored_ips) and bool(resolved_ips) and not (stored_ips & set(resolved_ips))
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
    # Validated on both sides so a malformed peer IP cannot forge an IP match; a
    # malformed candidate can still be found by its trustworthy ID.
    wanted_ips = _valid_ip_set(ips) or set()
    for node in candidates:
        raw_id = node.get("ID") or node.get("StableID")
        nid = str(raw_id) if raw_id else None
        node_ips = _valid_ip_set(node.get("TailscaleIPs")) or set()
        if (node_id and nid == node_id) or (wanted_ips and wanted_ips & node_ips):
            return node
    return None


def node_online(status: dict[str, Any], node_id: Optional[str], ips: list[str]) -> Optional[bool]:
    node = _find_node(status, node_id, ips)
    if node is None:
        return None
    value = node.get("Online")
    return bool(value) if isinstance(value, bool) else None


_DEFAULT_ROUTE_NETS = (ipaddress.ip_network("0.0.0.0/0"), ipaddress.ip_network("::/0"))


def node_routes(status: dict[str, Any], node_id: Optional[str], ips: list[str]) -> Optional[list[str]]:
    """App-connector / subnet routes a connector currently advertises, best-effort
    from the client's status view. Prefers PrimaryRoutes; falls back to AllowedIPs
    minus the node's own host addresses. Default (exit-node) routes are excluded.

    Semantics:
    - node absent -> None (unknown).
    - the authoritative field null / absent (a Go nil slice marshals to ``null``) ->
      ``[]`` (authoritatively no routes), matching the pre-hardening behavior.
    - the field is a wrong type (string/number/object), or ANY element is not a
      valid non-empty CIDR/IP -> None (fail closed). In the AllowedIPs branch, if
      ``TailscaleIPs`` is not a valid, non-empty IP set (non-list, an invalid-IP
      element, or empty) the own-host exclusion is untrustworthy, so it also fails
      closed rather than risk counting the node's own address as a route.
    - otherwise the list of non-default, non-own-host routes.

    Each element is parsed with ``ipaddress`` so a non-canonical default such as
    ``0.0.0.1/0`` is excluded; own-host exclusion preserves the prior address-part
    match (any prefix). This single strict result feeds the JSON ``routes`` field,
    the ``serving`` / health verdict, AND the Prometheus ``_routes`` gauge, so they
    always agree; it is still a best-effort signal, not proof that AI app routes
    resolve."""
    node = _find_node(status, node_id, ips)
    if node is None:
        return None
    use_allowedips = "PrimaryRoutes" not in node
    raw = node.get("AllowedIPs") if use_allowedips else node.get("PrimaryRoutes")
    if raw is None:
        return []  # null / absent -> authoritatively "no routes" (prior behavior)
    if not isinstance(raw, list):
        return None  # wrong type -> fail closed
    self_ips: set[str] = set()
    if use_allowedips and raw:
        # A non-empty AllowedIPs is the connector's own addresses PLUS advertised
        # routes; excluding the own addresses needs a trustworthy TailscaleIPs. If it
        # is not a valid, non-empty IP set the exclusion cannot be trusted, so fail
        # closed rather than count the node's own address as an advertised route.
        valid = _valid_ip_set(node.get("TailscaleIPs"))
        if not valid:
            return None
        self_ips = valid
    routes: list[str] = []
    for entry in raw:
        if not isinstance(entry, str) or not entry.strip():
            return None  # malformed element -> the whole field is untrustworthy
        text = entry.strip()
        if not _safe_ip_token(text):
            return None  # scoped, dotted-mask, or over-long prefix -> Tailscale never emits
        try:
            net = ipaddress.ip_network(text, strict=False)
        except ValueError:
            return None  # not a valid CIDR/IP -> fail closed
        if net in _DEFAULT_ROUTE_NETS:
            continue  # a (canonical) default route is not an app-connector route
        if use_allowedips and _canon_addr(text) in self_ips:
            continue  # the connector's own address (AllowedIPs fallback; any prefix, canonical match)
        routes.append(text)
    return routes


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
    "latency_ms",
)


def _null_metrics() -> dict[str, Any]:
    return {key: None for key in PEER_METRIC_KEYS}


def _int_or_none(value: Any) -> Optional[int]:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _str_or_none(value: Any) -> Optional[str]:
    return value if isinstance(value, str) and value else None


def _bool_or_none(value: Any) -> Optional[bool]:
    return value if isinstance(value, bool) else None


def _latency_or_none(value: Any) -> Optional[float]:
    """Validate a caller-supplied latency in ms for the metrics object: a finite,
    non-negative number (int or float; a string is rejected, matching the strict
    isinstance style of the other coercers). Rejects bool, NaN/Inf, and negatives;
    catches OverflowError so a huge int (whose float() overflows) degrades to null
    rather than aborting the whole extraction. Normalizes -0.0 to 0.0."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        parsed = float(value)  # normalize (incl. int/float subclasses) to a builtin float
    except Exception:
        # A validator must never raise: besides TypeError/ValueError/OverflowError,
        # a float subclass overriding __float__ can raise anything. Any failure to
        # produce a builtin float degrades to null (only latency is rejected, never
        # the whole metrics object). BaseException (KeyboardInterrupt/SystemExit)
        # is deliberately NOT caught.
        return None
    if not math.isfinite(parsed) or parsed < 0:  # validate the normalized (builtin) value
        return None
    return parsed + 0.0


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


_UNSET: Any = object()  # sentinel: "argument not supplied" (distinct from an explicit None)


def peer_metrics(
    status: dict[str, Any],
    available: bool,
    label: str,
    *,
    now: Optional[dt.datetime] = None,
    latency_ms: Optional[float] = None,
    ping_fn: Optional[Callable[[], Any]] = None,
    _node: Any = _UNSET,
) -> dict[str, Any]:
    """Read-only per-peer counters + liveness for a connector label. Returns the
    fixed-key metrics object; ALL values null on transport failure (status
    unavailable) or resolution failure (peer not found). On a resolved peer,
    individual missing / zero-value fields are null but keys are never omitted.

    tx/rx are cumulative byte counters since tailscaled started (reset on restart),
    NOT session or billing usage.

    ``latency_ms`` is a per-peer metric: it is stamped ONLY once the peer resolves,
    so a null-filled object stays all-null (the reachability RTT is never attributed
    to a label that does not resolve to a known peer). Callers with an RTT already
    in hand pass ``latency_ms``; ``ping_fn`` is an optional zero-arg callable invoked
    ONLY after a successful resolve (so a standalone caller never pings an
    unresolvable label). Both run inside the resolved path, so a raising ``ping_fn``
    is caught by ``_safe_peer_metrics`` along with any resolution error."""
    if not available:
        return _null_metrics()
    if _node is _UNSET:
        node_id, ips = resolve_identity(status, label)
        node = _find_node(status, node_id, ips)
    else:
        node = _node  # caller already resolved this label (dedups a 2nd resolve + ambiguity warning)
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
    lat = latency_ms
    if lat is None and ping_fn is not None:
        lat = ping_fn()  # reached only for a resolved peer
    obj["latency_ms"] = _latency_or_none(lat)
    return obj


def _safe_peer_metrics(
    status: dict[str, Any],
    available: bool,
    label: str,
    *,
    latency_ms: Optional[float] = None,
    ping_fn: Optional[Callable[[], Any]] = None,
    _node: Any = _UNSET,
) -> dict[str, Any]:
    """Non-gating wrapper: metrics extraction must NEVER abort the monitor report
    (the hard rule in docs/design/metrics-collection.md), so any unexpected error
    -- including a resolution error on a malformed status or a raising ``ping_fn``
    -- degrades to the null-filled object instead of propagating. ``_node`` lets a
    caller pass an already-resolved node so peer_metrics does not resolve twice."""
    try:
        return peer_metrics(status, available, label, latency_ms=latency_ms, ping_fn=ping_fn, _node=_node)
    except Exception as exc:
        eprint(f"warning: peer metrics unavailable for '{label}': {exc}")
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


# --------------------------------------------------------------------------- #
# Prometheus textfile emitter (read-only; a user-run scraper consumes it).
# Python owns the whole validated document AND the atomic write; the monitor is a
# thin wrapper. NEVER emits a silently-wrong value: a null / non-finite / negative
# / malformed metric is OMITTED (never a fake 0). See docs/design/metrics-collection.md.
# --------------------------------------------------------------------------- #
def _prom_escape(value: str) -> str:
    """Escape a Prometheus label value: backslash, double-quote, newline."""
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def _prom_labels(labels: dict[str, str]) -> str:
    if not labels:
        return ""
    inner = ",".join(f'{key}="{_prom_escape(str(val))}"' for key, val in labels.items())
    return "{" + inner + "}"


def _prom_num(value: Any) -> str:
    """Render a numeric sample value: floats via repr (finite), ints via str."""
    return repr(value) if isinstance(value, float) else str(int(value))


def _nonneg_int_or_none(value: Any) -> Optional[int]:
    """A counter sample must be a non-negative, non-bool int that is also finite as
    a float64 (Prometheus sample values are float64): an absurdly large int would
    parse as +Inf in a scraper, so omit it rather than emit a non-finite sample."""
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    try:
        if not math.isfinite(float(value)):
            return None
    except OverflowError:  # int too large to fit in a float64 -> would scrape as +Inf
        return None
    return value


def _prometheus_document(rows: list[dict[str, Any]], overall_healthy: bool) -> str:
    """Build the complete Prometheus text-exposition document from the connector
    rows. HELP/TYPE appear once per family, all its samples together; a family with
    no samples is skipped. `ai_egress_overall_healthy` is emitted exactly once, LAST
    (it is derived from a bool, so never null) as the completeness sentinel, and the
    document always ends with a trailing newline."""
    online: list[tuple[dict[str, str], Any]] = []
    reachable: list[tuple[dict[str, str], Any]] = []
    latency: list[tuple[dict[str, str], Any]] = []
    tx: list[tuple[dict[str, str], Any]] = []
    rx: list[tuple[dict[str, str], Any]] = []
    handshake: list[tuple[dict[str, str], Any]] = []
    routes: list[tuple[dict[str, str], Any]] = []
    info: list[tuple[dict[str, str], Any]] = []

    for row in rows:
        labels = {"connector": str(row["connector"]), "label": str(row["label"])}
        metrics = row["metrics"]
        if isinstance(row["online"], bool):
            online.append((labels, 1 if row["online"] else 0))
        if isinstance(row["reachable"], bool):
            reachable.append((labels, 1 if row["reachable"] else 0))
        lat = _latency_or_none(row["rtt_ms"])
        if lat is not None:
            latency.append((labels, lat))
        tx_val = _nonneg_int_or_none(metrics["tx_bytes_total"])
        if tx_val is not None:
            tx.append((labels, tx_val))
        rx_val = _nonneg_int_or_none(metrics["rx_bytes_total"])
        if rx_val is not None:
            rx.append((labels, rx_val))
        age = metrics["last_handshake_age_seconds"]
        if isinstance(age, int) and not isinstance(age, bool) and age >= 0:
            handshake.append((labels, age))
        route_count = _nonneg_int_or_none(row["routes"])  # strict node_routes count (or None)
        if route_count is not None:
            routes.append((labels, route_count))
        path = metrics["connection_path"]
        if isinstance(path, str) and path:
            info.append(({**labels, "connection_path": path}, 1))

    families = [
        ("ai_egress_connector_online", "gauge",
         "Connector peer online per tailscale status: 1 or 0.", online),
        ("ai_egress_connector_reachable", "gauge",
         "Connector reachable via tailscale ping this cycle: 1 or 0.", reachable),
        ("ai_egress_connector_latency_ms", "gauge",
         "Probe round-trip time from tailscale ping, in milliseconds.", latency),
        ("ai_egress_connector_tx_bytes_total", "counter",
         "Bytes sent to the connector peer, cumulative since tailscaled start (may reset on restart).", tx),
        ("ai_egress_connector_rx_bytes_total", "counter",
         "Bytes received from the connector peer, cumulative since tailscaled start (may reset on restart).", rx),
        ("ai_egress_connector_last_handshake_age_seconds", "gauge",
         "Seconds since the last handshake with the connector peer.", handshake),
        ("ai_egress_connector_routes", "gauge",
         "Non-default app-connector/subnet routes advertised (omitted if the route field is malformed).", routes),
        ("ai_egress_connector_info", "gauge",
         "Connector connection-path info; the value is always 1.", info),
    ]

    lines: list[str] = []
    for name, kind, help_text, samples in families:
        if not samples:
            continue
        lines.append(f"# HELP {name} {help_text}")
        lines.append(f"# TYPE {name} {kind}")
        for labels, value in samples:
            lines.append(f"{name}{_prom_labels(labels)} {_prom_num(value)}")
    # Sentinel LAST: proves the emitter ran to completion (the writer checks it).
    lines.append("# HELP ai_egress_overall_healthy Connector-pair health: 1 healthy, 0 degraded.")
    lines.append("# TYPE ai_egress_overall_healthy gauge")
    lines.append(f"ai_egress_overall_healthy {1 if overall_healthy else 0}")
    return "\n".join(lines) + "\n"


_SENTINEL_RE = re.compile(r"ai_egress_overall_healthy [01]")
# Any sample line of the sentinel SERIES: optional leading blanks/tabs, the metric
# name, then `{labels}` or a space/tab separator (both valid Prometheus grammar), so a
# timestamped (`... 0 123`), labeled, leading-whitespace, tab-separated, or duplicate
# form is caught -- not only the exact bare line that _SENTINEL_RE matches. The
# separator class stops it from matching a different metric such as
# `ai_egress_overall_healthy_extra`.
_SENTINEL_FAMILY_RE = re.compile(r"[ \t]*ai_egress_overall_healthy(?:\{|[ \t])")


def _write_textfile_atomic(dest: str, content: str) -> None:
    """Atomically publish a prometheus `.prom` document to `dest`. Uses a
    same-directory temp + fd-based `fchmod(0644)` + `fsync` + `os.replace`, so a
    reader never sees a partial file and the mode is set on the fd (no symlink
    race). Validates the destination AND the document, and raises on any failure,
    leaving an existing `dest` untouched (the temp is unlinked).

    Completeness boundary: refuses to publish a document that is not sentinel-
    terminated (exactly one `ai_egress_overall_healthy [01]` as the final non-empty
    line, plus a trailing newline) -- a truncated / partial document must fail
    rather than clobber a good file. Security note: the destination directory must
    be writable only by the writer; a world-writable, non-sticky parent permits a
    pathname-swap race that `os.replace` cannot defend against (operator's
    responsibility, like any node_exporter textfile directory)."""
    if not content.endswith("\n"):
        raise ValueError("refusing to publish a document without a trailing newline")
    family = [line for line in content.split("\n") if _SENTINEL_FAMILY_RE.match(line)]
    last = content.rstrip("\n").rsplit("\n", 1)[-1]
    if len(family) != 1 or not _SENTINEL_RE.fullmatch(family[0]) or last != family[0]:
        raise ValueError("refusing to publish an incomplete document (a single bare ai_egress_overall_healthy [01] must be the final line)")
    if not dest.endswith(".prom"):
        raise ValueError("output path must end in .prom (node_exporter textfile collector reads *.prom)")
    dirpath = os.path.dirname(dest) or "."
    if not os.path.isdir(dirpath):
        raise ValueError(f"parent directory does not exist or is not a directory: {dirpath}")
    if os.path.isdir(dest):
        raise IsADirectoryError(f"output path is a directory: {dest}")
    fd, tmp = tempfile.mkstemp(dir=dirpath, prefix=".ai-egress-prom.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fchmod(handle.fileno(), 0o644)  # fd-based: no symlink-follow race
            os.fsync(handle.fileno())
        os.replace(tmp, dest)  # atomic; raises rather than nesting into a directory
        tmp = ""  # published; nothing to clean up
    finally:
        if tmp:
            with contextlib.suppress(OSError):
                os.unlink(tmp)


def cmd_connectors(args: argparse.Namespace) -> int:
    if getattr(args, "output", None) is not None and not getattr(args, "prometheus", False):
        eprint("error: --output requires --prometheus")
        return 2
    status, available = get_status(args.status_json_file)
    # A well-formed status always carries a string BackendState; treat anything
    # other than "Running" (including a missing / null / non-string value) as down.
    backend_down = status.get("BackendState") != "Running"
    rows: list[dict[str, Any]] = []
    reachable_ok = True
    serving = "none"
    for role, label in (("primary", args.primary), ("fallback", args.fallback)):
        node_id, ips = resolve_identity(status, label)
        node = _find_node(status, node_id, ips)  # resolve once; reused for metrics (avoids a 2nd ambiguity warning)
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
            # if unavailable. latency_ms reuses the reachability ping above (one
            # ping per connector); it is stamped only when the peer also resolves.
            "metrics": _safe_peer_metrics(status, available, label, latency_ms=result.rtt_ms, _node=node),
        })
    # In oldest-first HA exactly one connector serves routes at a time, so the
    # pair is route-healthy when at least one advertises app-connector/subnet
    # routes; "none" means routing is broken even if both nodes are pingable.
    routes_ok = serving != "none"
    overall_healthy = available and not backend_down and reachable_ok and (not args.require_routes or routes_ok)
    order, order_reason = connector_ordering(load_devices(args.devices_json_file), args.primary, args.fallback)
    overall = "healthy" if overall_healthy else "degraded"
    if getattr(args, "prometheus", False):
        document = _prometheus_document(rows, overall_healthy)
        output = getattr(args, "output", None)
        if output is not None:  # NOT truthiness: an empty --output must error, not fall back to stdout
            try:
                _write_textfile_atomic(output, document)
            except Exception as exc:
                eprint(f"error: could not write prometheus textfile: {exc}")
                return 1
            # Write succeeded: exit 0 even when degraded -- health is carried by the
            # ai_egress_overall_healthy gauge, not the exit code (R3-04).
            return 0
        print(document, end="")
        return 0 if overall_healthy else 1
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
                f"handshake_age={_fmt_metric(m['last_handshake_age_seconds'])} "
                f"latency_ms={_fmt_metric(m['latency_ms'])}"
            )
        print(f"ordering={order} reason={order_reason}")
        print(f"routes_serving={serving}")
        print(f"overall={overall}")
    return 0 if overall_healthy else 1


def cmd_peer_metrics(args: argparse.Namespace) -> int:
    """Print the read-only metrics object for one connector label as JSON. Always
    exits 0 when it can print the object (including the null-filled object on
    transport/resolution failure); non-zero only for usage/arg errors. This is the
    single source of truth for the metrics object shape used by the monitor.

    With ``--ping`` (default off) latency_ms is measured with a single tailscale
    ping, but only for a peer that resolves: the ping runs inside peer_metrics'
    resolved path, so an unresolvable label is never pinged and stays null-filled.
    The ping timeout (``--ping-timeout``, then ``PING_TIMEOUT``, then the default)
    is resolved ONLY when --ping is set, so an invalid ``PING_TIMEOUT`` in the
    environment can never break the default no-ping path (which ignores it)."""
    status, available = get_status(args.status_json_file)
    ping_fn: Optional[Callable[[], Any]] = None
    if getattr(args, "ping", False):
        node = args.node
        ping_timeout = getattr(args, "ping_timeout", None)
        if ping_timeout is None:
            # No explicit --ping-timeout: read PING_TIMEOUT defensively (env_float
            # never raises) and clamp, rather than validating it at parse time.
            ping_timeout = env_float("PING_TIMEOUT", DEFAULT_PING_TIMEOUT)
            if not (0 < ping_timeout <= MAX_TIMEOUT_SECONDS):
                ping_timeout = DEFAULT_PING_TIMEOUT

        def ping_fn() -> Optional[float]:  # invoked only after a successful resolve
            return tailscale_ping(node, ping_timeout).rtt_ms

    print(json.dumps(_safe_peer_metrics(status, available, args.node, ping_fn=ping_fn), sort_keys=True))
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
    connectors_out = connectors.add_mutually_exclusive_group()
    connectors_out.add_argument("--json", action="store_true")
    connectors_out.add_argument(
        "--prometheus",
        action="store_true",
        help="Emit Prometheus text-exposition of per-connector gauges (instead of text/JSON).",
    )
    connectors.add_argument(
        "--output",
        help="With --prometheus, atomically write the document to this .prom file "
        "(node_exporter textfile) instead of stdout; exit reflects write success, not health.",
    )
    connectors.set_defaults(func=cmd_connectors)

    peer = sub.add_parser(
        "peer-metrics",
        help="Read-only per-peer counters + liveness for one connector label (JSON; always exits 0).",
    )
    peer.add_argument("--node", required=True)
    peer.add_argument("--status-json-file")
    peer.add_argument("--json", action="store_true", help="Accepted for consistency; output is always JSON.")
    peer.add_argument(
        "--ping",
        action="store_true",
        help="Also measure latency_ms with a single tailscale ping to a resolved peer (default off; adds one ping).",
    )
    peer.add_argument(
        "--ping-timeout",
        type=_pos_float,
        default=None,
        help="Seconds for the --ping probe; read only when --ping is set (else PING_TIMEOUT/5).",
    )
    peer.set_defaults(func=cmd_peer_metrics)

    return parser


def main(argv: Optional[list[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
