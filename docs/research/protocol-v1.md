# Research Protocol v1 (WP0)

Frozen provider-free research protocol for the local-agent-dispatch research
program. Normative source: Sections 1-9, 14, and 17 of
[`docs/superpowers/plans/2026-08-12-local-agent-dispatch-research-program.md`](../superpowers/plans/2026-08-12-local-agent-dispatch-research-program.md).

## 1. Status

- **Version:** v1, frozen at WP0.
- **Change process:** amendments are recorded in `docs/research/decision-log.md`
  (created by a later work package); a frozen threshold may not be moved after
  results are seen.

## 2. Thesis and validated progress

The research target is the complete control loop, not a single model router:

```text
Observe -> Compile -> Plan -> Reserve -> Execute
        -> Validate -> Review -> Learn -> Replan
```

**Validated progress** means an artifact accepted by a deterministic validator,
test suite, numerical invariant, bounded independent review, or explicit human
evidence gate. An agent response, running PID, catalog entry, or nonempty file
is not sufficient completion evidence.

## 3. Evidence ladder

| Level | Evidence | Permitted claim |
| --- | --- | --- |
| E0 | schema/design review | coherent specification only |
| E1 | provider-free fixture/replay | control-plane behavior under modeled inputs |
| E2 | bounded real provider or SSH canary | observed behavior for that exact run and environment |
| E3 | repeated fault injection and 24-hour soak | bounded reliability evidence for tested scenarios |
| E4 | clean-clone, cross-platform, public reproducibility | public alpha engineering evidence |

Rules:

- No E1 result may be described as real-provider, real-GPU, scientific, or
  production evidence.
- No model ranking may be inferred from old runs unless task, model, effort,
  quota attribution, validation, and environment are comparable.
- Every result artifact carries a machine-readable run manifest with an
  evidence level (Section 7); a result without one fails closed.

## 4. Research questions (preregistered)

| ID | Research question | Primary success gate |
| --- | --- | --- |
| RQ0 | Can the system be measured reliably? | 100% irreversible actions traceable; >=99% attempts have a terminal or legitimate censored state |
| RQ1 | Can mission language be compiled safely? | 100% hard-constraint recall on the frozen corpus; zero silent side-effect or claim-boundary expansion |
| RQ2 | Can resource demand be estimated? | P90 coverage 85-95%; catastrophic underestimation <1%; reservation waste 20% below static baseline |
| RQ3 | Does joint placement help? | >=15% lower median makespan; zero quota violations; no increase in catastrophic resource failure |
| RQ4 | Does dynamic model routing save quota? | Success-rate non-inferiority margin -3 percentage points; >=20% quota reduction |
| RQ5 | Does dynamic concurrency help? | >=20% throughput improvement; failure non-inferiority +2 points; zero unrecoverable write conflict |
| RQ6 | Does CPS compilation help? | >=25% context/token reduction; success non-inferiority -3 points; fewer unauthorized tool attempts |
| RQ7 | Can the system run through interruption? | Zero accepted-task loss and duplicate irreversible effects; recovery within two lease periods plus one scan |
| RQ8 | Can human attention be reduced safely? | >=30% fewer interruptions or review minutes; critical-error discovery non-inferiority -2 points |
| RQ9 | Can online calibration improve future plans? | Improvement on a frozen future-window set; zero policy escape; shadow before canary |

If a confidence interval crosses a non-inferiority boundary, quota savings come
only from lower quality, or utilization gains materially increase failures,
the corresponding hypothesis fails.

## 5. Measurement rules

Report components separately even if a scalar objective is used internally:

```text
Validated Utility
  = accepted task value
  - rework penalty
  - deadline penalty
  - quota and cost penalty
  - human-attention penalty
  - high-risk policy violation penalty
```

Separate claim families:

- **Quality:** validated success, first-pass success, compile/test/numerical
  results, artifact freshness and hash integrity, reviewer disagreement,
  claim-boundary or permission violations.
- **Cost/quota:** exact pool/model/effort when attributable; before/after
  window values with source and TTL; tokens/USD when exposed; attribution
  class `exclusive|confounded|unknown`; strong-model escalation rate.
