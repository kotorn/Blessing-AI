# AGY Durable Queue

The AGY queue is a local, durable helper pipeline for repository work and
evidence generation:

```text
submit -> SQLite WAL queue -> worker -> AGY stream-json -> result store
       -> get/list/watch
```

AGY is not a Binance execution authority. `TRADING_ORDER` and emergency
control jobs are rejected at submission. Cloud writes remain behind a separate
authorization and verification boundary and are not claimed by the AGY
worker.

## Runtime storage

The default state directory is outside the repository:

```text
%LOCALAPPDATA%\agy-queue\blessing-ai\
```

SQLite is the default backend and is opened with WAL mode, full synchronous
durability, foreign keys, and a busy timeout. JSONL is available only when the
operator explicitly sets `AGY_QUEUE_BACKEND=jsonl`; it never replaces SQLite
silently. Neither backend is suitable for a network filesystem.

Results and raw event files are redacted before they are persisted. Prompts are
read from a file or standard input, never from a command-line argument, and
credential-like content is rejected before the job is written.

## CLI

Use a prompt file and an absolute repository path:

```text
python -m apps.agy_queue submit --prompt-file C:\path\prompt.txt --kind read_only --repo H:\Blessing AI
python -m apps.agy_queue get <job_id>
python -m apps.agy_queue list --status pending
python -m apps.agy_queue watch <job_id>
python -m apps.agy_queue retry <job_id>
python -m apps.agy_queue worker --wait
```

The submit command returns a durable identifier immediately. A job is not
considered successful from the process exit code alone: the worker requires a
terminal AGY `result` event, a successful result status, non-empty output, and
the applicable repository or verification evidence.

## Job gates

The queue keeps the operational state small:

```text
pending -> running -> succeeded | failed | cancelled
```

Authorization and verification are represented by `gate_state`, including
`WAITING_AUTHORIZATION` and `WAITING_VERIFICATION`; those jobs cannot be
claimed until their gate is ready. External-effect jobs must have an
authorization reference and, where applicable, a separate verification job.
Mainnet preflight is also held behind an authorization reference because it
uses protected account-read credentials; only the fixed trading/control-plane
path may execute it.

## AGY process policy

Before a worker starts, it verifies `agy --version` and `agy --help` against
the configured version. It launches AGY with an explicit working directory,
streaming NDJSON input/output, configured model and effort, and a policy-safe
permission mode. It does not assume a `--cwd` flag, combine `-p` with streaming
input, or use `--dangerously-skip-permissions`.

One persistent process is reused only for related jobs with the same session,
repository, branch/base SHA, scope, model, effort, and permission class. Jobs
outside that scope receive a fresh process. A heartbeat maintains the lease;
timeouts and protocol errors terminate the process, persist evidence, and use
only the bounded retry policy.

A repository-change job additionally needs a clean diff check and explicit
`tests_passed` evidence in its structured result. Mainnet preflight and other
release operations must be performed by the fixed release/control-plane path,
with operation and read-back verification recorded separately.
