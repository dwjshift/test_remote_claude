"""Create sessions and exchange prompts through an outbound-only Git mailbox."""

from __future__ import annotations

import argparse
import json
import math
import sys
import time
from pathlib import Path
from typing import Any

from common.config import ConfigError, DEFAULT_CONFIG_PATH, load_config, save_config
from common.git_transport import GitError, GitMailbox, ImmutableConflict
from common.protocol import (
    ProtocolError, TERMINAL_STATES, canonical_json, new_id, utc_now, validate_control,
    validate_id, validate_request, validate_session,
)


class ClientError(ValueError):
    """An operation cannot be submitted in the current session state."""


def _message_bytes(value: dict) -> bytes:
    payload = canonical_json(value)
    if len(payload) > 1024 * 1024:
        raise ClientError("A mailbox JSON message must not exceed 1 MiB")
    return payload


def _read_json(path: Path, root: Path) -> dict:
    _safe_file(path, root)
    if path.stat().st_size > 1024 * 1024:
        raise ClientError("A mailbox JSON message must not exceed 1 MiB")
    try:
        result = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ClientError(f"Cannot read mailbox JSON {path.relative_to(root)}: {exc}") from exc
    if not isinstance(result, dict):
        raise ClientError(f"Mailbox JSON must contain an object: {path.relative_to(root)}")
    return result


def _safe_file(path: Path, root: Path) -> None:
    try:
        parts = path.relative_to(root).parts
    except ValueError as exc:
        raise ClientError("A mailbox path points outside the checkout") from exc
    current = root
    for part in parts:
        current = current / part
        if current.is_symlink():
            raise ClientError("Mailbox files must not be symlinks")
    if not path.resolve().is_relative_to(root.resolve()):
        raise ClientError("A mailbox path points outside the checkout")
    if not path.is_file():
        raise ClientError("A mailbox file is missing or is not a regular file")


def _mailbox(config: dict, create: bool = False) -> GitMailbox:
    return GitMailbox(
        remote=config["remote"], branch=config["branch"],
        path=Path(config["state_dir"]) / "mailbox", create=create,
        git_timeout=config.get("git_timeout", 30),
    )


def _session(mailbox: GitMailbox, session_id: str) -> tuple[Path, dict, dict]:
    validate_id(session_id)
    directory = mailbox.path / "sessions" / session_id
    settings_file = directory / "settings.json"
    if not settings_file.is_file():
        raise ClientError(f"Session does not exist: {session_id}")
    settings = validate_session(_read_json(settings_file, mailbox.path))
    if settings["session_id"] != session_id:
        raise ProtocolError("Session settings ID does not match its directory")
    status_file = directory / "status.json"
    status = _read_json(status_file, mailbox.path) if status_file.is_file() else {
        "session_id": session_id, "revision": 0, "state": "new", "active_request_id": None,
    }
    revision = status.get("revision", 0)
    if type(revision) is not int or revision < 0:
        raise ProtocolError("Session status revision must be a nonnegative integer")
    if status.get("session_id", session_id) != session_id:
        raise ProtocolError("Session status ID does not match its directory")
    if not isinstance(status.get("state"), str):
        raise ProtocolError("Session status state must be a string")
    if status.get("active_request_id") is not None:
        validate_id(status["active_request_id"])
    return directory, settings, status


def _read_result(path: Path, mailbox: GitMailbox, session_id: str, request_id: str) -> dict:
    result = _read_json(path, mailbox.path)
    if result.get("session_id") != session_id or result.get("request_id") != request_id:
        raise ProtocolError("Response IDs do not match their mailbox path")
    status = result.get("status")
    if not isinstance(status, str) or status not in TERMINAL_STATES:
        raise ProtocolError("Response status must be a terminal state")
    return result


def _pending(directory: Path, mailbox: GitMailbox) -> list[str]:
    pending = []
    for path in sorted((directory / "requests").glob("*.json")):
        request = validate_request(_read_json(path, mailbox.path))
        request_id = request["request_id"]
        if request["session_id"] != directory.name or request_id != path.stem:
            raise ProtocolError("Request IDs do not match their mailbox path")
        result_path = directory / "responses" / request_id / "result.json"
        if not result_path.exists():
            pending.append(request_id)
            continue
        result = _read_result(result_path, mailbox, directory.name, request_id)
        if result.get("status") not in TERMINAL_STATES:
            pending.append(request_id)
    return pending


