"""Exercise the server through its Git mailbox and fake Claude executable."""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
import uuid

import pytest

from common.git_transport import GitMailbox
from common.protocol import canonical_json, utc_now
from server.engine import Engine, EngineError


def new_id() -> str:
    return str(uuid.uuid4())


@dataclass
class Harness:
    client: GitMailbox
    engine: Engine | None
    workspace: Path
    fake_state: Path
    config: dict

    def request(
        self,
        session_id: str,
        prompt: str,
        revision: int = 0,
        request_id: str | None = None,
    ) -> dict:
        return {
            "schema_version": 1,
            "session_id": session_id,
            "request_id": request_id or new_id(),
            "expected_revision": revision,
            "prompt": prompt,
            "created_at": utc_now(),
        }

    def submit(
        self,
        prompt: str = "Hello from the test suite.",
        *,
        workspace: Path | None = None,
        timeout: float = 30,
    ) -> tuple[str, str]:
        session_id = new_id()
        selected = workspace or self.workspace
        settings = {
            "schema_version": 1,
            "session_id": session_id,
            "created_at": utc_now(),
            "workspace": "~/" + str(selected.relative_to(Path.home())),
            "model": "sonnet",
            "effort": "high",
            "timeout_seconds": timeout,
        }
        request = self.request(session_id, prompt)
        self.client.publish(
            {
                f"sessions/{session_id}/settings.json": canonical_json(settings),
                self.request_path(request): canonical_json(request),
            },
            "Create an integration test session",
        )
        return session_id, request["request_id"]

    @staticmethod
    def request_path(request: dict) -> str:
        return (
            f"sessions/{request['session_id']}/requests/"
            f"{request['request_id']}.json"
        )

    def send(self, session_id: str, prompt: str, revision: int) -> str:
        request = self.request(session_id, prompt, revision)
        self.client.publish(
            {self.request_path(request): canonical_json(request)},
            "Send an integration test prompt",
        )
        return request["request_id"]

    def control(self, session_id: str, kind: str, **fields) -> str:
        control_id = new_id()
        control = {
            "schema_version": 1,
            "control_id": control_id,
            "session_id": session_id,
            "kind": kind,
            "created_at": utc_now(),
            **fields,
        }
        self.client.publish(
            {f"sessions/{session_id}/controls/{control_id}.json": canonical_json(control)},
            "Submit an integration test control",
        )
        return control_id

    def wait_until(self, predicate, *, timeout: float = 15):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.engine.tick()
            value = predicate()
            if value:
                return value
            time.sleep(0.02)
        raise AssertionError("The engine did not reach the expected state before timeout")

    def result(self, session_id: str, request_id: str) -> dict:
        relative = f"sessions/{session_id}/responses/{request_id}/result.json"

        def read_result():
            self.client.sync()
            path = self.client.path / relative
            return json.loads(path.read_text()) if path.exists() else None

        return self.wait_until(read_result)

    def answer(self, session_id: str, request_id: str) -> str:
        self.client.sync()
        return (
            self.client.path
            / f"sessions/{session_id}/responses/{request_id}/answer.md"
        ).read_text(encoding="utf-8")

    def control_result(self, session_id: str, control_id: str) -> dict:
        def read_result():
            self.client.sync()
            path = self.client.path / f"sessions/{session_id}/controls/results/{control_id}.json"
            return json.loads(path.read_text()) if path.exists() else None

        return self.wait_until(read_result)

    def invocations(self) -> list[dict]:
        path = self.fake_state / "invocations.jsonl"
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text().splitlines() if line]


@pytest.fixture
def mailbox_setup(bare_remote, tmp_path, workspace, fake_claude, fake_state):
    client = GitMailbox(str(bare_remote), "mailbox", tmp_path / "client", create=True)
    config = {
        "remote": str(bare_remote),
        "branch": "mailbox",
        "state_dir": str(tmp_path / "server"),
        "claude_executable": str(fake_claude),
        "poll_interval": 0,
        "heartbeat_interval": 3600,
        "max_workers": 2,
        "default_timeout": 30,
    }
    return Harness(client, None, workspace, fake_state, config)


