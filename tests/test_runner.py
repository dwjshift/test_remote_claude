from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path
import threading
from uuid import uuid4

import pytest

from server.artifacts import collect_artifacts
from server.runner import RunSpec, check_claude, process_start_identity, run_claude


FAKE = Path(__file__).with_name("fake_claude.py").resolve()


@pytest.fixture
def spec(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    state = tmp_path / "fake-state"
    monkeypatch.setenv("FAKE_CLAUDE_STATE", str(state))
    return RunSpec(str(uuid4()), str(uuid4()), str(uuid4()), workspace,
                   '{"fake":"success","answer":"Hello"}', "sonnet", "high", 5, False,
                   tmp_path / "runs" / "first", str(FAKE))


def test_version_check_is_offline_and_detects_incompatible_cli(monkeypatch):
    assert check_claude(str(FAKE))["ok"]
    monkeypatch.setenv("FAKE_CLAUDE_VERSION", "2.1.112")
    monkeypatch.setenv("FAKE_CLAUDE_OMIT_FLAG", "--permission-prompts")
    report = check_claude(str(FAKE))
    assert not report["ok"]
    assert report["version"] == "2.1.112"
    assert "--permission-prompts" in report["missing_flags"]


def test_first_turn_and_resume_use_persistent_session_and_controlled_config(spec):
    starts = []
    first = run_claude(spec, threading.Event(), lambda pid, start: starts.append((pid, start)))
    assert first.status == "completed"
    assert first.answer == "Hello"
    assert first.session_initialized
    assert starts == [(first.pid, first.process_start)]
    assert first.process_start.isdecimal()
    marker = json.loads((spec.run_dir / "session-initialized.json").read_text())
    assert marker["claude_session_id"] == spec.claude_session_id
    second = run_claude(replace(spec, resume=True, request_id=str(uuid4()),
                                run_dir=spec.run_dir.parent / "second"), threading.Event())
    assert second.status == "completed"
    records = [json.loads(line) for line in (spec.workspace.parent / "fake-state" / "invocations.jsonl").read_text().splitlines()]
    assert len(records) == 2
    assert records[0]["cwd"] == str(spec.workspace)
    assert records[1]["resume"] is True
    assert records[0]["session_id"] == records[1]["session_id"] == spec.claude_session_id
    args = records[0]["argv"]
    assert args[args.index("--permission-prompts") + 1] == "none"
    assert args[args.index("--permission-mode") + 1] == "auto"
    assert args[args.index("--setting-sources") + 1] == ""
    assert args[args.index("--system-prompt-snapshot") + 1] == "off"
    assert json.loads(args[args.index("--settings") + 1])["disableAllHooks"]
    assert "--strict-mcp-config" in args
    assert "--bare" not in args
    instruction = args[args.index("--append-system-prompt") + 1]
    assert "GitHub-flavored Markdown" in instruction
    assert "PNG, JPEG or WebP" in instruction
    assert str((spec.run_dir / "exports").resolve()) in instruction
    assert not records[0]["stdin_isatty"]
    assert not records[0]["stdout_isatty"]
    assert records[0]["disable_autoupdater"] == "1"


@pytest.mark.parametrize("scenario,status,error", [
    ("failure", "failed", "Configured fake failure"),
    ("blocked", "failed", "permission_denied"),
    ("malformed", "unknown", "without a valid final result"),
    ("preinit_failure", "failed", "exited with code 1"),
    ("preinit_result_failure", "failed", "Authentication unavailable"),
])
def test_failures_are_explicit(spec, scenario, status, error):
    outcome = run_claude(replace(spec, prompt=json.dumps({"fake": scenario})), threading.Event())
    assert outcome.status == status
    assert error in outcome.error
    if scenario == "blocked":
        assert outcome.metadata["permission_denials"][0]["tool_name"] == "Bash"
    if scenario.startswith("preinit"):
        assert not outcome.session_initialized


def test_bounded_stream_parser_recovers_after_oversized_line_and_rotates_logs(spec):
    outcome = run_claude(replace(spec, prompt='{"fake":"oversize","answer":"After large event"}'), threading.Event())
    assert outcome.status == "completed"
    assert outcome.answer == "After large event"
    assert outcome.metadata["oversized_events"] == 1
    for basename in ("stdout.jsonl", "stderr.log"):
        logs = list(spec.run_dir.glob(basename + "*"))
        assert len(logs) == 3
        assert all(path.stat().st_size <= 2 * 1024 * 1024 for path in logs)


def test_timeout_terminates_process_group_and_persists_init(spec):
    outcome = run_claude(replace(spec, prompt='{"fake":"child"}', timeout_seconds=0.3), threading.Event())
    assert outcome.status == "timed_out"
    assert outcome.session_initialized
    child = int((spec.workspace.parent / "fake-state" / "child.pid").read_text())
    assert_not_running(child)
    assert process_start_identity(outcome.pid) is None


def assert_not_running(pid):
    try:
        text = Path(f"/proc/{pid}/stat").read_text()
    except FileNotFoundError:
        return
    assert text[text.rfind(")") + 2 :].split()[0] == "Z", f"Process {pid} is still running"


def test_cancellation_and_preexecution_cancellation(spec):
    event = threading.Event()
    timer = threading.Timer(0.2, event.set)
    timer.start()
    try:
        outcome = run_claude(replace(spec, prompt='{"fake":"hang"}'), event)
    finally:
        timer.cancel()
    assert outcome.status == "cancelled"
    assert_not_running(outcome.pid)
    before = run_claude(spec, event)
    assert before.status == "cancelled"
    assert before.pid is None


def test_normal_exit_also_stops_background_children(spec):
    outcome = run_claude(replace(spec, prompt='{"fake":"child_after_result"}'), threading.Event())
    assert outcome.status == "completed"
    child = int((spec.workspace.parent / "fake-state" / "child.pid").read_text())
    assert_not_running(child)


def test_export_instructions_produce_small_image(spec):
    outcome = run_claude(replace(spec, prompt='{"image":true}'), threading.Event())
    bundle = collect_artifacts(spec.run_dir / "exports", outcome.answer, remaining_bytes=10000)
    assert outcome.status == "completed"
    assert len(bundle.artifacts) == 1
    assert "artifacts/preview.png" in bundle.answer


def test_start_callback_failure_cleans_up_subprocess(spec):
    captured = []

    def fail(pid, identity):
        captured.append(pid)
        raise RuntimeError("Cannot persist process identity")

    with pytest.raises(RuntimeError, match="Cannot persist"):
        run_claude(replace(spec, prompt='{"fake":"hang"}'), threading.Event(), fail)
    assert_not_running(captured[0])


def test_missing_executable_returns_clear_failure(spec):
    outcome = run_claude(replace(spec, executable="/missing/claude"), threading.Event())
    assert outcome.status == "failed"
    assert outcome.pid is None
    assert "Could not start" in outcome.error


def test_budget_is_passed_through(spec):
    outcome = run_claude(replace(spec, max_budget_usd=0.25), threading.Event())
    assert outcome.status == "completed"
    record = json.loads((spec.workspace.parent / "fake-state" / "invocations.jsonl").read_text())
    assert record["argv"][record["argv"].index("--max-budget-usd") + 1] == "0.25"


def test_full_answer_is_retained_locally_before_memory_limit(spec):
    answer = "a" * 700000
    outcome = run_claude(replace(spec, prompt=json.dumps({"answer": answer})), threading.Event())
    assert outcome.status == "completed"
    assert len(outcome.answer) == 512 * 1024
    assert (spec.run_dir / "answer.full.md").read_text() == answer


def test_denied_file_contents_do_not_escape_via_metadata(spec):
    content = "data:image/png;base64," + "a" * 300000
    prompt = json.dumps({"fake": "blocked", "answer": "Denied", "denied_content": content})
    outcome = run_claude(replace(spec, prompt=prompt), threading.Event())
    assert outcome.status == "failed"
    metadata = json.dumps(outcome.metadata)
    assert len(metadata) < 10000
    assert "data:image" not in metadata
    denial = outcome.metadata["permission_denials"][0]
    assert denial["tool_name"] == "Write"
    assert denial["tool_input_summary"]["content"]["omitted_bytes"] == len(content)


def test_long_errors_are_summarized_for_publication(spec):
    prompt = json.dumps({"fake": "failure", "answer": "Failed", "errors": ["x" * 300000]})
    outcome = run_claude(replace(spec, prompt=prompt), threading.Event())
    assert outcome.status == "failed"
    assert len(outcome.error.encode()) <= 4096
    assert len(json.dumps(outcome.metadata)) < 10000


def test_mixed_assistant_content_preserves_markdown_and_reports_only_unsupported_blocks(spec):
    payload = "private-unsupported-payload:" + "a" * 10000
    answer = "# Result\n\n**Ready**: [reference](https://example.com/) \u2713\n"
    content = [
        {"type": "text", "text": "Intermediate text"},
        {"type": "image", "source": {"type": "base64", "data": payload}},
        {"type": "image", "source": {"url": payload}},
        {"type": "audio", "data": payload},
        {"type": "tool_use", "name": "Read", "input": {"file_path": payload}},
        {"type": "server_tool_use", "name": "WebSearch", "input": {"query": payload}},
        {"type": "tool_result", "content": payload},
        {"type": "thinking", "thinking": payload},
        {"type": "redacted_thinking", "data": payload},
    ]
    prompt = json.dumps({"assistant_messages": [content], "answer": answer})
    outcome = run_claude(replace(spec, prompt=prompt), threading.Event())
    assert outcome.status == "completed"
    assert outcome.error is None
    assert outcome.answer == answer
    assert {item["content_type"]: item["count"] for item in outcome.metadata["output_warnings"]} == {
        "image": 2, "audio": 1,
    }
    assert all(item["code"] == "unsupported_content" for item in outcome.metadata["output_warnings"])
    assert payload not in json.dumps(outcome.metadata)
    assert payload not in outcome.answer
    records = (spec.workspace.parent / "fake-state" / "invocations.jsonl").read_text().splitlines()
    assert len(records) == 1


def test_nontext_only_assistant_content_finishes_with_warning_metadata(spec):
    prompt = json.dumps({"assistant_messages": [[{"type": "image", "data": "hidden-image"}]],
                         "answer": ""})
    outcome = run_claude(replace(spec, prompt=prompt), threading.Event())
    assert outcome.status == "completed"
    assert outcome.answer == ""
    assert outcome.metadata["output_warnings"] == [
        {"code": "unsupported_content", "content_type": "image", "count": 1},
    ]


@pytest.mark.parametrize("result,expected", [(None, "Usable assistant text"), ("", "")])
def test_unsupported_final_payload_keeps_assistant_text_but_explicit_empty_text_wins(spec, result, expected):
    prompt = json.dumps({"assistant_messages": [[{"type": "text", "text": "Usable assistant text"}]],
                         "answer": result})
    outcome = run_claude(replace(spec, prompt=prompt), threading.Event())
    assert outcome.status == "completed"
    assert outcome.answer == expected
    assert bool(outcome.metadata["output_warnings"]) == (result is None)
    assert (spec.run_dir / "answer.full.md").read_text() == expected


@pytest.mark.parametrize("content,count", [
    ([{"type": "text", "text": 42}, None, "unexpected", {}, {"type": ["image"]},
      {"type": "<script>unsafe-name</script>"}, {"type": "x" * 10000}], 7),
    (None, 1),
    ("unexpected content shape", 1),
    ({"payload": "unexpected content shape"}, 1),
])
def test_malformed_assistant_content_is_visible_without_parser_failure(spec, content, count):
    prompt = json.dumps({"assistant_messages": [content], "answer": "Final text"})
    outcome = run_claude(replace(spec, prompt=prompt), threading.Event())
    assert outcome.status == "completed"
    assert outcome.answer == "Final text"
    assert outcome.metadata["output_warnings"] == [
        {"code": "unsupported_content", "content_type": "unknown", "count": count},
    ]


@pytest.mark.parametrize("result", [
    [{"type": "text", "text": "**Supported text**"}, {"type": "image", "data": "hidden-image"}],
    {"content": [{"type": "text", "text": "**Supported text**"},
                 {"type": "image", "data": "hidden-image"}]},
])
def test_structured_final_result_extracts_text_without_serializing_nontext(spec, result):
    outcome = run_claude(replace(spec, prompt=json.dumps({"answer": result})), threading.Event())
    assert outcome.status == "completed"
    assert outcome.answer == "**Supported text**"
    assert outcome.metadata["output_warnings"] == [
        {"code": "unsupported_content", "content_type": "image", "count": 1},
    ]
    assert "hidden-image" not in json.dumps(outcome.metadata)
    assert (spec.run_dir / "answer.full.md").read_text() == "**Supported text**"


@pytest.mark.parametrize("result,answer,warnings", [
    ({"type": "text", "text": "A text block"}, "A text block", []),
    ({"type": "image", "data": "hidden-image"}, "",
     [{"code": "unsupported_content", "content_type": "image", "count": 1}]),
])
def test_single_content_block_final_result(spec, result, answer, warnings):
    outcome = run_claude(replace(spec, prompt=json.dumps({"answer": result})), threading.Event())
    assert outcome.status == "completed"
    assert outcome.answer == answer
    assert outcome.metadata["output_warnings"] == warnings
    assert "hidden-image" not in json.dumps(outcome.metadata)


@pytest.mark.parametrize("result", [
    {"payload": "hidden-unknown-result"},
    17,
    None,
    [{"type": "text", "text": {"payload": "hidden-unknown-result"}}],
])
def test_unrecognized_final_result_is_reported_without_copying_raw_data(spec, result):
    outcome = run_claude(replace(spec, prompt=json.dumps({"answer": result})), threading.Event())
    assert outcome.status == "completed"
    assert outcome.answer == ""
    assert outcome.metadata["output_warnings"] == [
        {"code": "unsupported_content", "content_type": "unknown", "count": 1},
    ]
    assert "hidden-unknown-result" not in json.dumps(outcome.metadata)


def test_unsupported_content_warnings_bound_type_count_and_omit_unsafe_names(spec):
    payload = "private-content:" + "b" * 20000
    content = [{"type": f"custom_{index}", "payload": payload} for index in range(40)]
    content.extend([{"type": "<script>unsafe-name</script>", "payload": payload},
                    {"type": "x" * 10000, "payload": payload},
                    {"type": "custom_0", "payload": payload}])
    prompt = json.dumps({"assistant_messages": [content], "answer": "Done"})
    outcome = run_claude(replace(spec, prompt=prompt), threading.Event())
    warnings = outcome.metadata["output_warnings"]
    assert outcome.status == "completed"
    assert len(warnings) <= 17
    assert sum(item["count"] for item in warnings) == len(content)
    assert any(item["content_type"] == "other" for item in warnings)
    assert next(item["count"] for item in warnings if item["content_type"] == "custom_0") == 2
    assert all(set(item) == {"code", "content_type", "count"} for item in warnings)
    serialized = json.dumps(outcome.metadata)
    assert len(serialized) < 10000
    assert "unsafe-name" not in serialized
    assert payload not in serialized
    assert outcome.answer == "Done"
