#!/usr/bin/env python3
"""Render, merge, validate, and plan/apply-plan a Tailscale app connector policy.

This intentionally uses only the Python standard library so it can run on a
fresh Linux VPS without installing Node packages.
"""

from __future__ import annotations

import argparse
import base64
import copy
import datetime as dt
import difflib
import getpass
import hashlib
import json
import os
import re
import secrets
import shutil
import socket
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, NoReturn


API_BASE = "https://api.tailscale.com/api/v2"
__version__ = "1.1.1"
# Frozen for the v1.x series. Plan bundles produced by v0.4 use the same major
# version and remain readable by v1.x tooling; see docs/Stability.md.
MANIFEST_SCHEMA_VERSION = 1
DEFAULT_TAG_OWNER = "autogroup:admin"
DEFAULT_MEMBER_SRC = "autogroup:member"
APP_CONNECTORS_KEY = "tailscale.com/app-connectors"
TRANSIENT_HTTP_STATUSES = {500, 502, 503, 504}
BROAD_WILDCARD_BLOCKLIST = {
    "*.com",
    "*.google.com",
    "*.microsoft.com",
    "*.cloudflare.com",
    "*.googleapis.com",
}
# Broad CDN / shared-infrastructure base domains that WARN (not block): routing them
# can pull unrelated traffic that happens to share the CDN through the connector. A
# conservative, non-exhaustive starting set; matched against both `cdn.net` and
# `*.cdn.net` (see normalize_domains). Blocked domains still fail; these only warn.
BROAD_WILDCARD_WARNLIST = {
    "cloudfront.net",
    "amazonaws.com",
    "googleusercontent.com",
    "azureedge.net",
    "akamaihd.net",
    "fastly.net",
}
CONNECTOR_NAME_RE = re.compile(r"^[A-Za-z0-9_-]+$")
CONNECTOR_TAG_RE = re.compile(r"^tag:[a-z0-9]+(?:-[a-z0-9]+)*$")
PLAN_ID_RE = re.compile(r"^\d{8}T\d{6}Z-[0-9a-f]{8}$")
SECRET_PATTERNS = [
    re.compile(r"(Authorization:\s*(?:Bearer|Basic)\s+)[^\s,;]+", re.IGNORECASE),
    re.compile(r"(authorization[\"']?\s*[:=]\s*[\"']?(?:Bearer|Basic)\s+)[^\s\"',;}]+", re.IGNORECASE),
    re.compile(r"tskey-[A-Za-z0-9_-]+"),
]


def default_region() -> str:
    return os.environ.get("REGION", "JP")


def default_connector_name() -> str:
    return os.environ.get("CONNECTOR_NAME", f"AI-Egress-{default_region().upper()}")


def default_connector_tag() -> str:
    return os.environ.get("CONNECTOR_TAG", f"tag:ai-egress-{default_region().lower()}")


class PolicyError(RuntimeError):
    pass


def eprint(*parts: object) -> None:
    print(*parts, file=sys.stderr)


def redact_sensitive(text: str) -> str:
    redacted = text
    for pattern in SECRET_PATTERNS:
        if pattern.groups:
            redacted = pattern.sub(r"\1[REDACTED]", redacted)
        else:
            redacted = pattern.sub("[REDACTED]", redacted)
    return redacted


def safe_response_text(text: str) -> str:
    return redact_sensitive(text.strip())


def strip_hujson(text: str) -> str:
    """Remove common HuJSON features: comments and trailing commas.

    This is not a full HuJSON parser, but it is enough for the policy files
    normally returned by the Tailscale API. If parsing still fails, the caller
    falls back safely instead of applying anything.
    """
    out: list[str] = []
    i = 0
    in_string = False
    escape = False

    while i < len(text):
        ch = text[i]
        nxt = text[i + 1] if i + 1 < len(text) else ""

        if in_string:
            out.append(ch)
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_string = False
            i += 1
            continue

        if ch == '"':
            in_string = True
            out.append(ch)
            i += 1
            continue

        if ch == "/" and nxt == "/":
            i += 2
            while i < len(text) and text[i] not in "\r\n":
                i += 1
            continue

        if ch == "/" and nxt == "*":
            i += 2
            closed = False
            while i + 1 < len(text):
                if text[i] == "*" and text[i + 1] == "/":
                    closed = True
                    break
                i += 1
            if not closed:
                raise PolicyError("Unterminated block comment in HuJSON policy.")
            i += 2
            continue

        out.append(ch)
        i += 1

    no_comments = "".join(out)
    out = []
    i = 0
    in_string = False
    escape = False
    while i < len(no_comments):
        ch = no_comments[i]
        if in_string:
            out.append(ch)
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_string = False
            i += 1
            continue

        if ch == '"':
            in_string = True
            out.append(ch)
            i += 1
            continue

        if ch == ",":
            j = i + 1
            while j < len(no_comments) and no_comments[j].isspace():
                j += 1
            if j < len(no_comments) and no_comments[j] in "}]":
                i += 1
                continue

        out.append(ch)
        i += 1

    return "".join(out)


def parse_policy(text: str) -> dict[str, Any]:
    try:
        data = json.loads(text)
    except json.JSONDecodeError as json_exc:
        try:
            stripped = strip_hujson(text)
        except PolicyError as exc:
            raise PolicyError(
                f"Could not parse policy as JSON/HuJSON: {exc} Original JSON error: {json_exc}"
            ) from exc
        try:
            data = json.loads(stripped)
        except json.JSONDecodeError as exc:
            raise PolicyError(
                "Could not parse policy as JSON/HuJSON. "
                f"Original JSON error: {json_exc}. "
                f"After removing common HuJSON comments/trailing commas: {exc}"
            ) from exc

    if not isinstance(data, dict):
        raise PolicyError("Tailnet policy must be a JSON object.")
    return data


def dumps(data: Any) -> str:
    return json.dumps(data, indent=2, ensure_ascii=False) + "\n"


def read_json_or_text_list(path: Path) -> list[str]:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise PolicyError(f"Could not read {path}: {exc}") from exc

    stripped = text.strip()
    if not stripped:
        return []

    if stripped[0] == "[":
        try:
            parsed = json.loads(stripped)
        except json.JSONDecodeError as exc:
            raise PolicyError(f"{path} is not valid JSON: {exc}") from exc
        if not isinstance(parsed, list) or not all(isinstance(x, str) for x in parsed):
            raise PolicyError(f"{path} must be a JSON string array.")
        return parsed

    domains: list[str] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        domains.append(line)
    return domains


DOMAIN_RE = re.compile(
    r"^(?:\*\.)?(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)+"
    r"[A-Za-z][A-Za-z0-9-]{0,61}[A-Za-z0-9]$"
)


def finding(status: str, finding_id: str, message: str, details: dict[str, Any] | None = None) -> dict[str, Any]:
    item: dict[str, Any] = {
        "id": finding_id,
        "status": status,
        "message": message,
    }
    if details:
        item["details"] = details
    return item


def new_report(command: str) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "tool": "policy_tool.py",
        "command": command,
        "summary": {"ok": 0, "warn": 0, "fail": 0},
        "findings": [],
    }


def add_finding(
    report: dict[str, Any],
    status: str,
    finding_id: str,
    message: str,
    details: dict[str, Any] | None = None,
) -> None:
    report["findings"].append(finding(status, finding_id, message, details))


def add_findings(report: dict[str, Any], findings: list[dict[str, Any]]) -> None:
    report["findings"].extend(findings)


def refresh_summary(report: dict[str, Any]) -> None:
    findings = report["findings"]
    report["summary"] = {
        "ok": sum(1 for item in findings if item["status"] == "ok"),
        "warn": sum(1 for item in findings if item["status"] == "warn"),
        "fail": sum(1 for item in findings if item["status"] == "fail"),
    }


def has_failures(report: dict[str, Any]) -> bool:
    return any(item["status"] == "fail" for item in report["findings"])


def first_failure(findings: list[dict[str, Any]]) -> dict[str, Any] | None:
    return next((item for item in findings if item["status"] == "fail"), None)


