"""Non-interactive Claude Code subprocess adapter with bounded local logging."""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import threading
import time
from typing import Callable


CORE_TOOLS = "Read,Write,Edit,Glob,Grep,Bash,WebFetch,WebSearch"
_LOG_BYTES = 2 * 1024 * 1024
_MAX_LINE_BYTES = 2 * 1024 * 1024
_MAX_ANSWER_BYTES = 512 * 1024
_GRACE_SECONDS = 0.3


def _short_text(value, maximum: int = 1024) -> str:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    encoded = text.encode("utf-8", errors="replace")
    if len(encoded) <= maximum:
        return text
    return encoded[: max(0, maximum - 16)].decode("utf-8", errors="ignore") + " [truncated]"


def _summarize_denials(value) -> list[dict]:
    """Keep diagnostic identities without copying denied file bodies to Git."""
    if not isinstance(value, list):
        return []
    records = []
    for item in value[:20]:
        if not isinstance(item, dict):
            continue
        details = {}
        tool_input = item.get("tool_input")
        if isinstance(tool_input, dict):
            for key, content in list(tool_input.items())[:8]:
                if key.lower() in {"content", "text", "data", "image", "images", "base64"}:
                    raw = (content if isinstance(content, str) else json.dumps(content)).encode("utf-8", errors="replace")
                    details[_short_text(key, 80)] = {"omitted_bytes": len(raw), "sha256": sha256(raw).hexdigest()}
                else:
                    details[_short_text(key, 80)] = _short_text(content, 512)
        else:
            details = {"description": _short_text(tool_input, 512)}
        records.append({"tool_name": _short_text(item.get("tool_name", "unknown"), 128),
                        "tool_use_id": _short_text(item.get("tool_use_id", ""), 128),
                        "tool_input_summary": details})
    return records


@dataclass(frozen=True)
class RunSpec:
    request_id: str
    session_id: str
    claude_session_id: str
    workspace: Path
    prompt: str
    model: str
    effort: str
    timeout_seconds: float
    resume: bool
    run_dir: Path
    executable: str = "claude"
    max_budget_usd: float | None = None


@dataclass(frozen=True)
class RunOutcome:
    status: str
    answer: str
    error: str | None
    metadata: dict
    session_initialized: bool
    pid: int | None
    process_start: str | None


def process_start_identity(pid: int) -> str | None:
    """Read Linux /proc start ticks, guarding against PID reuse on this boot."""
    try:
        text = Path(f"/proc/{pid}/stat").read_text()
        fields = text[text.rfind(")") + 2 :].split()
        return fields[19]
    except (OSError, IndexError):
        return None


class _RotatingLog:
    def __init__(self, path: Path, limit: int = _LOG_BYTES):
        self.path = path
        self.limit = limit
        self.handle = path.open("wb")
        self.size = 0

    def write(self, data: bytes) -> None:
        while data:
            available = self.limit - self.size
            if available == 0:
                self.handle.close()
                for suffix in (2, 1):
                    source = self.path if suffix == 1 else self.path.with_name(self.path.name + ".1")
                    target = self.path.with_name(self.path.name + f".{suffix}")
                    if source.exists():
                        os.replace(source, target)
                self.handle = self.path.open("wb")
                self.size = 0
                available = self.limit
            chunk, data = data[:available], data[available:]
            self.handle.write(chunk)
            self.handle.flush()
            self.size += len(chunk)

    def close(self) -> None:
        self.handle.close()


def _child_environment() -> dict[str, str]:
    environment = os.environ.copy()
    environment.update({
        "DISABLE_AUTOUPDATER": "1",
        "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
        "CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1",
        "CLAUDE_CODE_DISABLE_BACKGROUND_TASKS": "1",
        "GIT_TERMINAL_PROMPT": "0",
        "GCM_INTERACTIVE": "Never",
        "SSH_ASKPASS_REQUIRE": "never",
        "NO_COLOR": "1",
        "PAGER": "cat",
        "CI": "1",
    })
    return environment