@pytest.fixture
def harness(mailbox_setup):
    with Engine(mailbox_setup.config) as engine:
        mailbox_setup.engine = engine
        yield mailbox_setup


def test_two_turns_resume_and_preserve_unicode(harness):
    first_prompt = "Please preserve this user text: \u4f60\u597d\uff0c\u4e16\u754c."
    session_id, first_id = harness.submit(first_prompt)
    first = harness.result(session_id, first_id)
    assert first["status"] == "completed"
    assert "\u4f60\u597d\uff0c\u4e16\u754c" in harness.answer(session_id, first_id)
    assert harness.engine.store.get_session(session_id)["revision"] == 1

    second_id = harness.send(session_id, "Continue this session.", revision=1)
    second = harness.result(session_id, second_id)
    assert second["status"] == "completed"
    assert harness.engine.store.get_session(session_id)["revision"] == 2
    assert len(harness.invocations()) == 2
    serialized = json.dumps(harness.invocations())
    assert "--session-id" in serialized
    assert "--resume" in serialized
    assert str(harness.workspace) in serialized


def test_duplicate_request_does_not_execute_twice(harness):
    session_id, request_id = harness.submit()
    harness.result(session_id, request_id)
    relative = f"sessions/{session_id}/requests/{request_id}.json"
    original = (harness.client.path / relative).read_bytes()
    harness.client.publish({relative: original}, "Retry the same request")
    for _ in range(3):
        harness.engine.tick()
    assert len(harness.invocations()) == 1
    assert harness.engine.store.get_session(session_id)["revision"] == 1


def test_second_server_cannot_use_the_same_state_directory(harness):
    with pytest.raises(EngineError, match="Another server owns"):
        with Engine(harness.config):
            pytest.fail("A second server acquired an already-held state lock")


def test_changed_request_content_is_reported_without_reexecution(harness):
    session_id, request_id = harness.submit("The original prompt.")
    harness.result(session_id, request_id)
    relative = f"sessions/{session_id}/requests/{request_id}.json"
    original = harness.engine.store.get_request(request_id)["body_json"]
    changed = json.loads(original)
    changed["prompt"] = "The edited prompt must never be executed."
    harness.client.publish(
        {relative: canonical_json(changed)},
        "Simulate an external edit to an immutable request",
        mutable_paths={relative},
    )
    harness.engine.tick()
    harness.client.sync()
    events = [
        json.loads(path.read_text())
        for path in (harness.client.path / f"sessions/{session_id}/events").glob("*.json")
    ]
    assert any(event.get("kind") == "immutable_request_conflict" for event in events)
    assert harness.engine.store.get_request(request_id)["body_json"] == original
    assert len(harness.invocations()) == 1
    assert harness.engine.store.get_session(session_id)["revision"] == 1


def test_stale_revision_is_rejected_without_advancing_session(harness):
    session_id, request_id = harness.submit()
    harness.result(session_id, request_id)
    stale_id = harness.send(session_id, "This request uses an outdated revision.", 0)
    assert harness.result(session_id, stale_id)["status"] == "rejected"
    assert harness.engine.store.get_session(session_id)["revision"] == 1
    assert len(harness.invocations()) == 1


@pytest.mark.parametrize("kind", ["missing", "escaping-symlink"])
def test_invalid_workspace_is_rejected_before_execution(harness, tmp_path, kind):
    invalid = harness.workspace / "invalid-workspace"
    if kind == "escaping-symlink":
        invalid.symlink_to(tmp_path, target_is_directory=True)
    session_id, request_id = harness.submit(workspace=invalid)
    assert harness.result(session_id, request_id)["status"] == "rejected"
    assert harness.engine.store.get_session(session_id)["revision"] == 0
    assert not harness.invocations()