def emit_report(report: dict[str, Any], report_format: str, stream: Any) -> None:
    refresh_summary(report)
    if report_format == "json":
        json.dump(report, stream, indent=2)
        stream.write("\n")
        return

    for item in report["findings"]:
        label = item["status"].upper()
        stream.write(f"[{label}] {item['id']}: {item['message']}\n")
    summary = report["summary"]
    stream.write(
        f"Summary: {summary['ok']} ok, {summary['warn']} warning(s), "
        f"{summary['fail']} failure(s).\n"
    )


def connector_name_error(connector_name: str) -> str | None:
    if not connector_name:
        return "Connector name cannot be empty."
    if not CONNECTOR_NAME_RE.match(connector_name):
        return "Connector name must contain only letters, numbers, underscores, and hyphens."
    return None


def connector_tag_error(connector_tag: str) -> str | None:
    if not connector_tag:
        return "Connector tag cannot be empty."
    if not CONNECTOR_TAG_RE.match(connector_tag):
        return "Connector tag must be tag:<lowercase-alphanumeric-hyphens>."
    return None


def normalize_domain_entry(raw: str, *, allow_broad_wildcard: bool = False) -> str | None:
    domain = raw.strip().lower().rstrip(".")
    if not domain:
        return None
    if "://" in domain or "/" in domain or ":" in domain or " " in domain:
        raise PolicyError(f"Invalid domain entry: {raw!r}")
    if domain in BROAD_WILDCARD_BLOCKLIST:
        if not allow_broad_wildcard:
            raise PolicyError(
                f"Broad wildcard domain {domain!r} is not allowed; pass --allow-broad-wildcard to override."
            )
        return domain
    if not DOMAIN_RE.match(domain):
        raise PolicyError(f"Invalid domain entry: {raw!r}")
    return domain


def normalize_domains(
    domains: list[str],
    *,
    allow_broad_wildcard: bool = False,
    findings: list[dict[str, Any]] | None = None,
) -> list[str]:
    clean: list[str] = []
    seen: set[str] = set()
    duplicates: set[str] = set()
    for raw in domains:
        domain = normalize_domain_entry(raw, allow_broad_wildcard=allow_broad_wildcard)
        if domain is None:
            continue
        if domain not in seen:
            clean.append(domain)
            seen.add(domain)
        else:
            duplicates.add(domain)

    if duplicates and findings is not None:
        findings.append(
            finding(
                "warn",
                "duplicate-domains",
                "Duplicate domain entries were ignored.",
                {"domains": sorted(duplicates)},
            )
        )

    # Broad CDN / shared-infrastructure domains warn (do not block): `cdn.net` and
    # `*.cdn.net` both match the base-domain warn set. `removeprefix("*.")` alone
    # covers both forms. Domains are still returned; only surfaced when a caller
    # collects findings (validate / plan / merge --report).
    warned = sorted(d for d in clean if d.removeprefix("*.") in BROAD_WILDCARD_WARNLIST)
    if warned and findings is not None:
        findings.append(
            finding(
                "warn",
                "broad-wildcard-warning",
                "Broad CDN / shared-infrastructure domains can route unrelated traffic "
                "through the connector; add them only if your use case needs them.",
                {"domains": warned},
            )
        )

    if not clean:
        raise PolicyError("At least one domain is required.")
    return clean


def ordered_union(existing: Any, additions: list[str]) -> list[str]:
    values: list[str] = []
    if isinstance(existing, list):
        values = [str(item) for item in existing]
    elif isinstance(existing, str):
        values = [existing]

    seen = set(values)
    for item in additions:
        if item not in seen:
            values.append(item)
            seen.add(item)
    return values


def build_patch(
    *,
    connector_name: str,
    connector_tag: str,
    domains: list[str],
    tag_owner: str,
    member_src: str,
) -> dict[str, Any]:
    return {
        "tagOwners": {
            connector_tag: [tag_owner],
        },
        "autoApprovers": {
            "routes": {
                "0.0.0.0/0": [connector_tag],
                "::/0": [connector_tag],
            },
        },
        "grants": [
            {
                "src": [member_src],
                "dst": ["autogroup:internet"],
                "ip": ["*"],
            },
            {
                "src": [member_src],
                "dst": [connector_tag],
                "ip": ["tcp:53", "udp:53"],
            },
        ],
        "nodeAttrs": [
            {
                "target": ["*"],
                "app": {
                    APP_CONNECTORS_KEY: [
                        {
                            "name": connector_name,
                            "connectors": [connector_tag],
                            "domains": domains,
                        }
                    ]
                },
            }
        ],
    }


def canonical_grant_value(value: Any) -> Any:
    if isinstance(value, list):
        return sorted(str(item) for item in value)
    return str(value)


def grant_key(grant: dict[str, Any]) -> str:
    canonical = {
        key: canonical_grant_value(grant[key])
        for key in ("src", "dst", "ip")
        if key in grant
    }
    return json.dumps(canonical, sort_keys=True, separators=(",", ":"))


def ensure_dict(parent: dict[str, Any], key: str) -> dict[str, Any]:
    value = parent.get(key)
    if value is None:
        value = {}
        parent[key] = value
    if not isinstance(value, dict):
        raise PolicyError(f"Policy field {key!r} exists but is not an object.")
    return value


def ensure_list(parent: dict[str, Any], key: str) -> list[Any]:
    value = parent.get(key)
    if value is None:
        value = []
        parent[key] = value
    if not isinstance(value, list):
        raise PolicyError(f"Policy field {key!r} exists but is not an array.")
    return value


def merge_policy(
    policy: dict[str, Any],
    *,
    connector_name: str,
    connector_tag: str,
    domains: list[str],
    tag_owner: str,
    member_src: str,
) -> dict[str, Any]:
    patch = build_patch(
        connector_name=connector_name,
        connector_tag=connector_tag,
        domains=domains,
        tag_owner=tag_owner,
        member_src=member_src,
    )

    tag_owners = ensure_dict(policy, "tagOwners")
    tag_owners[connector_tag] = ordered_union(tag_owners.get(connector_tag), [tag_owner])

    auto_approvers = ensure_dict(policy, "autoApprovers")
    routes = ensure_dict(auto_approvers, "routes")
    for route, approvers in patch["autoApprovers"]["routes"].items():
        routes[route] = ordered_union(routes.get(route), approvers)

    grants = ensure_list(policy, "grants")
    existing_grants = {grant_key(g) for g in grants if isinstance(g, dict)}
    for grant in patch["grants"]:
        key = grant_key(grant)
        if key not in existing_grants:
            grants.append(grant)
            existing_grants.add(key)

    node_attrs = ensure_list(policy, "nodeAttrs")
    connector_config = patch["nodeAttrs"][0]["app"][APP_CONNECTORS_KEY][0]
    updated = False
    for attr in node_attrs:
        if not isinstance(attr, dict):
            continue
        app = attr.get("app")
        if not isinstance(app, dict):
            continue
        connectors = app.get(APP_CONNECTORS_KEY)
        if not isinstance(connectors, list):
            continue
        for connector in connectors:
            if isinstance(connector, dict) and connector.get("name") == connector_name:
                connector["connectors"] = ordered_union(connector.get("connectors"), [connector_tag])
                connector["domains"] = ordered_union(connector.get("domains"), domains)
                updated = True
                break
        if updated:
            break

    if not updated:
        target_attr = None
        for attr in node_attrs:
            if isinstance(attr, dict) and attr.get("target") == ["*"]:
                app = attr.setdefault("app", {})
                if isinstance(app, dict):
                    target_attr = attr
                    break

        if target_attr is None:
            node_attrs.append(patch["nodeAttrs"][0])
        else:
            app = ensure_dict(target_attr, "app")
            connectors = app.setdefault(APP_CONNECTORS_KEY, [])
            if not isinstance(connectors, list):
                raise PolicyError(f"Policy app field {APP_CONNECTORS_KEY!r} is not an array.")
            connectors.append(connector_config)

    return policy


