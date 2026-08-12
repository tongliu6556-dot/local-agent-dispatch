# Public release checklist

This checklist records the current evidence for a public GitHub alpha of
local-agent-dispatch. It is deliberately separate from the product roadmap:
green local tests do not prove provider availability, remote-runtime readiness,
or a safe public release.

Use it from a clean checkout. Do not run provider prompts, remote jobs, model
downloads, or bulk transfers as part of this checklist.

## Verified provider-free commands

The following commands were run successfully on 2026-08-12 in the working
tree. Repeat them after rebasing or changing packaging files:

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests -p 'test_*.py'
python3 -m compileall -q src scripts
for script in scripts/*.sh; do bash -n "$script"; done
PYTHONPATH=src python3 -m local_agent_dispatch.cli --version
PYTHONPATH=src python3 -m local_agent_dispatch.cli doctor --offline
PYTHONPATH=src python3 -m local_agent_dispatch.cli demo --offline
```

The expected evidence is: all 487 provider-free unit tests pass in the current
Alpha snapshot; Python and shell syntax checks
pass; `lad --version` reports the package version; and the two offline commands
report `local-only` evidence without contacting a provider or sending a model
prompt.

The wheel smoke gate is also provider-free:

```bash
wheel_dir=$(mktemp -d)
install_dir=$(mktemp -d)
python3 -m pip wheel --no-deps --no-build-isolation -w "$wheel_dir" .
python3 -m pip install --no-deps --target "$install_dir" "$wheel_dir"/*.whl
PYTHONPATH="$install_dir" python3 -m local_agent_dispatch.cli --version
PYTHONPATH="$install_dir" python3 -m local_agent_dispatch.cli doctor --offline
```

The GitHub workflow additionally enumerates every public top-level document,
script, schema, architecture document, reference, and template and fails when
one is missing from the wheel's data files.

## CI and operating-system matrix

| Area | Current evidence | Release interpretation |
| --- | --- | --- |
| Linux | GitHub Actions `ubuntu-latest`, Python 3.10, 3.11, 3.12, and 3.13; unit, compile, shell, offline CLI, and wheel-data checks | CI-verified |
| macOS | Local scanner and path logic have platform branches; no hosted macOS job yet | Compatibility intent, not CI proof |
| Windows | Local scanner has Windows probes; no hosted Windows job yet | Compatibility intent, not CI proof |
| Providers | No real provider call in CI | Catalog/runtime/quota readiness remains unverified |
| SSH/servers | No remote probe or workload in CI | Host capacity, route, and durable worker remain unverified |
| GPU/model runtimes | No model download or GPU workload in CI | Runtime fit must be established by an explicitly authorized server smoke |

The package declares Python `>=3.10`. Adding macOS or Windows jobs is a
follow-up portability gate, not something that can be inferred from the Linux
matrix.

## Packaging inventory

`pyproject.toml` must ship the following source-controlled groups in a
non-editable wheel:

- the CLI package under `src/`;
- public top-level documents: `README.md`, `SKILL.md`, `CONTRIBUTING.md`,
  `SECURITY.md`, and `CHANGELOG.md`;
- every `scripts/*.py` and `scripts/*.sh` helper;
- every `schemas/*.json` contract;
- every `docs/*.md`, `references/*.md`, and `templates/*.md` document.

The CI wheel-data gate checks this inventory. `AGENTS.md`, fixtures, generated
build metadata, caches, runtime state, logs, task packets, artifacts, host
inventories, and credentials are intentionally not package data.

## Release blockers and open gates

The following items are not complete and must be resolved or explicitly
accepted before calling the repository a public release:

| Gate | Current status | Required action |
| --- | --- | --- |
| License | **Completed for Alpha** | Apache-2.0 is included in `LICENSE`; review contributor copyright headers before later releases |
| Git repository | **Alpha publication step** | Publish only a clean public snapshot; never push the historical private working-tree commits |
| Secret scan | **Not configured** | Run a repository secret scanner and manually review historical/runtime files; revoke any exposed credential before publication |
| Public endpoint/path scrub | **Required** | Audit scripts, references, fixtures, and generated metadata for private endpoints, machine-specific paths, account data, prompts, and tokens; keep only sanitized examples |
| Packaging | **Locally/CI covered, release pending** | Re-run the wheel-data gate from a clean checkout and inspect the final artifact before publishing |
| CI branch protection | **Pending repository setup** | Enable required CI checks and review permissions after the GitHub repository exists |
| Dependency/provenance review | **Pending release review** | Review build-system pins, generated files, and third-party actions before tagging |
| Cross-platform CI | **Linux only** | Add or explicitly defer hosted macOS and Windows jobs; do not claim full OS support from the Linux matrix |
| Provider/server E2E | **Explicitly outside CI** | Run only with authorization, recorded host/resource evidence, and the server-first/bulk-transfer gates |

The SQLite-to-monitor read-only boundary is covered by
`scripts/controller_monitor_adapter.py` and `lad monitor-state`; it does not
replace the provider/server E2E gate or claim runtime telemetry when a PID/log
breadcrumb was not explicitly recorded.

## Final handoff evidence

Before publication, retain the exact commit/tag, wheel filename and SHA-256,
test output, CI run URL, secret-scan report, and the approved license decision.
Do not place runtime snapshots, credentials, private host inventories, or
unredacted logs in the repository or release assets.
