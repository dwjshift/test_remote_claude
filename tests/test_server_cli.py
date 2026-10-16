"""Check public server startup and shutdown separately from engine fixtures."""

import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
from uuid import uuid4

import pytest

from client.cli import main as client_main
from common.config import load_config, save_config
from common.git_transport import GitMailbox
from server.__main__ import authentication, pinned_executable
from server.engine import EngineError, validate_config


def test_unauthenticated_server_stops_before_consuming_mailbox(tmp_path, fake_claude, monkeypatch, capsys):
    from server.__main__ import main

    config_path = tmp_path / "server.json"
    save_config(config_path, {"remote": str(tmp_path / "missing.git"),
                             "state_dir": "server-state", "claude_executable": str(fake_claude)})
    monkeypatch.setenv("FAKE_CLAUDE_AUTH", "false")
    assert main(["--config", str(config_path), "serve"]) == 1
    assert "not authenticated" in capsys.readouterr().err
    assert not (tmp_path / "server-state" / "ledger.sqlite3").exists()


@pytest.mark.parametrize(
    ("returncode", "payload", "expected"),
    [
        (0, {"loggedIn": True}, True),
        (0, {"loggedIn": False}, False),
        (0, {"loggedIn": "false"}, False),
        (0, [], False),
        (0, None, False),
        (1, {"loggedIn": True}, False),
    ],
)
def test_authentication_requires_successful_object_response(monkeypatch, returncode, payload, expected):
    monkeypatch.setattr(
        subprocess, "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(args, returncode, json.dumps(payload), ""),
    )
    assert authentication("claude")["logged_in"] is expected


def test_authentication_report_excludes_account_details(monkeypatch):
    payload = {
        "loggedIn": True, "authMethod": "claude.ai", "apiProvider": "firstParty",
        "email": "private@example.test", "token": "never-print-this",
    }
    monkeypatch.setattr(
        subprocess, "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(args, 0, json.dumps(payload), ""),
    )
    assert authentication("claude") == {
        "logged_in": True, "auth_method": "claude.ai", "api_provider": "firstParty",
    }


def test_pinned_executable_resolves_symlinks_once(tmp_path, fake_claude):
    link = tmp_path / "claude"
    link.symlink_to(fake_claude)
    assert pinned_executable(str(link)) == str(fake_claude)


def test_documented_config_examples_are_valid_and_keep_separate_state():
    examples = Path(__file__).resolve().parents[1] / "examples"
    client = load_config(examples / "client.json")
    server = validate_config(load_config(examples / "server.json"))
    assert client["state_dir"] != server["state_dir"]
    assert client["branch"] == server["branch"] == "mailbox"
    assert server["claude_version"] == "2.1.300"
    assert server["artifact_limits"]["text_limit"] == 256 * 1024
    assert server["workspace_policy"] == "shared"


def test_workspace_policy_defaults_to_shared_for_existing_configs():
    config = {"remote": "example.git", "state_dir": "server-state"}
    assert validate_config(config)["workspace_policy"] == "shared"
    assert "workspace_policy" not in config


@pytest.mark.parametrize("policy", ["shared", "exclusive"])
def test_workspace_policy_accepts_documented_values(policy):
    config = {"remote": "example.git", "state_dir": "server-state", "workspace_policy": policy}
    assert validate_config(config)["workspace_policy"] == policy


@pytest.mark.parametrize("policy", [None, True, False, 0, "", "Shared", "parallel", [], {}])
def test_workspace_policy_rejects_invalid_values(policy):
    config = {"remote": "example.git", "state_dir": "server-state", "workspace_policy": policy}
    with pytest.raises(EngineError, match="workspace_policy"):
        validate_config(config)


def test_server_cli_sigterm_cancels_worker_and_publishes_result(
    tmp_path, bare_remote, workspace, fake_claude, fake_state, capsys,
):
    project = Path(__file__).resolve().parents[1]
    client_config = tmp_path / "client.json"
    server_config = tmp_path / "server.json"
    assert client_main([
        "--config", str(client_config), "--json", "init", "--remote", str(bare_remote),
        "--state-dir", "client-state",
    ]) == 0
    capsys.readouterr()
    session_id, request_id = str(uuid4()), str(uuid4())
    assert client_main([
        "--config", str(client_config), "--json", "new",
        "--workspace", "~/" + str(workspace.relative_to(Path.home())),
        "--session-id", session_id, "--request-id", request_id,
        "--prompt", '{"fake":"hang"}', "--timeout", "60",
    ]) == 0
    capsys.readouterr()
    save_config(server_config, {
        "remote": str(bare_remote), "state_dir": "server-state",
        "claude_executable": str(fake_claude), "poll_interval": 0, "adaptive_polling": False,
        "heartbeat_interval": 3600,
    })
    process = None
    log = tmp_path / "server-output.log"
    try:
        with log.open("wb") as output:
            process = subprocess.Popen(
                [sys.executable, "-m", "server", "serve", "--config", str(server_config)],
                cwd=project, stdin=subprocess.DEVNULL, stdout=output, stderr=subprocess.STDOUT,
                start_new_session=True,
            )
            deadline = time.monotonic() + 15
            invocations = fake_state / "invocations.jsonl"
            while time.monotonic() < deadline and not invocations.exists():
                if process.poll() is not None:
                    pytest.fail(f"The server exited before starting its worker: {log.read_text()}")
                time.sleep(0.05)
            assert invocations.exists(), log.read_text()
            process.send_signal(signal.SIGTERM)
            assert process.wait(timeout=15) == 0, log.read_text()
        mailbox = GitMailbox(str(bare_remote), "mailbox", tmp_path / "reader")
        result_path = mailbox.path / f"sessions/{session_id}/responses/{request_id}/result.json"
        result = json.loads(result_path.read_text())
        assert result["status"] == "cancelled"
        assert result["revision"] == 1
        assert "Stopping server" in log.read_text()
        assert (tmp_path / "server-state" / "server.log").is_file()
    finally:
        if process is not None and process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=10)
        records = fake_state / "invocations.jsonl"
        if records.exists():
            for line in records.read_text().splitlines():
                pid = json.loads(line)["pid"]
                try:
                    cmdline = Path(f"/proc/{pid}/cmdline").read_bytes()
                    if str(fake_claude).encode() in cmdline:
                        os.killpg(pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                except FileNotFoundError:
                    pass
