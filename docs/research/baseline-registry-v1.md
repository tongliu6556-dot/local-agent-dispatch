# Baseline Registry v1 (WP0)

Provider-free baseline for the local-agent-dispatch research program. The
registry records **commands and a dated snapshot**, not permanent facts.

## 1. Snapshot rule

> A recorded count is a snapshot of one run on one machine on one date. It is
> not a permanent fact about the repository. Re-run the commands to re-derive
> the current state; never cite an old count as if it were the suite size.

The baseline is reproducible when the same commands on identical fixtures
produce the same aggregate twice. If any baseline command fails or depends on
credentials/network, repair isolation before any comparative study (Gate G0
stop condition).

## 2. Baseline commands (provider-free)

```bash
# Full provider-free test suite (no network, no provider, no SSH)
python3 -m unittest discover -s tests -p 'test_*.py'

# Focused research replay laboratory (WP9)
python3 -m unittest tests.test_research_replay -v

# Focused run-manifest contract (WP0)
python3 -m unittest tests.test_research_manifest -v

# Compile checks for production, scripts, and research modules
python3 -m compileall -q src scripts research

# Shell syntax checks
for script in scripts/*.sh; do bash -n "$script"; done

# Offline CLI surface (no provider calls)
python3 -m local_agent_dispatch.cli --version
python3 -m local_agent_dispatch.cli doctor --offline
python3 -m local_agent_dispatch.cli demo --offline

# Skill validation (repository skill definition)
python3 $HOME/.codex/skills/.system/skill-creator/scripts/quick_validate.py \
  $HOME/.codex/skills/local-agent-dispatch
```

## 3. Aggregate snapshot

Captured 2026-08-12 in the public Alpha staging tree on a Linux server,
Python 3.x. This is a dated snapshot, not a permanent suite-size claim;
determinism of the focused replay suites is asserted by tests, not claimed by
the snapshot.

| Check | Command | Outcome |
| --- | --- | --- |
| Provider-free suite | `unittest discover -s tests -p 'test_*.py'` | 487 tests, OK (19.7 s on Alpha staging server) |
| Replay laboratory | `unittest tests.test_research_replay` | 39 tests, OK |
| Run-manifest contract | `unittest tests.test_research_manifest` | 21 tests, OK |
| Compile | `compileall -q src scripts research` | exit 0 |
| Shell syntax | `bash -n scripts/*.sh` | all pass |
| CLI offline | `cli --version` / `doctor --offline` / `demo --offline` | covered by CI; run offline in this slice |

The suite aggregate is stored here only as a small dated table per the WP0
plan ("store only the small aggregate report").

## 4. Isolation requirements

- No baseline command may make a network, provider, or SSH call, download
  data, or spend quota.
- Fixtures are deterministic and in-repo (`research/scenarios/`,
  `research/fixtures/`, `research/corpus/`); replay never sleeps.
- Gate G0: clean provider-free baseline reproduced twice from identical
  fixtures; every result carries a manifest and an evidence level
  ([`protocol-v1.md`](protocol-v1.md) Section 7).
