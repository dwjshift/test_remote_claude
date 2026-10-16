# Remote Claude Mailbox Usage

This project uses a Git branch as an asynchronous transport between a client and an unattended Claude Code server. Both sides can run on the same Linux machine with independent state directories. Neither side needs an inbound network listener.

For an EC2 instance with an existing Amazon Bedrock Claude installation, follow [EC2_SETUP.md](EC2_SETUP.md). It covers provider-specific compatibility work, real GitHub branch tests, verified client model choices, and persistent service installation. The local claude.ai authentication examples below are not a substitute for that Bedrock setup. No EC2 instance or Bedrock model has been certified by this document yet; the target agent must add a **Verified EC2 Deployment** section after testing the actual machine.

## Requirements and Installation

- Python 3.11 or newer; validated here with Python 3.13.5.
- Git and outbound access to the selected repository. SSH authentication must already work without a password prompt.
- Linux with `/proc` for server process identity checks and process-group cleanup. The current implementation uses POSIX file locks on both sides.
- Claude Code **2.1.300** on the server, with an existing authenticated account.
- Repository writers must be trusted to run commands as the server account. Prefer a private repository for private prompts and results.

Clone the project, then install from its root:

```bash
git clone git@github.com:carl-qi/prompt-tooling.git
cd prompt-tooling
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[test]'
remote-claude-client --help
remote-claude-server --help
```

Pillow validates images. Pytest is only needed for tests; use `pip install -e .` for a runtime-only installation. `python -m client` and `python -m server` are equivalent to the installed console commands.

Install and verify the pinned Claude release:

```bash
DISABLE_AUTOUPDATER=1 claude install 2.1.300
export PATH="$HOME/.local/bin:$PATH"
hash -r
claude --version
claude auth status --json
git ls-remote git@github.com:carl-qi/prompt-tooling.git
```

If authentication is missing, run `claude auth login` manually before starting the server. Resolve SSH host-key or credential prompts interactively during setup. The service never performs interactive login.

On the implementation machine, the default command now points to 2.1.300, its native executable is `~/.local/share/claude/versions/2.1.300`, and the previous 2.1.112 executable remains at `/usr/local/bin/claude`. An additional backup exists at `.runtime/claude-2.1.112.backup`. The old release does not satisfy this server's required CLI flags.

The service verifies the configured version and required flags at startup, resolves executable symlinks once, and disables automatic CLI updates in workers. Retest before changing the pinned version.

## Server Usage

### Configuration and Mailbox Initialization

Copy [examples/server.json](examples/server.json), then review its repository URL and adjust any machine-specific paths. The examples use `carl-qi/prompt-tooling`; change the remote on both sides if using a different mailbox repository.

```bash
mkdir -p .runtime
cp examples/server.json .runtime/server.json
```

Use the same remote and branch on both sides, but different `state_dir` values. A relative state directory or local Git remote is resolved relative to its configuration file. Tilde paths in configuration are expanded locally by the process reading that configuration.

Initialize the orphan data branch once using the client:

```bash
remote-claude-client --config .runtime/client.json init \
  --remote git@github.com:carl-qi/prompt-tooling.git \
  --branch mailbox \
  --state-dir "$HOME/.local/state/remote-claude/client"
```

This creates only the data branch and its protocol marker. It does not copy code from `main`. The transport rejects `main`, `master`, branches without the mailbox marker, remote symlinks, submodules, and Git attribute files. A previously initialized cache refuses to silently recreate a deleted remote branch.

Run **exactly one server with one persistent state directory per mailbox branch**. The local singleton lock prevents two processes sharing a state directory; it is not a distributed lock between different state directories or machines.

