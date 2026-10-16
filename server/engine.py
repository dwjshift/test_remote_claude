"""Single-writer orchestration with durable execution and publication state."""

from __future__ import annotations

import base64
from concurrent.futures import ThreadPoolExecutor
import fcntl
import json
import logging
import math
import os
from pathlib import Path
import signal
import threading
import time
from uuid import uuid4

from common.git_transport import GitMailbox
from common.protocol import (ProtocolError, canonical_json, content_hash, new_id, utc_now,
                             validate_control, validate_id, validate_request, validate_session)
from .artifacts import collect_artifacts
from .publication import validate_publication
from .runner import RunOutcome, RunSpec, process_start_identity, run_claude
from .store import Store

LOGGER = logging.getLogger(__name__)


class EngineError(RuntimeError):
    """An unsafe or invalid server configuration prevents operation."""


def resolve_workspace(value: str, home: Path | None = None) -> Path:
    """Resolve a server-home path and reject missing or escaping directories."""
    home = (home or Path.home()).resolve()
    if not isinstance(value, str) or not value.startswith("~/") or "\x00" in value:
        raise EngineError("Workspace must start with ~/ and refer to the server account")
    path = (home / value[2:]).resolve()
    if not path.is_relative_to(home) or not path.is_dir():
        raise EngineError("Workspace must be an existing directory inside the server home")
    return path


def workspaces_overlap(first: str | Path, second: str | Path) -> bool:
    left, right = Path(first), Path(second)
    return left.is_relative_to(right) or right.is_relative_to(left)


def validate_config(config: dict) -> dict:
    selected = dict(config)
    defaults = {"branch": "mailbox", "max_workers": 10, "poll_interval": 60,
                "adaptive_polling": True, "active_poll_interval": 10, "warm_poll_interval": 30,
                "heartbeat_interval": 300, "idle_ttl_seconds": 86400,
                "image_budget_bytes": 500 * 1024 * 1024, "claude_executable": "claude",
                "claude_version": "2.1.300", "retry_interval": 1}
    for name, default in defaults.items():
        selected.setdefault(name, default)
    for required in ("remote", "state_dir", "claude_executable"):
        if not isinstance(selected.get(required), str) or not selected[required]:
            raise EngineError(f"{required} must be a nonempty string")
    for name in ("max_workers", "image_budget_bytes"):
        if type(selected[name]) is not int or selected[name] < (1 if name == "max_workers" else 0):
            raise EngineError(f"Invalid {name}")
    if type(selected["adaptive_polling"]) is not bool:
        raise EngineError("adaptive_polling must be a boolean")
    for name in ("poll_interval", "active_poll_interval", "warm_poll_interval",
                 "heartbeat_interval", "idle_ttl_seconds", "retry_interval"):
        value = selected[name]
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
            raise EngineError(f"{name} must be a nonnegative finite number")
    if selected["adaptive_polling"] and any(selected[name] <= 0 for name in (
            "poll_interval", "active_poll_interval", "warm_poll_interval")):
        raise EngineError("Adaptive polling intervals must be positive")
    if selected.get("max_budget_usd") is not None:
        value = selected["max_budget_usd"]
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
            raise EngineError("max_budget_usd must be a positive finite number")
    limits = selected.get("artifact_limits", {})
    if not isinstance(limits, dict):
        raise EngineError("artifact_limits must be an object")
    for name, value in limits.items():
        if name not in {"text_limit", "image_limit", "image_count", "turn_image_limit"} or type(value) is not int or value < 0:
            raise EngineError(f"Invalid artifact limit: {name}")
    return selected