def validate_policy_shape(
    policy: dict[str, Any],
    *,
    connector_tag: str | None = None,
) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = [
        finding("ok", "policy-root", "Policy root is a JSON/HuJSON object.")
    ]

    field_shapes = {
        "tagOwners": dict,
        "autoApprovers": dict,
        "grants": list,
        "nodeAttrs": list,
    }
    for key, expected_type in field_shapes.items():
        if key not in policy:
            findings.append(finding("info", f"{key}-shape", f"Policy field {key!r} is absent and can be created."))
        elif isinstance(policy[key], expected_type):
            type_name = "object" if expected_type is dict else "array"
            findings.append(finding("ok", f"{key}-shape", f"Policy field {key!r} is a mergeable {type_name}."))
        else:
            type_name = "object" if expected_type is dict else "array"
            findings.append(finding("fail", f"{key}-shape", f"Policy field {key!r} must be a {type_name}."))

    auto_approvers = policy.get("autoApprovers")
    if isinstance(auto_approvers, dict) and "routes" in auto_approvers:
        if isinstance(auto_approvers["routes"], dict):
            findings.append(finding("ok", "autoApprovers.routes-shape", "Policy autoApprovers.routes is mergeable."))
        else:
            findings.append(
                finding("fail", "autoApprovers.routes-shape", "Policy autoApprovers.routes must be an object.")
            )

    grants = policy.get("grants")
    if isinstance(grants, list):
        seen_grants: set[str] = set()
        duplicate_grants = 0
        invalid_grants = 0
        for grant in grants:
            if not isinstance(grant, dict):
                invalid_grants += 1
                continue
            key = grant_key(grant)
            if key in seen_grants:
                duplicate_grants += 1
            else:
                seen_grants.add(key)
        if invalid_grants:
            findings.append(finding("fail", "grants-items-shape", "Every grant entry must be an object."))
        if duplicate_grants:
            findings.append(
                finding(
                    "warn",
                    "duplicate-grants",
                    "Duplicate grants are already present and will be preserved.",
                    {"count": duplicate_grants},
                )
            )

    node_attrs = policy.get("nodeAttrs")
    connector_names: dict[str, int] = {}
    duplicate_connector_domains: dict[str, list[str]] = {}
    if isinstance(node_attrs, list):
        for attr in node_attrs:
            if not isinstance(attr, dict):
                findings.append(finding("fail", "nodeAttrs-items-shape", "Every nodeAttrs entry must be an object."))
                continue
            app = attr.get("app")
            if app is not None and not isinstance(app, dict):
                findings.append(finding("fail", "nodeAttrs.app-shape", "nodeAttrs app field must be an object."))
                continue
            if not isinstance(app, dict) or APP_CONNECTORS_KEY not in app:
                continue
            connectors = app[APP_CONNECTORS_KEY]
            if not isinstance(connectors, list):
                findings.append(
                    finding(
                        "fail",
                        "app-connectors-shape",
                        f"Policy app field {APP_CONNECTORS_KEY!r} must be an array.",
                    )
                )
                continue
            for connector in connectors:
                if not isinstance(connector, dict):
                    findings.append(
                        finding("fail", "app-connectors-items-shape", "Every app connector entry must be an object.")
                    )
                    continue
                name = connector.get("name")
                if isinstance(name, str):
                    connector_names[name] = connector_names.get(name, 0) + 1
                domains = connector.get("domains")
                if domains is not None:
                    if not isinstance(domains, list) or not all(isinstance(item, str) for item in domains):
                        findings.append(
                            finding(
                                "fail",
                                "app-connectors-domains-shape",
                                "App connector domains must be a string array.",
                            )
                        )
                    else:
                        seen_domains: set[str] = set()
                        duplicates: set[str] = set()
                        for raw in domains:
                            domain = raw.strip().lower().rstrip(".")
                            if domain in seen_domains:
                                duplicates.add(domain)
                            else:
                                seen_domains.add(domain)
                        if duplicates and isinstance(name, str):
                            duplicate_connector_domains[name] = sorted(duplicates)
                connector_tags = connector.get("connectors")
                if connector_tags is not None and not isinstance(connector_tags, (list, str)):
                    findings.append(
                        finding(
                            "fail",
                            "app-connectors-tags-shape",
                            "App connector connectors must be a string or string array.",
                        )
                    )

    duplicate_names = sorted(name for name, count in connector_names.items() if count > 1)
    if duplicate_names:
        findings.append(
            finding(
                "warn",
                "duplicate-connector-names",
                "Duplicate app connector names are already present and will be preserved.",
                {"names": duplicate_names},
            )
        )
    for name, domains in duplicate_connector_domains.items():
        findings.append(
            finding(
                "warn",
                "duplicate-connector-domains",
                "Duplicate domains are already present on an app connector and will be preserved.",
                {"connector_name": name, "domains": domains},
            )
        )

    if connector_tag and isinstance(auto_approvers, dict):
        routes = auto_approvers.get("routes")
        if isinstance(routes, dict):
            present_routes: list[str] = []
            for route in ("0.0.0.0/0", "::/0"):
                approvers = routes.get(route)
                if approvers == connector_tag:
                    present_routes.append(route)
                elif isinstance(approvers, list) and connector_tag in approvers:
                    present_routes.append(route)
            if present_routes:
                findings.append(
                    finding(
                        "warn",
                        "auto-approvers-present",
                        "Route auto-approvers for this connector tag are already present.",
                        {"routes": present_routes, "connector_tag": connector_tag},
                    )
                )

    return findings


def validate_policy_text(
    text: str,
    *,
    connector_tag: str | None = None,
) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    try:
        policy = parse_policy(text)
    except PolicyError as exc:
        return None, [finding("fail", "policy-parse", str(exc))]
    return policy, validate_policy_shape(policy, connector_tag=connector_tag)


def validate_connector_config(
    *,
    connector_name: str,
    connector_tag: str,
    raw_domains: list[str],
    allow_broad_wildcard: bool = False,
) -> tuple[list[str], list[dict[str, Any]]]:
    findings: list[dict[str, Any]] = []

    name_error = connector_name_error(connector_name)
    if name_error:
        findings.append(finding("fail", "connector-name", name_error, {"connector_name": connector_name}))
    else:
        findings.append(finding("ok", "connector-name", "Connector name is valid."))

    tag_error = connector_tag_error(connector_tag)
    if tag_error:
        findings.append(finding("fail", "connector-tag", tag_error, {"connector_tag": connector_tag}))
    else:
        findings.append(finding("ok", "connector-tag", "Connector tag is valid."))

    domains: list[str] = []
    try:
        domains = normalize_domains(
            raw_domains,
            allow_broad_wildcard=allow_broad_wildcard,
            findings=findings,
        )
        findings.append(finding("ok", "domains", "Domain entries are valid.", {"count": len(domains)}))
    except PolicyError as exc:
        findings.append(finding("fail", "domains", str(exc)))

    return domains, findings


def read_domains_for_args(args: argparse.Namespace, *, findings: list[dict[str, Any]] | None = None) -> list[str]:
    raw_domains = read_json_or_text_list(Path(args.domains_file))
    return normalize_domains(
        raw_domains,
        allow_broad_wildcard=args.allow_broad_wildcard,
        findings=findings,
    )


def validate_domain_config_for_args(args: argparse.Namespace) -> tuple[list[str], list[dict[str, Any]]]:
    try:
        raw_domains = read_json_or_text_list(Path(args.domains_file))
    except PolicyError as exc:
        return [], [finding("fail", "domains-file", str(exc))]
    return validate_connector_config(
        connector_name=args.connector_name,
        connector_tag=args.connector_tag,
        raw_domains=raw_domains,
        allow_broad_wildcard=args.allow_broad_wildcard,
    )


def unified_policy_diff(before_text: str, after_text: str, *, fromfile: str, tofile: str = "merged policy") -> str:
    return "".join(
        difflib.unified_diff(
            before_text.splitlines(keepends=True),
            after_text.splitlines(keepends=True),
            fromfile=fromfile,
            tofile=tofile,
        )
    )