| Server field | Default | Meaning |
| --- | --- | --- |
| `remote` | Required | Git SSH/HTTPS URL or local bare repository path |
| `branch` | `mailbox` | Dedicated data branch |
| `state_dir` | `state` | Private, persistent local state; keep separate from workspaces |
| `claude_executable` | `claude` | Executable path; the example pins the native 2.1.300 file |
| `claude_version` | `2.1.300` | Required version, checked with required CLI flags |
| `max_workers` | `10` | Maximum concurrent turns; excess work waits |
| `poll_interval` | `60` | Idle polling interval and upper bound for adaptive intervals; fixed interval when adaptation is disabled |
| `adaptive_polling` | `true` | Adjust the central polling interval according to activity |
| `active_poll_interval` | `10` | Polling interval while work is queued/running or interaction occurred less than five minutes ago |
| `warm_poll_interval` | `30` | Polling interval when the latest interaction was five to ten minutes ago |
| `heartbeat_interval` | `300` | Seconds between server heartbeat snapshots |
| `idle_ttl_seconds` | `86400` | Idle time before a conversation becomes dormant |
| `retry_interval` | `1` | Initial publication retry delay; exponential backoff caps at 300 seconds |
| `git_timeout` | `30` | Per-Git-command timeout in seconds |
| `max_budget_usd` | Unset | Optional Claude `--max-budget-usd` limit; useful for smoke tests |
| `image_budget_bytes` | `524288000` | Lifetime image budget, including pending publications |
| `artifact_limits` | See below | Per-turn export limits |

Model, effort, workspace, and turn timeout are immutable **session** settings supplied by `client new`, not server defaults. Change those settings by creating another session. Restart the server to apply changes to server configuration.

### Preflight, Start, and Stop

```bash
remote-claude-server --config .runtime/server.json doctor
remote-claude-server --config .runtime/server.json serve
```

`doctor` checks the exact CLI version and flags, authenticated status, Linux prerequisites, and mailbox access. It does not make a model call. `serve` repeats the version, flag, and authentication checks before opening the execution ledger or accepting requests.

Keep the server in the foreground for local use. Press **Ctrl+C**, or send **SIGTERM** to the server PID, to stop normally. Active turns are cancelled and their process groups are cleaned up; results are persisted and publication is attempted. Queued requests and unpublished results remain in SQLite for the next start.

One central poller serves every session. It synchronizes immediately at startup and then uses elapsed monotonic time, without aligning requests to wall-clock minute boundaries. With the defaults, queued/running work or interaction within five minutes selects 10 seconds; interaction five to ten minutes ago selects 30 seconds; longer inactivity selects 60 seconds. New requests, new controls, and task completion count as interaction. Duplicate messages, heartbeats, and ordinary status publications do not. Recent activity survives a server restart.

Adaptive intervals are capped at `poll_interval`. Set `adaptive_polling` to `false` for a fixed interval. Adaptive intervals must be positive; zero is supported only in fixed mode for offline tests. Network failures use exponential backoff independently of activity, and a task completing during a synchronization outage does not override that backoff. Result publication does not wait for the next scheduled input poll. Client `watch` still polls every ten seconds by default.

The server reads changed settings, requests, and controls from Git commit differences. An unchanged HEAD skips message discovery and repeat full-tree validation; commits containing only responses, events, attachments, or heartbeats do not cause historical input JSON to be parsed. A durable SQLite ingestion checkpoint advances only after the input batch is handled. Publisher fetches and pushes never advance that checkpoint, so requests arriving during publication remain discoverable. Missing checkpoints, unavailable Git objects, or rewritten history trigger a safe full scan with request deduplication.

Each newly opened Git cache performs complete tree validation. Subsequent changes retain path, file-type, and mailbox-marker checks before checkout. Checkout and disposable-cache cleanup still run, so Git index and filesystem work can depend on repository size even when application-level history scanning is eliminated. Short-lived client commands open a cache each time and retain the initial validation cost.

Workers never push. One publisher commits final Markdown, metadata, and all accepted attachments together. Push conflicts are fetched and retried without force-pushing. Network failures leave durable results waiting for publication. Scheduling reads indexed queued/running/unknown records, and dormancy is checked once per minute. Existing ledgers are upgraded automatically, including a transactional image-budget counter and recovery of interrupted rejection results; retain the same state directory during an upgrade.

### Workspaces and Unattended Execution

Create the intended project directory on the **server** first. For example, `--workspace '~/projects/example'` selects the server account's existing `projects/example` directory. Quote the argument so the client shell does not expand `~` on the wrong machine.

The path must resolve to an existing directory inside the server account's home. Paths using `..` or symlinks are rejected if their resolved destination escapes home; symlinks to directories within home are allowed. The server records the resolved absolute path and checks it again before later turns. A workspace cannot contain, or be inside, the server state directory.