def _prompt(args: argparse.Namespace, required: bool = True) -> str | None:
    if args.prompt_file is not None:
        if args.prompt_file == "-":
            value = sys.stdin.read()
        else:
            value = Path(args.prompt_file).expanduser().read_text(encoding="utf-8")
    else:
        value = args.prompt
    if value is None:
        if required:
            raise ClientError("Provide --prompt or --prompt-file (use - for stdin)")
        return None
    if not value.strip():
        raise ClientError("The prompt must not be empty")
    return value


def _make_request(session_id: str, request_id: str, revision: int, prompt: str) -> dict:
    return validate_request({
        "schema_version": 1, "session_id": session_id, "request_id": request_id,
        "expected_revision": revision, "prompt": prompt, "created_at": utc_now(),
    })


def _request_result(request: dict, head: str) -> dict:
    return {
        "session_id": request["session_id"], "request_id": request["request_id"],
        "expected_revision": request["expected_revision"], "head": head,
    }


def _existing_request(
    directory: Path, mailbox: GitMailbox, request_id: str,
    prompt: str, expected_revision: int | None,
) -> dict | None:
    path = directory / "requests" / f"{request_id}.json"
    if not path.exists():
        return None
    request = validate_request(_read_json(path, mailbox.path))
    if request["session_id"] != directory.name or request["request_id"] != request_id:
        raise ProtocolError("Request IDs do not match their mailbox path")
    if request["prompt"] != prompt or (
        expected_revision is not None and request["expected_revision"] != expected_revision
    ):
        raise ImmutableConflict("The request ID is already associated with different content")
    return request


def _check_sendable(directory: Path, mailbox: GitMailbox, status: dict) -> None:
    if status.get("state") in {"closed", "closing", "unknown", "frozen"}:
        raise ClientError(f"Session cannot accept prompts while {status['state']}")
    pending = _pending(directory, mailbox)
    if status.get("active_request_id") or pending:
        raise ClientError("The session already has an unfinished request; wait, cancel, or recover it")


def _init(args: argparse.Namespace) -> dict:
    path = Path(args.config).expanduser().resolve()
    config = load_config(path) if path.exists() else {}
    for name in ("remote", "branch", "state_dir", "watch_interval"):
        value = getattr(args, name)
        if value is not None:
            config[name] = value
    if not config.get("remote"):
        raise ClientError("init requires --remote for a new config")
    config.setdefault("branch", "mailbox")
    config.setdefault("state_dir", "state")
    config.setdefault("watch_interval", 10)
    interval = config["watch_interval"]
    if (
        isinstance(interval, bool) or not isinstance(interval, (int, float))
        or not math.isfinite(interval) or interval <= 0
    ):
        raise ConfigError("watch_interval must be a positive finite number")
    state_dir = Path(config["state_dir"]).expanduser()
    if not state_dir.is_absolute():
        state_dir = path.parent / state_dir
    config["state_dir"] = str(state_dir.resolve())
    mailbox = _mailbox(config, create=True)
    config["remote"] = mailbox.remote
    save_config(path, config)
    return {"config": str(path), "branch": mailbox.branch, "head": mailbox.head}