def test_competing_requests_have_one_deterministic_winner(harness):
    session_id, first_id = harness.submit()
    harness.result(session_id, first_id)
    requests = [
        harness.request(session_id, "Concurrent request one.", 1),
        harness.request(session_id, "Concurrent request two.", 1),
    ]
    harness.client.publish(
        {harness.request_path(request): canonical_json(request) for request in requests},
        "Submit competing requests in one commit",
    )
    requests.sort(key=lambda request: request["request_id"])
    assert harness.result(session_id, requests[0]["request_id"])["status"] == "completed"
    assert harness.result(session_id, requests[1]["request_id"])["status"] == "rejected"
    assert harness.engine.store.get_session(session_id)["revision"] == 2
    assert len(harness.invocations()) == 2


def test_two_clients_compete_without_losing_either_request(harness, tmp_path):
    session_id, first_id = harness.submit()
    harness.result(session_id, first_id)
    second_client = GitMailbox(
        harness.config["remote"], "mailbox", tmp_path / "second-client"
    )
    requests = [
        harness.request(session_id, "Submitted by the first client.", 1),
        harness.request(session_id, "Submitted by the second client.", 1),
    ]
    for client, request in zip((harness.client, second_client), requests):
        client.publish(
            {harness.request_path(request): canonical_json(request)},
            "Submit from an independent client checkout",
        )
    requests.sort(key=lambda request: request["request_id"])
    assert harness.result(session_id, requests[0]["request_id"])["status"] == "completed"
    assert harness.result(session_id, requests[1]["request_id"])["status"] == "rejected"
    harness.client.sync()
    assert all((harness.client.path / harness.request_path(item)).exists() for item in requests)
    assert len(harness.invocations()) == 2


@pytest.mark.parametrize(
    ("fake_mode", "expected_status"),
    [("failure", "failed"), ("blocked", "failed"), ("hang", "timed_out")],
)
def test_worker_failures_become_published_terminal_results(harness, fake_mode, expected_status):
    timeout = 1 if fake_mode == "hang" else 30
    session_id, request_id = harness.submit(json.dumps({"fake": fake_mode}), timeout=timeout)
    result = harness.result(session_id, request_id)
    assert result["status"] == expected_status
    assert harness.engine.store.get_session(session_id)["revision"] == 1


def test_malformed_output_freezes_session_until_explicit_recovery(harness):
    session_id, request_id = harness.submit(json.dumps({"fake": "malformed"}))
    harness.wait_until(
        lambda: harness.engine.store.get_request(request_id)
        and harness.engine.store.get_request(request_id)["state"] == "unknown"
    )
    assert harness.engine.store.get_session(session_id)["revision"] == 0
    assert len(harness.invocations()) == 1
    harness.control(session_id, "recover", acknowledge=True)
    assert harness.result(session_id, request_id)["status"] == "interrupted"
    assert harness.engine.store.get_session(session_id)["revision"] == 1


def test_auth_failure_before_initialization_does_not_resume_missing_history(harness):
    session_id, request_id = harness.submit(json.dumps({"fake": "preinit_failure"}))
    assert harness.result(session_id, request_id)["status"] == "failed"
    assert harness.engine.store.get_session(session_id)["initialized"] == 0
    next_id = harness.send(session_id, "Try again after authentication is repaired.", 1)
    assert harness.result(session_id, next_id)["status"] == "completed"
    assert len(harness.invocations()) == 2
    assert harness.invocations()[-1]["resume"] is False


def test_recovery_does_not_signal_an_unrelated_reused_pid(harness, monkeypatch):
    session_id, request_id = harness.submit(json.dumps({"fake": "malformed"}))
    harness.wait_until(
        lambda: harness.engine.store.get_request(request_id)
        and harness.engine.store.get_request(request_id)["state"] == "unknown"
    )
    with harness.engine.store.transaction() as connection:
        connection.execute(
            "UPDATE requests SET pid=?,process_start=? WHERE request_id=?",
            (os.getpid(), "0", request_id),
        )

    def unexpected_signal(*args, **kwargs):
        pytest.fail("The server tried to signal a process with a reused PID")

    monkeypatch.setattr("server.engine.os.killpg", unexpected_signal)
    control_id = harness.control(session_id, "cancel", request_id=request_id)
    assert harness.control_result(session_id, control_id)["status"] == "worker_stopped_recovery_required"
    harness.control(session_id, "recover", acknowledge=True)
    assert harness.result(session_id, request_id)["status"] == "interrupted"
    assert harness.engine.store.get_session(session_id)["revision"] == 1