def merge_with_report(
    policy: dict[str, Any],
    *,
    connector_name: str,
    connector_tag: str,
    domains: list[str],
    tag_owner: str,
    member_src: str,
    report: dict[str, Any] | None = None,
) -> dict[str, Any]:
    merged = merge_policy(
        copy.deepcopy(policy),
        connector_name=connector_name,
        connector_tag=connector_tag,
        domains=domains,
        tag_owner=tag_owner,
        member_src=member_src,
    )
    if report is None:
        return merged

    second_merge = merge_policy(
        copy.deepcopy(merged),
        connector_name=connector_name,
        connector_tag=connector_tag,
        domains=domains,
        tag_owner=tag_owner,
        member_src=member_src,
    )
    if second_merge == merged:
        add_finding(report, "ok", "merge-idempotent", "Second merge produced no semantic changes.")
    else:
        add_finding(report, "fail", "merge-idempotent", "Second merge produced semantic changes.")
    return merged


def auth_header_from_token(token: str, mode: str) -> str:
    if mode == "basic":
        encoded = base64.b64encode(f"{token}:".encode("utf-8")).decode("ascii")
        return f"Basic {encoded}"
    return f"Bearer {token}"


def api_timeout() -> float:
    raw = os.environ.get("TAILSCALE_API_TIMEOUT", "60")
    try:
        timeout = float(raw)
    except ValueError as exc:
        raise PolicyError("TAILSCALE_API_TIMEOUT must be a positive number of seconds.") from exc
    if timeout <= 0:
        raise PolicyError("TAILSCALE_API_TIMEOUT must be a positive number of seconds.")
    return timeout


def is_retryable_url_error(exc: urllib.error.URLError) -> bool:
    reason = getattr(exc, "reason", None)
    if isinstance(reason, (ssl.SSLError, socket.gaierror)):
        return False
    return isinstance(reason, (TimeoutError, ConnectionError, OSError))


def http_request(
    method: str,
    url: str,
    *,
    token: str | None = None,
    token_mode: str = "bearer",
    body: str | None = None,
    content_type: str = "application/json",
    extra_headers: dict[str, str] | None = None,
) -> tuple[int, str, dict[str, str]]:
    data = body.encode("utf-8") if body is not None else None
    headers = {
        "Accept": "application/json, application/hujson, text/plain",
        "User-Agent": f"tailscale-ai-egress-policy-tool/{__version__}",
    }
    if token:
        headers["Authorization"] = auth_header_from_token(token, token_mode)
    if data is not None:
        headers["Content-Type"] = content_type
    if extra_headers:
        headers.update(extra_headers)
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    for attempt in range(2):
        try:
            with urllib.request.urlopen(req, timeout=api_timeout()) as resp:
                response_headers = {k: v for k, v in resp.headers.items()}
                return resp.status, resp.read().decode("utf-8"), response_headers
        except urllib.error.HTTPError as exc:
            response_headers = {k: v for k, v in exc.headers.items()} if exc.headers else {}
            text = exc.read().decode("utf-8", errors="replace")
            if exc.code in TRANSIENT_HTTP_STATUSES and attempt == 0:
                time.sleep(1)
                continue
            return exc.code, text, response_headers
        except urllib.error.URLError as exc:
            if attempt == 0 and is_retryable_url_error(exc):
                time.sleep(1)
                continue
            raise PolicyError(f"Network request failed for {url}: {redact_sensitive(str(exc))}") from exc
    raise PolicyError(f"Network request failed for {url}: retry attempts exhausted.")


def get_oauth_token(client_id: str, client_secret: str, scopes: str) -> str:
    body = urllib.parse.urlencode(
        {
            "grant_type": "client_credentials",
            "client_id": client_id,
            "client_secret": client_secret,
            "scope": scopes,
        }
    )
    status, text, _ = http_request(
        "POST",
        "https://api.tailscale.com/api/v2/oauth/token",
        body=body,
        content_type="application/x-www-form-urlencoded",
    )
    if status < 200 or status >= 300:
        raise PolicyError(f"OAuth token request failed ({status}): {safe_response_text(text)}")
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise PolicyError(f"OAuth token response was not valid JSON: {exc}") from exc
    token = data.get("access_token")
    if not isinstance(token, str) or not token:
        raise PolicyError("OAuth token response did not include access_token.")
    return token


def get_api_token(args: argparse.Namespace) -> tuple[str, str]:
    token_mode = os.environ.get("TAILSCALE_API_AUTH", "bearer").lower()
    api_key = args.api_key or os.environ.get("TAILSCALE_API_KEY")
    if api_key:
        return api_key, token_mode

    client_id = args.oauth_client_id or os.environ.get("TAILSCALE_OAUTH_CLIENT_ID")
    client_secret = args.oauth_client_secret or os.environ.get("TAILSCALE_OAUTH_CLIENT_SECRET")
    if client_id and client_secret:
        scopes = args.oauth_scopes or os.environ.get(
            "TAILSCALE_OAUTH_SCOPES",
            "policy_file devices:core:read devices:posture_attributes:read",
        )
        return get_oauth_token(client_id, client_secret, scopes), "bearer"

    if args.prompt_token:
        token = getpass.getpass("Tailscale API token (input hidden): ").strip()
        if token:
            return token, token_mode

    raise PolicyError(
        "Missing credential. Set TAILSCALE_API_KEY or TAILSCALE_OAUTH_CLIENT_ID/"
        "TAILSCALE_OAUTH_CLIENT_SECRET."
    )


def tailscale_api(
    method: str,
    path: str,
    *,
    token: str,
    token_mode: str,
    body: str | None = None,
    allow_basic_fallback: bool = True,
    extra_headers: dict[str, str] | None = None,
) -> tuple[int, str, str, dict[str, str]]:
    url = f"{API_BASE}{path}"
    status, text, headers = http_request(
        method,
        url,
        token=token,
        token_mode=token_mode,
        body=body,
        extra_headers=extra_headers,
    )
    used_mode = token_mode

    if status == 401 and token_mode == "bearer" and allow_basic_fallback:
        status, text, headers = http_request(
            method,
            url,
            token=token,
            token_mode="basic",
            body=body,
            extra_headers=extra_headers,
        )
        used_mode = "basic"

    return status, text, used_mode, headers


def tailnet_path(tailnet: str, suffix: str) -> str:
    return f"/tailnet/{urllib.parse.quote(tailnet, safe='')}{suffix}"


def utc_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def isoformat_utc(moment: dt.datetime | None = None) -> str:
    value = moment or utc_now()
    return value.isoformat(timespec="seconds").replace("+00:00", "Z")


def new_plan_id() -> str:
    return f"{utc_now().strftime('%Y%m%dT%H%M%SZ')}-{secrets.token_hex(4)}"


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def case_insensitive_header(headers: dict[str, str], name: str) -> str | None:
    expected = name.lower()
    for key, value in headers.items():
        if key.lower() == expected:
            return value
    return None


def manifest_major_version(schema_version: Any) -> int:
    if isinstance(schema_version, int):
        return schema_version
    if isinstance(schema_version, str):
        match = re.match(r"^(\d+)(?:\.|$)", schema_version)
        if match:
            return int(match.group(1))
    raise PolicyError("Plan manifest schema_version is missing or invalid; regenerate the plan.")


def ensure_supported_manifest(manifest: dict[str, Any]) -> None:
    major = manifest_major_version(manifest.get("schema_version"))
    if major != MANIFEST_SCHEMA_VERSION:
        raise PolicyError(
            "Unsupported plan manifest schema_version "
            f"{manifest.get('schema_version')!r}; this policy tool supports major version "
            f"{MANIFEST_SCHEMA_VERSION}. Regenerate the plan with a compatible policy_tool.py."
        )


def read_manifest(plan_dir: Path) -> dict[str, Any]:
    manifest_path = plan_dir / "manifest.json"
    try:
        text = manifest_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise PolicyError(f"Missing or unreadable manifest.json in {plan_dir}: {exc}") from exc
    try:
        manifest = json.loads(text)
    except json.JSONDecodeError as exc:
        raise PolicyError(f"manifest.json is not valid JSON: {exc}") from exc
    if not isinstance(manifest, dict):
        raise PolicyError("manifest.json must be a JSON object.")
    ensure_supported_manifest(manifest)
    return manifest


def write_text_atomic(path: Path, text: str) -> None:
    tmp = path.with_name(f".{path.name}.tmp.{os.getpid()}.{secrets.token_hex(4)}")
    try:
        tmp.write_text(text, encoding="utf-8")
        # The sole caller writes the plan manifest, which carries policy detail.
        # Set 0600 on the temp before the atomic replace so a later status
        # rewrite cannot widen the manifest back to the process umask.
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except OSError as exc:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise PolicyError(f"Could not write {path}: {exc}") from exc


