import io
import json
from pathlib import Path
import re
import subprocess
import time
from uuid import UUID, uuid4

import pytest

from client.cli import build_parser, main
from common.config import load_config, save_config
from common.git_transport import GitMailbox
from common.protocol import canonical_json


def assert_timestamp_id(identifier):
    assert re.fullmatch(r"[0-9]{8}T[0-9]{6}-0800_[a-f0-9-]{36}", identifier)
    assert str(UUID(identifier.split("_", 1)[1])) == identifier.split("_", 1)[1]


@pytest.fixture
def client_setup(tmp_path, capsys):
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", "--quiet", str(remote)], check=True)
    config = tmp_path / "client.json"

    def call(*args, success=True):
        code = main(["--config", str(config), "--json", *args])
        captured = capsys.readouterr()
        assert code == (0 if success else 2), captured.err
        return json.loads(captured.out if success else captured.err)

    call("init", "--remote", str(remote))
    return remote, config, call


def test_client_session_prompt_retry_and_published_response(client_setup, tmp_path):
    remote, config, call = client_setup
    created = call("new", "--workspace", "~/projects/demo")
    session_id = created["session_id"]
    assert_timestamp_id(session_id)
    request_id = str(uuid4())
    sent = call("send", session_id, "--prompt", "Inspect the project", "--request-id", request_id)
    retry = call("send", session_id, "--prompt", "Inspect the project", "--request-id", request_id)
    assert retry == sent
    assert call("send", session_id, "--prompt", "Different", "--request-id", request_id, success=False)["type"] == "ImmutableConflict"
    assert "unfinished" in call("send", session_id, "--prompt", "Another", success=False)["error"]

    server = GitMailbox(str(remote), "mailbox", tmp_path / "server")
    prefix = f"sessions/{session_id}"
    result = {"session_id": session_id, "request_id": request_id, "status": "completed", "revision": 1}
    status = {"session_id": session_id, "revision": 1, "state": "idle", "active_request_id": None}
    server.publish({
        f"{prefix}/responses/{request_id}/result.json": canonical_json(result),
        f"{prefix}/responses/{request_id}/answer.md": b"The project has been inspected.\n",
        f"{prefix}/status.json": canonical_json(status),
    }, "Publish result", mutable_paths={f"{prefix}/status.json"})
    shown = call("show", session_id)
    assert shown["responses"][0]["answer"] == "The project has been inspected.\n"
    assert call("list")[0]["revision"] == 1
    assert call("watch", session_id, "--request-id", request_id)["status"]["state"] == "idle"
    next_request = call("send", session_id, "--prompt", "Continue", "--expected-revision", "1")
    assert next_request["expected_revision"] == 1
    assert next_request["request_id"] != request_id
    assert_timestamp_id(next_request["request_id"])


def test_new_can_atomically_include_initial_prompt_from_stdin(client_setup, monkeypatch):
    _, _, call = client_setup
    session_id, request_id = str(uuid4()), str(uuid4())
    monkeypatch.setattr("sys.stdin", io.StringIO("A multiline\nprompt.\n"))
    result = call(
        "new", "--workspace", "~/demo", "--session-id", session_id,
        "--request-id", request_id, "--prompt-file", "-",
    )
    assert result["request_id"] == request_id
    shown = call("show", session_id)
    assert shown["requests"][0]["prompt"] == "A multiline\nprompt.\n"
    retry = call(
        "new", "--workspace", "~/demo", "--session-id", session_id,
        "--request-id", request_id, "--prompt", "A multiline\nprompt.\n",
    )
    assert retry == result


def test_cancel_close_recover_controls_and_watch_once(client_setup):
    _, config, call = client_setup
    created = call("new", "--workspace", "~/demo", "--prompt", "Run something")
    session_id = created["session_id"]
    cancel = call("cancel", session_id)
    close = call("close", session_id)
    recover = call("recover", session_id, "--acknowledge")
    for control in (cancel, close, recover):
        assert_timestamp_id(control["control_id"])
    root = load_config(config)["state_dir"]
    directory = Path(root) / "mailbox" / "sessions" / session_id / "controls"
    cancel_data = json.loads((directory / f"{cancel['control_id']}.json").read_text())
    assert cancel_data["request_id"] == created["request_id"]
    assert json.loads((directory / f"{close['control_id']}.json").read_text())["kind"] == "close"
    assert json.loads((directory / f"{recover['control_id']}.json").read_text())["acknowledge"] is True
    assert call("watch", session_id, "--once")["settings"]["session_id"] == session_id