def test_recovery_reloads_durable_session_initialization(harness):
    session_id, request_id = harness.submit(json.dumps({"fake": "malformed"}))
    harness.wait_until(
        lambda: harness.engine.store.get_request(request_id)
        and harness.engine.store.get_request(request_id)["state"] == "unknown"
    )
    with harness.engine.store.transaction() as connection:
        connection.execute("UPDATE sessions SET initialized=0 WHERE session_id=?", (session_id,))
    harness.control(session_id, "recover", acknowledge=True)
    assert harness.result(session_id, request_id)["status"] == "interrupted"
    assert harness.engine.store.get_session(session_id)["initialized"] == 1
    next_id = harness.send(session_id, "Continue using the original native history.", 1)
    assert harness.result(session_id, next_id)["status"] == "completed"
    assert harness.invocations()[-1]["resume"] is True


def test_cancel_running_task(harness):
    session_id, request_id = harness.submit(json.dumps({"fake": "hang"}))
    harness.wait_until(lambda: bool(harness.invocations()))
    harness.control(session_id, "cancel", request_id=request_id)
    assert harness.result(session_id, request_id)["status"] == "cancelled"
    assert harness.engine.store.get_session(session_id)["revision"] == 1


def test_cancel_received_with_initial_request_prevents_execution(harness):
    session_id, request_id = harness.submit()
    harness.control(session_id, "cancel", request_id=request_id)
    assert harness.result(session_id, request_id)["status"] == "cancelled"
    assert not harness.invocations()


def test_overlapping_workspaces_are_serialized(harness):
    child = harness.workspace / "nested"
    child.mkdir()
    first_session, first_id = harness.submit(json.dumps({"fake": "hang"}))
    harness.wait_until(lambda: bool(harness.invocations()))
    second_session, second_id = harness.submit(workspace=child)
    for _ in range(3):
        harness.engine.tick()
    assert len(harness.invocations()) == 1
    harness.control(first_session, "cancel", request_id=first_id)
    assert harness.result(first_session, first_id)["status"] == "cancelled"
    assert harness.result(second_session, second_id)["status"] == "completed"
    assert len(harness.invocations()) == 2


def test_cancel_queued_request_does_not_spawn_a_worker(harness):
    first_session, first_id = harness.submit(json.dumps({"fake": "hang"}))
    harness.wait_until(lambda: bool(harness.invocations()))
    second_session, second_id = harness.submit()
    harness.engine.tick()
    harness.control(second_session, "cancel", request_id=second_id)
    assert harness.result(second_session, second_id)["status"] == "cancelled"
    assert len(harness.invocations()) == 1
    harness.control(first_session, "cancel", request_id=first_id)
    assert harness.result(first_session, first_id)["status"] == "cancelled"


def test_worker_limit_queues_independent_workspaces(harness):
    sessions = []
    for index in range(3):
        directory = harness.workspace / f"project-{index}"
        directory.mkdir()
        sessions.append(
            harness.submit(json.dumps({"fake": "hang"}), workspace=directory)
        )
        harness.engine.tick()
    harness.wait_until(lambda: len(harness.invocations()) >= 2)
    for _ in range(3):
        harness.engine.tick()
    assert len(harness.invocations()) == 2
    first_session, first_id = sessions[0]
    harness.control(first_session, "cancel", request_id=first_id)
    assert harness.result(first_session, first_id)["status"] == "cancelled"
    harness.wait_until(lambda: len(harness.invocations()) == 3)
    for session_id, request_id in sessions[1:]:
        harness.control(session_id, "cancel", request_id=request_id)
        assert harness.result(session_id, request_id)["status"] == "cancelled"


