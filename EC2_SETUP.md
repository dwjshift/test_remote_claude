# EC2 Deployment Handoff: Claude Code through Amazon Bedrock

This is an execution runbook for the agent running **on the target EC2 instance**. Complete the setup, make and push necessary compatibility fixes, test both sides on EC2 through temporary GitHub branches, update `USAGE.md` with verified machine/model information, and leave the production server running.

The instance is expected to have a working Claude Code installation using Amazon Bedrock. Preserve that provider and credential setup. This document does **not** establish that EC2 deployment or any particular Bedrock model has already been verified. Repository behavior was reviewed at `a898e6d`; official references were checked on October 15, 2026, America/Los_Angeles. Recheck the installed CLI and current documentation when executing this runbook.

## 1. Required outcome and operating rules

Complete these phases in order:

1. Inspect the machine, preserve the working Bedrock setup, and install the required Python dependencies and a tested Claude version.
2. Adapt only what the machine requires. Run offline tests and real Bedrock/GitHub tests, including the public server command under its eventual service environment. Push tested source changes to `main`.
3. Record the actual EC2 configuration and usable client model strings in `USAGE.md`, then push that documentation.
4. Start one persistent production server with the polling parameters in section 9. Verify service readiness and publish the final operating instructions.

All code, comments, CLI messages, and documentation must be English. Preserve user prompts and replies in their original language. The user has requested the code pushes, temporary test branches, branch cleanup, and persistent server startup described here; do the work through completion rather than stopping after proposing a plan. Report concrete blockers if the required account access or noninteractive model capability is unavailable.

Use the existing non-root account that can invoke Bedrock. Its home directory owns the AWS configuration, Claude session histories, workspaces, and service state. Do not switch the service to root to work around authentication. No inbound port or public IP is required by this application. Outbound GitHub access and the existing Bedrock/credential endpoints must work from the service environment.

Keep credentials, private environment files, databases, caches, raw outputs, and generated reports out of Git. Store setup evidence under ignored `.runtime/setup/`; put persistent service configuration under `~/.config/remote-claude/`. Do not dump complete environment variables, AWS credential files, or Claude settings into logs or commits. Preserve the working checkout and any unrelated changes. Never force-push or reset another person's changes.

## 2. Establish the account, checkout, and local evidence directory

Run the shell examples as the service account, in Bash. The examples use a checkout named `~/prompt-tooling`; adjust every corresponding service path if the actual path differs. Paths in the unit-generation example must not contain whitespace or `%`.

```bash
set -euo pipefail
export RC_REMOTE='git@github.com:carl-qi/prompt-tooling.git'
export RC_REPO="$HOME/prompt-tooling"
export RC_RUN_ID="$(date -u +%Y%m%dT%H%M%SZ)-$(python3 -c 'import uuid; print(uuid.uuid4().hex[:8])')"
export RC_SETUP_DIR="$RC_REPO/.runtime/setup/$RC_RUN_ID"
export RC_ENV_FILE="$HOME/.config/remote-claude/bedrock.env"
export GIT_TERMINAL_PROMPT=0
```

If the checkout does not exist, clone `RC_REMOTE` into `RC_REPO`. Otherwise inspect `git status` and the current branch first, preserve local work, and update `main` without discarding changes. Verify that `origin` is the intended repository. The checkout must be under the account's real home for this project's test workspaces.

```bash
cd "$RC_REPO"
git status --short
git remote -v
git branch --show-current
git fetch origin
git log -1 --oneline
mkdir -p "$RC_SETUP_DIR" "$HOME/.config/remote-claude"
chmod 700 "$RC_SETUP_DIR" "$HOME/.config/remote-claude"
id
uname -m
cat /etc/os-release
python3 --version
git --version
command -v claude
claude --version
```

Inspect the existing Claude launcher: it may be a native binary, package-manager installation, alias, function, or wrapper that injects Bedrock variables. Record the version, installation method, resolved binary, and rollback procedure locally. A copied wrapper is not a backup of the package or credentials it depends on.

Verify Git authentication with a noninteractive operation:

```bash
GIT_SSH_COMMAND='ssh -oBatchMode=yes -oConnectTimeout=10' git ls-remote "$RC_REMOTE" refs/heads/main
```

The application sets its own SSH command with `BatchMode=yes`. Configure the correct key, host, and port in the account's `~/.ssh/config` when necessary. Authentication that works only through a forwarded SSH agent from the current login is insufficient for an unattended service unless a persistent agent is deliberately configured and verified.