def test_init_preserves_server_config_and_accepts_global_options_after_command(tmp_path, capsys):
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", "--quiet", str(remote)], check=True)
    path = tmp_path / "config.json"
    save_config(path, {"remote": str(remote), "state_dir": "runtime", "max_workers": 4})
    assert main(["init", "--config", str(path), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["branch"] == "mailbox"
    assert load_config(path)["max_workers"] == 4


def test_parser_requires_recovery_acknowledgement_and_rejects_ambiguous_prompt_options():
    with pytest.raises(SystemExit) as error:
        build_parser().parse_args(["recover", str(uuid4())])
    assert error.value.code == 2
    with pytest.raises(SystemExit):
        build_parser().parse_args(["send", str(uuid4()), "--prompt", "one", "--prompt-file", "two"])


def test_init_will_not_push_main(tmp_path, capsys):
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", "--quiet", str(remote)], check=True)
    config = tmp_path / "config.json"
    assert main(["--config", str(config), "init", "--remote", str(remote), "--branch", "main"]) == 2
    assert "main" in capsys.readouterr().err
    assert not config.exists()


def test_local_remote_from_init_survives_a_change_of_working_directory(tmp_path, capsys, monkeypatch):
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", "--quiet", str(remote)], check=True)
    config = tmp_path / "config.json"
    monkeypatch.chdir(tmp_path)
    assert main(["--config", str(config), "--json", "init", "--remote", "remote.git"]) == 0
    capsys.readouterr()
    other = tmp_path / "elsewhere"
    other.mkdir()
    monkeypatch.chdir(other)
    assert main(["--config", str(config), "--json", "list"]) == 0
    assert json.loads(capsys.readouterr().out) == []


@pytest.mark.parametrize("legacy_session", [False, True])
def test_cli_roundtrip_with_real_engine_and_fake_claude(
    client_setup, tmp_path, workspace, fake_claude, fake_state, legacy_session,
):
    from server.engine import Engine

    remote, client_config, call = client_setup
    identity = ["--session-id", str(uuid4())] if legacy_session else []
    created = call(
        "new", "--workspace", "~/" + str(workspace.relative_to(Path.home())),
        "--prompt", "First turn through the CLI", *identity,
    )
    session_id = created["session_id"]
    if not legacy_session:
        assert_timestamp_id(session_id)
    assert_timestamp_id(created["request_id"])
    session_directory = Path(load_config(client_config)["state_dir"]) / "mailbox" / "sessions" / session_id
    assert (session_directory / "requests" / f"{created['request_id']}.json").is_file()
    config = {
        "remote": str(remote), "state_dir": str(tmp_path / "server-state"),
        "claude_executable": str(fake_claude), "poll_interval": 0, "adaptive_polling": False,
        "heartbeat_interval": 3600,
    }
    with Engine(config) as engine:
        def wait_for_response(request_id):
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                engine.tick()
                snapshot = call("show", session_id, "--request-id", request_id)
                if snapshot["responses"]:
                    return snapshot["responses"][0]
                time.sleep(0.02)
            raise AssertionError("The CLI did not receive an engine response")

        first = wait_for_response(created["request_id"])
        assert first["status"] == "completed"
        assert first["revision"] == 1
        assert Path(first["answer_path"]).parent.name == created["request_id"]
        assert call("watch", session_id, "--request-id", created["request_id"])["responses"][0]["status"] == "completed"
        second = call("send", session_id, "--prompt", "Second turn through the CLI")
        assert_timestamp_id(second["request_id"])
        assert created["request_id"].split("_", 1)[0] <= second["request_id"].split("_", 1)[0]
        assert second["expected_revision"] == 1
        response = wait_for_response(second["request_id"])
        assert response["status"] == "completed"
        assert Path(response["answer_path"]).parent.name == second["request_id"]
        assert response["claude_session_id"] == first["claude_session_id"]
        assert str(UUID(response["claude_session_id"])) == response["claude_session_id"]
        for event in (session_directory / "events").glob("*.json"):
            assert_timestamp_id(event.stem)
        invocations = [json.loads(line) for line in (fake_state / "invocations.jsonl").read_text().splitlines()]
        assert len(invocations) == 2
        assert invocations[1]["resume"] is True
        closed = call("close", session_id)
        assert_timestamp_id(closed["control_id"])
        engine.tick()
        acknowledgements = call("show", session_id)["control_results"]
        assert acknowledgements[0]["control_id"] == closed["control_id"]
        assert acknowledgements[0]["status"] == "closed"