def update_manifest_status(plan_dir: Path, manifest: dict[str, Any], status: str, timestamp_key: str) -> None:
    updated = dict(manifest)
    timestamp = isoformat_utc()
    updated["status"] = status
    if timestamp_key in updated:
        history_key = f"{timestamp_key}_history"
        history = updated.get(history_key)
        if not isinstance(history, list):
            history = []
        history.append(timestamp)
        updated[history_key] = history
    else:
        updated[timestamp_key] = timestamp
    write_text_atomic(plan_dir / "manifest.json", dumps(updated))


def write_plan_directory(final_dir: Path, files: dict[str, str]) -> Path:
    # Plan bundles contain the full tailnet policy; keep them private (0700 dir,
    # 0600 files) rather than at the process umask on shared hosts.
    tmp_dir = final_dir.parent / f".tmp.{final_dir.name}.{os.getpid()}.{secrets.token_hex(4)}"
    try:
        final_dir.parent.mkdir(parents=True, exist_ok=True)
        tmp_dir.mkdir()
        os.chmod(tmp_dir, 0o700)
        for name, content in files.items():
            target = tmp_dir / name
            if target.parent != tmp_dir:
                raise PolicyError(f"Invalid plan artifact path: {name}")
            target.write_text(content, encoding="utf-8")
            os.chmod(target, 0o600)
        os.replace(tmp_dir, final_dir)
    except Exception:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        raise
    return final_dir


def diff_summary(diff_text: str) -> dict[str, int]:
    added = 0
    removed = 0
    for line in diff_text.splitlines():
        if line.startswith("+++") or line.startswith("---"):
            continue
        if line.startswith("+"):
            added += 1
        elif line.startswith("-"):
            removed += 1
    return {"added": added, "removed": removed}


def write_backup(backup_dir: Path, text: str) -> Path:
    # The backup holds the full tailnet policy; keep the directory and file
    # private (0700/0600) rather than at the process umask on shared hosts.
    backup_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(backup_dir, 0o700)
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = backup_dir / f"tailnet-policy.backup.{stamp}.hujson"
    path.write_text(text, encoding="utf-8")
    os.chmod(path, 0o600)
    return path


def failed_plan_artifacts(
    *,
    plans_dir: Path,
    plan_id: str,
    report: dict[str, Any],
    current_text: str | None = None,
    merged_text: str | None = None,
    diff_text: str | None = None,
) -> Path:
    refresh_summary(report)
    files = {"report.invalid.json": dumps(report)}
    if current_text is not None:
        files["current.hujson"] = current_text
    if merged_text is not None:
        files["merged.json"] = merged_text
    if diff_text is not None:
        files["diff.patch"] = diff_text
    try:
        return write_plan_directory(plans_dir / f"failed.{plan_id}", files)
    except OSError as exc:
        raise PolicyError(f"Could not write failed plan artifacts: {exc}") from exc


def valid_plan_artifacts(
    *,
    plans_dir: Path,
    plan_id: str,
    manifest: dict[str, Any],
    current_text: str,
    merged_text: str,
    diff_text: str,
    preview_text: str | None,
) -> Path:
    files = {
        "current.hujson": current_text,
        "merged.json": merged_text,
        "diff.patch": diff_text,
        "manifest.json": dumps(manifest),
    }
    if preview_text is not None:
        files["api-preview.json"] = preview_text
    try:
        return write_plan_directory(plans_dir / f"plan.{plan_id}", files)
    except OSError as exc:
        raise PolicyError(f"Could not write plan bundle: {exc}") from exc


def print_plan_result(*, status: str, plan_id: str, path: Path, report: dict[str, Any], diff_text: str | None) -> None:
    refresh_summary(report)
    summary = report["summary"]
    print(f"Plan status: {status}")
    print(f"Plan ID: {plan_id}")
    label = "Plan directory" if status == "valid" else "Failed plan artifacts"
    print(f"{label}: {path}")
    if diff_text is not None:
        changes = diff_summary(diff_text)
        print(f"Diff summary: +{changes['added']} -{changes['removed']} policy line(s)")
    print(f"Validation summary: {summary['ok']} ok, {summary['warn']} warning(s), {summary['fail']} failure(s)")


def preview_policy_with_api(
    args: argparse.Namespace,
    *,
    token: str,
    token_mode: str,
    merged_text: str,
    report: dict[str, Any],
) -> str | None:
    preview_path = tailnet_path(args.tailnet, "/acl/preview")
    try:
        preview_status, raw_preview_text, _used_mode, _ = tailscale_api(
            "POST",
            preview_path,
            token=token,
            token_mode=token_mode,
            body=merged_text,
            allow_basic_fallback=False,
        )
        if 200 <= preview_status < 300:
            add_finding(report, "ok", "tailscale-api-preview", "Tailscale policy preview succeeded.")
            return raw_preview_text if raw_preview_text.strip() else "{}\n"

        add_finding(
            report,
            "warn",
            "tailscale-api-preview",
            f"Tailscale policy preview was unavailable or failed ({preview_status}); validation still passed.",
            {"status": preview_status, "response": safe_response_text(raw_preview_text)},
        )
        return None
    except PolicyError as exc:
        add_finding(
            report,
            "warn",
            "tailscale-api-preview",
            f"Tailscale policy preview was unavailable; validation still passed: {redact_sensitive(str(exc))}",
        )
        return None


def create_policy_plan(args: argparse.Namespace) -> int:
    plan_id = new_plan_id()
    if not PLAN_ID_RE.match(plan_id):
        raise PolicyError(f"Generated invalid plan id: {plan_id}")
    plans_dir = Path(args.plans_dir)
    report = new_report("plan")
    created_at = isoformat_utc()

    domains, config_findings = validate_domain_config_for_args(args)
    add_findings(report, config_findings)
    if first_failure(config_findings) is not None:
        path = failed_plan_artifacts(plans_dir=plans_dir, plan_id=plan_id, report=report)
        print_plan_result(status="failed", plan_id=plan_id, path=path, report=report, diff_text=None)
        return 1

    token, token_mode = get_api_token(args)
    acl_path = tailnet_path(args.tailnet, "/acl")
    status, current_text, used_mode, get_headers = tailscale_api("GET", acl_path, token=token, token_mode=token_mode)
    if status < 200 or status >= 300:
        raise PolicyError(f"Could not fetch current policy ({status}): {safe_response_text(current_text)}")
    etag = case_insensitive_header(get_headers, "ETag")

    policy, policy_findings = validate_policy_text(current_text, connector_tag=args.connector_tag)
    add_findings(report, policy_findings)
    policy_failure = first_failure(policy_findings)
    if policy is None or policy_failure is not None:
        path = failed_plan_artifacts(
            plans_dir=plans_dir,
            plan_id=plan_id,
            report=report,
            current_text=current_text,
        )
        print_plan_result(status="failed", plan_id=plan_id, path=path, report=report, diff_text=None)
        return 1

    try:
        merged = merge_with_report(
            policy,
            connector_name=args.connector_name,
            connector_tag=args.connector_tag,
            domains=domains,
            tag_owner=args.tag_owner,
            member_src=args.member_src,
            report=report,
        )
    except PolicyError as exc:
        add_finding(report, "fail", "merge-policy", str(exc))
        path = failed_plan_artifacts(
            plans_dir=plans_dir,
            plan_id=plan_id,
            report=report,
            current_text=current_text,
        )
        print_plan_result(status="failed", plan_id=plan_id, path=path, report=report, diff_text=None)
        return 1

    merged_text = dumps(merged)
    diff_text = unified_policy_diff(current_text, merged_text, fromfile="current API policy")
    _merged_policy, merged_findings = validate_policy_text(merged_text, connector_tag=args.connector_tag)
    add_findings(report, merged_findings)
    if first_failure(merged_findings) is not None:
        path = failed_plan_artifacts(
            plans_dir=plans_dir,
            plan_id=plan_id,
            report=report,
            current_text=current_text,
            merged_text=merged_text,
            diff_text=diff_text,
        )
        print_plan_result(status="failed", plan_id=plan_id, path=path, report=report, diff_text=diff_text)
        return 1

    validate_path = tailnet_path(args.tailnet, "/acl/validate")
    status, validate_text, used_mode, _ = tailscale_api(
        "POST",
        validate_path,
        token=token,
        token_mode=used_mode,
        body=merged_text,
        allow_basic_fallback=True,
    )
    if status < 200 or status >= 300:
        add_finding(
            report,
            "fail",
            "tailscale-api-validate",
            f"Tailscale policy validation failed ({status}): {safe_response_text(validate_text)}",
            {"status": status},
        )
        path = failed_plan_artifacts(
            plans_dir=plans_dir,
            plan_id=plan_id,
            report=report,
            current_text=current_text,
            merged_text=merged_text,
            diff_text=diff_text,
        )
        print_plan_result(status="failed", plan_id=plan_id, path=path, report=report, diff_text=diff_text)
        return 1
    add_finding(report, "ok", "tailscale-api-validate", "Tailscale policy validation passed.")

    preview_text = preview_policy_with_api(
        args,
        token=token,
        token_mode=used_mode,
        merged_text=merged_text,
        report=report,
    )

    refresh_summary(report)
    manifest = {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "tool_version": __version__,
        "plan_id": plan_id,
        "status": "valid",
        "created_at": created_at,
        "tailnet": args.tailnet,
        "connector_name": args.connector_name,
        "connector_tag": args.connector_tag,
        "domains_sha256": sha256_text(dumps(domains)),
        "current_sha256": sha256_text(current_text),
        "merged_sha256": sha256_text(merged_text),
        "etag": etag,
        "summary": report["summary"],
        "findings": report["findings"],
    }
    path = valid_plan_artifacts(
        plans_dir=plans_dir,
        plan_id=plan_id,
        manifest=manifest,
        current_text=current_text,
        merged_text=merged_text,
        diff_text=diff_text,
        preview_text=preview_text,
    )
    print_plan_result(status="valid", plan_id=plan_id, path=path, report=report, diff_text=diff_text)
    print(f"Apply with: python3 scripts/policy_tool.py apply-plan {path}")
    return 0