This is the actual subprocess `cwd`: Claude operates on the original project files. It is not a copy, a temporary sandbox, or a separate filesystem. The home check constrains the starting directory, not every file a tool can access. Workspaces that are equal or nested cannot execute simultaneously. Independent workspaces can run in parallel.

Each prompt starts a fresh CLI process. The first turn uses a persisted Claude session UUID; subsequent turns explicitly use `--resume` with that UUID. Native Claude history supplies the context. No process remains alive merely to wait for another prompt. Dormancy preserves the session UUID, revision, and files.

Workers use `-p`, `stream-json`, `auto`, and `--permission-prompts none` without a terminal. Auto mode can deny operations; it does not grant every permission. A denial is reported as a failed turn with diagnostic summaries. A plain-language follow-up question is returned as the answer to that turn. A successful CLI turn does not imply that the user's overall task is complete.

The initial implementation enables the built-in Read, Write, Edit, Glob, Grep, Bash, WebFetch, and WebSearch tools and project instructions. User/project settings discovery, custom hooks, plugins, MCP servers, slash-command discovery, and browser integration are disabled for these workers. Process groups handle ordinary child cleanup; deliberately detached processes require stronger isolation than this implementation provides.

### Local State and Logs

Within the server `state_dir`:

```text
ledger.sqlite3               Durable requests, revisions, image ledger, and outbox
server.lock                 Single-instance lock
server.log                  Rotating operational log from the serve command
mailbox/                    Disposable Git cache
runs/<request-id>/
  stdout.jsonl              Structured native events, with .1 and .2 rotations
  stderr.log                Native stderr, with .1 and .2 rotations
  answer.full.md            Full parsed final answer, before publication truncation
  session-initialized.json  Durable evidence that the native session exists
  exports/                  Explicit per-turn image export directory
```

Each native event/stderr log rotates at 2 MiB with two backups. The daemon log has three backups. Oversized or malformed event streams are diagnosed; a missing trustworthy result can freeze the turn as `unknown`. Raw logs and full answers stay local. Native Claude session histories are also stored by Claude under its own user data directory.

Preserve project workspaces, the server state directory, and native Claude histories together when moving or backing up a server. The Git mailbox does not contain the project files or large datasets. Back up SQLite consistently, preferably after stopping the server. If the ledger is lost, a fresh server refuses to adopt an existing executed mailbox; restore the original state or use a new mailbox branch. Deleting cached Git files does not recover lost execution history.

### Interrupted Execution and Recovery

After a crash during execution, the request becomes `unknown`. Its session and overlapping workspaces remain frozen, and the revision does not advance. The server does not automatically resend the old prompt.

1. Restart with the **same server configuration and state directory**.
2. Inspect the published status, local logs, and actual workspace side effects.
3. Submit `cancel` to stop a surviving worker when its stored identity can be verified.
4. If identity is unavailable, stop the old process manually after verifying it belongs to this request. The server scans for the persisted Claude session UUID and refuses recovery while a matching process remains alive.
5. Acknowledge the interruption and wait for its terminal result:

```bash
remote-claude-client --config .runtime/client.json show SESSION_ID
remote-claude-client --config .runtime/client.json cancel SESSION_ID --request-id REQUEST_ID
remote-claude-client --config .runtime/client.json recover SESSION_ID --acknowledge
remote-claude-client --config .runtime/client.json watch SESSION_ID --request-id REQUEST_ID
```

`recover` records `interrupted`, advances the revision once, and releases the frozen workspace after verified shutdown. It acknowledges possible partial side effects; it does not undo them. Submit a **new** prompt to continue. If native session initialization was recorded, that next turn resumes the original context.

An upload failure is different: leave the server running or restart it with the same state. It retries publication only. A push that succeeded immediately before a crash is reconciled with the same immutable output, without another Claude execution.

## Client Usage

The client only needs Python dependencies, Git, and repository access. It does not need a local Claude login or a local copy of the server project workspace.

The default configuration path is `~/.config/remote-claude/config.json`; the server default is `~/.config/remote-claude/server.json`. Use `--config` to select separate configurations explicitly. [examples/client.json](examples/client.json) lists the client fields: `remote`, `branch`, `state_dir`, `watch_interval`, and `git_timeout`.

