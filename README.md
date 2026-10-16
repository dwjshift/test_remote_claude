# Remote Claude Mailbox

Run Claude Code on a Linux machine that can reach GitHub but cannot accept inbound connections. A client submits immutable prompts to a dedicated Git branch; a server runs unattended Claude turns and publishes Markdown responses, diagnostics, and small validated images.

**[Installation and usage](USAGE.md)** covers the server, client, local examples, recovery, and testing. Generated validation reports, logs, and runtime state are kept locally and ignored by Git.

## Layout

| Path | Purpose |
| --- | --- |
| `client/` | Session creation, prompts, history, watch, and control commands |
| `server/` | Scheduling, Claude subprocesses, SQLite ledger, publication, and attachments |
| `common/` | Versioned protocol, configuration, and retrying Git transport |
| `tests/` | Unit tests and offline integration tests with real Git and a fake Claude process |
| `scripts/live_smoke.py` | Opt-in real Claude and isolated GitHub integration checks |
| `scripts/benchmark_mailbox.py` | Reproducible local and GitHub pressure tests, with optional baseline comparison |
| `examples/` | English client and server JSON configurations |

Code lives on `main`. The separate orphan `mailbox` branch contains `.mailbox.json`, `sessions/`, and low-frequency server status. Runtime databases, raw logs, caches, credentials, and native Claude histories stay outside the data branch.

## Behavior

- Claude Code **2.1.300**, print mode, structured output, `auto` permissions, and no permission prompts. The server pins the resolved executable when it starts.
- New session and request IDs start with a second-precision timestamp in fixed PST (UTC-08:00). Responses share their request ID; existing UUID identifiers remain supported.
- One unfinished request per conversation; explicit revisions and deterministic conflict rejection.
- Up to ten concurrent turns in independent workspaces. Equal or nested workspaces are serialized. Idle conversations keep their history without retaining a Claude process.
- One central poller uses 60 seconds when idle, 30 seconds after recent interaction, and 10 seconds during active work. Results are published as soon as they are ready.
- Commit differences identify new inputs; indexed SQLite queries keep completed history out of routine scheduling. Durable ingestion checkpoints are separate from Git validation and publication.
- Durable results and a SQLite publication outbox. Network retries never rerun a completed request.
- Interrupted executions become `unknown` and freeze the workspace until an explicit recovery closes the interrupted request.
- Claude is instructed to answer in GitHub-flavored Markdown and export charts as small static PNG, JPEG, or WebP images. Unsupported response content is omitted with a visible notice and diagnostic metadata; large data stays on the server.

Use a repository whose writers are trusted to run code as the server account. Workspace validation restricts the starting directory to the server home; it is not an operating-system sandbox. Run exactly one server state directory per mailbox branch. Linux local operation is verified; AWS deployment remains a separate step.