def test_close_cancels_work_and_rejects_new_requests(harness):
    session_id, request_id = harness.submit(json.dumps({"fake": "hang"}))
    harness.wait_until(lambda: bool(harness.invocations()))
    harness.control(session_id, "close")
    assert harness.result(session_id, request_id)["status"] == "cancelled"
    later = harness.send(session_id, "A closed session must reject this request.", 1)
    assert harness.result(session_id, later)["status"] == "rejected"
    assert len(harness.invocations()) == 1


def test_close_during_publication_outage_publishes_a_closed_snapshot(harness, monkeypatch):
    session_id, request_id = harness.submit()
    publish = harness.engine.mailbox.publish

    def unavailable(*args, **kwargs):
        raise RuntimeError("Simulated publication outage")

    monkeypatch.setattr(harness.engine.mailbox, "publish", unavailable)
    harness.wait_until(
        lambda: harness.engine.store.get_request(request_id)
        and harness.engine.store.get_request(request_id)["state"] == "publishing"
    )
    control_id = harness.control(session_id, "close")
    harness.engine.tick()
    monkeypatch.setattr(harness.engine.mailbox, "publish", publish)
    assert harness.result(session_id, request_id)["status"] == "completed"
    assert harness.control_result(session_id, control_id)["status"] == "closed"
    snapshot = json.loads(
        (harness.client.path / f"sessions/{session_id}/status.json").read_text()
    )
    assert snapshot["state"] == "closed"
    assert snapshot["active_request_id"] is None
    assert snapshot["closed"] is True


def test_recovery_and_control_acknowledgment_are_atomic(harness, monkeypatch):
    session_id, request_id = harness.submit(json.dumps({"fake": "malformed"}))
    harness.wait_until(
        lambda: harness.engine.store.get_request(request_id)
        and harness.engine.store.get_request(request_id)["state"] == "unknown"
    )
    finalize = harness.engine._finalize
    raised = False

    def fail_after_commit(*args, **kwargs):
        nonlocal raised
        finalize(*args, **kwargs)
        if kwargs.get("recovery_control") and not raised:
            raised = True
            raise RuntimeError("Simulated server failure after the recovery transaction")

    monkeypatch.setattr(harness.engine, "_finalize", fail_after_commit)
    control_id = harness.control(session_id, "recover", acknowledge=True)
    assert harness.result(session_id, request_id)["status"] == "interrupted"
    assert harness.control_result(session_id, control_id)["status"] == "recovered"
    assert harness.engine.store.get_session(session_id)["revision"] == 1
    assert raised
    assert len(harness.invocations()) == 1


def test_publish_failure_retries_results_without_rerunning(harness, monkeypatch):
    session_id, request_id = harness.submit()
    original_publish = harness.engine.mailbox.publish
    blocked = True

    def failing_publish(*args, **kwargs):
        if blocked:
            raise RuntimeError("Simulated remote publication failure")
        return original_publish(*args, **kwargs)

    monkeypatch.setattr(harness.engine.mailbox, "publish", failing_publish)
    harness.wait_until(
        lambda: harness.engine.store.get_session(session_id)["revision"] == 1
        if harness.engine.store.get_session(session_id)
        else False
    )
    assert len(harness.invocations()) == 1
    blocked = False
    assert harness.result(session_id, request_id)["status"] == "completed"
    assert len(harness.invocations()) == 1