Initialize as shown above, then create a session and initial prompt in one commit:

```bash
remote-claude-client --config .runtime/client.json new \
  --workspace '~/projects/example' \
  --model sonnet --effort high --timeout 3600 \
  --prompt 'Inspect this project and summarize its structure.'
```

The command prints the session and request IDs. New IDs use this format:

```text
20261015T184530-0800_550e8400-e29b-41d4-a716-446655440000
YYYYMMDDTHHMMSS-0800_<random-uuid>
```

The prefix records creation time to the second in **fixed Pacific Standard Time (PST, UTC-08:00)** on every machine. It does not depend on the host timezone and does not switch to daylight time in summer. This avoids filenames moving backward during the autumn clock change. The UUID suffix prevents collisions when multiple clients create IDs in the same second; IDs within that second sort by their suffix. Keep machine clocks synchronized, since timezone normalization cannot correct clock skew.

Session directories, request filenames, control IDs, and event IDs use this convention. Response directories reuse their request ID so matching requests and responses sort together; their prefix represents request creation time, not response completion time. Structured `created_at` and execution timestamps remain ISO 8601 UTC values.

Existing UUID-only IDs remain readable, resumable, and valid for retries; existing files are not renamed. Upgrade both the client and server before using the new timestamp format. Claude's internal native session UUID remains separate from these mailbox IDs and is still used for `--resume`.

Use the printed values in the following commands; the uppercase ID placeholders are not literal IDs.

```bash
remote-claude-client --config .runtime/client.json list
remote-claude-client --config .runtime/client.json show SESSION_ID
remote-claude-client --config .runtime/client.json watch SESSION_ID --request-id REQUEST_ID
remote-claude-client --config .runtime/client.json send SESSION_ID \
  --prompt 'Continue with the smallest useful implementation change.'
```

`watch` polls every ten seconds by default. `--interval 2` changes that interval, `--once` prints one snapshot, and `--timeout 120` stops watching after two minutes. A watch timeout does not cancel server work. With `--request-id`, watch exits when that request has a terminal result. Inspect the result's `status` to distinguish success, failure, cancellation, timeout, interruption, or rejection; a successful watch command is not proof of a successful Claude task.

Use UTF-8 files or stdin for longer prompts:

```bash
remote-claude-client --config .runtime/client.json send SESSION_ID --prompt-file prompt.txt
printf '%s\n' 'Review the previous changes and report remaining problems.' | \
  remote-claude-client --config .runtime/client.json send SESSION_ID --prompt-file -
```

Prompt and response text is preserved in its original language. A complete prompt JSON message, including its metadata, must fit within 1 MiB. `--json` emits machine-readable output; `watch --json` emits JSON Lines when snapshots change. `show` returns response metadata and local answer paths. Open `answer.md` from the local mailbox cache or on GitHub to view its relative image links.

Only one ordinary request may be unfinished per session. Wait for its result before sending another. The client normally uses the latest published revision; `send --expected-revision N` additionally asserts the revision you intend. Simultaneous requests at the same revision are sorted by request ID by the server; one is accepted and the others receive `rejected` without advancing the revision.

For safe retries, retain IDs printed on submission failure and repeat with `--session-id`, `--request-id`, or `--control-id` as appropriate. Identical content with an existing ID is idempotent; different content is rejected. Do not generate a new ID merely because a push acknowledgement was lost.

Cancel a request or close a conversation:

```bash
remote-claude-client --config .runtime/client.json cancel SESSION_ID --request-id REQUEST_ID
remote-claude-client --config .runtime/client.json close SESSION_ID
```

Controls are asynchronous and take effect when the server next polls. `close` prevents new requests and cancels unfinished work; it does not delete the history or workspace. Published control acknowledgements appear in `show`. Accepted completed, failed, cancelled, timed-out, and interrupted turns each advance the revision once. Rejected requests do not.

## Output and Attachment Limits

Each conversation on the data branch has this structure:

```text
sessions/<session-id>/
  settings.json
  requests/<request-id>.json
  responses/<request-id>/
    result.json
    answer.md
    artifacts/<image-name>
  controls/<control-id>.json
  controls/results/<control-id>.json
  events/<event-id>.json
  status.json
```