def check_claude(executable: str, expected_version: str = "2.1.300", timeout: float = 20) -> dict:
    """Inspect version/help without running a model, login, or interactive doctor."""
    report: dict = {"ok": False, "executable": executable, "expected_version": expected_version,
                    "version": None, "missing_flags": [], "errors": []}
    outputs: dict[str, str] = {}
    for flag in ("--version", "--help"):
        try:
            result = subprocess.run([executable, flag], stdin=subprocess.DEVNULL,
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                    timeout=timeout, check=False, env=_child_environment())
            if result.returncode:
                report["errors"].append(f"{flag} exited with code {result.returncode}")
            outputs[flag] = result.stdout[:262144].decode("utf-8", errors="replace")
        except (OSError, subprocess.TimeoutExpired) as exc:
            report["errors"].append(f"{flag}: {type(exc).__name__}")
    match = re.search(r"\b(\d+\.\d+\.\d+)\b", outputs.get("--version", ""))
    if match:
        report["version"] = match.group(1)
    if report["version"] != expected_version:
        report["errors"].append(f"Claude Code {expected_version} is required; found {report['version'] or 'unknown'}")
    required = ["--print", "--output-format", "--verbose", "--model", "--effort",
                "--permission-mode", "--permission-prompts", "--session-id", "--resume",
                "--setting-sources", "--settings", "--strict-mcp-config", "--mcp-config",
                "--tools", "--disable-slash-commands", "--no-chrome", "--append-system-prompt",
                "--max-budget-usd", "--system-prompt-snapshot"]
    report["missing_flags"] = [flag for flag in required if flag not in outputs.get("--help", "")]
    if report["missing_flags"]:
        report["errors"].append("Required non-interactive CLI flags are unavailable")
    report["ok"] = not report["errors"]
    return report


def _signal_group(pid: int, sig: signal.Signals) -> bool:
    try:
        os.killpg(pid, sig)
        return True
    except ProcessLookupError:
        return False


def _group_exists(pid: int) -> bool:
    try:
        os.killpg(pid, 0)
        return True
    except ProcessLookupError:
        return False


def _stop_group(process: subprocess.Popen, interrupt: bool = True) -> None:
    sequence = (signal.SIGINT, signal.SIGTERM, signal.SIGKILL) if interrupt else (
        signal.SIGTERM, signal.SIGKILL)
    for sig in sequence:
        if not _signal_group(process.pid, sig):
            break
        deadline = time.monotonic() + _GRACE_SECONDS
        while time.monotonic() < deadline:
            process.poll()
            if not _group_exists(process.pid):
                break
            time.sleep(0.02)
        if not _group_exists(process.pid):
            break
    try:
        process.wait(timeout=1)
    except subprocess.TimeoutExpired:
        pass


