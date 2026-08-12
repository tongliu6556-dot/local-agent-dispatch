# Research

Provider-free research laboratory for local-agent-dispatch. Everything in
this directory runs without a provider, without the network, and without
sleep; it falsifies scheduler and continuity policies inside a controlled
world before any shadow or canary run.

## Layout

| Path | Purpose |
| --- | --- |
| `corpus/missions.jsonl` | seeded missions (S0-S3, includes fail-closed reject cases) |
| `corpus/task-labels.jsonl` | task labels per `benchmark-taxonomy-v1.md` Section 2 |
| `scenarios/quota-windows.json` | quota window scenarios and job corpus for replay |
| `scenarios/resource-topologies.json` | host/mount/route topologies |
| `simulator/fake_clock.py` | deterministic clock and quota windows |
| `simulator/fake_cluster.py` | deterministic replay cluster + fault injection |
| `replay/run_manifest.py` | provider-free run-manifest validator (WP0, stdlib only) |
| `replay/import_legacy_runs.py` | legacy JSON importer for the provenance ledger |
| `replay/materialize_observations.py` | estimator observation gate |
| `analysis/paired_report.py` | paired statistics with censored survival handling |
| `fixtures/quota_console.json` | sanitized quota console fixture |

## Evidence ceilings

Simulator and replay output may claim **E0/E1 only**
(`evidence_ceiling: provider_free_replay_only`). Numeric values from replay
are mechanism evidence under modeled inputs; they are never physics,
provider billing, or real latency/cost. See
[`docs/research/protocol-v1.md`](../docs/research/protocol-v1.md) and
[`docs/research/promotion-checklist-v1.md`](../docs/research/promotion-checklist-v1.md).

## Run manifest

Every research result must carry a machine-readable manifest (policy digest,
seed, fixture digest, start commit or source digest, evidence level,
validator identity, result digest). Validation fails closed; missing manifest,
validator, or evidence rejects the result:

```bash
python3 -m unittest tests.test_research_manifest -v
```

## Reproducing the laboratory

```bash
python3 -m unittest tests.test_research_replay -v
python3 -m compileall -q research
```

## Golden mission

The FEM/MPB/PWE mission is the compile-only golden example (adapter DAG,
claim envelope, no physics). This repository never implements physics and
never claims continuous Maxwell, full-BZ, localizer, or Chern results from a
planning trial.