Settings, requests, controls, events, and responses are immutable. Only status snapshots and the server heartbeat are replaced. Result metadata uses `status` for the terminal outcome; session snapshots use `state` for scheduling or recovery progress.

| Configuration key | Default |
| --- | --- |
| `artifact_limits.text_limit` | 262144 bytes of UTF-8 Markdown per turn |
| `artifact_limits.image_limit` | 2097152 bytes per image |
| `artifact_limits.image_count` | 10 images per turn |
| `artifact_limits.turn_image_limit` | 10485760 image bytes per turn |
| `image_budget_bytes` | 524288000 lifetime image bytes per mailbox |

The runner explicitly instructs Claude to write its final answer in GitHub-flavored Markdown, use ordinary HTTP/HTTPS links, and export charts or visualizations as static PNG, JPEG, or WebP images. Interactive embeds and clickable server-local file links are discouraged; server paths should appear as plain text or inline code. The server extracts response text from structured CLI events and saves it as Markdown. It does not run a separate format conversion step, render interactive content, or guarantee that every answer follows the Markdown instructions.

Unsupported assistant or final-result content blocks produce a visible notice at the top of the published answer and bounded `metadata.output_warnings` entries in `result.json`, with `code`, `content_type`, and `count` fields. Counts describe stream occurrences, so repeated blocks may be counted again; at most 16 distinct type names are retained, with additional types grouped as `other`. Unsupported payloads are not copied into the answer or warning metadata. Internal tool-call and thinking blocks do not produce these notices. A presentation warning alone does not fail an otherwise successful turn; missing final results and execution errors retain their usual status handling. These notices share the configured Markdown size limit; metadata remains available even when a zero or very small limit prevents displaying the notice. Text containing HTML or other unsupported markup is still text; it is not automatically converted into an image or checked for Markdown conformance.

The runner tells Claude the current export directory. Only ordinary files directly in that directory are considered. PNG, JPEG, and WebP must decode successfully, match their filename extension, be static, and contain at most 20 million pixels. Symbolic links, hard links, nested directories, special files, archives, datasets, videos, and model weights are excluded. Filenames containing backslashes, line breaks, or invalid UTF-8 are skipped. At most 1000 directory entries are inspected per turn.

Oversized images are skipped with their name, size when available, and reason; they are never silently compressed or split. The server snapshots validated image bytes and checks the publication manifest again before staging. Attachment failures are recorded separately from Claude's execution status.

Long Markdown is explicitly truncated; its full parsed final answer remains in `answer.full.md`. Inline data URIs are omitted. HTTP/HTTPS links remain ordinary links and are never downloaded by the publisher. Transfer large data through another channel and put its location in the answer.

The durable image ledger counts both published images and reserved pending results. Retries do not count twice, and deleting remote files does not refund the history budget. Changing limits requires a server restart. Finish pending publications before lowering limits, since the publisher rechecks its stored manifest against the current limits.

## Verified Local Two-Sided Example

The following command creates a unique local bare Git repository, independent client/server configurations and caches, and an existing workspace under this checkout's `.runtime/live/` directory. The checkout must be inside your actual home directory.

```bash
source .venv/bin/activate
python scripts/live_smoke.py --budget 1 --timeout 120
```

It runs the real client commands and server engine, writes a synthetic file through Claude, sends a second prompt with the same native session UUID, verifies Read/Bash tool calls, and publishes a small PNG, UTF-8 text, and a hyperlink. It also verifies an explicit permission denial in `auto` mode and a short real CLI timeout. The budget is passed to each Claude invocation; CLI-reported costs are diagnostic totals, not a statement of actual billed charges.

The final line prints the unique run directory. To continue using the generated local pair, substitute that path below. Its report records the created mailbox session ID.

Terminal 1:

```bash
source .venv/bin/activate
remote-claude-server --config .runtime/live/RUN_ID/server.json doctor
remote-claude-server --config .runtime/live/RUN_ID/server.json serve
```

Terminal 2:

