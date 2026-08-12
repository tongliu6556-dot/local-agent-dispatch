# Provider-free durable worker contract

`scripts/remote_worker.py` is the small, local spool contract used to test the
continuity boundary without a provider, SSH session, network request, shell
command, or model prompt. It is deliberately a fake executor seam, not a
remote-service implementation.

## Lifecycle

The durable lifecycle is:

1. `prepare` validates a task packet and writes a redacted manifest plus a
   prepare-time artifact/hash baseline.
2. `claim_job` creates a fenced owner/token lease. `heartbeat` renews that
   lease; owner, token, and expiry are checked on every mutating operation.
3. `recover` marks an expired or crashed lease `recoverable` and clears the
   old fence. A new owner can then claim it.
4. An executor records final artifact hashes and calls `complete_job`. Success
   requires every declared artifact to be a non-empty regular file and to be
   new or hash-changed from the prepare baseline. Otherwise completion fails
   closed with a deterministic error code.
5. `resume_handoff` emits a bounded, prompt/argv-safe report containing packet
   identity, placement/write scope, artifact evidence, lease state, and an
   allow-listed event tail. A later controller can use `next_action` to decide
   whether to wait, recover/claim, or review a terminal result.

`recover_and_handoff` (CLI: `resume`/`recover-handoff`) combines lease
reconciliation and handoff generation under one spool lock. This is the
reconnect boundary for a controller returning after Codex quota loss or a
short-lived SSH/chat disconnect: an expired lease is marked `recoverable`,
the handoff records `recovery.performed` and its reason, and the caller gets a
single claim/review action without a race between separate `recover` and
`handoff` calls.

The manifest and event log are written atomically under a spool lock. Owner
hashes are exposed instead of owner IDs, lease tokens are never returned by a
handoff, and raw packet prompt/argv fields are never copied into events or the
handoff artifact.

## CI/fake execution seam

`fake-execute` (alias `fake-run`) is intentionally bounded: it writes only the
declared artifact paths with a small fixture string, records SHA-256 hashes,
and finalizes through the same lease and freshness gate. It does not interpret
`attempts`, invoke a subprocess, contact a provider, or open SSH. This makes it
safe for unit tests and recovery demonstrations while preserving the contract
that a future server worker must satisfy.

```bash
python3 scripts/remote_worker.py prepare \
  --packet packet.json --project-root /trusted/project --spool /tmp/lad-spool
python3 scripts/remote_worker.py fake-execute \
  --spool /tmp/lad-spool --job-id example-job --owner ci-fake
python3 scripts/remote_worker.py handoff \
  --spool /tmp/lad-spool --job-id example-job --output handoff.json

# On reconnect, reconcile an expired lease and emit the handoff atomically.
python3 scripts/remote_worker.py resume \
  --spool /tmp/lad-spool --job-id example-job --output handoff.json
```

The output is evidence for the local control-plane tests only. It must not be
reported as proof that a provider, remote host, GPU runtime, or bulk data route
is available. Real remote execution remains behind explicit server-first
capacity/route checks and an authorized adapter.

### Independent fake-service smoke

`fake-service` is a process-level continuation smoke for service managers and
chat-loss recovery tests. It accepts one explicit `job_id`, waits for that
prepared/recoverable manifest, reconciles an expired lease, and invokes only
the deterministic `fake-execute` fixture before returning a safe handoff:

```bash
python3 scripts/remote_worker.py fake-service \
  --spool /var/lib/local-agent-dispatch/spool \
  --job-id approved-fixture --owner lad-service \
  --poll-seconds 1 --max-idle-rounds 0
```

This command is intentionally provider-free and is not a model fallback. A
production service must replace it with a separately reviewed adapter while
preserving the same lease, artifact-freshness, validation, and handoff gates.
The SSH client exposes the same operation as an explicit, dry-run-by-default
transport for fake-SSH CI; use a service manager or a durable remote supervisor
for real detached execution rather than assuming an interactive SSH session
survives chat termination.

## SSH transport seam

`scripts/remote_worker_client.py` is the bounded transport adapter for an
already-verified private inventory. It accepts only `transport=ssh` hosts with
an explicit port, user, worker script, project path, and spool path. Remote
paths are absolute and use a conservative shell-safe character set. The
client builds an argv list with `shell=False`; it never concatenates a remote
shell command or accepts arbitrary SSH options.

All client operations are dry-run by default. `--execute` is an explicit gate
for `prepare`, `status`, `recover`, `handoff`, `resume`, or `fake-execute`.
`prepare`
passes the redacted packet to `remote_worker.py prepare --packet -` through SSH
stdin. The other operations use an empty stdin stream and retrieve only the
worker's JSON result. Stderr is represented by a byte count and SHA-256 digest,
not copied into local logs. Prompt text, raw provider argv, and credentials are
not returned in client reports.

The client maps known local absolute packet paths (workspace, artifact,
prompt/result and runtime paths) into the selected host's declared
`project_path`; absolute paths outside the captured local workspace are
rejected. This keeps a Mac `/Users/...` path from entering a server manifest
and avoids treating a remote path as a local file. The client preserves
`execution_host` and `workload_host` in its placement evidence. A split
placement must carry a declared workload wrapper; this seam records that
declaration but never executes it. A fake SSH binary can therefore exercise
`prepare -> fake-execute -> resume` in CI without a network, provider,
download, or remote shell.

## Controller SSH runtime boundary

The SQLite controller also supports an explicitly prepared `server_openai`
attempt over SSH when the inventory host exposes a loopback OpenAI-compatible
runtime. The prompt is read from the controller workspace, the short request
script is sent over the authenticated SSH stdin, and the remote runtime writes
the declared result artifact under `remote_workspace`. Artifact observation and
validation both use the SSH host; a local absolute validator path is rejected
by the packet bridge. This is an authorized, bounded runtime path, not a
generic shell or public endpoint, and completion still requires a fresh,
hashable artifact plus a successful remote validator.

## Server-side OpenCode Go boundary

`remote_opencode_client.py` is the explicit transport for a child agent that
must run on the server rather than on the local Mac. Its inventory entry names
the remote project root, `opencode_remote_run.py`, and the already-installed
OpenCode binary. The client never transfers `auth.json`; the operator must run
`opencode auth login` once under the server-side `HOME` and verify the provider
there. A request is dry-run by default. With `--execute`, the prompt bytes are
written to the SSH stdin stream, while model, variant, cwd, result path, and
timeout remain fixed argv fields. The wrapper returns only a redacted JSON
status and result SHA-256/size. This keeps model context, child-process memory,
and full output on the server while the local controller retains leases,
quota policy, and human approval.
