#!/usr/bin/env python3
"""Offline CLI fixture. Its protocol and state are intentionally inspectable."""

from __future__ import annotations

import base64
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import time


FLAGS = """--print --output-format --verbose --model --effort --permission-mode
--permission-prompts --session-id --resume --setting-sources --settings
--strict-mcp-config --mcp-config --tools --disable-slash-commands --no-chrome
--append-system-prompt --max-budget-usd --system-prompt-snapshot"""


def option(name: str, default: str = "") -> str:
    try:
        return sys.argv[sys.argv.index(name) + 1]
    except (ValueError, IndexError):
        return default


def emit(value: dict) -> None:
    print(json.dumps(value), flush=True)


def main() -> int:
    if "--version" in sys.argv:
        print(os.environ.get("FAKE_CLAUDE_VERSION", "2.1.300") + " (Claude Code fake)")
        return 0
    if "--help" in sys.argv:
        print(FLAGS.replace(os.environ.get("FAKE_CLAUDE_OMIT_FLAG", "__none__"), ""))
        return 0
    if sys.argv[1:3] == ["auth", "status"]:
        emit({"loggedIn": os.environ.get("FAKE_CLAUDE_AUTH", "true") == "true",
              "authMethod": "offline-test", "apiProvider": "fake"})
        return 0
    prompt = sys.stdin.read()
    try:
        scenario = json.loads(prompt)
        if not isinstance(scenario, dict):
            scenario = {}
    except ValueError:
        scenario = {}
    mode = scenario.get("fake", "success")
    session_id = option("--resume") or option("--session-id")
    state_dir = Path(os.environ["FAKE_CLAUDE_STATE"]) if "FAKE_CLAUDE_STATE" in os.environ else None
    if state_dir:
        state_dir.mkdir(parents=True, exist_ok=True)
        record = {"pid": os.getpid(), "argv": sys.argv[1:], "cwd": os.getcwd(), "prompt": prompt,
                  "session_id": session_id, "resume": "--resume" in sys.argv,
                  "stdin_isatty": sys.stdin.isatty(), "stdout_isatty": sys.stdout.isatty(),
                  "disable_autoupdater": os.environ.get("DISABLE_AUTOUPDATER")}
        fd = os.open(state_dir / "invocations.jsonl", os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            os.write(fd, (json.dumps(record) + "\n").encode())
        finally:
            os.close(fd)
        if mode == "preinit_failure":
            print("Authentication unavailable", file=sys.stderr)
            return 1
        if mode == "preinit_result_failure":
            emit({"type": "result", "subtype": "error_during_execution", "is_error": True,
                  "session_id": session_id, "errors": ["Authentication unavailable"]})
            return 1
        session = state_dir / f"session-{session_id}.json"
        if "--resume" in sys.argv and not session.exists():
            emit({"type": "result", "subtype": "error_during_execution", "is_error": True,
                  "session_id": session_id, "errors": ["Unknown fake session"]})
            return 1
        if "--session-id" in sys.argv and session.exists():
            emit({"type": "result", "subtype": "error_during_execution", "is_error": True,
                  "session_id": session_id, "errors": ["Fake session ID is already in use"]})
            return 1
        session.write_text(json.dumps({"prompt": prompt}))
    if mode == "preinit_failure":
        print("Authentication unavailable", file=sys.stderr)
        return 1
    emit({"type": "system", "subtype": "init", "session_id": session_id,
          "model": option("--model", "sonnet")})
    if scenario.get("delay"):
        time.sleep(float(scenario["delay"]))
    if mode in ("hang", "child"):
        signal.signal(signal.SIGINT, signal.SIG_IGN)
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        if mode == "child":
            child = subprocess.Popen([sys.executable, "-c",
                                      "import signal,time;signal.signal(signal.SIGINT,signal.SIG_IGN);"
                                      "signal.signal(signal.SIGTERM,signal.SIG_IGN);time.sleep(120)"])
            if state_dir:
                (state_dir / "child.pid").write_text(str(child.pid))
        while True:
            time.sleep(1)
    if mode == "malformed":
        print("this is not a JSON event", flush=True)
        return 0
    if mode == "oversize":
        sys.stdout.write("x" * (7 * 1024 * 1024) + "\n")
        sys.stdout.flush()
        sys.stderr.write("e" * (7 * 1024 * 1024))
        sys.stderr.flush()
    if mode == "child_after_result":
        child = subprocess.Popen([sys.executable, "-c", "import time;time.sleep(120)"])
        if state_dir:
            (state_dir / "child.pid").write_text(str(child.pid))
    for content in scenario.get("assistant_messages", []):
        emit({"type": "assistant", "session_id": session_id, "message": {"content": content}})
    if scenario.get("image"):
        match = re.search(r"export directory: (.*?)\. Do not export", option("--append-system-prompt"))
        if match:
            export_dir = Path(match.group(1))
            export_dir.mkdir(parents=True, exist_ok=True)
            # A real static 1x1 PNG; no Pillow dependency in the fake subprocess.
            (export_dir / "preview.png").write_bytes(base64.b64decode(
                "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="))
    denied = [{"tool_name": "Bash", "tool_use_id": "fake-tool", "tool_input": {"command": "blocked"}}] if mode == "blocked" else []
    if mode == "blocked" and "denied_content" in scenario:
        denied[0] = {"tool_name": "Write", "tool_use_id": "fake-tool",
                     "tool_input": {"file_path": "denied.txt", "content": scenario["denied_content"]}}
    failed = mode == "failure"
    result = {"type": "result", "subtype": "error_during_execution" if failed else "success",
              "session_id": session_id, "is_error": failed,
              "result": scenario.get("answer", "Fake answer: " + prompt),
              "permission_denials": denied, "num_turns": 1, "total_cost_usd": 0,
              "usage": {"input_tokens": 5, "output_tokens": 5}}
    if failed:
        result["errors"] = scenario.get("errors", ["Configured fake failure"])
    emit(result)
    return int(failed)


if __name__ == "__main__":
    raise SystemExit(main())