```bash
source .venv/bin/activate
remote-claude-client --config .runtime/live/RUN_ID/client.json list
remote-claude-client --config .runtime/live/RUN_ID/client.json show SESSION_ID
remote-claude-client --config .runtime/live/RUN_ID/client.json send SESSION_ID \
  --prompt 'Read the synthetic token file again and summarize the current workspace.'
remote-claude-client --config .runtime/live/RUN_ID/client.json watch SESSION_ID
```

Stop both foreground commands with Ctrl+C when finished. All local smoke data remains ignored by Git for review. Local smoke branches intentionally remain in their disposable bare repositories; successful real GitHub smoke branches are deleted.

## Tests and Real GitHub Integration

Offline tests need no network, Claude account, or model calls:

```bash
source .venv/bin/activate
python -m pytest -q
```

The suite uses temporary bare Git repositories, independent clients, a real server engine, and a fake Claude subprocess. The project checkout must be below the current account's home so test workspaces satisfy the actual server policy. It covers revisions, duplicate/conflicting requests, push races, failed publication, crash recovery, surviving workers, cancellation, permissions, malformed output, timeout cleanup, quotas, and attachment validation.

For real GitHub integration, first verify SSH write access and Claude authentication. Then run:

```bash
python scripts/live_smoke.py \
  --remote git@github.com:carl-qi/prompt-tooling.git \
  --budget 1 --timeout 120
```

After a successful local live run, add `--skip-adapter-checks` to avoid repeating its permission and timeout probes. `--claude-executable PATH` selects another explicitly verified installation; the smoke still requires 2.1.300. `--run-dir PATH` chooses a new diagnostics directory inside the server home.

The script creates a uniquely named `test/remote-claude/<run-id>` branch containing only synthetic mailbox data. It checks that `main`/`master` refs remain unchanged. On success it deletes its test branch and verifies its absence. On failure it retains the branch, report, logs, and state for investigation.

To clean up a failed test after inspection, copy the exact `branch` from its `report.json`:

```bash
git push git@github.com:carl-qi/prompt-tooling.git \
  --delete test/remote-claude/EXACT_FAILED_RUN_BRANCH
```

Never substitute `main`, `master`, or your production mailbox. Generated verification records such as `TEST_RESULTS.md` and `.runtime/live/*/report.json` remain local and are ignored by Git; run the commands above to produce reports for your own checkout.

### Pressure Tests

The benchmark uses real client send/show handlers, Git operations, SQLite, and fake Claude subprocesses with a fixed 100 ms delay. Client handlers run in process, so Python interpreter startup is excluded. It makes no model API calls. Run it from a checkout under the server account's home, with test dependencies installed. A full run retains several gigabytes of generated files; keep sufficient free disk space and avoid running other tests during timed samples.

Validate the current version locally:

```bash
python scripts/benchmark_mailbox.py --variants current --cases local --policy
```

Validate the current version through GitHub, using existing noninteractive SSH write access:

```bash
python scripts/benchmark_mailbox.py \
  --variants current --cases github \
  --remote git@github.com:carl-qi/prompt-tooling.git \
  --policy
```

The default corpus has 1,000 sessions with ten completed turns each: 10,000 requests, 30,000 events, 40,000 published outbox entries, and 20,001 Git commits. The last ten sessions remain active in separate workspaces. Historical prompts are 1 KiB and answers are 4 KiB, with distinct synthetic content and no images. The script builds matching Git history, SQLite state, and fake native session histories; historical model calls are not replayed. Fixture preparation and submission-cache priming are outside the request latency measurements. The first result observer opens its cache during the measured request, so that initial fetch remains included in the first sample and maximum latency.

The current version uses the working tree in an isolated process. Local runs also include a ten-session control. Each full case measures 100 idle ticks, unchanged-HEAD synchronization (30 local or 10 GitHub samples), 30 sequential requests, five batches of ten simultaneous submissions, ten output-only changes, and three restarts. Concurrent clients use independent caches and real competing pushes. Ordinary request samples make server synchronization due after submission, separating processing latency from the polling interval.

`--policy` additionally seeds recent-activity ages to select the actual 10/30/60-second policy intervals, waits each full interval using the real clock, and runs the real client `watch` command at ten seconds. These samples include polling waits and are separate from processing benchmarks. They test specified polling phases, rather than estimate average latency for randomly timed arrivals. Restart samples time `Engine.start`; the subsequent synchronization is performed separately.