## 3. Install dependencies and pin Claude without losing Bedrock access

Select an existing Python 3.11+ interpreter, preferably 3.13 when available. If missing, install a supported version and venv support using the instance's OS/package policy; do not replace the OS Python. Install Git and AWS CLI v2 if needed for the diagnostic commands below. AWS CLI is a diagnostic aid, not a new application runtime dependency.

```bash
export RC_PYTHON=python3
"$RC_PYTHON" -c 'import sys; assert sys.version_info >= (3, 11), sys.version'
cd "$RC_REPO"
"$RC_PYTHON" -m venv .venv
.venv/bin/python -m pip install -e '.[test]'
.venv/bin/remote-claude-client --help
.venv/bin/remote-claude-server --help
```

The repository currently validates Claude **2.1.300**, not an automatically changing `latest`. Preserve the prior working installation before changing the default launcher. For an installation supporting `claude install`, use:

```bash
DISABLE_AUTOUPDATER=1 claude install 2.1.300
export PATH="$HOME/.local/bin:$PATH"
hash -r
type -a claude
claude --version
export RC_CLAUDE="$(readlink -f "$(type -P claude)")"
export RC_CLAUDE_VERSION='2.1.300'
"$RC_CLAUDE" --version
```

`type -P` finds the PATH executable even when an alias or function exists. Inspect that executable too: if it is a wrapper, resolve and pin the actual installed native binary after preserving its provider configuration. PATH changes and `hash -r` do not remove aliases or functions; inspect and update the shell's default launcher separately so `claude --version` and the pinned executable both select the intended release.