def _new(args: argparse.Namespace, config: dict) -> dict:
    mailbox = _mailbox(config)
    session_id = validate_id(args.session_id or new_id())
    args.session_id = session_id
    settings = validate_session({
        "schema_version": 1, "session_id": session_id, "workspace": args.workspace,
        "model": args.model, "effort": args.effort, "timeout_seconds": args.timeout,
        "created_at": utc_now(),
    })
    directory = mailbox.path / "sessions" / session_id
    prefix = f"sessions/{session_id}"
    settings_path = directory / "settings.json"
    if settings_path.exists():
        existing = validate_session(_read_json(settings_path, mailbox.path))
        if any(existing.get(key) != value for key, value in settings.items() if key != "created_at"):
            raise ImmutableConflict("The session ID is already associated with different settings")
        settings = existing
    files = {f"{prefix}/settings.json": _message_bytes(settings)}
    prompt = _prompt(args, required=False)
    if args.request_id and prompt is None:
        raise ClientError("--request-id requires an initial prompt")
    request = None
    if prompt is not None:
        request_id = validate_id(args.request_id or new_id())
        args.request_id = request_id
        request = _existing_request(directory, mailbox, request_id, prompt, 0)
        if request is None:
            if settings_path.exists():
                _, _, status = _session(mailbox, session_id)
                _check_sendable(directory, mailbox, status)
                if status.get("revision", 0) != 0:
                    raise ClientError("Use send to add prompts to an existing conversation")
            request = _make_request(session_id, request_id, 0, prompt)
        files[f"{prefix}/requests/{request_id}.json"] = _message_bytes(request)
    head = mailbox.publish(files, f"Create session {session_id}")
    if request is not None:
        return _request_result(request, head)
    return {"session_id": session_id, "head": head}


def _send(args: argparse.Namespace, config: dict) -> dict:
    mailbox = _mailbox(config)
    directory, _, status = _session(mailbox, args.session_id)
    prompt = _prompt(args)
    request_id = validate_id(args.request_id or new_id())
    args.request_id = request_id
    existing = _existing_request(directory, mailbox, request_id, prompt, args.expected_revision)
    if existing is not None:
        return _request_result(existing, mailbox.head)
    _check_sendable(directory, mailbox, status)
    revision = status.get("revision", 0)
    if args.expected_revision is not None:
        if args.expected_revision != revision:
            raise ClientError(f"Expected revision {args.expected_revision}, but the session is at {revision}")
        revision = args.expected_revision
    request = _make_request(args.session_id, request_id, revision, prompt)
    head = mailbox.publish(
        {f"sessions/{args.session_id}/requests/{request_id}.json": _message_bytes(request)},
        f"Submit request {request_id}",
    )
    return _request_result(request, head)


def _list(config: dict) -> list[dict]:
    mailbox = _mailbox(config)
    sessions = []
    for path in sorted((mailbox.path / "sessions").glob("*/settings.json")):
        directory, settings, status = _session(mailbox, path.parent.name)
        sessions.append({
            **status, "session_id": directory.name, "workspace": settings["workspace"],
            "model": settings["model"], "created_at": settings["created_at"],
        })
    return sessions


def _show(mailbox: GitMailbox, session_id: str, request_id: str | None = None) -> dict:
    directory, settings, status = _session(mailbox, session_id)
    if request_id is not None:
        validate_id(request_id)
    requests = []
    for path in sorted((directory / "requests").glob("*.json")):
        if request_id is not None and path.stem != request_id:
            continue
        request = validate_request(_read_json(path, mailbox.path))
        if request["session_id"] != session_id or request["request_id"] != path.stem:
            raise ProtocolError("Request IDs do not match their mailbox path")
        requests.append(request)
    requests.sort(key=lambda item: (item.get("created_at", ""), item.get("request_id", "")))
    if request_id is not None and not requests:
        raise ClientError(f"Request does not exist: {request_id}")
    responses = []
    for request in requests:
        response_dir = directory / "responses" / validate_id(request["request_id"])
        result_path = response_dir / "result.json"
        if result_path.is_file():
            result = _read_result(result_path, mailbox, session_id, request["request_id"])
            answer_path = response_dir / "answer.md"
            if answer_path.is_file():
                _safe_file(answer_path, mailbox.path)
                result = {
                    **result, "answer": answer_path.read_text(encoding="utf-8"),
                    "answer_path": str(answer_path),
                }
            responses.append(result)
    control_results = [
        _read_json(path, mailbox.path)
        for path in sorted((directory / "controls" / "results").glob("*.json"))
    ]
    return {
        "settings": settings, "status": status, "requests": requests,
        "responses": responses, "control_results": control_results,
    }