For a quick fixture and end-to-end check:

```bash
python scripts/benchmark_mailbox.py --variants current --cases local --skip-small \
  --sessions 2 --history-turns 2 --active 2 --quick
```

`--sessions`, `--history-turns`, and `--active` adjust the corpus. An optional comparison is available with `--variants both` (the script default); `--variants baseline` selects only the archived baseline. `--baseline-ref` selects an available Git commit, defaulting to `a438da6`, which also supplies the historical ledger schema for upgrade testing. Both variants use the same corpus. `--cases github` runs only full-size GitHub cases and requires `--remote`. `--run-dir PATH` permits staged execution with the same fixtures, for example:

```bash
python scripts/benchmark_mailbox.py --cases local --variants current --policy \
  --run-dir .runtime/benchmarks/example-run
python scripts/benchmark_mailbox.py --cases github --variants current --policy \
  --remote git@github.com:carl-qi/prompt-tooling.git \
  --run-dir .runtime/benchmarks/example-run
```

Completed cases are reused only when their parameters and source identity match. Changing the baseline, measured source, sample counts, or policy settings requires a new run directory. Failed cases remain available for diagnosis and also require a new directory for a fresh run.

Reports are written under `.runtime/benchmarks/RUN_ID/`: `REPORT.md` summarizes cases, `report.json` retains results, and `samples.csv` contains individual samples. Each case also has its configuration, raw timing events, attempt status, resource usage, and any watch logs. Measurements distinguish submission, batch waiting, polling, server fetch/validation/discovery, queueing, execution, persistence, publication, and client visibility. Summaries include sample counts, p50, p95, maximum, and success rate. Git durations from concurrent clients can overlap and must not be added to reconstruct wall time. Concurrent result observation waits for the complete batch; compare batch completion as well as individual submission latency. CPU includes waited-for children; parent and child peak RSS are reported separately rather than summed.

GitHub cases use unique `test/remote-claude/perf-RUN_ID-CASE` branches containing the complete synthetic history. They verify that `main`, `master`, and `mailbox` refs remain unchanged. Successful cases delete their test branches and verify removal. Failed cases retain their branch and an exact `cleanup_command` in the case report. All generated histories, databases, logs, and benchmark reports are ignored by Git; only the benchmark source and tests are committed. These measurements describe the tested machine and network, not AWS performance or real Claude response time.

## Troubleshooting and Current Scope

| Symptom | Action |
| --- | --- |
| Mailbox branch missing or marker invalid | Run client `init` for a new dedicated branch. Do not use a code branch. Use a fresh cache when intentionally creating a different mailbox. |
| Workspace rejected | Create it on the server; quote `~/...`; check realpath stays inside home and outside server state. |
| Unfinished request or revision conflict | Read `show`, wait for publication, then send a new request against the current revision. |
| `unknown` execution | Inspect side effects, stop the old worker, and use explicit recovery. |
| Permission denied | Read the denial summaries and revise the task or server policy in code; the service will not wait for an approval click. |
| Missing native session on resume | Restore the matching Claude history, or deliberately start a new conversation. There is no silent context reset. |
| Git or SSH unavailable | Fix outbound connectivity/authentication and retain the same state directory. Pending outputs are retried. |
| Version/flag preflight failure | Point configuration at the verified 2.1.300 executable; check PATH and `doctor`. |
| Another server owns the state directory | Stop the existing process normally. Do not bypass the lock by starting another state directory on the same branch. |
| Mailbox already has execution history | Restore the original ledger and native history; a fresh ledger cannot safely adopt executed requests. |
| Images missing | Check `skipped` metadata, file format/size, export location, and lifetime quota. Download large data separately. |
| Result visible remotely but local session still publishing | Leave the server running; its immutable outbox will retry and confirm the previous publication. |

This release validates local Linux operation and outbound GitHub transport. It does not include AWS provisioning, service installation, machine migration automation, multi-server coordination, containers/cgroups, protection against deliberately detached processes, large-file transfer, interactive approvals, plugins, or MCP integrations. Those remain deployment or future design work.