- **Time/scheduling:** queue wait, time to validated artifact, critical-path
  makespan, deadline hit rate, starvation, decision latency, replan count.
- **Resources:** P50/P90 pinball loss and P90 coverage; CPU/RAM/GPU/VRAM;
  bytes by data class; mount/path accuracy; OOM/ENOSPC/route/runtime-fit
  failures; reservation waste.
- **Reliability:** accepted-task loss, orphan/stale duration, duplicates,
  recovery time, retry count, quarantine, transfer integrity.
- **Human:** review minutes, interruption count, decision latency, items
  displayed before decision, correction/reversal count, one-minute
  comprehension check.

Unknown quota or cost is never converted into a fabricated number; it is
reported `unknown`.

## 6. Promotion tiers and failure taxonomy

- Promotion tiers, gates, and standing prohibitions:
  [`promotion-checklist-v1.md`](promotion-checklist-v1.md).
- Fault matrix and per-fault replay tests:
  [`fault-injection-matrix-v1.md`](fault-injection-matrix-v1.md).
- Automatic execution stops (short form): unknown/unverified route for
  sensitive or bulk data; unauthorized provider/host/data location; predicted
  hard-limit breach; write-scope conflict or stale reservation; duplicate
  irreversible action; missing validator or artifact manifest; unclassified
  repeated failure; attempted scientific claim expansion; authority-lacking
  submit/merge/publish/delete/deploy/credential operation; incomplete
  telemetry.
- Research stops (short form): event completeness below G0/G1; primary
  interval in the harm region; quota savings explained by lower accepted
  quality; privacy leak or unauthorized side effect; provider/model/policy
  drift making groups incomparable; preregistered maximum sample reached
  without separation; simulator result not replicated where promotion
  requires it.

## 7. Run manifest contract

Every research result must carry a machine-readable manifest with:

| Field | Meaning |
| --- | --- |
| `schema_version` | manifest schema version (`1`) |
| `policy_digest` | sha256 of the canonical policy definition |
| `seed` | integer seed of the run |
| `fixture_digest` | sha256 of the canonical source/fixture payload |
| `start_commit` or `source_digest` | start commit, or sha256 of the source tree (at least one) |
| `evidence_level` | one of `E0|E1|E2|E3|E4` |
| `validator_id` | identity of the validator that must accept the result |
| `result_digest` | sha256 of the canonical result payload |

Fail-closed rules:

- A result without a manifest, without a validator identity, or without an
  evidence level is rejected before use.
- Unknown schema version, unknown evidence level, malformed digests, or a
  fixture/result digest mismatch reject the result.
- Missing or unknown values are never fabricated.

Implementation: `research/replay/run_manifest.py` (stdlib only); contract
tests: `tests/test_research_manifest.py`. Replay/simulator artifacts may only
claim `E0` or `E1` (`evidence_ceiling: provider_free_replay_only`).

## 8. Golden mission (FEM/MPB/PWE)

The FEM/MPB/PWE mission is the golden compile-only example:

```text
S0  system/provider/host/mount/route preflight
  |
  +--> S1a FEM output adapter      --+
  +--> S1b MPB output adapter      --+--> S2 interface/schema integration
  +--> S1c PWE output adapter      --+        |
  +--> S1d LDL/inertia prototype   --+        +--> S3 localizer index
                                            |
                                            +--> S4 deterministic tests + numerical checks
                                                     |
                                                     +--> S5 independent review
                                                              |
                                                              +--> S6 human commit gate
```

S1a-S1d run in parallel only with disjoint write scopes. Every node carries
`input_digest`, `output_paths`, `write_scope`, `validator`, `model/pool`,
`execution_host`, `workload_host`, `resource_estimate`, and `claim_effect`.
This WP0 slice implements **no physics**: no continuous Maxwell, full-BZ,
localizer, or Chern claims, and no scientific execution flags.

## 9. Gate G0

- Clean provider-free baseline is reproducible twice from identical fixtures
  (see [`baseline-registry-v1.md`](baseline-registry-v1.md)).
- Every result has a manifest and an evidence level (Section 7).
- If baseline state cannot be reproduced or existing tests depend on
  credentials/network, repair isolation before any comparative study.
