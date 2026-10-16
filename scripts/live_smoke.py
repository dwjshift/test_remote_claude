#!/usr/bin/env python3
"""Opt-in real Claude smoke, optionally through an isolated GitHub branch.

This script makes billable model calls. With --remote, it creates an orphan
test/remote-claude/<unique-id> mailbox branch and deletes that branch only after
every check succeeds. Failures retain the branch and all local diagnostics.
"""

from __future__ import annotations

import argparse
import base64
from dataclasses import asdict
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import signal
import shutil
import subprocess
import sys
import threading
import time
import traceback
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from common.protocol import TERMINAL_STATES  # noqa: E402
from server.engine import Engine  # noqa: E402
from server.runner import RunSpec, check_claude, run_claude  # noqa: E402


PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)
UNICODE_MARKER = "\u4f60\u597d"


def save_json(path: Path, value: dict) -> None:
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def require(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def git(*arguments: str, cwd: Path = ROOT, check: bool = True) -> subprocess.CompletedProcess:
    environment = os.environ.copy()
    environment.update({"GIT_TERMINAL_PROMPT": "0", "GCM_INTERACTIVE": "Never",
                        "GIT_SSH_COMMAND": "ssh -oBatchMode=yes -oConnectTimeout=10"})
    result = subprocess.run(["git", "-c", "core.hooksPath=/dev/null", *arguments], cwd=cwd,
                            stdin=subprocess.DEVNULL, capture_output=True, text=True,
                            timeout=60, env=environment)
    if check and result.returncode:
        raise RuntimeError(f"Git {arguments[0]} failed: {result.stderr.strip()}")
    return result


def events(path: Path) -> list[dict]:
    parsed = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            value = json.loads(line)
        except ValueError:
            continue
        if isinstance(value, dict):
            parsed.append(value)
    return parsed


def tool_names(values: list[dict]) -> list[str]:
    return [block["name"] for event in values if event.get("type") == "assistant"
            for block in event.get("message", {}).get("content", [])
            if isinstance(block, dict) and block.get("type") == "tool_use"]


def client(config: Path, *arguments: str) -> dict:
    result = subprocess.run([sys.executable, "-m", "client", "--config", str(config),
                             "--json", *arguments], cwd=ROOT, stdin=subprocess.DEVNULL,
                            capture_output=True, text=True, timeout=180)
    with (config.parent / "client-commands.jsonl").open("a", encoding="utf-8") as log:
        log.write(json.dumps({"arguments": arguments, "exit_code": result.returncode,
                              "stdout": result.stdout, "stderr": result.stderr}) + "\n")
    if result.returncode:
        raise RuntimeError(f"Client {arguments[0]} failed: {result.stderr.strip()}")
    return json.loads(result.stdout)


def wait_for_result(engine: Engine, request_id: str, timeout: float) -> dict:
    deadline = time.monotonic() + timeout + 90
    last_notice = time.monotonic()
    while time.monotonic() < deadline:
        engine.tick()
        row = engine.store.get_request(request_id)
        if row and row["state"] in TERMINAL_STATES:
            return json.loads(row["result_json"])
        if row and row["state"] == "unknown":
            raise AssertionError("Execution became unknown; see preserved server state and raw logs")
        if time.monotonic() - last_notice >= 15:
            print(f"Waiting for {request_id}: {row['state'] if row else 'not received'}", flush=True)
            last_notice = time.monotonic()
        time.sleep(0.2)
    raise TimeoutError("The turn did not finish and publish before the smoke deadline")


def denial_probe(arguments, directory: Path, executable: str) -> dict:
    directory.mkdir()
    canary = directory / "protected.txt"
    deny_rule = f"Edit(/{canary})"
    settings = {"disableAllHooks": True, "enabledPlugins": {},
                "permissions": {"deny": [deny_rule]}}
    command = [executable, "-p", "--output-format", "stream-json", "--verbose",
               "--model", arguments.model, "--effort", "low",
               "--permission-mode", "auto", "--permission-prompts", "none",
               "--setting-sources", "", "--settings", json.dumps(settings),
               "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}',
               "--tools", "Read,Write", "--disable-slash-commands", "--no-chrome",
               "--system-prompt-snapshot", "off", "--session-id", str(uuid4()),
               "--max-budget-usd", str(arguments.budget)]
    prompt = (
        "This is a controlled permission-denial test using a disposable synthetic file. "
        f"Attempt the Write tool exactly once to create {canary} containing CANARY. "
        "The explicit policy should refuse that action. Do not use an alternative tool or path. "
        "Then return a short sentence describing whether the write was denied. Do not ask for approval."
    )
    environment = os.environ.copy()
    environment.update({"DISABLE_AUTOUPDATER": "1", "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
                        "CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1", "CLAUDE_CODE_DISABLE_BACKGROUND_TASKS": "1"})
    process = subprocess.Popen(command, cwd=directory, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, start_new_session=True, env=environment)
    started = time.monotonic()
    try:
        stdout, stderr = process.communicate(prompt.encode(), timeout=arguments.timeout)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGKILL)
        stdout, stderr = process.communicate()
        (directory / "stdout.jsonl").write_bytes(stdout)
        (directory / "stderr.log").write_bytes(stderr)
        raise AssertionError("Permission probe waited beyond its deadline")
    (directory / "stdout.jsonl").write_bytes(stdout)
    (directory / "stderr.log").write_bytes(stderr)
    values = events(directory / "stdout.jsonl")
    finals = [value for value in values if value.get("type") == "result"]
    require(bool(finals), "Permission probe produced no structured final result")
    final = finals[-1]
    require(not canary.exists(), "Denied synthetic canary was unexpectedly written")
    require("Write" in tool_names(values), "Permission probe did not attempt the Write tool")
    require(bool(final.get("permission_denials")), "Permission probe did not report a structured denial")
    return {"passed": True, "permission_mode": "auto", "deny_rule": deny_rule,
            "permission_denials": final["permission_denials"], "exit_code": process.returncode,
            "duration_seconds": round(time.monotonic() - started, 3),
            "cost_usd": final.get("total_cost_usd"), "answer": final.get("result")}