def _control(args: argparse.Namespace, config: dict) -> dict:
    mailbox = _mailbox(config)
    directory, _, status = _session(mailbox, args.session_id)
    control_id = validate_id(args.control_id or new_id())
    args.control_id = control_id
    name = f"sessions/{args.session_id}/controls/{control_id}.json"
    existing_path = mailbox.path / name
    existing = validate_control(_read_json(existing_path, mailbox.path)) if existing_path.exists() else None
    if existing is not None and existing["kind"] != args.command:
        raise ImmutableConflict("The control ID is already associated with a different command")
    control = {
        "schema_version": 1, "session_id": args.session_id,
        "control_id": control_id, "kind": args.command, "created_at": utc_now(),
    }
    if args.command == "cancel":
        request_id = args.request_id or (existing or {}).get("request_id") or status.get("active_request_id")
        if request_id is None:
            pending = _pending(directory, mailbox)
            if len(pending) == 1:
                request_id = pending[0]
        if request_id is None:
            raise ClientError("No unambiguous active request; provide --request-id")
        control["request_id"] = validate_id(request_id)
    elif args.command == "recover":
        control["acknowledge"] = args.acknowledge
    validate_control(control)
    if existing is not None:
        if any(existing.get(key) != value for key, value in control.items() if key != "created_at"):
            raise ImmutableConflict("The control ID is already associated with different content")
        control = existing
    head = mailbox.publish({name: _message_bytes(control)}, f"Submit {args.command} control {control_id}")
    return {"session_id": args.session_id, "control_id": control_id, "kind": args.command, "head": head}


def _emit(value: Any, as_json: bool, command: str) -> None:
    if as_json:
        sys.stdout.write(canonical_json(value).decode("utf-8"))
    elif command == "list":
        for item in value:
            print(f"{item['session_id']}\t{item.get('state', 'new')}\trevision={item.get('revision', 0)}\t{item['workspace']}")
    elif command in {"show", "watch"}:
        status = value["status"]
        print(f"Session {status['session_id']}: {status.get('state', 'new')} (revision {status.get('revision', 0)})")
        for response in value["responses"]:
            print(f"\nRequest {response.get('request_id', '?')}: {response.get('status', '?')}")
            if "answer" in response:
                print(response["answer"], end="" if response["answer"].endswith("\n") else "\n")
                print(f"Saved answer: {response['answer_path']}")
        for result in value["control_results"]:
            print(f"Control {result.get('control_id', '?')}: {result.get('state', result.get('status', '?'))}")
    else:
        for key, item in value.items():
            print(f"{key}: {item}")
    sys.stdout.flush()


def _watch(args: argparse.Namespace, config: dict) -> int:
    mailbox = _mailbox(config)
    interval = args.interval if args.interval is not None else config["watch_interval"]
    if not math.isfinite(interval) or interval <= 0:
        raise ClientError("--interval must be positive")
    if args.timeout is not None and (not math.isfinite(args.timeout) or args.timeout <= 0):
        raise ClientError("--timeout must be positive")
    deadline = time.monotonic() + args.timeout if args.timeout is not None else None
    previous = None
    while True:
        snapshot = _show(mailbox, args.session_id, args.request_id)
        encoded = canonical_json(snapshot)
        if encoded != previous:
            _emit(snapshot, args.json, "watch")
            previous = encoded
        if args.once:
            return 0
        if args.request_id is not None and any(
            result.get("request_id") == args.request_id and result.get("status") in TERMINAL_STATES
            for result in snapshot["responses"]
        ):
            return 0
        if snapshot["status"].get("state") == "closed":
            return 0
        if deadline is not None and time.monotonic() >= deadline:
            return 0
        delay = min(interval, max(0, deadline - time.monotonic())) if deadline is not None else interval
        time.sleep(delay)
        mailbox.sync()


def _common_arguments(parser: argparse.ArgumentParser, suppress_defaults: bool = False) -> None:
    parser.add_argument(
        "--config", default=argparse.SUPPRESS if suppress_defaults else str(DEFAULT_CONFIG_PATH),
        help="Path to the JSON configuration file",
    )
    parser.add_argument(
        "--json", action="store_true", default=argparse.SUPPRESS if suppress_defaults else False,
        help="Print machine-readable JSON (JSON Lines for watch)",
    )