def confirm_exact_action(*, action: str, plan_id: str, yes: bool) -> None:
    expected = f"{action} {plan_id}"
    if yes:
        if os.environ.get("POLICY_RISK_ACK") != "1":
            raise PolicyError(
                f"{action.lower()}-plan --yes also requires POLICY_RISK_ACK=1 and an explicit plan directory."
            )
        return

    if not sys.stdin.isatty():
        raise PolicyError(
            f"Missing confirmation. Interactive {action.lower()} requires exact confirmation: {expected}. "
            f"Non-interactive use requires --yes, POLICY_RISK_ACK=1, and an explicit plan directory."
        )

    answer = input(f'Type "{expected}" to continue: ')
    if answer != expected:
        raise PolicyError(f"Missing confirmation; expected exactly: {expected}")


def resolve_plan_tailnet(args: argparse.Namespace, manifest: dict[str, Any]) -> str:
    tailnet = args.tailnet or manifest.get("tailnet") or os.environ.get("TAILSCALE_TAILNET") or "-"
    if not isinstance(tailnet, str) or not tailnet:
        raise PolicyError("Plan manifest tailnet is missing or invalid.")
    return tailnet


def apply_policy_plan(args: argparse.Namespace) -> int:
    plan_dir = Path(args.plan_dir)
    manifest = read_manifest(plan_dir)
    plan_id = manifest.get("plan_id")
    if not isinstance(plan_id, str) or not PLAN_ID_RE.match(plan_id):
        raise PolicyError("Plan manifest has an invalid plan_id; regenerate the plan.")
    if manifest.get("status") != "valid":
        raise PolicyError(f"apply-plan requires a plan with status 'valid' (got {manifest.get('status')!r}).")

    try:
        merged_text = (plan_dir / "merged.json").read_text(encoding="utf-8")
    except OSError as exc:
        raise PolicyError(f"Could not read merged.json: {exc}") from exc
    if sha256_text(merged_text) != manifest.get("merged_sha256"):
        raise PolicyError("merged.json SHA-256 does not match manifest.json; regenerate the plan.")

    parse_policy(merged_text)
    confirm_exact_action(action="APPLY", plan_id=plan_id, yes=args.yes)

    token, token_mode = get_api_token(args)
    tailnet = resolve_plan_tailnet(args, manifest)
    validate_path = tailnet_path(tailnet, "/acl/validate")
    status, validate_text, used_mode, _ = tailscale_api(
        "POST",
        validate_path,
        token=token,
        token_mode=token_mode,
        body=merged_text,
        allow_basic_fallback=True,
    )
    if status < 200 or status >= 300:
        raise PolicyError(f"Plan revalidation failed ({status}): {safe_response_text(validate_text)}")

    apply_headers = {"If-Match": manifest["etag"]} if manifest.get("etag") else None
    status, apply_text, used_mode, _ = tailscale_api(
        "POST",
        tailnet_path(tailnet, "/acl"),
        token=token,
        token_mode=used_mode,
        body=merged_text,
        allow_basic_fallback=False,
        extra_headers=apply_headers,
    )
    if status == 412:
        raise PolicyError(
            "Planning ETag is stale (412). The tailnet policy changed after this plan was generated; "
            "regenerate the plan."
        )
    if status < 200 or status >= 300:
        raise PolicyError(f"Tailscale policy apply failed ({status}): {safe_response_text(apply_text)}")

    try:
        update_manifest_status(plan_dir, manifest, "applied", "applied_at")
    except PolicyError as exc:
        raise PolicyError(
            f"Tailscale policy plan {plan_id} was applied, but manifest.json could not be updated: {exc}. "
            "Do not re-run apply-plan for this bundle; regenerate a new plan against the current policy."
        ) from exc
    eprint(f"Tailscale policy plan applied: {plan_id}")
    return 0


def restore_policy_plan(args: argparse.Namespace) -> int:
    plan_dir = Path(args.plan_dir)
    manifest = read_manifest(plan_dir)
    plan_id = manifest.get("plan_id")
    if not isinstance(plan_id, str) or not PLAN_ID_RE.match(plan_id):
        raise PolicyError("Plan manifest has an invalid plan_id; regenerate the plan.")
    if manifest.get("status") not in {"applied", "restored"}:
        raise PolicyError(
            "restore-plan requires a plan with status 'applied' or 'restored' "
            f"(got {manifest.get('status')!r})."
        )

    try:
        restore_text = (plan_dir / "current.hujson").read_text(encoding="utf-8")
    except OSError as exc:
        raise PolicyError(f"Could not read current.hujson: {exc}") from exc
    if sha256_text(restore_text) != manifest.get("current_sha256"):
        raise PolicyError("current.hujson SHA-256 does not match manifest.json; restore is unsafe.")

    policy, findings = validate_policy_text(restore_text)
    if policy is None or first_failure(findings) is not None:
        failure = first_failure(findings)
        raise PolicyError(failure["message"] if failure else "Could not validate current.hujson.")

    confirm_exact_action(action="RESTORE", plan_id=plan_id, yes=args.yes)

    token, token_mode = get_api_token(args)
    tailnet = resolve_plan_tailnet(args, manifest)
    status, current_text, used_mode, get_headers = tailscale_api(
        "GET",
        tailnet_path(tailnet, "/acl"),
        token=token,
        token_mode=token_mode,
    )
    if status < 200 or status >= 300:
        raise PolicyError(f"Could not fetch current policy ({status}): {safe_response_text(current_text)}")
    etag = case_insensitive_header(get_headers, "ETag")

    validate_path = tailnet_path(tailnet, "/acl/validate")
    status, validate_text, used_mode, _ = tailscale_api(
        "POST",
        validate_path,
        token=token,
        token_mode=used_mode,
        body=restore_text,
        allow_basic_fallback=False,
    )
    if status < 200 or status >= 300:
        raise PolicyError(f"Restore policy validation failed ({status}): {safe_response_text(validate_text)}")

    apply_headers = {"If-Match": etag} if etag else None
    status, apply_text, used_mode, _ = tailscale_api(
        "POST",
        tailnet_path(tailnet, "/acl"),
        token=token,
        token_mode=used_mode,
        body=restore_text,
        allow_basic_fallback=False,
        extra_headers=apply_headers,
    )
    if status == 412:
        raise PolicyError(
            "Current policy ETag is stale (412). Re-run restore-plan so it can fetch a fresh policy ETag."
        )
    if status < 200 or status >= 300:
        raise PolicyError(f"Tailscale policy restore failed ({status}): {safe_response_text(apply_text)}")

    try:
        update_manifest_status(plan_dir, manifest, "restored", "restored_at")
    except PolicyError as exc:
        raise PolicyError(
            f"Tailscale policy plan {plan_id} was restored, but manifest.json could not be updated: {exc}. "
            "Inspect the current tailnet policy before re-running restore-plan."
        ) from exc
    eprint(f"Tailscale policy plan restored: {plan_id}")
    return 0