def run_claude(
    spec: RunSpec,
    cancel_event: threading.Event,
    on_start: Callable[[int, str | None], None] | None = None,
) -> RunOutcome:
    """Run one turn, never using a terminal or leaving an approval host open."""
    if cancel_event.is_set():
        return RunOutcome("cancelled", "", "Cancelled before execution", {}, False, None, None)
    run_dir = Path(spec.run_dir)
    run_dir.mkdir(parents=True, exist_ok=True)
    export_dir = run_dir / "exports"
    export_dir.mkdir(exist_ok=True)
    instruction = (
        "This turn is executed unattended. Do not wait for human interaction or use interactive "
        "programs. If information is missing, return a concise question as your final answer. "
        "Export only small static PNG, JPEG or WebP images intended for the user directly into "
        f"this turn's export directory: {export_dir.resolve()}. "
        "Do not export data files, archives, videos, model weights, or inline base64 data. "
        "Keep each image below 2 MiB, at most 10 images and 10 MiB total. "
        "Leave large data in the workspace and mention its server path. External HTTP(S) links "
        "may appear in your Markdown answer. Exported images will receive relative links automatically."
    )
    args = [spec.executable, "-p", "--output-format", "stream-json", "--verbose",
            "--model", spec.model, "--effort", spec.effort,
            "--permission-mode", "auto", "--permission-prompts", "none",
            "--setting-sources", "", "--settings", '{"disableAllHooks":true,"enabledPlugins":{}}',
            "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}',
            "--tools", CORE_TOOLS, "--disable-slash-commands", "--no-chrome",
            "--system-prompt-snapshot", "off",
            "--append-system-prompt", instruction,
            "--resume" if spec.resume else "--session-id", spec.claude_session_id]
    if spec.max_budget_usd is not None:
        args.extend(["--max-budget-usd", str(spec.max_budget_usd)])
    metadata: dict = {"claude_session_id": spec.claude_session_id, "model": spec.model,
                      "effort": spec.effort, "permission_denials": [], "malformed_events": 0,
                      "oversized_events": 0, "resume": spec.resume}
    try:
        process = subprocess.Popen(args, cwd=spec.workspace, env=_child_environment(),
                                   stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   start_new_session=True, bufsize=0)
    except OSError as exc:
        return RunOutcome("failed", "", f"Could not start Claude: {exc}", metadata,
                          False, None, None)
    identity = process_start_identity(process.pid)
    if on_start:
        try:
            on_start(process.pid, identity)
        except Exception:
            _stop_group(process)
            for stream in (process.stdin, process.stdout, process.stderr):
                if stream:
                    stream.close()
            raise
    state: dict = {"result": None, "initialized": False, "last_text": "", "io_errors": []}
    started = time.monotonic()

    def record_io_error(message: str) -> None:
        if len(state["io_errors"]) < 20:
            state["io_errors"].append(message[:500])

    def mark_initialized() -> None:
        if state["initialized"]:
            return
        temporary = run_dir / ".session-initialized.tmp"
        with temporary.open("w", encoding="utf-8") as handle:
            json.dump({"claude_session_id": spec.claude_session_id}, handle)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, run_dir / "session-initialized.json")
        directory_fd = os.open(run_dir, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
        state["initialized"] = True

    def persist_full_answer(answer: str) -> None:
        temporary = run_dir / ".answer.full.tmp"
        with temporary.open("w", encoding="utf-8") as handle:
            handle.write(answer)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, run_dir / "answer.full.md")

    def event(line: bytes) -> None:
        if not line.strip():
            return
        try:
            value = json.loads(line)
        except (ValueError, UnicodeDecodeError):
            metadata["malformed_events"] += 1
            return
        if not isinstance(value, dict):
            metadata["malformed_events"] += 1
            return
        reported_session = value.get("session_id")
        if reported_session is not None and reported_session != spec.claude_session_id:
            record_io_error("Claude returned a different session ID")
            return
        if value.get("type") == "system" and value.get("subtype") == "init":
            mark_initialized()
            if "model" in value:
                metadata["actual_model"] = _short_text(value["model"], 256)
        elif value.get("type") == "assistant":
            message = value.get("message", {})
            blocks = message.get("content", []) if isinstance(message, dict) else []
            if isinstance(blocks, list):
                text = "\n".join(block.get("text", "") for block in blocks
                                 if isinstance(block, dict) and block.get("type") == "text"
                                 and isinstance(block.get("text"), str))
                if text:
                    state["last_text"] = text.encode("utf-8")[:_MAX_ANSWER_BYTES].decode("utf-8", errors="ignore")
        elif value.get("type") == "result":
            state["result"] = value
            if isinstance(value.get("result"), str):
                persist_full_answer(value["result"])
            if value.get("subtype") == "success" and not value.get("is_error"):
                mark_initialized()

    def consume(stream, path: Path, parse: bool) -> None:
        pending = bytearray()
        discarding = False
        log = None
        try:
            log = _RotatingLog(path)
            while chunk := stream.read(65536):
                log.write(chunk)
                if not parse:
                    continue
                for index, piece in enumerate(chunk.split(b"\n")):
                    if index:
                        if not discarding:
                            event(bytes(pending))
                        pending.clear()
                        discarding = False
                    if not discarding:
                        if len(pending) + len(piece) > _MAX_LINE_BYTES:
                            metadata["oversized_events"] += 1
                            pending.clear()
                            discarding = True
                        else:
                            pending.extend(piece)
            if parse and pending and not discarding:
                event(bytes(pending))
        except (OSError, ValueError) as exc:
            record_io_error(f"{path.name}: {type(exc).__name__}")
        finally:
            if log:
                log.close()
            stream.close()

    def write_prompt() -> None:
        try:
            view = memoryview(spec.prompt.encode("utf-8"))
            while view:
                written = process.stdin.write(view)
                if not written:
                    break
                view = view[written:]
        except (BrokenPipeError, OSError, ValueError):
            pass
        finally:
            process.stdin.close()

    threads = [
        threading.Thread(target=consume, args=(process.stdout, run_dir / "stdout.jsonl", True), daemon=True),
        threading.Thread(target=consume, args=(process.stderr, run_dir / "stderr.log", False), daemon=True),
        threading.Thread(target=write_prompt, daemon=True),
    ]
    for thread in threads:
        thread.start()
    forced_status: str | None = None
    try:
        while process.poll() is None:
            if cancel_event.is_set():
                forced_status = "cancelled"
                break
            if time.monotonic() - started >= spec.timeout_seconds:
                forced_status = "timed_out"
                break
            time.sleep(0.03)
    finally:
        # Also stop descendants after normal CLI exit: a shell background job
        # must not retain stdout or mutate the project after a completed turn.
        _stop_group(process, interrupt=forced_status is not None)
        for thread in threads:
            thread.join(timeout=2)
            if thread.is_alive():
                record_io_error("A subprocess pipe remained open after cleanup; detached descendants may still exist")
    metadata["exit_code"] = process.poll()
    metadata["duration_seconds"] = round(time.monotonic() - started, 3)
    metadata["io_errors"] = state["io_errors"]
    result = state["result"]
    answer = state["last_text"]
    error: str | None = None
    status = "completed"
    if result:
        if isinstance(result.get("result"), str):
            answer = result["result"].encode("utf-8")[:_MAX_ANSWER_BYTES].decode("utf-8", errors="ignore")
        for key in ("subtype", "is_error", "total_cost_usd", "usage", "modelUsage", "duration_ms",
                    "duration_api_ms", "num_turns"):
            if key in result:
                encoded = json.dumps(result[key], ensure_ascii=False).encode("utf-8")
                metadata[key] = result[key] if len(encoded) <= 8192 else {"truncated": True}
        metadata["permission_denials"] = _summarize_denials(result.get("permission_denials", []))
        if isinstance(result.get("permission_denials"), list) and len(result["permission_denials"]) > 20:
            metadata["permission_denials_omitted"] = len(result["permission_denials"]) - 20
        raw_errors = result.get("errors", [])
        metadata["errors"] = [_short_text(item, 512) for item in raw_errors[:10]] if isinstance(raw_errors, list) else [_short_text(raw_errors, 512)]
        if result.get("is_error") or result.get("subtype") != "success":
            status = "failed"
            error = _short_text("; ".join(metadata["errors"]), 4096)
            error = error or _short_text(result.get("subtype", "Claude reported an error"), 512)
        if result.get("permission_denials"):
            status = "failed"
            error = "permission_denied: one or more tool operations were denied"
    elif process.returncode:
        status = "failed"
        error = f"Claude exited with code {process.returncode} without a final result"
    else:
        status = "unknown"
        error = "Claude exited without a valid final result; execution may have had side effects"
    if process.returncode and status == "completed":
        status = "failed"
        error = f"Claude exited with code {process.returncode}"
    if state["io_errors"]:
        status = "unknown"
        error = "; ".join(state["io_errors"])
    if forced_status:
        status = forced_status
        error = "Turn cancelled" if status == "cancelled" else "Turn exceeded its execution timeout"
    return RunOutcome(status, answer, error, metadata, state["initialized"], process.pid, identity)