def _prompt_arguments(parser: argparse.ArgumentParser) -> None:
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--prompt", help="Prompt text")
    group.add_argument("--prompt-file", help="UTF-8 prompt file, or - to read stdin")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, prog="remote-claude-client")
    _common_arguments(parser)
    commands = parser.add_subparsers(dest="command", required=True)

    init = commands.add_parser("init", help="Create or connect to the dedicated mailbox branch")
    _common_arguments(init, True)
    init.add_argument("--remote", help="Git remote URL or local bare repository path")
    init.add_argument("--branch", help="Mailbox branch (default: mailbox)")
    init.add_argument("--state-dir", help="Local state directory, relative to the config file")
    init.add_argument("--watch-interval", type=float)

    new = commands.add_parser("new", help="Create a session, optionally including its first prompt")
    _common_arguments(new, True)
    new.add_argument("--workspace", required=True, help="Server directory such as '~/projects/example'")
    new.add_argument("--session-id", help="Stable session ID for retrying creation (timestamp ID or legacy UUID)")
    new.add_argument("--request-id", help="Stable initial request ID (timestamp ID or legacy UUID)")
    new.add_argument("--model", default="sonnet")
    new.add_argument("--effort", default="high")
    new.add_argument("--timeout", "--timeout-seconds", type=float, default=3600, dest="timeout")
    _prompt_arguments(new)

    send = commands.add_parser("send", help="Submit the next prompt in a session")
    _common_arguments(send, True)
    send.add_argument("session_id")
    send.add_argument("--request-id", help="Stable request ID for safe submission retries (timestamp ID or legacy UUID)")
    send.add_argument("--expected-revision", type=int)
    _prompt_arguments(send)

    listing = commands.add_parser("list", help="List sessions and their latest published status")
    _common_arguments(listing, True)

    for command in ("show", "watch"):
        sub = commands.add_parser(command, help=f"{command.capitalize()} session responses and status")
        _common_arguments(sub, True)
        sub.add_argument("session_id")
        sub.add_argument("--request-id")
        if command == "watch":
            sub.add_argument("--interval", type=float)
            sub.add_argument("--timeout", type=float, help="Stop watching after this many seconds")
            sub.add_argument("--once", action="store_true", help="Print one snapshot and exit")

    for command in ("cancel", "close", "recover"):
        sub = commands.add_parser(command, help=f"Submit a {command} control message")
        _common_arguments(sub, True)
        sub.add_argument("session_id")
        sub.add_argument("--control-id", help="Stable control ID (timestamp ID or legacy UUID)")
        if command == "cancel":
            sub.add_argument("--request-id", help="Request to cancel (defaults to the active request)")
        if command == "recover":
            sub.add_argument(
                "--acknowledge", action="store_true", required=True,
                help="Acknowledge possible partial side effects; close the interrupted turn without replaying it",
            )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "init":
            result = _init(args)
        else:
            config = load_config(args.config)
            if args.command == "watch":
                return _watch(args, config)
            if args.command == "new":
                result = _new(args, config)
            elif args.command == "send":
                result = _send(args, config)
            elif args.command == "list":
                result = _list(config)
            elif args.command == "show":
                result = _show(_mailbox(config), args.session_id, args.request_id)
            else:
                result = _control(args, config)
        _emit(result, args.json, args.command)
        return 0
    except (ClientError, ConfigError, ProtocolError, GitError, OSError) as exc:
        identifiers = {
            key: value for key in ("session_id", "request_id", "control_id")
            if (value := getattr(args, key, None)) is not None
        }
        if args.json:
            sys.stderr.write(canonical_json({"error": str(exc), "type": type(exc).__name__, **identifiers}).decode("utf-8"))
        else:
            print(f"Error: {exc}", file=sys.stderr)
            for key, value in identifiers.items():
                print(f"{key}: {value}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("Interrupted.", file=sys.stderr)
        return 130