def test_remote_result_is_not_duplicated_after_local_ack_failure(harness, monkeypatch):
    session_id, request_id = harness.submit()
    acknowledge = harness.engine.store.acknowledge
    failed_once = False

    def interrupted_ack(rows):
        nonlocal failed_once
        if any(row.get("terminal_state") == "completed" for row in rows) and not failed_once:
            failed_once = True
            raise RuntimeError("Simulated interruption after a successful Git push")
        return acknowledge(rows)

    monkeypatch.setattr(harness.engine.store, "acknowledge", interrupted_ack)
    assert harness.result(session_id, request_id)["status"] == "completed"
    harness.wait_until(lambda: harness.engine.store.get_request(request_id)["state"] == "completed")
    assert failed_once
    assert len(harness.invocations()) == 1
    assert harness.engine.store.get_session(session_id)["revision"] == 1


def test_pending_result_survives_server_restart(mailbox_setup, monkeypatch):
    with Engine(mailbox_setup.config) as engine:
        mailbox_setup.engine = engine
        session_id, request_id = mailbox_setup.submit()

        def unavailable_remote(*args, **kwargs):
            raise RuntimeError("The remote is unavailable during this server lifetime")

        monkeypatch.setattr(engine.mailbox, "publish", unavailable_remote)
        mailbox_setup.wait_until(
            lambda: engine.store.get_request(request_id)
            and engine.store.get_request(request_id)["state"] == "publishing"
        )
        assert len(mailbox_setup.invocations()) == 1

    with Engine(mailbox_setup.config) as restarted:
        mailbox_setup.engine = restarted
        assert mailbox_setup.result(session_id, request_id)["status"] == "completed"
        assert restarted.store.get_session(session_id)["revision"] == 1
        assert len(mailbox_setup.invocations()) == 1


def test_images_are_published_as_per_turn_artifacts(harness):
    session_id, request_id = harness.submit(json.dumps({"image": True}))
    assert harness.result(session_id, request_id)["status"] == "completed"
    directory = harness.client.path / f"sessions/{session_id}/responses/{request_id}"
    images = list((directory / "artifacts").glob("*.png"))
    assert len(images) == 1
    first_bytes = images[0].read_bytes()
    assert "artifacts/" in harness.answer(session_id, request_id)
    second_id = harness.send(session_id, json.dumps({"image": True}), 1)
    assert harness.result(session_id, second_id)["status"] == "completed"
    assert images[0].read_bytes() == first_bytes
    assert harness.engine.store.image_bytes() == 2 * len(first_bytes)


def test_image_budget_is_persistent_across_sessions_and_restart(mailbox_setup):
    mailbox_setup.config["image_budget_bytes"] = 100
    with Engine(mailbox_setup.config) as engine:
        mailbox_setup.engine = engine
        first_session, first_id = mailbox_setup.submit(json.dumps({"image": True}))
        assert mailbox_setup.result(first_session, first_id)["status"] == "completed"
        first_usage = engine.store.image_bytes()
        assert 0 < first_usage <= 100

    with Engine(mailbox_setup.config) as restarted:
        mailbox_setup.engine = restarted
        second_session, second_id = mailbox_setup.submit(json.dumps({"image": True}))
        assert mailbox_setup.result(second_session, second_id)["status"] == "completed"
        directory = (
            mailbox_setup.client.path
            / f"sessions/{second_session}/responses/{second_id}/artifacts"
        )
        assert not list(directory.glob("*.png"))
        assert restarted.store.image_bytes() == first_usage


def test_dormant_session_resumes_the_original_context(harness):
    session_id, request_id = harness.submit()
    harness.result(session_id, request_id)
    original = harness.engine.store.get_session(session_id)
    with harness.engine.store.transaction() as connection:
        connection.execute(
            "UPDATE sessions SET last_activity=? WHERE session_id=?",
            (time.time() - 90000, session_id),
        )
    harness.engine.tick()
    assert harness.engine.store.get_session(session_id)["state"] == "dormant"
    next_id = harness.send(session_id, "Continue after dormancy.", 1)
    assert harness.result(session_id, next_id)["status"] == "completed"
    updated = harness.engine.store.get_session(session_id)
    assert updated["claude_session_id"] == original["claude_session_id"]
    assert updated["workspace"] == original["workspace"]
    assert updated["revision"] == 2