def run(arguments, directory: Path, report: dict) -> None:
    selected = shutil.which(arguments.claude_executable) or arguments.claude_executable
    executable = str(Path(selected).expanduser().resolve())
    report["preflight"] = check_claude(executable)
    require(report["preflight"]["ok"], "Claude preflight failed")
    report["claude_executable"] = executable
    report["model"] = arguments.model
    save_json(directory / "report.json", report)
    print(f"Claude {report['preflight']['version']} ready at {executable}", flush=True)
    remote = arguments.remote
    if remote is None:
        remote = str(directory / "remote.git")
        git("init", "--bare", "--quiet", remote)
    report["remote"] = remote
    report["branch"] = branch = f"test/remote-claude/{directory.name}-{uuid4().hex[:8]}"
    report["branch_deleted"] = False
    if arguments.remote:
        before = git("ls-remote", "--heads", remote, "refs/heads/main", "refs/heads/master").stdout
        report["protected_refs_before"] = before
    config = directory / "client.json"
    client(config, "init", "--remote", remote, "--branch", branch,
           "--state-dir", str(directory / "client-state"))
    save_json(directory / "report.json", report)
    workspace = directory / "workspace"
    workspace.mkdir()
    (workspace / "source.png").write_bytes(PNG)
    workspace_arg = "~/" + str(workspace.relative_to(Path.home()))
    token = "synthetic-" + uuid4().hex
    token_file = f"token-{uuid4().hex[:8]}.txt"
    first_prompt = (
        f"Use the Write tool to create {token_file} in the current workspace with exactly {token} "
        "followed by a newline. This is a disposable smoke-test fixture. Do not read other files "
        "or use the network. Remember this filename and token for the next conversation turn. "
        "After the write succeeds, reply briefly with FIRST_OK."
    )
    first = client(config, "new", "--workspace", workspace_arg, "--model", arguments.model,
                   "--effort", "low", "--timeout", str(arguments.timeout), "--prompt", first_prompt)
    session_id, first_id = first["session_id"], first["request_id"]
    report["session_id"] = session_id
    report["requests"] = [first_id]
    recorded: list[dict] = []

    def observed_runner(spec, cancel_event, on_start):
        recorded.append({"request_id": spec.request_id, "claude_session_id": spec.claude_session_id,
                         "resume": spec.resume, "workspace": str(spec.workspace)})
        save_json(directory / "runner-invocations.json", {"invocations": recorded})
        return run_claude(spec, cancel_event, on_start)

    server_config = {"remote": remote, "branch": branch, "state_dir": str(directory / "server-state"),
                     "claude_executable": executable, "claude_version": "2.1.300",
                     "poll_interval": 5 if arguments.remote else 0.2, "heartbeat_interval": 600,
                     "max_workers": 1, "max_budget_usd": arguments.budget}
    save_json(directory / "server.json", server_config)
    with Engine(server_config, runner=observed_runner) as engine:
        print("Running real first turn: Write a synthetic token file", flush=True)
        result = wait_for_result(engine, first_id, arguments.timeout)
        report["first_turn"] = result
        save_json(directory / "report.json", report)
        require(result["status"] == "completed", f"First turn failed: {result.get('error')}")
        require((workspace / token_file).read_text().strip() == token, "First turn did not write the expected token")
        first_events = events(directory / "server-state" / "runs" / first_id / "stdout.jsonl")
        require("Write" in tool_names(first_events), "First turn did not call the Write tool")
        first_view = client(config, "show", session_id, "--request-id", first_id)
        require(first_view["responses"][0]["status"] == "completed", "Client did not read the published first result")
        second_prompt = (
            "Continue the previous turn. Use Read to verify the exact token file you created earlier; "
            "recall its filename from our conversation. Use Bash to copy the supplied source.png "
            "to preview.png directly in this turn's export directory given in your system instructions. "
            "Do not use the network. Your final Markdown must include SECOND_OK, the exact token, "
            f"the text {UNICODE_MARKER}, and [Example](https://example.com). Keep it brief."
        )
        second = client(config, "send", session_id, "--prompt", second_prompt)
        second_id = second["request_id"]
        report["requests"].append(second_id)
        print("Running real resumed turn: Read token and export a small PNG", flush=True)
        result = wait_for_result(engine, second_id, arguments.timeout)
        report["second_turn"] = result
        save_json(directory / "report.json", report)
        require(result["status"] == "completed", f"Second turn failed: {result.get('error')}")
        require(len(recorded) == 2 and not recorded[0]["resume"] and recorded[1]["resume"], "Resume flag was not used")
        require(recorded[0]["claude_session_id"] == recorded[1]["claude_session_id"], "Claude session ID changed")
        second_events = events(directory / "server-state" / "runs" / second_id / "stdout.jsonl")
        require({"Read", "Bash"}.issubset(set(tool_names(second_events))), "Second turn did not call Read and Bash")
        view = client(config, "show", session_id, "--request-id", second_id)
        published = view["responses"][0]
        answer = published["answer"]
        require(all(text in answer for text in ("SECOND_OK", token, UNICODE_MARKER, "https://example.com")),
                "Published answer lost its token, UTF-8 text, or hyperlink")
        require(len(result["artifacts"]) == 1, "Expected exactly one small exported PNG")
        artifact = result["artifacts"][0]
        require(artifact["mime"] == "image/png" and artifact["size"] == len(PNG), "Unexpected exported image")
        artifact_path = Path(published["answer_path"]).parent / artifact["path"]
        require(artifact_path.read_bytes() == PNG, "Published attachment bytes changed")
        report["tool_evidence"] = {"first": tool_names(first_events), "second": tool_names(second_events)}
        report["answer_utf8_bytes"] = len(answer.encode("utf-8"))
        report["image_bytes"] = len(PNG)
    if not arguments.skip_adapter_checks:
        print("Checking a scoped real Write denial in auto mode", flush=True)
        report["permission_probe"] = denial_probe(arguments, directory / "denial-probe", executable)
        save_json(directory / "report.json", report)
        print("Checking real CLI timeout and process-group cleanup", flush=True)
        timeout_spec = RunSpec(str(uuid4()), str(uuid4()), str(uuid4()), workspace,
                               "Return TIMEOUT_TEST without tools.", arguments.model, "low", 0.1,
                               False, directory / "timeout-probe", executable, arguments.budget)
        timeout_result = run_claude(timeout_spec, threading.Event())
        require(timeout_result.status == "timed_out", f"Unexpected timeout result: {timeout_result.status}")
        require(not Path(f"/proc/{timeout_result.pid}").exists(), "Timed-out Claude process is still alive")
        report["timeout_probe"] = asdict(timeout_result)
    report["total_reported_cost_usd"] = sum(
        float(report[key].get("metadata", {}).get("total_cost_usd", 0)) for key in ("first_turn", "second_turn")
    ) + float(report.get("permission_probe", {}).get("cost_usd") or 0)
    if arguments.remote:
        after = git("ls-remote", "--heads", remote, "refs/heads/main", "refs/heads/master").stdout
        report["protected_refs_after"] = after
        require(after == report["protected_refs_before"], "Protected branch refs changed during the smoke")
        git("push", "--delete", "origin", branch, cwd=directory / "client-state" / "mailbox")
        absent = git("ls-remote", "--exit-code", "--heads", remote, f"refs/heads/{branch}", check=False)
        require(absent.returncode == 2, "Test branch deletion could not be confirmed")
        report["branch_deleted"] = True
    report["passed"] = True


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--remote", help="Git URL; omit for an isolated local bare repository")
    default_executable = Path.home() / ".local/share/claude/versions/2.1.300"
    if not default_executable.exists():
        default_executable = Path.home() / ".local/bin/claude"
    parser.add_argument("--claude-executable", default=str(default_executable))
    parser.add_argument("--run-dir", type=Path)
    parser.add_argument("--model", default="sonnet")
    parser.add_argument("--budget", type=float, default=1.0, help="Maximum reported USD budget per Claude call")
    parser.add_argument("--timeout", type=float, default=120)
    parser.add_argument("--skip-adapter-checks", action="store_true",
                        help="Skip denial/timeout probes already verified by a prior local smoke")
    arguments = parser.parse_args()
    if not math.isfinite(arguments.budget) or arguments.budget <= 0:
        parser.error("--budget must be a positive finite number")
    if not math.isfinite(arguments.timeout) or arguments.timeout <= 0:
        parser.error("--timeout must be a positive finite number")
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid4().hex[:8]
    directory = (arguments.run_dir or ROOT / ".runtime" / "live" / run_id).resolve()
    require(directory.is_relative_to(Path.home()), "The smoke workspace must be under the actual account home")
    directory.mkdir(parents=True, exist_ok=False)
    report = {"passed": False, "run_directory": str(directory), "started_at": datetime.now(timezone.utc).isoformat()}
    try:
        run(arguments, directory, report)
    except BaseException as exc:
        report["error"] = f"{type(exc).__name__}: {exc}"
        (directory / "failure.txt").write_text(traceback.format_exc(), encoding="utf-8")
        print(report["error"], file=sys.stderr, flush=True)
        return_code = 1
    else:
        return_code = 0
    report["finished_at"] = datetime.now(timezone.utc).isoformat()
    save_json(directory / "report.json", report)
    print(json.dumps({key: report.get(key) for key in ("passed", "run_directory", "branch",
                     "branch_deleted", "total_reported_cost_usd", "error")}), flush=True)
    return return_code


if __name__ == "__main__":
    raise SystemExit(main())
