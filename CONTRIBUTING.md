# Contributing

Thanks for your interest in improving the Tailscale AI Egress Connector toolkit.
This project favors stability and trust over feature count, so contributions are
reviewed with that bias.

## Project scope

In scope:

- Reliability, safety, and clarity of the existing command surface.
- Diagnostics accuracy and helpful error messages.
- Documentation, provider notes, and the validation matrix.
- Bug fixes and test coverage.

Out of scope for 1.x (see [docs/Stability.md](docs/Stability.md)):

- A GUI, provider API provisioning, or automatic VPS purchasing.
- Telemetry or any project-operated servers.
- Additional proxy protocols (Shadowsocks, sing-box, Xray, etc.).
- Breaking changes to the frozen CLI surface or the plan manifest schema major
  version.

If you want one of the out-of-scope items, please open an issue to discuss it
first rather than sending a large pull request.

## Project layout

```text
.
├── .github/
│   ├── ISSUE_TEMPLATE/
│   │   ├── bug_report.yml
│   │   ├── config.yml
│   │   ├── feature_request.yml
│   │   └── question.yml
│   ├── PULL_REQUEST_TEMPLATE.md
│   └── workflows/
│       ├── ci.yml
│       └── release.yml
├── VERSION
├── LICENSE
├── CHANGELOG.md
├── CONTRIBUTING.md
├── SUPPORT.md
├── SECURITY.md
├── PRIVACY.md
├── pyproject.toml
├── README.md
├── README.zh-HK.md
├── bootstrap.sh
├── check-client-routes.sh
├── diagnose.sh
├── disable-exit-node.sh
├── enable-exit-node.sh
├── install.sh
├── restore-connector.sh
├── rollback.sh
├── policy/
│   ├── app-connector.example.json
│   └── default-ai-domains.json
├── scripts/
│   ├── check_docs_links.py
│   ├── maintainer/
│   │   └── apply-github-ruleset.sh
│   ├── package.sh
│   ├── policy_tool.py
│   ├── smoke-test.sh
│   └── validation-e2e.sh
├── tests/
│   ├── test_maintainer_scripts.py
│   ├── test_policy_tool.py
│   ├── test_release_metadata.py
│   └── test_shell_scripts.py
├── docs/
│   ├── AWS-Lightsail.md
│   ├── Configuration.md
│   ├── Failover.md
│   ├── Generic-VPS.md
│   ├── Home-Mac-Exit-Node.md
│   ├── Release-Checklist.md
│   ├── Stability.md
│   ├── Tailscale-API-mode.md
│   ├── Troubleshooting.md
│   ├── Uninstall.md
│   ├── Validation-Matrix.md
│   ├── WebARENA.md
│   └── maintainers/
│       └── Branch-Protection.md
└── generated/
```

## Development setup

No build step and no third-party Python packages are required at runtime. For
the full local QA suite, install the same tools CI uses. You need:

- `bash`, `python3` (3.9+), and `shellcheck`.
- `ruff`, `mypy`, and `coverage[toml]` for lint, type, and coverage checks.
- Optional: a POSIX environment for running the shell scripts.

```bash
git clone https://github.com/F-e-u-e-r/tailscale-ai-egress.git
cd tailscale-ai-egress
python3 -m pip install ruff==0.9.10 mypy==1.15.0 'coverage[toml]==7.6.10'
```

## Running checks locally

Run the same checks CI runs before opening a pull request:

```bash
# 1. Shell syntax
for f in bootstrap.sh check-client-routes.sh diagnose.sh disable-exit-node.sh \
         enable-exit-node.sh install.sh restore-connector.sh rollback.sh \
         scripts/smoke-test.sh scripts/package.sh scripts/validation-e2e.sh; do
  bash -n "$f"
done

# 2. Shell lint
shellcheck bootstrap.sh check-client-routes.sh diagnose.sh disable-exit-node.sh \
  enable-exit-node.sh install.sh restore-connector.sh rollback.sh \
  scripts/smoke-test.sh scripts/package.sh scripts/validation-e2e.sh

# 3. Python lint/type checks
ruff check scripts tests
mypy scripts

# 4. Python unit tests and coverage gate
python3 -B -m unittest discover -s tests
python3 -m coverage run -m unittest discover -s tests
python3 -m coverage report --fail-under=70

# 5. Credential-free smoke test (local + dry-run paths only)
./scripts/smoke-test.sh

# 6. Offline docs link check + bilingual README entrypoint parity (if docs/READMEs changed)
python3 scripts/check_docs_links.py
python3 scripts/check_readme_parity.py

# 7. Packaging check
./scripts/package.sh --check
```

## Coding conventions

- **Shell:** target Bash but stay compatible with Bash 3.2 (the default macOS
  Bash). Keep `set -euo pipefail` (or the existing `set -uo pipefail` for the
  diagnostics scripts), guard empty-array expansions under `set -u`, and keep
  each entrypoint self-contained so it can be downloaded and run standalone.
  Every entrypoint must keep a stable `--help` and a `--version`.
- **Python:** standard library only. The policy tool must run on a fresh VPS
  without `pip install`. Match the existing style (type hints, `PolicyError`
  for user-facing failures, secret redaction on all output).
- **Secrets:** never print tokens or auth keys. Route any new output that might
  contain them through `redact_sensitive`.
- **Tests:** add or update tests under `tests/` for any behavior change. Shell
  behavior is tested with fake commands on `PATH`; the API is tested with mocks.

## Versioning and changelog

- Bump the version in [`VERSION`](VERSION) and keep `__version__` in
  `scripts/policy_tool.py` identical (a unit test enforces this).
- Add an entry under `## [Unreleased]` in [`CHANGELOG.md`](CHANGELOG.md).
- Follow [Semantic Versioning](https://semver.org). Within 1.x, do not break the
  frozen surface listed in [docs/Stability.md](docs/Stability.md).

## Pull requests

- Keep changes focused; one logical change per PR.
- Describe the motivation and how you tested it. The PR template will prompt you.
- Confirm all local checks above pass.
- By contributing, you agree that your contributions are licensed under the
  project's [MIT License](LICENSE).

## Reporting security issues

Do not open a public issue for a vulnerability. Follow the process in
[SECURITY.md](SECURITY.md).