def plan_id_from_dir_name(path: Path) -> str | None:
    for prefix in ("plan.", "failed."):
        if path.name.startswith(prefix):
            plan_id = path.name[len(prefix) :]
            if PLAN_ID_RE.match(plan_id):
                return plan_id
    return None


def collect_plan_records(plans_dir: Path) -> list[dict[str, Any]]:
    if not plans_dir.exists():
        return []
    records: list[dict[str, Any]] = []
    for child in sorted(plans_dir.iterdir(), key=lambda item: item.name):
        if not child.is_dir():
            continue
        inferred_plan_id = plan_id_from_dir_name(child)
        if inferred_plan_id is None:
            continue
        manifest_path = child / "manifest.json"
        if manifest_path.exists():
            try:
                manifest = read_manifest(child)
                record = {
                    "status": manifest.get("status", "unknown"),
                    "plan_id": manifest.get("plan_id", inferred_plan_id),
                    "created_at": manifest.get("created_at", ""),
                    "connector_name": manifest.get("connector_name", ""),
                    "connector_tag": manifest.get("connector_tag", ""),
                    "path": str(child),
                }
            except PolicyError as exc:
                record = {
                    "status": "invalid",
                    "plan_id": inferred_plan_id,
                    "created_at": "",
                    "connector_name": "",
                    "connector_tag": "",
                    "path": str(child),
                    "error": str(exc),
                }
        else:
            status = "failed" if child.name.startswith("failed.") else "invalid"
            record = {
                "status": status,
                "plan_id": inferred_plan_id,
                "created_at": "",
                "connector_name": "",
                "connector_tag": "",
                "path": str(child),
            }
            report_path = child / "report.invalid.json"
            if report_path.exists():
                try:
                    report = json.loads(report_path.read_text(encoding="utf-8"))
                    if isinstance(report, dict):
                        record["summary"] = report.get("summary", {})
                except (OSError, json.JSONDecodeError):
                    pass
        records.append(record)
    return records


def list_policy_plans(args: argparse.Namespace) -> int:
    plans_dir = Path(args.plans_dir)
    records = collect_plan_records(plans_dir)
    if args.json:
        print(dumps({"plans_dir": str(plans_dir), "plans": records}), end="")
        return 0
    if not records:
        print(f"No policy plans found in {plans_dir}")
        return 0
    print(f"{'STATUS':<10} {'PLAN ID':<26} {'CREATED AT':<22} {'CONNECTOR':<20} PATH")
    for record in records:
        connector = record.get("connector_name") or record.get("connector_tag") or "-"
        print(
            f"{str(record.get('status', '-')):<10} "
            f"{str(record.get('plan_id', '-')):<26} "
            f"{str(record.get('created_at', '-')):<22} "
            f"{str(connector):<20} "
            f"{record.get('path', '-')}"
        )
    return 0


def apply_removed(_args: argparse.Namespace) -> NoReturn:
    # Migration tombstone: the direct 'apply' command was removed after its
    # one-release deprecation. Auditable planning replaces it.
    raise PolicyError(
        "'apply' has been removed. Create an auditable bundle with 'plan', review "
        "it, then run 'apply-plan <plan-dir>'. See docs/Stability.md."
    )


def restore_policy(args: argparse.Namespace) -> None:
    try:
        restore_text = Path(args.input).read_text(encoding="utf-8")
    except OSError as exc:
        raise PolicyError(f"Could not read {args.input}: {exc}") from exc

    parse_policy(restore_text)
    token, token_mode = get_api_token(args)
    path = tailnet_path(args.tailnet, "/acl")
    status, current_text, used_mode, get_headers = tailscale_api("GET", path, token=token, token_mode=token_mode)
    if status < 200 or status >= 300:
        raise PolicyError(f"Could not fetch current policy ({status}): {safe_response_text(current_text)}")
    etag = case_insensitive_header(get_headers, "ETag")

    backup_path = write_backup(Path(args.backup_dir), current_text)
    eprint(f"Saved current policy backup before restore: {backup_path}")

    validate_path = tailnet_path(args.tailnet, "/acl/validate")
    status, validate_text, used_mode, _ = tailscale_api(
        "POST",
        validate_path,
        token=token,
        token_mode=used_mode,
        body=restore_text,
        allow_basic_fallback=False,
    )
    if status < 200 or status >= 300:
        raise PolicyError(f"Backup policy validation failed ({status}): {safe_response_text(validate_text)}")

    if args.dry_run:
        print(restore_text, end="")
        eprint("Dry run only; backup policy was not restored.")
        return

    apply_headers = {"If-Match": etag} if etag else None
    status, apply_text, used_mode, _ = tailscale_api(
        "POST",
        path,
        token=token,
        token_mode=used_mode,
        body=restore_text,
        allow_basic_fallback=False,
        extra_headers=apply_headers,
    )
    if status == 412:
        raise PolicyError(
            "Tailscale policy changed after it was fetched (ETag conflict). "
            "Re-run the rollback against the latest policy."
        )
    if status < 200 or status >= 300:
        raise PolicyError(f"Tailscale policy restore failed ({status}): {safe_response_text(apply_text)}")
    eprint("Tailscale policy restored.")


def add_common_args(
    parser: argparse.ArgumentParser,
    *,
    domains_file_default: str | None = "policy/default-ai-domains.json",
) -> None:
    parser.add_argument("--domains-file", default=domains_file_default)
    parser.add_argument("--connector-name", default=default_connector_name())
    parser.add_argument("--connector-tag", default=default_connector_tag())
    parser.add_argument("--tag-owner", default=DEFAULT_TAG_OWNER)
    parser.add_argument("--member-src", default=DEFAULT_MEMBER_SRC)
    parser.add_argument(
        "--allow-broad-wildcard",
        action="store_true",
        help="Allow explicitly blocked broad wildcard domains such as *.com.",
    )


def add_report_arg(parser: argparse.ArgumentParser, *, default: str | None = None) -> None:
    parser.add_argument("--report", choices=("json", "text"), default=default)


def load_domains(args: argparse.Namespace) -> list[str]:
    return read_domains_for_args(args)


def handle_validate(args: argparse.Namespace) -> int:
    if not args.input and not args.domains_file:
        raise PolicyError("validate requires --input, --domains-file, or both.")

    report = new_report("validate")
    if args.input:
        try:
            policy_text = Path(args.input).read_text(encoding="utf-8")
        except OSError as exc:
            add_finding(report, "fail", "policy-file", f"Could not read {args.input}: {exc}")
        else:
            _policy, policy_findings = validate_policy_text(
                policy_text,
                connector_tag=args.connector_tag if args.domains_file else None,
            )
            add_findings(report, policy_findings)

    if args.domains_file:
        _domains, config_findings = validate_domain_config_for_args(args)
        add_findings(report, config_findings)

    emit_report(report, args.report, sys.stdout)
    return 1 if has_failures(report) else 0