class Engine:
    """Run one server instance; worker threads never access the Git checkout."""

    def __init__(self, config: dict, runner=run_claude):
        self.config = validate_config(config)
        self.state_dir = Path(self.config["state_dir"]).expanduser().resolve()
        self.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.runner = runner
        self.workers: dict = {}
        self._executor = None
        self._lock = None
        self._started = False
        self._stopping = False
        self._scanned_head = None
        self._schedule_dirty = True
        # A restart may follow a partial batch. Read its controls before any
        # durable queued request can execute, including during fetch outages.
        self._ingest_pending = True
        self._next_idle_sweep = 0.0
        self._last_interaction_monotonic = None
        self._next_sync = self._next_publish = 0.0
        self._sync_failures = self._publish_failures = 0
        self._heartbeat = time.monotonic()
        self.instance_id = str(uuid4())
        self.boot_id = Path("/proc/sys/kernel/random/boot_id").read_text().strip()

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, *_):
        self.shutdown()

    def start(self) -> None:
        if self._started:
            return
        self._lock = (self.state_dir / "server.lock").open("a+b")
        try:
            fcntl.flock(self._lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            self._lock.close()
            self._lock = None
            raise EngineError("Another server owns this state directory") from exc
        try:
            self.store = Store(self.state_dir / "ledger.sqlite3")
            identity = {"remote": self.config["remote"], "branch": self.config["branch"]}
            existing = self.store.get_meta("mailbox")
            if existing is not None and existing != identity:
                raise EngineError("The state directory belongs to a different mailbox")
            self.mailbox = GitMailbox(
                self.config["remote"], self.config["branch"], self.state_dir / "mailbox",
                git_timeout=self.config.get("git_timeout", 30),
            )
            if existing is None:
                if (self.mailbox.path / "_server" / "status.json").exists() or list(self.mailbox.path.glob("sessions/*/responses/*/result.json")):
                    raise EngineError("Mailbox already has execution history; restore its original server state or use a fresh branch")
                self.store.set_meta("mailbox", identity)
            self._scanned_head = self.store.get_meta("ingested_head")
            last_interaction = self.store.get_meta("last_interaction_time")
            if last_interaction is not None:
                self._last_interaction_monotonic = time.monotonic() - max(0, time.time() - last_interaction)
            self._recover_startup()
            self._executor = ThreadPoolExecutor(max_workers=self.config["max_workers"], thread_name_prefix="claude")
            self._started = True
            self._queue_heartbeat()
        except BaseException:
            if hasattr(self, "store"):
                self.store.close()
            fcntl.flock(self._lock.fileno(), fcntl.LOCK_UN)
            self._lock.close()
            self._lock = None
            raise

    def _initialized_on_disk(self, request: dict) -> bool:
        marker = self.state_dir / "runs" / request["request_id"] / "session-initialized.json"
        try:
            session = self.store.get_session(request["session_id"])
            return json.loads(marker.read_text())["claude_session_id"] == session["claude_session_id"]
        except (OSError, ValueError, KeyError, TypeError):
            return False

    def _recover_startup(self) -> None:
        for request in self.store.running_requests():
            initialized = int(self._initialized_on_disk(request))
            with self.store.transaction() as db:
                db.execute("UPDATE requests SET state='unknown' WHERE request_id=?", (request["request_id"],))
                db.execute("UPDATE sessions SET state='unknown', initialized=MAX(initialized,?), error=? WHERE session_id=?",
                           (initialized, "Execution was interrupted; explicit recovery is required", request["session_id"]))
                self._queue_snapshot(db, request["session_id"], "execution_unknown")
        for request in self.store.incomplete_rejections():
            self._finalize_rejection(request)

    def _finalize_rejection(self, request: dict) -> None:
        self._finalize(request["request_id"], RunOutcome(
            "rejected", "", request.get("rejection_error") or "Request was rejected before execution",
            {}, False, None, None,
        ))

    def _mark_interaction(self, db, session_id: str) -> None:
        now = time.time()
        db.execute("INSERT INTO metadata(key,value) VALUES ('last_interaction_time',?) "
                   "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (json.dumps(now),))
        db.execute("UPDATE sessions SET last_activity=? WHERE session_id=?", (now, session_id))
        self._last_interaction_monotonic = time.monotonic()
        self._schedule_dirty = True
        if self.config["adaptive_polling"] and not self._sync_failures:
            self._next_sync = min(self._next_sync, self._last_interaction_monotonic + min(
                self.config["poll_interval"], self.config["active_poll_interval"]))

    def _poll_interval(self) -> float:
        base = self.config["poll_interval"]
        if not self.config["adaptive_polling"]:
            return base
        age = (float("inf") if self._last_interaction_monotonic is None
               else time.monotonic() - self._last_interaction_monotonic)
        if self.workers or self.store.has_live_requests() or age < 300:
            return min(base, self.config["active_poll_interval"])
        if age < 600:
            return min(base, self.config["warm_poll_interval"])
        return base

    def _snapshot(self, session: dict, **overrides) -> dict:
        return {"schema_version": 1, "session_id": session["session_id"],
                "revision": session["revision"], "state": session["state"],
                "active_request_id": session["active_request_id"], "closed": bool(session["closed"]),
                "workspace": session["workspace"], "claude_session_id": session["claude_session_id"],
                "updated_at": utc_now(), "error": session["error"], **overrides}

    def _queue_snapshot(self, db, session_id: str, event: str, **overrides) -> None:
        session = dict(db.execute("SELECT * FROM sessions WHERE session_id=?", (session_id,)).fetchone())
        snapshot = self._snapshot(session, **overrides)
        event_id = new_id()
        status_path = f"sessions/{session_id}/status.json"
        Store.enqueue(db, event_id, {status_path: canonical_json(snapshot),
                      f"sessions/{session_id}/events/{event_id}.json": canonical_json({
                          "schema_version": 1, "event_id": event_id, "kind": event, **snapshot})},
                      mutable={status_path})

    def _queue_heartbeat(self) -> None:
        with self.store.transaction() as db:
            Store.enqueue(db, str(uuid4()), {"_server/status.json": canonical_json({
                "schema_version": 1, "instance_id": self.instance_id, "updated_at": utc_now(),
                "active_workers": len(self.workers), "state": "stopping" if self._stopping else "running",
                "claude_version": self.config["claude_version"],
            })}, mutable={"_server/status.json"})
        self._heartbeat = time.monotonic()

    def _read_json(self, path: Path) -> dict:
        relative = path.relative_to(self.mailbox.path)
        current = self.mailbox.path
        for part in relative.parts:
            current = current / part
            if current.is_symlink():
                raise ProtocolError("Mailbox messages cannot use symbolic links")
        if not path.is_file() or path.stat().st_size > 1024 * 1024:
            raise ProtocolError("Mailbox message must be a regular file of at most 1 MiB")
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise ProtocolError("Mailbox messages must be JSON objects")
        canonical_json(value)
        return value

    def _discover(self) -> None:
        """Ingest only changed inputs, falling back safely for unavailable history."""
        sessions_dir = self.mailbox.path / "sessions"
        if not sessions_dir.exists():
            return
        if sessions_dir.is_symlink():
            raise ProtocolError("The sessions directory cannot be a symbolic link")
        paths = self.mailbox.changed_paths(self._scanned_head, self.mailbox.head)
        settings_paths, request_paths, control_paths = set(), set(), set()
        if paths is None:
            paths = [str(path.relative_to(self.mailbox.path)) for pattern in (
                "*/settings.json", "*/requests/*.json", "*/controls/*.json")
                for path in sessions_dir.glob(pattern)]
        for relative in paths:
            parts = Path(relative).parts
            if len(parts) not in {3, 4} or parts[0] != "sessions":
                continue
            try:
                validate_id(parts[1])
            except ValueError:
                continue
            path = self.mailbox.path / relative
            if not path.is_file():
                # Deletions do not modify the ledger or replay historical work.
                continue
            if len(parts) == 3 and parts[2] == "settings.json":
                settings_paths.add(path)
            elif len(parts) == 4 and parts[3].endswith(".json"):
                if parts[2] == "requests":
                    request_paths.add(path)
                elif parts[2] == "controls":
                    control_paths.add(path)
        for path in sorted(settings_paths):
            sid = path.parent.name
            try:
                settings = self._read_json(path)
                existing = self.store.get_session(sid)
                digest = content_hash(settings)
                if existing:
                    if existing["settings_hash"] != digest:
                        LOGGER.error("Ignoring changed immutable settings for session %s", sid)
                    # Registration may have committed before a failed scan's
                    # late-input backfill. Replaying settings must backfill too.
                    request_paths.update((path.parent / "requests").glob("*.json"))
                    control_paths.update((path.parent / "controls").glob("*.json"))
                    continue
                error = None
                workspace = ""
                try:
                    validate_session(settings)
                    if settings["session_id"] != sid:
                        raise ProtocolError("Session ID does not match its directory")
                    workspace = str(resolve_workspace(settings["workspace"]))
                    if workspaces_overlap(workspace, self.state_dir):
                        raise EngineError("Workspace cannot contain or be inside the server state directory")
                except (ValueError, EngineError) as exc:
                    error = str(exc)
                with self.store.transaction() as db:
                    db.execute("INSERT INTO sessions(session_id,settings_json,settings_hash,workspace,claude_session_id,state,last_activity,created_at,error) VALUES (?,?,?,?,?,?,?,?,?)",
                               (sid, canonical_json(settings).decode(), digest, workspace, str(uuid4()),
                                "invalid" if error else "idle", time.time(), utc_now(), error))
                    self._queue_snapshot(db, sid, "session_created")
                # A request can arrive before its settings. New registration
                # revisits that session's inputs without scanning other sessions.
                request_paths.update((path.parent / "requests").glob("*.json"))
                control_paths.update((path.parent / "controls").glob("*.json"))
                self._schedule_dirty = True
            except ValueError as exc:
                LOGGER.warning("Ignoring invalid session path %s: %s", sid, exc)
        # All requests precede controls so cancellation in the same sync wins.
        for paths_to_ingest, ingest in ((request_paths, self._ingest_request),
                                       (control_paths, self._ingest_control)):
            for path in sorted(paths_to_ingest):
                sid = path.parent.parent.name
                if self.store.get_session(sid):
                    ingest(path, sid)

    def _ingest_request(self, path: Path, sid: str) -> None:
        try:
            rid = validate_id(path.stem)
            body = self._read_json(path)
            digest = content_hash(body)
            existing = self.store.get_request(rid)
            if existing:
                if existing["body_hash"] != digest:
                    key = f"content-conflict:{rid}:{digest}"
                    event_id = new_id()
                    with self.store.transaction() as db:
                        Store.enqueue(db, key, {f"sessions/{sid}/events/{event_id}.json": canonical_json({
                            "kind": "immutable_request_conflict", "request_id": rid,
                            "session_id": sid, "created_at": utc_now(),
                            "error": "An existing request ID was reused with different content"})})
                if not existing["accepted"] and existing["result_json"] is None:
                    self._finalize_rejection(existing)
                return
            session = self.store.get_session(sid)
            error = None
            try:
                validate_request(body)
                if body["request_id"] != rid or body["session_id"] != sid:
                    raise ProtocolError("Request IDs do not match the mailbox path")
                if session["state"] == "invalid":
                    raise ProtocolError(session["error"])
                if session["closed"]:
                    raise ProtocolError("Session is closed")
                if session["active_request_id"]:
                    raise ProtocolError("Session already has an unfinished request")
                if body["expected_revision"] != session["revision"]:
                    raise ProtocolError("expected_revision does not match the server revision")
            except ValueError as exc:
                error = str(exc)
            with self.store.transaction() as db:
                db.execute("INSERT INTO requests(request_id,session_id,body_json,body_hash,state,accepted,received_at,rejection_error) VALUES (?,?,?,?,?,?,?,?)",
                           (rid, sid, canonical_json(body).decode(), digest, "rejected" if error else "queued", int(error is None), utc_now(), error))
                self._mark_interaction(db, sid)
                if error is None:
                    db.execute("UPDATE sessions SET active_request_id=?,state='queued',last_activity=?,error=NULL WHERE session_id=?",
                               (rid, time.time(), sid))
                    self._queue_snapshot(db, sid, "request_accepted")
            if error:
                self._finalize(rid, RunOutcome("rejected", "", error, {}, False, None, None))
        except ValueError as exc:
            LOGGER.warning("Ignoring invalid request %s: %s", path.name, exc)

    def _matching_session_worker_exists(self, request: dict) -> bool:
        """Find a worker whose PID callback was lost during server failure."""
        session = self.store.get_session(request["session_id"])
        session_id = session["claude_session_id"].encode("ascii")
        for item in Path("/proc").iterdir():
            if not item.name.isdigit():
                continue
            try:
                stat = (item / "stat").read_text()
                if stat[stat.rfind(")") + 2:].split()[0] == "Z":
                    continue
                arguments = (item / "cmdline").read_bytes().split(b"\x00")
                if any(
                    argument in {b"--session-id", b"--resume"}
                    and index + 1 < len(arguments)
                    and arguments[index + 1] == session_id
                    for index, argument in enumerate(arguments)
                ):
                    return True
            except (OSError, ValueError, IndexError):
                continue
        return False

    def _process_alive(self, request: dict) -> bool:
        pid = request["pid"]
        if not pid or not request["process_start"] or request.get("process_boot") != self.boot_id:
            return self._matching_session_worker_exists(request)
        identity = process_start_identity(pid)
        if identity is not None and identity != request["process_start"]:
            # The saved process is gone. Its recycled PID and process group
            # belong to unrelated work and must never be inspected as ours.
            return self._matching_session_worker_exists(request)
        # A dead group leader can leave live descendants. A group with this ID
        # cannot be reused while those descendants still belong to it.
        for item in Path("/proc").iterdir():
            if not item.name.isdigit():
                continue
            try:
                stat = (item / "stat").read_text()
                fields = stat[stat.rfind(")") + 2:].split()
                if int(fields[2]) == pid and fields[0] != "Z":
                    return True
            except (OSError, ValueError, IndexError):
                continue
        return self._matching_session_worker_exists(request)

    def _cancel_unknown(self, request: dict) -> None:
        identity = process_start_identity(request["pid"]) if request["pid"] else None
        if (
            not request["pid"] or not request["process_start"]
            or request.get("process_boot") != self.boot_id
            or (identity is not None and identity != request["process_start"])
        ):
            if self._matching_session_worker_exists(request):
                raise EngineError("Worker identity is unavailable; verify and stop the old worker manually before recovery")
            return
        for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGKILL):
            if not self._process_alive(request):
                return
            os.killpg(request["pid"], sig)
            deadline = time.monotonic() + 0.3
            while time.monotonic() < deadline and self._process_alive(request):
                time.sleep(0.02)

    def _cancel(self, request: dict) -> str:
        rid = request["request_id"]
        if request["state"] == "queued":
            self._finalize(rid, RunOutcome("cancelled", "", "Cancelled before execution", {}, False, None, None))
            return "cancelled"
        if request["state"] == "running" and rid in self.workers:
            self.workers[rid][1].set()
            return "cancellation_requested"
        if request["state"] == "unknown":
            self._cancel_unknown(request)
            return "worker_stopped_recovery_required"
        return "already_finished"

    def _ingest_control(self, path: Path, sid: str) -> None:
        try:
            cid = validate_id(path.stem)
            body = self._read_json(path)
            digest = content_hash(body)
            previous = self.store.connection.execute("SELECT body_hash FROM controls WHERE control_id=?", (cid,)).fetchone()
            if previous:
                if previous[0] != digest:
                    LOGGER.error("Ignoring changed immutable control %s", cid)
                return
            response = {"schema_version": 1, "session_id": sid, "control_id": cid, "created_at": utc_now()}
            try:
                validate_control(body)
                if body["control_id"] != cid or body["session_id"] != sid:
                    raise ProtocolError("Control IDs do not match the mailbox path")
                session = self.store.get_session(sid)
                if body["kind"] == "cancel":
                    request = self.store.get_request(body["request_id"])
                    if not request or request["session_id"] != sid:
                        raise ProtocolError("Request has not been accepted by this server")
                    response["status"] = self._cancel(request)
                elif body["kind"] == "close":
                    active = self.store.get_request(session["active_request_id"]) if session["active_request_id"] else None
                    snapshot_overrides = (
                        {"state": "closed", "active_request_id": None}
                        if active and active["result_json"] is not None else {}
                    )
                    with self.store.transaction() as db:
                        db.execute("UPDATE sessions SET closed=1 WHERE session_id=?", (sid,))
                        if not session["active_request_id"]:
                            db.execute("UPDATE sessions SET state='closed' WHERE session_id=?", (sid,))
                        self._queue_snapshot(db, sid, "session_closed", **snapshot_overrides)
                    if active:
                        self._cancel(active)
                    response["status"] = "closed"
                else:
                    request = self.store.get_request(session["active_request_id"]) if session["active_request_id"] else None
                    if not request or request["state"] != "unknown":
                        raise ProtocolError("Session has no unknown execution to recover")
                    if self._process_alive(request):
                        raise ProtocolError("Old worker is still running; cancel it before recovery")
                    response["status"] = "recovered"
                    initialized = bool(session["initialized"]) or self._initialized_on_disk(request)
                    self._finalize(
                        request["request_id"],
                        RunOutcome("interrupted", "", "Interrupted execution was acknowledged; its prompt will not be replayed", {}, initialized, request["pid"], request["process_start"]),
                        recovery_control=(cid, digest, response),
                    )
                    return
            except (ValueError, EngineError, OSError) as exc:
                response.update(status="rejected", error=str(exc))
            with self.store.transaction() as db:
                db.execute("INSERT INTO controls VALUES (?,?,?)", (cid, digest, canonical_json(response).decode()))
                self._mark_interaction(db, sid)
                Store.enqueue(db, f"control:{cid}", {f"sessions/{sid}/controls/results/{cid}.json": canonical_json(response)})
        except ValueError as exc:
            LOGGER.warning("Ignoring invalid control %s: %s", path.name, exc)

    def _schedule(self) -> None:
        self._schedule_dirty = False
        blocked = self.store.blocked_workspaces()
        for request in self.store.queued_requests():
            if len(self.workers) >= self.config["max_workers"]:
                break
            session = self.store.get_session(request["session_id"])
            workspace = session["workspace"]
            if any(workspaces_overlap(workspace, other) for other in blocked):
                continue
            settings = json.loads(session["settings_json"])
            try:
                current = resolve_workspace(settings["workspace"])
                if str(current) != workspace:
                    raise EngineError("Workspace resolved to a different directory since session creation")
            except (OSError, ValueError, EngineError) as exc:
                self._finalize(request["request_id"], RunOutcome("failed", "", str(exc), {}, False, None, None))
                continue
            rid = request["request_id"]
            with self.store.transaction() as db:
                db.execute("UPDATE requests SET state='running',started_at=? WHERE request_id=?", (utc_now(), rid))
                db.execute("UPDATE sessions SET state='running' WHERE session_id=?", (session["session_id"],))
                self._queue_snapshot(db, session["session_id"], "execution_started")
            spec = RunSpec(rid, session["session_id"], session["claude_session_id"], Path(workspace),
                           json.loads(request["body_json"])["prompt"], settings["model"], settings["effort"],
                           settings["timeout_seconds"], bool(session["initialized"]), self.state_dir / "runs" / rid,
                           self.config["claude_executable"], self.config.get("max_budget_usd"))
            event = threading.Event()

            def started(pid, process_start, request_id=rid):
                with self.store.transaction() as db:
                    db.execute("UPDATE requests SET pid=?,process_start=?,process_boot=? WHERE request_id=?",
                               (pid, process_start, self.boot_id, request_id))

            self.workers[rid] = (self._executor.submit(self.runner, spec, event, started), event)
            blocked.append(workspace)

    def _finish_workers(self) -> None:
        for rid, (future, _) in list(self.workers.items()):
            if not future.done():
                continue
            try:
                outcome = future.result()
                self._finalize(rid, outcome)
            except Exception:
                LOGGER.exception("Worker completion is uncertain for request %s", rid)
                request = self.store.get_request(rid)
                with self.store.transaction() as db:
                    db.execute("UPDATE requests SET state='unknown' WHERE request_id=?", (rid,))
                    db.execute("UPDATE sessions SET state='unknown',error=? WHERE session_id=?",
                               ("Worker completion could not be persisted; explicit recovery is required", request["session_id"]))
                    self._queue_snapshot(db, request["session_id"], "execution_unknown")
                    self._mark_interaction(db, request["session_id"])
            del self.workers[rid]
            self._schedule_dirty = True

    def _finalize(self, rid: str, outcome: RunOutcome, *, recovery_control: tuple | None = None) -> None:
        request = self.store.get_request(rid)
        if request["result_json"] is not None:
            return
        session = self.store.get_session(request["session_id"])
        if outcome.status == "unknown":
            with self.store.transaction() as db:
                db.execute("UPDATE requests SET state='unknown' WHERE request_id=?", (rid,))
                db.execute("UPDATE sessions SET state='unknown',initialized=MAX(initialized,?),error=? WHERE session_id=?",
                           (int(outcome.session_initialized), outcome.error, session["session_id"]))
                self._queue_snapshot(db, session["session_id"], "execution_unknown")
                self._mark_interaction(db, session["session_id"])
            return
        accepted = bool(request["accepted"])
        revision = session["revision"] + int(accepted)
        run_dir = self.state_dir / "runs" / rid
        answer = outcome.answer or (f"Request {outcome.status}.\n\n{outcome.error}\n" if outcome.error else "")
        bundle = collect_artifacts(run_dir / "exports", answer, self.config.get("artifact_limits"),
                                   remaining_bytes=max(0, self.config["image_budget_bytes"] - self.store.image_bytes()),
                                   output_warnings=outcome.metadata.get("output_warnings"))
        settings = json.loads(session["settings_json"])
        result = {"schema_version": 1, "session_id": session["session_id"], "request_id": rid,
                  "status": outcome.status, "accepted": accepted, "revision": revision,
                  "started_at": request["started_at"], "ended_at": utc_now(), "error": outcome.error,
                  "configuration": {"workspace": session["workspace"], "model": settings.get("model"),
                                    "effort": settings.get("effort"), "timeout_seconds": settings.get("timeout_seconds"),
                                    "claude_version": self.config["claude_version"]},
                  "claude_session_id": session["claude_session_id"],
                  "metadata": outcome.metadata, "artifacts": bundle.artifacts, "skipped": bundle.skipped}
        prefix = f"sessions/{session['session_id']}/responses/{rid}/"
        files = {prefix + "result.json": canonical_json(result), prefix + "answer.md": bundle.answer.encode("utf-8")}
        files.update({prefix + name: data for name, data in bundle.files.items()})
        with self.store.transaction() as db:
            self._mark_interaction(db, session["session_id"])
            db.execute("UPDATE requests SET state='publishing',result_json=?,ended_at=? WHERE request_id=?",
                       (canonical_json(result).decode(), result["ended_at"], rid))
            if accepted:
                db.execute("UPDATE sessions SET revision=?,state='publishing',initialized=MAX(initialized,?),error=NULL WHERE session_id=?",
                           (revision, int(outcome.session_initialized), session["session_id"]))
            for artifact in bundle.artifacts:
                db.execute("INSERT INTO image_ledger VALUES (?,?,?,?)",
                           (rid, artifact["path"], artifact["size"], artifact["sha256"]))
            Store.enqueue(db, f"result:{rid}", files, request_id=rid, session_id=session["session_id"], terminal_state=outcome.status)
            if accepted:
                self._queue_snapshot(db, session["session_id"], "execution_finished",
                                     state="closed" if session["closed"] else "idle", active_request_id=None)
            if recovery_control:
                cid, digest, response = recovery_control
                db.execute("INSERT INTO controls VALUES (?,?,?)", (cid, digest, canonical_json(response).decode()))
                Store.enqueue(db, f"control:{cid}", {
                    f"sessions/{session['session_id']}/controls/results/{cid}.json": canonical_json(response)
                })

    def _publish(self) -> None:
        if time.monotonic() < self._next_publish:
            return
        pending = self.store.pending()
        if not pending:
            return
        try:
            files, mutable = {}, set()
            for row in pending:
                row_mutable = set(json.loads(row["mutable_json"]))
                mutable.update(row_mutable)
                for path, value in json.loads(row["files_json"]).items():
                    data = base64.b64decode(value, validate=True)
                    if path in files and files[path] != data and path not in mutable:
                        raise EngineError("Conflicting immutable files in the durable outbox")
                    files[path] = data
            validate_publication(files, self.config.get("artifact_limits"))
            self.mailbox.publish(files, "Publish remote Claude state and completed turns", mutable_paths=mutable)
            self.store.acknowledge(pending)
            if any(row["terminal_state"] for row in pending):
                self._schedule_dirty = True
            self._publish_failures = 0
        except Exception as exc:
            self._publish_failures += 1
            self._next_publish = time.monotonic() + min(300, self.config["retry_interval"] * 2 ** min(self._publish_failures - 1, 8))
            LOGGER.warning("Publication deferred; durable results will be retried: %s", exc)

    def tick(self) -> None:
        if not self._started:
            self.start()
        if self._stopping:
            return
        self._finish_workers()
        now = time.monotonic()
        if now >= self._next_sync:
            try:
                self.mailbox.sync()
                head = self.mailbox.head
                if head != self._scanned_head:
                    self._ingest_pending = True
                    self._discover()
                # The original input may have been deleted after its rejection
                # decision committed. Complete that durable decision even when
                # there is no longer a changed file to replay.
                for request in self.store.incomplete_rejections():
                    self._finalize_rejection(request)
                if head != self._scanned_head:
                    # Only ingestion advances this cursor. Publisher fetches can
                    # bring in unseen inputs and must never acknowledge them.
                    self.store.set_meta("ingested_head", head)
                    self._scanned_head = head
                self._ingest_pending = False
                self._sync_failures = 0
                self._next_sync = time.monotonic() + self._poll_interval()
            except Exception as exc:
                self._sync_failures += 1
                self._next_sync = time.monotonic() + min(300, max(1, self.config["retry_interval"]) * 2 ** min(self._sync_failures - 1, 8))
                LOGGER.warning("Mailbox synchronization deferred: %s", exc)
        if self._schedule_dirty and not self._ingest_pending:
            self._schedule()
        if now >= self._next_idle_sweep:
            for sid in self.store.dormant_candidates(time.time() - self.config["idle_ttl_seconds"]):
                with self.store.transaction() as db:
                    db.execute("UPDATE sessions SET state='dormant' WHERE session_id=?", (sid,))
                    self._queue_snapshot(db, sid, "session_dormant")
            self._next_idle_sweep = time.monotonic() + 60
        if now - self._heartbeat >= self.config["heartbeat_interval"]:
            self._queue_heartbeat()
        self._publish()

    def shutdown(self) -> None:
        if not self._started:
            return
        self._stopping = True
        for _, event in self.workers.values():
            event.set()
        self._executor.shutdown(wait=True, cancel_futures=False)
        self._finish_workers()
        self._queue_heartbeat()
        self._next_publish = 0
        self._publish()
        self.store.close()
        fcntl.flock(self._lock.fileno(), fcntl.LOCK_UN)
        self._lock.close()
        self._lock = None
        self._started = False