def _process_is_running(pid: int) -> bool:
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
        return stat[stat.rfind(")") + 2 :].split()[0] != "Z"
    except FileNotFoundError:
        return False


@pytest.mark.parametrize("lose_pid_callback", [False, True])
def test_crash_requires_explicit_recovery_without_replaying_prompt(
    bare_remote, tmp_path, workspace, fake_claude, fake_state, lose_pid_callback
):
    client = GitMailbox(str(bare_remote), "mailbox", tmp_path / "client", create=True)
    config = {
        "remote": str(bare_remote),
        "branch": "mailbox",
        "state_dir": str(tmp_path / "crashed-server"),
        "claude_executable": str(fake_claude),
        "poll_interval": 0,
        "heartbeat_interval": 3600,
        "max_workers": 1,
    }
    setup = Harness(client, None, workspace, fake_state, config)
    session_id, request_id = setup.submit(json.dumps({"fake": "hang"}))
    driver = """
import json
import os
from pathlib import Path
import sys
import time
from server.engine import Engine
from server.runner import run_claude

def lost_callback_runner(spec, cancel_event, on_start):
    return run_claude(spec, cancel_event)

config = json.loads(sys.argv[1])
invocations = Path(sys.argv[2]) / 'invocations.jsonl'
runner = lost_callback_runner if sys.argv[3] == 'lost-callback' else run_claude
with Engine(config, runner=runner) as engine:
    deadline = time.monotonic() + 12
    while time.monotonic() < deadline:
        engine.tick()
        if invocations.exists():
            time.sleep(0.15)
            os._exit(0)
        time.sleep(0.02)
    raise RuntimeError('The fake worker did not start before the crash deadline')
"""
    try:
        completed = subprocess.run(
            [sys.executable, "-c", driver, json.dumps(config), str(fake_state),
             "lost-callback" if lose_pid_callback else "normal"],
            cwd=Path(__file__).resolve().parents[1],
            capture_output=True,
            text=True,
            timeout=20,
        )
        assert completed.returncode == 0, completed.stderr
        assert len(setup.invocations()) == 1
        with Engine(config) as engine:
            resumed = Harness(client, engine, workspace, fake_state, config)
            request = engine.store.get_request(request_id)
            assert request["state"] == "unknown"
            assert engine.store.get_session(session_id)["revision"] == 0
            engine.tick()
            assert len(resumed.invocations()) == 1

            if lose_pid_callback:
                assert request["pid"] is None
                recover_id = resumed.control(session_id, "recover", acknowledge=True)
                assert resumed.control_result(session_id, recover_id)["status"] == "rejected"
                cancel_id = resumed.control(session_id, "cancel", request_id=request_id)
                cancel_result = resumed.control_result(session_id, cancel_id)
                assert cancel_result["status"] == "rejected"
                assert "manually" in cancel_result["error"]
                assert engine.store.get_session(session_id)["revision"] == 0
                pid = resumed.invocations()[0]["pid"]
                os.killpg(pid, signal.SIGKILL)
            else:
                resumed.control(session_id, "cancel", request_id=request_id)
                pid = request["pid"]
            resumed.wait_until(lambda: not _process_is_running(pid))
            resumed.control(session_id, "recover", acknowledge=True)
            assert resumed.result(session_id, request_id)["status"] == "interrupted"
            assert engine.store.get_session(session_id)["revision"] == 1
            assert len(resumed.invocations()) == 1

            next_id = resumed.send(session_id, "Continue after the interrupted turn.", 1)
            assert resumed.result(session_id, next_id)["status"] == "completed"
            assert len(resumed.invocations()) == 2
            assert resumed.invocations()[-1]["resume"] is True
    finally:
        for invocation in setup.invocations():
            pid = invocation.get("pid")
            if pid and _process_is_running(pid):
                try:
                    command = Path(f"/proc/{pid}/cmdline").read_bytes()
                    if str(fake_claude).encode() in command and os.getpgid(pid) == pid:
                        os.killpg(pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