Use the [official installation instructions](https://code.claude.com/docs/en/setup) if the existing install method cannot do this. Match the machine architecture. If EC2 already needs a different release, verify it separately and set the exact tested version/path in configuration; update version-sensitive tests and smoke tooling where necessary. Do not pretend a new version is validated by disabling version/flag checks. A host-specific version choice does not by itself require changing the repository-wide default.

The service must keep a resolved, tested executable path and `DISABLE_AUTOUPDATER=1`; do not replace that binary during active turns. Recheck the default `claude` command and the preserved rollback installation after the upgrade.

## 4. Reproduce the existing Bedrock environment explicitly

Inspect the working setup privately. Determine the provider selection, region, model mappings, credential source, and any required proxy/CA settings. Do not assume that a successful interactive `claude` command establishes service readiness.

**Repository-specific issue:** `server/runner.py` inherits process environment variables but passes `--setting-sources ''` and isolated settings that disable hooks, plugins, and MCP. Configuration present only in shell startup files, aliases, or `~/.claude/settings.json` may therefore be absent from workers. Keep those isolation controls; supply the needed provider configuration explicitly.

In particular, `awsAuthRefresh` and `awsCredentialExport` are Claude settings, not environment variables. Copying region/profile variables alone does not preserve those refresh helpers. If the working setup uses either, configure and test a noninteractive AWS profile/`credential_process`, or make a focused compatibility change that explicitly supplies only the required credential settings while retaining hook/plugin/MCP isolation. Verify refresh beyond the lifetime of cached credentials before claiming unattended access. [Bedrock credential configuration](https://code.claude.com/docs/en/amazon-bedrock)

Create `RC_ENV_FILE` with the verified values. Preserve an existing file before updating it. This is a **template**, not a set of ready-to-use credentials:

```dotenv
CLAUDE_CODE_USE_BEDROCK=1
AWS_REGION=REPLACE_WITH_EXISTING_VERIFIED_REGION
DISABLE_AUTOUPDATER=1
GIT_TERMINAL_PROMPT=0
# Add only mappings that have actually passed the tests below.
# ANTHROPIC_DEFAULT_SONNET_MODEL=verified-id-or-profile
# AWS_PROFILE=existing-noninteractive-profile
# ANTHROPIC_DEFAULT_OPUS_MODEL=verified-id-or-profile
# ANTHROPIC_DEFAULT_HAIKU_MODEL=verified-id-or-profile
```

Use literal `KEY=value` assignments with shell-safe quoting where needed: no `export`, shell commands, command substitution, or references such as `$HOME` inside values. This permits the same file to be used by Bash and systemd. Use absolute paths for optional credential/config files or helper executables. Set file mode `600`.

Bedrock can use the AWS credential chain, including an EC2 instance role. Preserve the existing working method. A configured `AWS_BEARER_TOKEN_BEDROCK` takes precedence over that chain; retain it only if it is the intended method, and never publish its value. Ensure credentials refresh without a browser or terminal prompt. [Official Bedrock configuration](https://code.claude.com/docs/en/amazon-bedrock)

Review conflicting provider selectors and stale credential overrides instead of indiscriminately copying the entire interactive environment. Retain a required `CLAUDE_CONFIG_DIR` consistently so resumed sessions use the same history. Check `CLAUDE_CODE_EFFORT_LEVEL`: it can override the session's requested effort; document or remove an unintended override. Managed caps can also affect effective effort. [Model and effort configuration](https://code.claude.com/docs/en/model-config#adjust-effort-level)

Load the reviewed file for setup commands:

```bash
chmod 600 "$RC_ENV_FILE"
set -a
. "$RC_ENV_FILE"
set +a
```

If using the AWS credential chain, verify identity without printing credentials:

```bash
aws sts get-caller-identity --query Arn --output text > "$RC_SETUP_DIR/aws-identity.txt"
```

This is not a Bedrock invocation test, and a Bedrock-only bearer token does not necessarily authorize STS. Record which method is actually in use. Do not replace working Bedrock authentication with `claude auth login` for a claude.ai subscription.

## 5. Discover and verify the client model options

Inventory candidates in the **actual region and account**:

```bash
aws bedrock list-foundation-models --region "$AWS_REGION" \
  --by-provider Anthropic --output json > "$RC_SETUP_DIR/foundation-models.json"
aws bedrock list-inference-profiles --region "$AWS_REGION" \
  --output json > "$RC_SETUP_DIR/inference-profiles.json"
```

These are discovery results, not proof that invocation, streaming, tools, or auto mode work. Listing permissions can also be absent even when a known model is invocable; in that case use the existing working configuration and actual calls. Some deployments require an inference profile ID/ARN instead of a base model ID. [AWS model discovery](https://docs.aws.amazon.com/bedrock/latest/userguide/models-get-info.html)

**Compatibility gate:** current official documentation lists Sonnet 5+, Opus 4.7+, Haiku 5.5, and Fable as supporting auto mode on Bedrock. This is a provider capability statement, not a claim that these models are enabled on this instance. An older model that can answer text may still fall back to Manual mode; classifier access can fail separately. Verify the installed version and emitted `system/init` `permissionMode` together with real tool execution. Do not silently switch to permission bypass or another provider to make a test pass. [Auto mode on Bedrock](https://code.claude.com/docs/en/permission-modes#auto-mode-on-bedrock-agent-platform-or-foundry)

For each model option you intend to advertise to the client, record:

| Required evidence | What to record |
| --- | --- |
| Client value | Exact `--model` string that passed, or a stable alias with a verified service mapping |
| Provider identity | Region, actual CLI-reported model, and applicable model/profile mapping |
| Effort | Values actually tested; test `high` before recommending the current client default |
| Noninteractive tools | Successful Write/Read/Bash and effective `auto`, with permission prompts disabled |
| Continuation | Successful second turn using the same native Claude session ID |
| Evidence | Date, CLI version, source commit, local report path, and any restrictions |

Test the intended default and every additional advertised option with small synthetic prompts. Mark other discovered models **unverified**, rather than declaring all listed models usable. If using aliases such as `sonnet`, explicitly pin their service-side mappings so a CLI update cannot silently select an untested model. Record any auxiliary model/classifier requirements exposed by real failures. Bedrock's built-in WebSearch is unavailable according to its provider documentation; document that limitation and verify the remaining tools needed here.

Set these variables only after selecting real candidate values; replace the placeholder before proceeding:

```bash
export RC_MODEL='REPLACE_WITH_CANDIDATE_CLIENT_MODEL_STRING'
export RC_EFFORT='high'
```

## 6. Make focused compatibility changes on EC2

Read the current source before editing. These are known inspection points, not instructions to remove checks unconditionally:

| File | Existing assumption | Required EC2 handling |
| --- | --- | --- |
| `server/__main__.py` | `authentication()` requires `claude auth status --json` to return `loggedIn=true`; failures recommend claude.ai login | Verify actual Bedrock output. If needed, implement provider-aware diagnostics/readiness without accepting the Bedrock selector alone as proof of credentials. Preserve rejection of missing/invalid credentials and first-party behavior. A real model probe must be explicit and bounded, not a hidden model call on every poll. |
| `server/runner.py` | Environment inheritance, isolated settings, a fixed built-in tool list, required headless flags | Supply the verified environment and handle genuinely unavailable provider tools if the installed CLI requires it. Keep `-p`, `stream-json`, `auto`, `--permission-prompts none`, session resume, deadlines, and process-group cleanup. |
| `scripts/live_smoke.py` | Version `2.1.300` and effort `low` are embedded; model/executable are configurable | If selecting a different version or effort, parameterize these assumptions and test the new options. Do not pass invented options before implementing them. |
| `scripts/live_smoke.py` | Uses `Engine` directly, bypassing public `doctor`/`serve` authentication; protected-ref check covers `main`/`master` | Also verify `mailbox`, and run the separate public daemon/service test below. Engine smoke success alone is insufficient. |
| Tests and examples | Local claude.ai-oriented defaults | Add Bedrock-specific auth/env/flag/model regressions while keeping existing tests working. Keep host credentials out of fixtures. |

`--permission-prompts none` turns unanswered approval requests into denials; the worker must surface those failures instead of waiting for input. A CLI budget flag uses estimated model cost and is not a strict AWS billing cap. Use short deadlines and synthetic prompts as well. [CLI reference](https://code.claude.com/docs/en/cli-reference)

Model, effort, and turn timeout are immutable **session** settings supplied by `client new`. They are not arbitrary server configuration fields. Full model IDs/profile strings already pass through the protocol as argument values; do not add unnecessary shell interpolation or a speculative model allowlist.

## 7. Validate offline, then through temporary GitHub branches

### 7.1 Offline regression

```bash
cd "$RC_REPO"
.venv/bin/python -m pytest -q > "$RC_SETUP_DIR/pytest.txt" 2>&1
git diff --check
```

Inspect the exit status and summary. Fix actual failures, including any tests needed for the compatibility changes. No old-version performance comparison or 1,000-session benchmark is required for this deployment.

### 7.2 Real Bedrock two-turn and adapter smoke

With the current script and version `2.1.300`, this command is valid. It uses effort `low`; parameterize the script first if that assumption is unsuitable, and separately verify the intended production effort.

```bash
.venv/bin/python scripts/live_smoke.py \
  --remote "$RC_REMOTE" \
  --claude-executable "$RC_CLAUDE" \
  --model "$RC_MODEL" \
  --budget 1 --timeout 120 \
  --run-dir "$RC_SETUP_DIR/live-bedrock"
```

The run directory must not already exist. The script initializes a unique orphan `test/remote-claude/...` branch, uses independent client/server state, runs real Write followed by resumed Read/Bash, checks UTF-8/link/PNG delivery, and tests permission denial and timeout cleanup. Do **not** skip adapter checks on the first EC2 run. A successful script deletes its test branch and verifies absence; failure retains diagnostics and the branch for investigation.

Require completed result statuses, revision 1 then 2, one native session ID, and expected tool evidence. Inspect local stream initialization for the actual model and `permissionMode=auto`; test that approvals cannot hang. Record the actual AWS/provider evidence privately. A passing command from an interactive shell is not yet proof of persistent service operation.

Do not push `main` while a test is checking protected refs. Snapshot `main`, `master`, and production `mailbox` before and after the test, or extend the script's check to include `mailbox`. Investigate unexpected changes rather than overwriting them.

### 7.3 Public daemon test with the final service environment

This catches the authentication and environment gaps that the Engine-based smoke cannot detect. Use another temporary branch and fresh persistent test state:

```bash
export RC_TEST_BRANCH="test/remote-claude/ec2-daemon-$RC_RUN_ID"
export RC_TEST_DIR="$RC_SETUP_DIR/daemon"
export RC_TEST_WORKSPACE="$HOME/remote-claude-workspaces/ec2-validation-$RC_RUN_ID"
export RC_TEST_WORKSPACE_ARG="~/remote-claude-workspaces/ec2-validation-$RC_RUN_ID"
mkdir -p "$RC_TEST_DIR" "$HOME/remote-claude-workspaces"
mkdir "$RC_TEST_WORKSPACE"
.venv/bin/remote-claude-client --config "$RC_TEST_DIR/client.json" init \
  --remote "$RC_REMOTE" --branch "$RC_TEST_BRANCH" \
  --state-dir "$RC_TEST_DIR/client-state" --watch-interval 10
.venv/bin/python - <<'PY'
import json
import os
from pathlib import Path

directory = Path(os.environ["RC_TEST_DIR"])
config = json.loads(Path("examples/server.json").read_text())
config.update(remote=os.environ["RC_REMOTE"], branch=os.environ["RC_TEST_BRANCH"],
              state_dir=str(directory / "server-state"),
              claude_executable=os.environ["RC_CLAUDE"],
              claude_version=os.environ["RC_CLAUDE_VERSION"],
              max_workers=2, max_budget_usd=1)
(directory / "server.json").write_text(json.dumps(config, indent=2) + "\n")
PY
.venv/bin/remote-claude-server --config "$RC_TEST_DIR/server.json" doctor \
  > "$RC_TEST_DIR/doctor.json"
```

Require `doctor` exit 0 and `ok=true` under the reviewed Bedrock environment. If authentication fails while raw Bedrock calls work, fix section 6; do not bypass the public command and call `Engine` instead.

Install the unit using section 8 with these test values:

```bash
export RC_UNIT_NAME='remote-claude-ec2-test'
export RC_SERVICE_CONFIG="$RC_TEST_DIR/server.json"
```

After starting the test unit, submit and retrieve a real request:

```bash
.venv/bin/remote-claude-client --config "$RC_TEST_DIR/client.json" --json new \
  --workspace "$RC_TEST_WORKSPACE_ARG" \
  --model "$RC_MODEL" --effort "$RC_EFFORT" --timeout 120 \
  --prompt 'Use Write to create readiness.txt containing EC2_DAEMON_OK. Reply briefly after the write succeeds.' \
  > "$RC_TEST_DIR/new.json"
export RC_SESSION_ID="$(.venv/bin/python -c 'import json,os; from pathlib import Path; print(json.loads((Path(os.environ["RC_TEST_DIR"])/"new.json").read_text())["session_id"])')"
export RC_REQUEST_ID="$(.venv/bin/python -c 'import json,os; from pathlib import Path; print(json.loads((Path(os.environ["RC_TEST_DIR"])/"new.json").read_text())["request_id"])')"
.venv/bin/remote-claude-client --config "$RC_TEST_DIR/client.json" --json watch \
  "$RC_SESSION_ID" --request-id "$RC_REQUEST_ID" --interval 10 --timeout 240 \
  > "$RC_TEST_DIR/watch-first.jsonl"
.venv/bin/remote-claude-client --config "$RC_TEST_DIR/client.json" --json show \
  "$RC_SESSION_ID" --request-id "$RC_REQUEST_ID" > "$RC_TEST_DIR/first.json"
.venv/bin/python - <<'PY'
import json
import os
from pathlib import Path

view = json.loads((Path(os.environ["RC_TEST_DIR"]) / "first.json").read_text())
assert len(view["responses"]) == 1, "No published response"
result = view["responses"][0]
assert result["status"] == "completed", result.get("error")
assert result["revision"] == 1
assert (Path(os.environ["RC_TEST_WORKSPACE"]) / "readiness.txt").read_text().strip() == "EC2_DAEMON_OK"
print("The public service completed and published its first real turn.")
PY
```

`watch` can exit successfully after its own deadline or after a failed terminal result; its exit code alone is **not** an acceptance check. Inspect result JSON as above and confirm Write tool evidence in the local stream log. The workspace is unique per run, and its creation deliberately fails if it already exists so an old marker cannot satisfy this check.

Restart the test service with the **same** state and home, then continue the same session:

```bash
systemctl --user restart remote-claude-ec2-test.service
.venv/bin/remote-claude-client --config "$RC_TEST_DIR/client.json" --json send \
  "$RC_SESSION_ID" --expected-revision 1 \
  --prompt 'Continue the previous task. Use Read on readiness.txt and report its exact contents in Markdown, with a link to https://example.com.' \
  > "$RC_TEST_DIR/second-request.json"
```

Extract the new request ID from `second-request.json`, repeat `watch` and `show`, and assert `status=completed`, revision 2, the same `claude_session_id`, `metadata.resume=true`, the expected token, and the HTTP link. Verify the unit's processes and logs use the intended Bedrock account/environment, not a browser login or transient shell-only credentials. Also exercise normal SIGTERM while a synthetic request is running: confirm worker cleanup, a terminal cancellation or explicit recoverable interruption, and no automatic replay. If a network outage delays publication, retain the outbox and allow it to publish after recovery.

### 7.4 Stop test services and remove successful test branches

Only delete a test branch after its server is stopped. Keep failed-run evidence until diagnosed; after a successful rerun, clean up the earlier synthetic branches as well. Never delete production `mailbox` to resolve a test failure.

```bash
systemctl --user disable --now remote-claude-ec2-test.service
rm "$HOME/.config/systemd/user/remote-claude-ec2-test.service"
systemctl --user daemon-reload
case "$RC_TEST_BRANCH" in
  test/remote-claude/ec2-daemon-*) ;;
  *) echo 'Refusing to delete an unexpected branch' >&2; exit 1 ;;
esac
git push "$RC_REMOTE" --delete "$RC_TEST_BRANCH"
RC_BRANCH_STATUS=0
git ls-remote --exit-code --heads "$RC_REMOTE" "refs/heads/$RC_TEST_BRANCH" \
  > "$RC_TEST_DIR/branch-after-cleanup.txt" || RC_BRANCH_STATUS=$?
test "$RC_BRANCH_STATUS" -eq 2
```

Exit 2 here means the ref is absent; a network/authentication error is not proof of deletion. Preserve ignored diagnostics and record each branch's cleanup result. Stop any manually launched test processes as well.

## 8. Persistent service unit template

Use this template first for the temporary daemon test, then for production with the values in section 9. It runs as the existing account through a systemd **user** service and reads the explicit provider environment. The service is not an interactive shell and does not source `.bashrc`.

```bash
mkdir -p "$HOME/.config/systemd/user"
.venv/bin/python - <<'PY'
import os
import re
from pathlib import Path

name = os.environ["RC_UNIT_NAME"]
assert re.fullmatch(r"remote-claude(?:-ec2-test)?", name), "Unexpected unit name"
repo = Path(os.environ["RC_REPO"]).resolve()
config = Path(os.environ["RC_SERVICE_CONFIG"]).resolve()
environment = Path(os.environ["RC_ENV_FILE"]).resolve()
for path in (repo, config, environment):
    assert path.exists(), path
    assert not any(c.isspace() or c == "%" for c in str(path)), "Adapt unit escaping for this path"
unit = f"""[Unit]
Description=GitHub mailbox server for Claude Code on Amazon Bedrock
StartLimitIntervalSec=300
StartLimitBurst=5

[Service]
Type=simple
WorkingDirectory={repo}
EnvironmentFile={environment}
Environment=PYTHONUNBUFFERED=1
ExecStart={repo}/.venv/bin/python -m server --config {config} serve
Restart=on-failure
RestartSec=10
KillSignal=SIGTERM
KillMode=mixed
TimeoutStopSec=600
UMask=0077

[Install]
WantedBy=default.target
"""
target = Path.home() / ".config/systemd/user" / f"{name}.service"
if target.exists():
    raise SystemExit(f"Inspect and preserve the existing unit before replacing it: {target}")
target.write_text(unit)
print(target)
PY
systemd-analyze --user verify "$HOME/.config/systemd/user/$RC_UNIT_NAME.service"
sudo loginctl enable-linger "$(id -un)"
systemctl --user daemon-reload
systemctl --user enable --now "$RC_UNIT_NAME.service"
systemctl --user status "$RC_UNIT_NAME.service" --no-pager
```

Lingering keeps the user manager available after logout and at boot; verify it with `loginctl show-user "$(id -un)" -p Linger`. If the machine has no functioning user manager or permission to enable lingering, adapt to a system service with an explicit `User`, `Group`, `HOME`, absolute paths, and the same environment file. Test that alternative before declaring persistent startup complete. [systemd login manager reference](https://github.com/systemd/systemd/blob/main/man/loginctl.xml)

`KillMode=mixed` sends the initial termination signal to the orchestrator, allowing its worker cancellation and outbox handling, with eventual cleanup of remaining service processes. The generous stop timeout allows Git operations to finish; forced termination still requires normal unknown-request recovery. [systemd kill behavior](https://github.com/systemd/systemd/blob/main/man/systemd.kill.xml)

Confirm the runtime PATH can find required workspace tools and any credential helper. Use literal absolute paths or an explicit reviewed PATH if necessary. For instance-role credentials, do not add service restrictions that make required metadata endpoints inaccessible. Reconnect after logout and verify the test service still works; verify boot enablement without rebooting a shared instance solely for this test.

## 9. Push compatibility changes and prepare production

After tests pass, inspect `git diff`, stage only intended source/tests/docs, commit in English, fetch/reconcile `origin/main`, and push the tested change to `main`. Re-run affected checks if reconciliation changes runtime code. Keep all generated evidence ignored. Do not force-push.

Use `mailbox` for production unless an explicitly documented existing deployment requires another branch. Check ownership first. **Exactly one server state directory on one machine may own a mailbox branch.** The local singleton lock does not protect against a second server on another host. If the branch already contains execution history, restore its original ledger and native Claude history as a deliberate migration; do not attach a new empty ledger, erase the branch, or start competing servers.

Once it is confirmed that this instance should own the production branch:

```bash
export RC_PROD_BRANCH='mailbox'
export RC_PROD_CONFIG="$HOME/.config/remote-claude/server.json"
.venv/bin/remote-claude-client --config "$HOME/.config/remote-claude/ec2-admin-client.json" init \
  --remote "$RC_REMOTE" --branch "$RC_PROD_BRANCH" \
  --state-dir "$HOME/.local/state/remote-claude/ec2-admin-client" --watch-interval 10
.venv/bin/python - <<'PY'
import json
import os
from pathlib import Path

path = Path(os.environ["RC_PROD_CONFIG"])
if path.exists():
    raise SystemExit("Inspect and preserve the existing production configuration before updating it")
config = json.loads(Path("examples/server.json").read_text())
config.update(remote=os.environ["RC_REMOTE"], branch=os.environ["RC_PROD_BRANCH"],
              state_dir=str(Path.home() / ".local/state/remote-claude/server"),
              claude_executable=os.environ["RC_CLAUDE"],
              claude_version=os.environ["RC_CLAUDE_VERSION"],
              adaptive_polling=True, poll_interval=60,
              active_poll_interval=10, warm_poll_interval=30,
              max_workers=10, heartbeat_interval=300, idle_ttl_seconds=86400)
path.write_text(json.dumps(config, indent=2) + "\n")
path.chmod(0o600)
PY
.venv/bin/remote-claude-server --config "$RC_PROD_CONFIG" doctor
```

Production parameters must be recorded explicitly in `USAGE.md`:

| Setting | Value / behavior |
| --- | --- |
| Code / data branches | `main` / actual production mailbox branch |
| Central polling | `adaptive_polling=true`, idle `poll_interval=60`, active `active_poll_interval=10`, warm `warm_poll_interval=30` |
| Activity windows | Queued/running work or interaction in the last 5 minutes: 10 seconds; 5–10 minutes: 30 seconds; older: 60 seconds |
| Timing | Initial sync immediately; normally wait from completion of the preceding sync; Git work can delay the next sync; activity can shorten the wait; errors use backoff |
| Scope | One central poller, no per-session GitHub polling; completed results enter publication immediately |
| Concurrency | `max_workers=10`; excess work queues; equal/parent/child workspaces remain mutually exclusive |
| Heartbeat / idle sessions | `heartbeat_interval=300`; `idle_ttl_seconds=86400`; dormant sessions retain history |
| Network retries | `retry_interval=1`, exponential backoff up to 300 seconds; `git_timeout=30` unless measured network conditions require adjustment |
| Session defaults | Verified model mapping, effort `high` only if verified, default turn timeout 3600 seconds; explicitly supplied through `client new` |
| Client observation | `watch_interval=10` |
| Attachments | Markdown 256 KiB; static PNG/JPEG/WebP: 2 MiB each, 10 files and 10 MiB per turn; 500 MiB lifetime image budget |
| CLI | Exact tested executable/version; no automatic upgrades during service operation |

If instance capacity or Bedrock quotas require lowering concurrency, document the tested value and reason. Keep the agreed polling schedule. Production configuration deliberately does not inherit the smoke's USD 1 budget cap; choose and document a production cap only when desired. Large data stays on the server. Workspaces must already exist under the service account's real home and must not contain or sit inside the server state directory.

## 10. Update USAGE.md with verified EC2 facts and models

Add a clearly labeled **Verified EC2 Deployment** section. Do not replace local-machine facts with guesses. Include the following concrete values; use no `REPLACE_...` placeholders in a completed deployment record:

- Verification date/time and timezone; machine label, OS, architecture, service account/home, Python, Claude version, and tested runtime commit.
- Actual remote, production mailbox branch, checkout path, pinned executable, configuration/environment file paths, persistent state path, and native history location. Never include credential values.
- Bedrock region, credential **method** and unattended refresh requirements, model alias mappings needed by clients, and provider tool restrictions.
- A model table with exact client `--model` strings, actual model/profile identity or a documented local alias mapping, tested effort values, auto/tool/resume results, verification date, and limitations. Distinguish unverified inventory entries from supported choices.
- Polling/concurrency/timeout/attachment values from section 9, actual unit name, service manager type, startup policy, stop/restart/status/log commands, and recovery procedure.
- Offline and real Bedrock/GitHub test outcomes, local evidence paths, and deleted test branch names. Do not commit the raw reports.
- A ready-to-copy client initialization and `new` example using an actual existing server `~/...` workspace and a verified model value. Only the server needs AWS credentials; clients need repository access.

For the client example, use the actual values in this pattern:

```bash
remote-claude-client --config ~/.config/remote-claude/ec2-client.json init \
  --remote git@github.com:carl-qi/prompt-tooling.git \
  --branch mailbox --state-dir ~/.local/state/remote-claude/ec2-client
remote-claude-client --config ~/.config/remote-claude/ec2-client.json new \
  --workspace '~/projects/ACTUAL_EXISTING_PROJECT' \
  --model 'ACTUAL_VERIFIED_CLIENT_VALUE' --effort high --timeout 3600 \
  --prompt 'Inspect this project and summarize its structure.'
```

Replace both placeholders, and replace `high` if a different effort was actually verified. The quoted workspace refers to the **EC2 service account's home**, not the client's home. Model, effort, and timeout are fixed when a session is created.

Push the verified machine/model documentation before production startup. After startup, update the deployment section with the final readiness result and exact running configuration, and push that final documentation update too. Distinguish the running runtime-code commit from a later documentation-only commit if they differ.

## 11. Start and verify the production server

Confirm every temporary daemon has stopped. Use section 8's unit installation with:

```bash
export RC_UNIT_NAME='remote-claude'
export RC_SERVICE_CONFIG="$RC_PROD_CONFIG"
```

Then verify:

```bash
systemctl --user is-enabled remote-claude.service
systemctl --user is-active remote-claude.service
systemctl --user show remote-claude.service -p MainPID -p ActiveEnterTimestamp -p ExecStart
journalctl --user -u remote-claude.service -n 80 --no-pager
.venv/bin/remote-claude-client --config "$HOME/.config/remote-claude/ec2-admin-client.json" list
```

Require a live PID, successful pinned-version startup, no repeating authentication/network errors, and a freshly published `_server/status.json` on the production branch with the configured version and server instance. `active` alone is not readiness evidence. Use the admin client's synchronized mailbox cache or GitHub to inspect the heartbeat. Keep synthetic model tests on temporary branches; no production prompt is required merely to verify startup after the completed daemon test.

Normal operation:

```bash
systemctl --user status remote-claude.service --no-pager
journalctl --user -u remote-claude.service -f
systemctl --user stop remote-claude.service
systemctl --user start remote-claude.service
systemctl --user restart remote-claude.service
```

These are separate operating actions, not a sequence to run after deployment. Stopping cancels active turns; schedule normal restarts when work is idle when possible. The application also writes `server.log` and per-request diagnostics below the recorded state directory. If startup rate limiting follows a configuration error, fix the cause, then use `systemctl --user reset-failed remote-claude.service` and start it again.

Preserve the SQLite ledger, outbox, native Claude histories, and workspaces across restarts and upgrades. Never delete state to clear an error. For an `unknown` request, establish that the old worker has stopped, inspect partial changes, and use `client recover SESSION_ID --acknowledge` as documented in `USAGE.md`; continuation requires a new prompt. Publication failures retry the stored result and do not rerun Claude. Before a code/CLI upgrade, stop the service cleanly, take a consistent state backup, install/test the selected revision, and restart using the same state. A code rollback after a schema change may require its matching backup; do not blindly run older code against a changed ledger.

## 12. Final handoff from the EC2 agent

Return the pushed commit IDs and confirm all of the following with actual evidence:

- The existing Bedrock authentication still works; the persistent service uses it without interactive login.
- Intended model/effort choices passed real noninteractive tool and resume checks, and `USAGE.md` tells the client exactly what to use.
- Offline tests and the temporary GitHub smoke/daemon checks passed, or any unresolved failure is explicitly identified.
- Every successful test branch was deleted; any retained failed branch has a specific reason and cleanup command.
- Production is running under the recorded account/unit, owns the intended mailbox, and has published a fresh heartbeat.
- The actual production config uses the agreed 60/30/10-second adaptive policy and the documented concurrency.
- Source and English operating documentation are pushed to `main`; credentials and generated data remain local and ignored.

If a required gate cannot be satisfied, leave the working Bedrock installation intact, retain local diagnostics, and identify the exact blocker and next action. Do not label an interactive-only configuration or an untested model as a completed unattended deployment.