def handle_merge(args: argparse.Namespace) -> int:
    report = new_report("merge") if args.report else None
    domains, config_findings = validate_domain_config_for_args(args)
    if report is not None:
        add_findings(report, config_findings)
    config_failure = first_failure(config_findings)
    if config_failure is not None:
        if report is not None:
            emit_report(report, args.report, sys.stderr)
            return 1
        raise PolicyError(config_failure["message"])

    try:
        policy_text = Path(args.input).read_text(encoding="utf-8")
    except OSError as exc:
        raise PolicyError(f"Could not read {args.input}: {exc}") from exc

    policy, policy_findings = validate_policy_text(policy_text, connector_tag=args.connector_tag)
    if report is not None:
        add_findings(report, policy_findings)
    policy_failure = first_failure(policy_findings)
    if policy is None or policy_failure is not None:
        if report is not None:
            emit_report(report, args.report, sys.stderr)
            return 1
        raise PolicyError(policy_failure["message"] if policy_failure else "Could not parse policy.")

    merged = merge_with_report(
        policy,
        connector_name=args.connector_name,
        connector_tag=args.connector_tag,
        domains=domains,
        tag_owner=args.tag_owner,
        member_src=args.member_src,
        report=report,
    )
    merged_text = dumps(merged)
    if args.diff:
        sys.stderr.write(unified_policy_diff(policy_text, merged_text, fromfile=args.input))
    if args.output:
        try:
            Path(args.output).write_text(merged_text, encoding="utf-8")
        except OSError as exc:
            raise PolicyError(f"Could not write {args.output}: {exc}") from exc
    else:
        print(merged_text, end="")
    if report is not None:
        emit_report(report, args.report, sys.stderr)
    return 0


def handle_diff(args: argparse.Namespace) -> int:
    domains, config_findings = validate_domain_config_for_args(args)
    config_failure = first_failure(config_findings)
    if config_failure is not None:
        raise PolicyError(config_failure["message"])

    try:
        policy_text = Path(args.input).read_text(encoding="utf-8")
    except OSError as exc:
        raise PolicyError(f"Could not read {args.input}: {exc}") from exc

    policy, policy_findings = validate_policy_text(policy_text, connector_tag=args.connector_tag)
    policy_failure = first_failure(policy_findings)
    if policy is None or policy_failure is not None:
        raise PolicyError(policy_failure["message"] if policy_failure else "Could not parse policy.")

    merged = merge_with_report(
        policy,
        connector_name=args.connector_name,
        connector_tag=args.connector_tag,
        domains=domains,
        tag_owner=args.tag_owner,
        member_src=args.member_src,
    )
    print(unified_policy_diff(policy_text, dumps(merged), fromfile=args.input), end="")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--version",
        action="version",
        version=f"tailscale-ai-egress policy_tool.py {__version__}",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    snippet = subparsers.add_parser("snippet", help="Print a policy fragment for manual paste/merge.")
    add_common_args(snippet)

    validate = subparsers.add_parser("validate", help="Validate a local policy file and/or connector domain config.")
    add_common_args(validate, domains_file_default=None)
    add_report_arg(validate, default="text")
    validate.add_argument("--input")

    merge = subparsers.add_parser("merge", help="Merge the connector config into a local policy file.")
    add_common_args(merge)
    add_report_arg(merge)
    merge.add_argument("--input", required=True)
    merge.add_argument("--output")
    merge.add_argument("--diff", action="store_true")

    diff_cmd = subparsers.add_parser("diff", help="Print a unified diff between a local policy and merged output.")
    add_common_args(diff_cmd)
    diff_cmd.add_argument("--input", required=True)

    plan = subparsers.add_parser("plan", help="Fetch, merge, validate, and write an auditable policy plan bundle.")
    add_common_args(plan)
    plan.add_argument("--tailnet", default=os.environ.get("TAILSCALE_TAILNET", "-"))
    plan.add_argument("--api-key")
    plan.add_argument("--oauth-client-id")
    plan.add_argument("--oauth-client-secret")
    plan.add_argument("--oauth-scopes")
    plan.add_argument("--prompt-token", action="store_true")
    plan.add_argument("--plans-dir", default="generated/policy-plans")

    apply_plan = subparsers.add_parser("apply-plan", help="Apply an existing valid policy plan bundle.")
    apply_plan.add_argument("plan_dir")
    apply_plan.add_argument("--tailnet")
    apply_plan.add_argument("--api-key")
    apply_plan.add_argument("--oauth-client-id")
    apply_plan.add_argument("--oauth-client-secret")
    apply_plan.add_argument("--oauth-scopes")
    apply_plan.add_argument("--prompt-token", action="store_true")
    apply_plan.add_argument("--yes", action="store_true")

    list_plans = subparsers.add_parser("list-plans", help="List generated policy plan bundles.")
    list_plans.add_argument("--plans-dir", default="generated/policy-plans")
    list_plans.add_argument("--json", action="store_true")

    restore_plan = subparsers.add_parser("restore-plan", help="Restore the current.hujson captured by an applied plan.")
    restore_plan.add_argument("plan_dir")
    restore_plan.add_argument("--tailnet")
    restore_plan.add_argument("--api-key")
    restore_plan.add_argument("--oauth-client-id")
    restore_plan.add_argument("--oauth-client-secret")
    restore_plan.add_argument("--oauth-scopes")
    restore_plan.add_argument("--prompt-token", action="store_true")
    restore_plan.add_argument("--yes", action="store_true")

    apply = subparsers.add_parser(
        "apply",
        help="Removed — use 'plan' then 'apply-plan'.",
        description="'apply' has been removed. Use 'plan' to create an auditable bundle, then 'apply-plan <plan-dir>'.",
    )
    add_common_args(apply)
    add_report_arg(apply)
    apply.add_argument("--tailnet", default=os.environ.get("TAILSCALE_TAILNET", "-"))
    apply.add_argument("--api-key")
    apply.add_argument("--oauth-client-id")
    apply.add_argument("--oauth-client-secret")
    apply.add_argument("--oauth-scopes")
    apply.add_argument("--prompt-token", action="store_true")
    apply.add_argument("--backup-dir", default="generated")
    apply.add_argument("--output")
    apply.add_argument("--dry-run", action="store_true")
    apply.add_argument("--diff", action="store_true")

    restore = subparsers.add_parser("restore", help="Validate and restore a saved tailnet policy backup.")
    restore.add_argument("--input", required=True)
    restore.add_argument("--tailnet", default=os.environ.get("TAILSCALE_TAILNET", "-"))
    restore.add_argument("--api-key")
    restore.add_argument("--oauth-client-id")
    restore.add_argument("--oauth-client-secret")
    restore.add_argument("--oauth-scopes")
    restore.add_argument("--prompt-token", action="store_true")
    restore.add_argument("--backup-dir", default="generated")
    restore.add_argument("--dry-run", action="store_true")

    domains_cmd = subparsers.add_parser("domains", help="Print normalized domains, one per line.")
    domains_cmd.add_argument("--domains-file", default="policy/default-ai-domains.json")
    domains_cmd.add_argument(
        "--allow-broad-wildcard",
        action="store_true",
        help="Allow explicitly blocked broad wildcard domains such as *.com.",
    )

    args = parser.parse_args(argv)

    try:
        if args.command == "domains":
            for domain in load_domains(args):
                print(domain)
            return 0

        if args.command == "validate":
            return handle_validate(args)

        if args.command == "snippet":
            domains = load_domains(args)
            name_error = connector_name_error(args.connector_name)
            tag_error = connector_tag_error(args.connector_tag)
            if name_error:
                raise PolicyError(name_error)
            if tag_error:
                raise PolicyError(tag_error)
            patch = build_patch(
                connector_name=args.connector_name,
                connector_tag=args.connector_tag,
                domains=domains,
                tag_owner=args.tag_owner,
                member_src=args.member_src,
            )
            print(dumps(patch), end="")
            return 0

        if args.command == "merge":
            return handle_merge(args)

        if args.command == "diff":
            return handle_diff(args)

        if args.command == "plan":
            return create_policy_plan(args)

        if args.command == "apply-plan":
            return apply_policy_plan(args)

        if args.command == "list-plans":
            return list_policy_plans(args)

        if args.command == "restore-plan":
            return restore_policy_plan(args)

        if args.command == "apply":
            return apply_removed(args)

        if args.command == "restore":
            restore_policy(args)
            return 0

    except PolicyError as exc:
        eprint(f"error: {redact_sensitive(str(exc))}")
        return 1

    parser.error(f"unknown command: {args.command}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
