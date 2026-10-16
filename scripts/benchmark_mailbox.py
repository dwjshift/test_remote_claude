#!/usr/bin/env python3
"""Reproduce mailbox latency with real Git and a deterministic fake Claude.

All generated data and reports live under .runtime/benchmarks. GitHub runs
create synthetic test branches and delete each branch only after success.
The baseline is imported from a Git archive in an isolated child process.
"""

from __future__ import annotations

import argparse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
import csv
from datetime import datetime, timezone
import hashlib
import io
import json
import math
import os
from pathlib import Path
import platform
import resource
import shutil
import sqlite3
import subprocess
import sys
import tarfile
import threading
import time
import traceback
from uuid import NAMESPACE_URL, uuid4, uuid5

ROOT = Path(__file__).resolve().parents[1]


def save(path: Path, value: dict) -> None:
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def git(*arguments: str, cwd: Path = ROOT, timeout: float = 600) -> str:
    environment = os.environ.copy()
    environment.update({"GIT_TERMINAL_PROMPT": "0", "GIT_SSH_COMMAND": "ssh -oBatchMode=yes -oConnectTimeout=10"})
    result = subprocess.run(["git", "-c", "core.hooksPath=/dev/null", *arguments], cwd=cwd,
                            env=environment, capture_output=True, text=True, timeout=timeout)
    if result.returncode:
        raise RuntimeError(f"Git {arguments[0]} failed: {result.stderr.strip()}")
    return result.stdout.strip()


def identity(value: str) -> str:
    return str(uuid5(NAMESPACE_URL, "remote-claude-benchmark/" + value))


def payload(value: str, size: int) -> str:
    return hashlib.shake_256(value.encode()).hexdigest((size + 1) // 2)[:size]


def distribution(values: list[float]) -> dict:
    ordered = sorted(values)
    if not ordered:
        return {"samples": 0, "p50": None, "p95": None, "max": None}
    return {"samples": len(ordered), "p50": ordered[math.ceil(len(ordered) * .5) - 1],
            "p95": ordered[math.ceil(len(ordered) * .95) - 1], "max": ordered[-1]}


def peak_parallel(intervals: list[tuple[float, float]]) -> int:
    active = peak = 0
    for _, change in sorted([(start, 1) for start, _ in intervals] + [(end, -1) for _, end in intervals]):
        active += change
        peak = max(peak, active)
    return peak


def summarize(records: list[dict]) -> dict:
    grouped = {}
    for item in records:
        group = grouped.setdefault(item["scenario"], {"samples": 0, "successful": 0, "metrics": {}})
        group["samples"] += 1
        group["successful"] += int(item.get("success", True))
        for key, value in item.items():
            if (key.endswith("_seconds") or key.endswith("_count")) and isinstance(value, (int, float)):
                group["metrics"].setdefault(key, []).append(value)
    for group in grouped.values():
        group["success_rate"] = group["successful"] / group["samples"]
        group["metrics"] = {key: distribution(values) for key, values in group["metrics"].items()}
    return grouped


class FastImport:
    def __init__(self, repository: Path):
        self.errors = (repository.parent / "fast-import.log").open("wb")
        self.process = subprocess.Popen(["git", "fast-import", "--quiet"], cwd=repository,
                                        stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=self.errors)
        self.count = 0

    def commit(self, files: dict[str, bytes], timestamp: int) -> None:
        self.count += 1
        output = self.process.stdin
        message = f"Synthetic history commit {self.count}\n".encode()
        output.write(f"commit refs/heads/fixture\ncommitter Benchmark <benchmark@localhost> {timestamp} +0000\ndata {len(message)}\n".encode())
        output.write(message)
        for path, data in files.items():
            output.write(f"M 100644 inline {path}\ndata {len(data)}\n".encode())
            output.write(data)
            output.write(b"\n")
        output.write(b"\n")

    def close(self) -> None:
        self.process.stdin.close()
        code = self.process.wait(timeout=600)
        self.errors.close()
        if code:
            raise RuntimeError("Fixture fast-import failed; see fast-import.log")


def make_fixture(directory: Path, count: int, turns: int, active: int) -> dict:
    """Create matching Git and ledger history without running historical workers."""
    from common.protocol import canonical_json, content_hash
    from server.store import Store

    manifest = directory / "fixture.json"
    if manifest.exists():
        value = json.loads(manifest.read_text())
        if (value["sessions"], value["history_turns"], value["active"]) != (count, turns, active):
            raise ValueError("Existing fixture parameters differ from the requested parameters")
        return value
    directory.mkdir(parents=True, exist_ok=True)
    remote = directory / "history.git"
    git("init", "--bare", "--quiet", str(remote))
    store = Store(directory / "ledger.sqlite3")
    writer = FastImport(remote)
    started = time.monotonic()
    epoch = int(time.time()) - max(172800, count * turns * 2 + 86400)
    timestamp = datetime.fromtimestamp(epoch, timezone.utc).isoformat()
    workspaces = directory / "workspaces"
    workspaces.mkdir()
    initial = {".mailbox.json": canonical_json({"schema_version": 1, "kind": "remote-claude-mailbox"})}
    session_records = []
    fake = directory / "fake-state"
    fake.mkdir()
    for index in range(count):
        sid = identity(f"session-{index}")
        native = identity(f"native-{index}")
        workspace = workspaces / str(index)
        workspace.mkdir()
        relative = workspace.relative_to(Path.home())
        settings = {"schema_version": 1, "session_id": sid, "created_at": timestamp,
                    "workspace": "~/" + str(relative), "model": "sonnet", "effort": "high", "timeout_seconds": 30}
        initial[f"sessions/{sid}/settings.json"] = canonical_json(settings)
        session_records.append((sid, native, str(workspace), settings))
        (fake / f"session-{native}.json").write_text('{"prompt":"Synthetic previous turn"}\n')
    writer.commit(initial, epoch)
    with store.transaction() as db:
        for index, (sid, native, workspace, settings) in enumerate(session_records):
            last_activity = time.time() if index >= count - active else epoch
            state = "idle" if index >= count - active else "dormant"
            db.execute("INSERT INTO sessions(session_id,settings_json,settings_hash,workspace,claude_session_id,initialized,revision,state,last_activity,created_at) VALUES (?,?,?,?,?,1,?,?,?,?)",
                       (sid, canonical_json(settings).decode(), content_hash(settings), workspace, native, turns, state, last_activity, timestamp))
            for turn in range(turns):
                rid = identity(f"request-{index}-{turn}")
                created = datetime.fromtimestamp(epoch + (index * turns + turn) * 2 + 1, timezone.utc).isoformat()
                ended = datetime.fromtimestamp(epoch + (index * turns + turn) * 2 + 2, timezone.utc).isoformat()
                request = {"schema_version": 1, "session_id": sid, "request_id": rid,
                           "expected_revision": turn, "created_at": created, "prompt": payload(rid + "prompt", 1024)}
                result = {"schema_version": 1, "session_id": sid, "request_id": rid, "status": "completed",
                          "accepted": True, "revision": turn + 1, "started_at": created, "ended_at": ended,
                          "error": None, "configuration": {"workspace": workspace, "model": "sonnet", "effort": "high",
                                                             "timeout_seconds": 30, "claude_version": "2.1.300"},
                          "claude_session_id": native, "metadata": {"synthetic": True}, "artifacts": [], "skipped": []}
                request_path = f"sessions/{sid}/requests/{rid}.json"
                writer.commit({request_path: canonical_json(request)}, epoch + (index * turns + turn) * 2 + 1)
                response = f"sessions/{sid}/responses/{rid}/"
                result_files = {response + "result.json": canonical_json(result),
                                response + "answer.md": payload(rid + "answer", 4096).encode()}
                db.execute("INSERT INTO requests(request_id,session_id,body_json,body_hash,state,accepted,received_at,started_at,ended_at,result_json) VALUES (?,?,?,?,'completed',1,?,?,?,?)",
                           (rid, sid, canonical_json(request).decode(), content_hash(request), created, created, ended, canonical_json(result).decode()))
                Store.enqueue(db, f"result:{rid}", result_files, request_id=rid, session_id=sid, terminal_state="completed")
                all_files = dict(result_files)
                for kind, event_state in (("request_accepted", "queued"), ("execution_started", "running"), ("execution_finished", state if turn == turns - 1 else "idle")):
                    eid = identity(f"event-{index}-{turn}-{kind}")
                    snapshot = {"schema_version": 1, "session_id": sid, "revision": turn + int(kind == "execution_finished"),
                                "state": event_state, "active_request_id": None if kind == "execution_finished" else rid,
                                "closed": False, "workspace": workspace, "claude_session_id": native, "updated_at": ended, "error": None}
                    status_path = f"sessions/{sid}/status.json"
                    event_files = {status_path: canonical_json(snapshot), f"sessions/{sid}/events/{eid}.json": canonical_json({**snapshot, "event_id": eid, "kind": kind})}
                    Store.enqueue(db, eid, event_files, mutable={status_path})
                    all_files.update(event_files)
                writer.commit(all_files, epoch + (index * turns + turn) * 2 + 2)
            if (index + 1) % 100 == 0:
                print(f"Fixture: {index + 1}/{count} sessions imported", flush=True)
        db.execute("UPDATE outbox SET published=1")
    writer.close()
    head = git("rev-parse", "refs/heads/fixture", cwd=remote)
    store.set_meta("ingested_head", head)
    store.close()
    value = {"sessions": count, "history_turns": turns, "active": active, "head": head,
             "commit_count": 1 + count * turns * 2, "historical_requests": count * turns,
             "historical_events": count * turns * 3, "published_outbox_rows": count * turns * 4,
             "prompt_bytes": 1024, "answer_bytes": 4096, "images": 0,
             "session_ids": [row[0] for row in session_records], "preparation_seconds": time.monotonic() - started}
    save(manifest, value)
    return value


class Probe:
    """Record actual implementation calls without replacing production operations."""

    def __init__(self):
        self.events = []
        self.lock = threading.Lock()
        self.requests = {}
        self.tracked_requests = set()
        self.input_reads = 0
        self.restore = []

    def event(self, kind: str, started: float, **details) -> None:
        with self.lock:
            self.events.append({"kind": kind, "start": started, "end": time.monotonic(), **details})

    def wrap(self, owner, name: str, kind: str, detail=None) -> None:
        original = getattr(owner, name)
        def measured(instance, *args, **kwargs):
            started = time.monotonic()
            try:
                return original(instance, *args, **kwargs)
            finally:
                self.event(kind, started, **(detail(instance, args, kwargs) if detail else {}))
        self.restore.append((owner, name, original))
        setattr(owner, name, measured)

    def install(self) -> None:
        from common.git_transport import GitMailbox
        from server.engine import Engine
        self.wrap(GitMailbox, "_run", "git", lambda instance, args, _: {"operation": args[0], "cache": str(instance.path)})
        self.wrap(GitMailbox, "_validate_remote_tree", "validation", lambda instance, *_: {"cache": str(instance.path)})
        original_read = Engine._read_json
        def read(instance, *args, **kwargs):
            self.input_reads += 1
            return original_read(instance, *args, **kwargs)
        self.restore.append((Engine, "_read_json", original_read))
        Engine._read_json = read
        self.wrap(Engine, "_discover", "discovery")
        original_ingest = Engine._ingest_request
        def ingest(instance, path, *args, **kwargs):
            if path.stem not in self.tracked_requests:
                return original_ingest(instance, path, *args, **kwargs)
            started = time.monotonic()
            try:
                return original_ingest(instance, path, *args, **kwargs)
            finally:
                self.event("ingest", started, request_id=path.stem)
        self.restore.append((Engine, "_ingest_request", original_ingest))
        Engine._ingest_request = ingest
        self.wrap(Engine, "_finalize", "persist", lambda _, args, __: {"request_id": args[0]})
        self.wrap(Engine, "_publish", "publish")

    def mark(self) -> tuple[int, int]:
        return len(self.events), self.input_reads

    def close(self) -> None:
        for owner, name, original in reversed(self.restore):
            setattr(owner, name, original)
        self.restore.clear()

    def metrics(self, start: tuple[int, int]) -> dict:
        events = self.events[start[0]:]
        git_events = [item for item in events if item["kind"] == "git"]
        network = [item for item in git_events if item["operation"] in {"fetch", "push", "ls-remote"}]
        return {"git_command_count": len(git_events),
                "input_json_count": self.input_reads - start[1],
                "git_network_seconds": sum(item["end"] - item["start"] for item in network),
                "git_local_seconds": sum(item["end"] - item["start"] for item in git_events if item not in network),
                "validation_seconds": sum(item["end"] - item["start"] for item in events if item["kind"] == "validation"),
                "discovery_seconds": sum(item["end"] - item["start"] for item in events if item["kind"] == "discovery")}

    def runner(self, spec, cancel, on_start):
        from server.runner import run_claude
        record = self.requests.setdefault(spec.request_id, {})
        record["worker_start"] = time.monotonic()
        def started(pid, process_start):
            record["process_start"] = time.monotonic()
            on_start(pid, process_start)
        try:
            return run_claude(spec, cancel, started)
        finally:
            record["worker_end"] = time.monotonic()


def resource_snapshot() -> dict:
    own = resource.getrusage(resource.RUSAGE_SELF)
    children = resource.getrusage(resource.RUSAGE_CHILDREN)
    return {"cpu_seconds": own.ru_utime + own.ru_stime + children.ru_utime + children.ru_stime,
            "peak_rss_kib": own.ru_maxrss, "child_peak_rss_kib": children.ru_maxrss}


def run_case(spec: dict) -> dict:
    """Run inside a child that imports exactly one selected source revision."""
    sys.path.insert(0, spec["source"])
    from client import cli
    from common.git_transport import GitMailbox, GitError
    from common.protocol import canonical_json, new_id
    from server.engine import Engine

    directory = Path(spec["directory"])
    fixture_dir = Path(spec["fixture"])
    fixture = json.loads((fixture_dir / "fixture.json").read_text())
    report = {"spec": spec, "fixture": fixture, "passed": False, "branch_deleted": False, "records": [], "attempts": [],
              "host": {"platform": platform.platform(), "python": platform.python_version(),
                       "logical_cpus": os.cpu_count(), "cpu_model": next((line.split(":", 1)[1].strip() for line in Path("/proc/cpuinfo").read_text().splitlines() if line.startswith("model name")), "unknown"),
                       "memory": Path("/proc/meminfo").read_text().splitlines()[0]},
              "measurement_notes": ["Client submission uses the actual client send handler, excluding Python interpreter startup.",
                                    "Immediate comparison makes the central sync due after all submissions, avoiding polling-policy bias.",
                                    "Concurrent samples use independent client caches and real competing Git pushes; batch latency includes submission contention.",
                                    "The immediate shared observer reads responses after the entire batch publishes; per-request visibility is a conservative batch observation.",
                                    "Submission-barrier wait is reported separately from the subsequent server polling wait.",
                                    "A watcher can finish before local publication acknowledgement; that overlap is reported separately from visibility wait.",
                                    "The fake Claude subprocess sleeps 0.1 seconds; no model API is called.",
                                    "The synthetic corpus contains no images. CPU includes waited-for child processes; RSS is reported separately for parent and children.",
                                    "Network-stage timings include Git process overhead and local fetch packing, not wire-only latency.",
                                    "Aggregated Git durations sum overlapping client operations and must not be added as sequential wall-clock stages."]}
    records = report["records"]
    branch = spec["branch"]
    remote = spec["remote"]
    path = directory / "server"
    path.mkdir(parents=True)
    shutil.copy2(fixture_dir / "ledger.sqlite3", path / "ledger.sqlite3")
    database = sqlite3.connect(path / "ledger.sqlite3")
    database.execute("INSERT OR REPLACE INTO metadata VALUES ('mailbox', ?)", (json.dumps({"remote": remote, "branch": branch}),))
    database.commit()
    database.close()
    shutil.copytree(fixture_dir / "fake-state", directory / "fake-state")
    os.environ["FAKE_CLAUDE_STATE"] = str(directory / "fake-state")
    fake = directory / "fake-claude"
    fake.write_text(f"#!{sys.executable}\nimport runpy\nrunpy.run_path({str(ROOT / 'tests' / 'fake_claude.py')!r}, run_name='__main__')\n")
    fake.chmod(0o700)
    config = {"remote": remote, "branch": branch, "state_dir": str(path), "claude_executable": str(fake),
              "poll_interval": 60, "adaptive_polling": False, "max_workers": fixture["active"],
              "heartbeat_interval": 3600, "git_timeout": 180}
    active_ids = fixture["session_ids"][-fixture["active"]:]
    client_configs = [{"remote": remote, "branch": branch, "state_dir": str(directory / f"client-{index}"),
                       "git_timeout": 180, "watch_interval": 10} for index in range(fixture["active"])]
    probe = Probe()
    probe.install()
    engine = None
    watchers = []
    started = time.monotonic()
    before_resources = resource_snapshot()

    def checkpoint() -> None:
        report["summary"] = summarize(records)
        report["attempt_summary"] = {"total": len(report["attempts"]),
                                     "completed": sum(item["outcome"] == "completed" for item in report["attempts"]),
                                     "unfinished": sum(item["outcome"] != "completed" for item in report["attempts"])}
        report["elapsed_seconds"] = time.monotonic() - started
        report["resources"] = resource_snapshot()
        report["resources"]["cpu_seconds"] -= before_resources["cpu_seconds"]
        save(directory / "report.json", report)

    def sample(name, operation) -> None:
        index = probe.mark()
        begin = time.monotonic()
        operation()
        records.append({"scenario": name, "success": True, "elapsed_seconds": time.monotonic() - begin,
                        **probe.metrics(index)})

    def settle() -> None:
        engine._next_sync = 0
        engine.tick()
        engine._next_sync = 0
        engine.tick()
        engine._next_sync = time.monotonic() + 3600

    def submit(index, record):
        args = argparse.Namespace(session_id=record["session_id"], request_id=record["request_id"],
                                  expected_revision=None, prompt=json.dumps({"delay": .1, "answer": payload(record["request_id"], 4096)}), prompt_file=None)
        record["submit_start"] = time.monotonic()
        for attempt in range(8):
            try:
                cli._send(args, client_configs[index])
                record["submit_end"] = time.monotonic()
                record["client_retries"] = attempt
                return
            except GitError:
                if attempt == 7:
                    raise
                time.sleep(.1 * (index + 1))

    def requests(name, width, sequence, policy=None):
        batch_start = time.monotonic()
        event_index = probe.mark()
        values = [{"session_id": active_ids[(sequence + index) % len(active_ids)], "request_id": new_id()}
                  for index in range(width)]
        attempts = [{**value, "scenario": name, "batch": sequence, "outcome": "started"} for value in values]
        report["attempts"].extend(attempts)
        probe.tracked_requests.update(value["request_id"] for value in values)
        client_indices = [(sequence + index) % len(active_ids) for index in range(width)]
        with ThreadPoolExecutor(max_workers=width) as executor:
            futures = [executor.submit(submit, index, value) for index, value in zip(client_indices, values)]
            for future in futures:
                future.result()
        ready = time.monotonic()
        if policy is None:
            engine._next_sync = 0
        else:
            # Establish a known polling phase after upload; use the real clock.
            # The setting is selected by the production policy method below.
            engine._last_interaction_monotonic = ready - {10: 0, 30: 301, 60: 601}[policy]
            selected_interval = engine._poll_interval()
            if selected_interval != policy:
                raise AssertionError(f"Production policy selected {selected_interval}, expected {policy}")
            engine._next_sync = ready + selected_interval
        visibility_deadline = ready if policy is None else ready + 10
        observer = None
        watcher = None
        watch_finished = threading.Event()
        watch_completion = {}
        watch_output = None
        if policy is not None:
            watch_config = directory / f"watch-{policy}.json"
            save(watch_config, {**client_configs[0], "state_dir": str(directory / "watch-client")})
            watch_output = (directory / f"watch-{policy}.jsonl").open("w")
            watch_error = (directory / f"watch-{policy}.stderr.log").open("w")
            environment = os.environ.copy()
            environment["PYTHONPATH"] = spec["source"]
            watcher = subprocess.Popen([sys.executable, "-m", "client", "--config", str(watch_config), "--json",
                                        "watch", values[0]["session_id"], "--request-id", values[0]["request_id"],
                                        "--interval", "10", "--timeout", "240"], cwd=spec["source"], env=environment,
                                       stdout=watch_output, stderr=watch_error)
            def wait_for_watch():
                watcher.wait()
                watch_completion["visible"] = time.monotonic()
                watch_finished.set()
            waiter = threading.Thread(target=wait_for_watch, name="benchmark-watch", daemon=True)
            waiter.start()
            watchers.append((watcher, watch_output, watch_error, waiter))
        observed = set()
        deadline = ready + max(240, policy or 0)
        while len(observed) < width and time.monotonic() < deadline:
            tick_start = time.monotonic()
            engine.tick()
            for value in values:
                row = engine.store.get_request(value["request_id"])
                if row and "received_seen" not in value:
                    value["received_seen"] = tick_start
                if row and row["state"] in {"unknown", "failed", "cancelled", "timed_out", "rejected"}:
                    raise AssertionError(f"Benchmark request did not complete: {row['state']}")
                if row and row["state"] == "completed" and "published_seen" not in value:
                    value["published_seen"] = time.monotonic()
            if watcher is not None and watch_finished.is_set():
                watch_output.close()
                watch_error.close()
                if watcher.returncode:
                    raise AssertionError(f"Production watch failed; see watch-{policy}.stderr.log")
                snapshots = [json.loads(line) for line in (directory / f"watch-{policy}.jsonl").read_text().splitlines()]
                if not snapshots or not snapshots[-1]["responses"] or snapshots[-1]["responses"][0]["status"] != "completed":
                    raise AssertionError("Production watch exited without the completed response")
                values[0]["visible"] = watch_completion["visible"]
                observed.add(values[0]["request_id"])
            if policy is None and time.monotonic() >= visibility_deadline and all("published_seen" in value for value in values):
                if observer is None:
                    observer = GitMailbox(remote, branch, directory / "observer", git_timeout=180)
                else:
                    observer.sync()
                for value in values:
                    snapshot = cli._show(observer, value["session_id"], value["request_id"])
                    if snapshot["responses"]:
                        response = snapshot["responses"][0]
                        if response["status"] != "completed":
                            raise AssertionError(f"Unexpected visible result: {response['status']}")
                        if value["request_id"] not in observed:
                            value["visible"] = time.monotonic()
                            observed.add(value["request_id"])
                visibility_deadline = time.monotonic() + (10 if policy is not None else .01)
            time.sleep(.1)
        if len(observed) != width:
            if watcher is not None and watcher.poll() is None:
                watcher.terminate()
                watcher.wait(timeout=10)
            raise TimeoutError("Benchmark request did not become visible before its deadline")
        captured = probe.events[event_index[0]:]
        sync_start = next((item["start"] for item in captured if item["kind"] == "git" and item["operation"] == "fetch" and item.get("cache") == str(path / "mailbox") and item["start"] >= ready), ready)
        discoveries = [item for item in captured if item["kind"] == "discovery"]
        discovery_end = discoveries[0]["end"] if discoveries else sync_start
        for value, attempt in zip(values, attempts):
            rid = value["request_id"]
            worker = probe.requests[rid]
            ingested = next(item["end"] for item in captured if item["kind"] == "ingest" and item["request_id"] == rid and item["end"] >= ready)
            persisted = next(item for item in captured if item["kind"] == "persist" and item["request_id"] == rid)
            record = {"scenario": name, "request_id": rid, "session_id": value["session_id"], "success": True,
                      "submit_seconds": value["submit_end"] - value["submit_start"],
                      "upload_to_poll_seconds": sync_start - value["submit_end"],
                      "submission_barrier_seconds": ready - value["submit_end"],
                      "poll_wait_seconds": sync_start - ready,
                      "fetch_validate_discover_seconds": (discoveries[0]["end"] if discoveries else ingested) - sync_start,
                      "server_fetch_seconds": sum(item["end"] - item["start"] for item in captured if item["kind"] == "git" and item["operation"] == "fetch" and item.get("cache") == str(path / "mailbox") and sync_start <= item["start"] < discovery_end),
                      "server_validation_seconds": sum(item["end"] - item["start"] for item in captured if item["kind"] == "validation" and item.get("cache") == str(path / "mailbox") and sync_start <= item["start"] < discovery_end),
                      "server_discovery_seconds": sum(item["end"] - item["start"] for item in discoveries if sync_start <= item["start"] < discovery_end),
                      "queue_seconds": worker["worker_start"] - ingested,
                      "worker_seconds": worker["worker_end"] - worker["worker_start"],
                      "persist_seconds": persisted["end"] - persisted["start"],
                      "worker_end_to_published_seconds": value["published_seen"] - worker["worker_end"],
                      "published_to_visible_seconds": max(0, value["visible"] - value["published_seen"]),
                      "acknowledgement_after_visibility_seconds": max(0, value["published_seen"] - value["visible"]),
                      "end_to_end_seconds": value["visible"] - value["submit_start"],
                      "client_retry_count": value["client_retries"], "batch": sequence}
            records.append(record)
            attempt["outcome"] = "completed"
        records.append({"scenario": name + "_batch", "success": True, "width": width,
                        "elapsed_seconds": time.monotonic() - batch_start,
                        "peak_worker_count": peak_parallel([(probe.requests[value["request_id"]]["worker_start"], probe.requests[value["request_id"]]["worker_end"]) for value in values]),
                        **probe.metrics(event_index)})
        print(f"{spec['name']}: {name} batch {sequence} ({width} requests), {time.monotonic() - batch_start:.3f}s", flush=True)
        checkpoint()
        if policy is None:
            engine._next_sync = time.monotonic() + 3600

    try:
        if ":" in remote:
            report["protected_refs_before"] = git("ls-remote", "--heads", remote, "refs/heads/main", "refs/heads/master", "refs/heads/mailbox")
        git("push", "--quiet", remote, f"refs/heads/fixture:refs/heads/{branch}", cwd=fixture_dir / "history.git")
        report["branch_created"] = True
        checkpoint()
        engine = Engine(config, runner=probe.runner)
        sample("cold_start", engine.start)
        settle()
        # Prime each independent cache outside the measured submission interval.
        for index, configuration in enumerate(client_configs, start=1):
            print(f"{spec['name']}: preparing client cache {index}/{len(client_configs)}", flush=True)
            cli._mailbox(configuration)
        counts = spec["samples"]
        for _ in range(counts["idle"]):
            sample("idle_tick", engine.tick)
        for _ in range(counts["unchanged"]):
            engine._next_sync = 0
            sample("unchanged_head", engine.tick)
        for index in range(counts["serial"]):
            requests("serial", 1, index)
        for index in range(counts["batches"]):
            requests("concurrent", fixture["active"], index)
        for _ in range(counts["output"]):
            engine._queue_heartbeat()
            engine._publish()
            engine._next_sync = 0
            sample("output_only", engine.tick)
        for _ in range(counts["restarts"]):
            engine.shutdown()
            engine = Engine(config, runner=probe.runner)
            sample("restart", engine.start)
            settle()
        if spec.get("policy"):
            engine.config.update(adaptive_polling=True, active_poll_interval=10, warm_poll_interval=30, poll_interval=60)
            for seconds in (10, 30, 60):
                requests(f"policy_{seconds}s_watch_10s", 1, 0, policy=seconds)
        invocations = [json.loads(line) for line in (directory / "fake-state" / "invocations.jsonl").read_text().splitlines()] if (directory / "fake-state" / "invocations.jsonl").exists() else []
        expected = counts["serial"] + counts["batches"] * fixture["active"] + (3 if spec.get("policy") else 0)
        if len(invocations) != expected or any(not item["resume"] for item in invocations):
            raise AssertionError("Unexpected fake worker invocation count or missing native session resume")
        if len({item["prompt"] for item in invocations}) != expected:
            raise AssertionError("A benchmark prompt was executed more than once")
        executed = Counter(item["session_id"] for item in records if "request_id" in item)
        for sid, count in executed.items():
            if engine.store.get_session(sid)["revision"] != fixture["history_turns"] + count:
                raise AssertionError("Session revision does not match the completed request count")
        for rid in probe.tracked_requests:
            request = engine.store.get_request(rid)
            if request is None or request["state"] != "completed":
                raise AssertionError("A submitted request was lost or did not complete")
            if json.loads(request["result_json"])["revision"] != json.loads(request["body_json"])["expected_revision"] + 1:
                raise AssertionError("A result advanced revision incorrectly")
        if engine.store.pending():
            raise AssertionError("Publication queue is not empty at the end of the benchmark")
        report["invocations"] = len(invocations)
        report["passed"] = True
    except BaseException as exc:
        report["error"] = str(exc)
        report["traceback"] = traceback.format_exc()
        raise
    finally:
        for watcher, output, error, waiter in watchers:
            if watcher.poll() is None:
                watcher.terminate()
                try:
                    watcher.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    watcher.kill()
                    watcher.wait(timeout=10)
            waiter.join(timeout=10)
            output.close()
            error.close()
        for attempt in report["attempts"]:
            if attempt["outcome"] != "completed":
                attempt.update(outcome="unfinished", error=report.get("error", "Benchmark did not finish"))
                records.append({"scenario": attempt["scenario"], "success": False,
                                "request_id": attempt["request_id"], "session_id": attempt["session_id"],
                                "error": attempt["error"]})
        if engine is not None:
            try:
                engine.shutdown()
            except Exception as exc:
                report["shutdown_error"] = str(exc)
                report["passed"] = False
        checkpoint()
        (directory / "events.jsonl").write_text("".join(json.dumps(item) + "\n" for item in probe.events))
        if report["passed"]:
            try:
                if "protected_refs_before" in report:
                    report["protected_refs_after"] = git("ls-remote", "--heads", remote, "refs/heads/main", "refs/heads/master", "refs/heads/mailbox")
                    if report["protected_refs_after"] != report["protected_refs_before"]:
                        raise AssertionError("A protected branch changed during the benchmark; inspect concurrent repository activity")
                git("push", "--quiet", remote, f":refs/heads/{branch}", cwd=fixture_dir / "history.git")
                if git("ls-remote", "--heads", remote, f"refs/heads/{branch}"):
                    raise AssertionError("Test branch still exists after deletion")
                report["branch_deleted"] = True
            except Exception as exc:
                report["cleanup_error"] = str(exc)
                report["passed"] = False
        report["cleanup_command"] = ["git", "push", remote, f":refs/heads/{branch}"]
        checkpoint()
        probe.close()
    return report


def source_fingerprint(directory: Path) -> str:
    digest = hashlib.sha256()
    for package in ("client", "common", "server"):
        for path in sorted((directory / package).glob("*.py")):
            digest.update(str(path.relative_to(directory)).encode() + b"\0" + path.read_bytes() + b"\0")
    return digest.hexdigest()


def archive_source(reference: str, directory: Path) -> str:
    commit = git("rev-parse", reference, cwd=ROOT)
    manifest = directory.with_name(directory.name + ".json")
    if directory.exists():
        if manifest.exists():
            saved = json.loads(manifest.read_text())
            if saved.get("commit") != commit:
                raise ValueError("The existing baseline archive belongs to a different commit; use a new run directory")
            if saved.get("source_fingerprint") != source_fingerprint(directory):
                raise ValueError("The existing baseline archive was modified; use a new run directory")
        else:
            revisions = {spec["source_revision"] for path in directory.parent.glob("*/spec.json")
                         if (spec := json.loads(path.read_text())).get("source") == str(directory)}
            if revisions != {commit}:
                raise ValueError("Cannot verify the existing baseline archive from consistent case specifications")
            save(manifest, {"commit": commit, "source_fingerprint": source_fingerprint(directory),
                            "provenance": "Existing case specifications"})
    else:
        archive = subprocess.run(["git", "archive", commit], cwd=ROOT, capture_output=True, check=True).stdout
        directory.mkdir(parents=True)
        with tarfile.open(fileobj=io.BytesIO(archive)) as stream:
            stream.extractall(directory, filter="data")
        save(manifest, {"commit": commit, "source_fingerprint": source_fingerprint(directory),
                        "provenance": "Git archive"})
    return commit


def validate_reuse(previous: dict, expected: dict, fixture: dict) -> None:
    """Refuse to pass off a quick, stale, or different-remote run as this run."""
    if not previous.get("passed") or not previous.get("branch_deleted"):
        raise ValueError("The existing case is incomplete; preserve it and use a new run directory")
    actual = previous["spec"]
    for name in ("name", "source", "source_revision", "fixture", "remote", "samples", "policy"):
        if actual.get(name) != expected.get(name):
            raise ValueError(f"Existing benchmark case has a different {name}; use a new run directory")
    for name in ("sessions", "history_turns", "active", "head"):
        if previous["fixture"].get(name) != fixture.get(name):
            raise ValueError(f"Existing benchmark fixture has a different {name}; use a new run directory")
    if actual.get("source_fingerprint") != expected["source_fingerprint"]:
        # Older baseline case specs predate fingerprints; the archive's commit
        # has already been verified. Never infer a dirty current tree's identity.
        if actual.get("source_fingerprint") is not None or not expected["name"].endswith("-baseline"):
            raise ValueError("Existing benchmark case used different source bytes; use a new run directory")


def report_markdown(directory: Path, results: list[dict]) -> None:
    lines = ["# Mailbox pressure test", "", "All times are seconds. Request measurements include the fixed 0.1-second fake model delay.", ""]
    for report in results:
        status = "passed" if report.get("passed") else "failed or incomplete"
        lines.append(f"- {report['spec']['name']}: {status}; branch deleted: {report.get('branch_deleted', False)}.")
        if report.get("error"):
            lines.append(f"  Error: {report['error']}")
    lines += ["", "| Case | Scenario | Samples | Success | p50 | p95 | Maximum |", "| --- | --- | ---: | ---: | ---: | ---: | ---: |"]
    for report in results:
        for name, summary in report.get("summary", {}).items():
            metric = summary["metrics"].get("end_to_end_seconds") or summary["metrics"].get("elapsed_seconds")
            if metric:
                lines.append(f"| {report['spec']['name']} | {name} | {summary['samples']} | {summary['success_rate']:.0%} | {metric['p50']:.6f} | {metric['p95']:.6f} | {metric['max']:.6f} |")
            else:
                lines.append(f"| {report['spec']['name']} | {name} | {summary['samples']} | {summary['success_rate']:.0%} | — | — | — |")
    lines += ["", "## Method and limits", "", "- Same generated Git and SQLite history for each source revision.",
              "- Historical turns have distinct 1 KiB prompts and 4 KiB answers, with three events and four published outbox rows each.",
              "- Concurrent submissions use separate client caches and actual Git push contention.",
              "- Timed comparison makes server synchronization due after upload; policy scenarios use real wall-clock waits and 10-second visibility polling.",
              "- Client command handlers run in process; Python interpreter startup is excluded.",
              "- No images or real Claude API calls are included; this measures relay overhead rather than model latency.",
              "- JSON reports retain stage timings, failures, resource usage, and exact case parameters."]
    (directory / "REPORT.md").write_text("\n".join(lines) + "\n")
    flattened = [{"case": report["spec"]["name"], **record} for report in results for record in report.get("records", [])]
    if flattened:
        fields = sorted({key for record in flattened for key in record})
        with (directory / "samples.csv").open("w", newline="") as output:
            writer = csv.DictWriter(output, fieldnames=fields)
            writer.writeheader()
            writer.writerows(flattened)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sessions", type=int, default=1000)
    parser.add_argument("--history-turns", type=int, default=10)
    parser.add_argument("--active", type=int, default=10)
    parser.add_argument("--baseline-ref", default="a438da6")
    parser.add_argument("--remote", help="Optional GitHub SSH remote; never use a production mailbox branch")
    parser.add_argument("--variants", choices=("both", "baseline", "current"), default="both")
    parser.add_argument("--cases", choices=("all", "local", "github"), default="all")
    parser.add_argument("--run-dir", type=Path, help="Reuse a fixture or append a different variant to an existing run")
    parser.add_argument("--quick", action="store_true", help="Use two samples per scenario; keep the selected fixture size")
    parser.add_argument("--skip-small", action="store_true", help="Omit the 10-session local comparison")
    parser.add_argument("--policy", action="store_true", help="Also measure real 10/30/60-second waits with a 10-second observer")
    parser.add_argument("--case-spec", type=Path, help=argparse.SUPPRESS)
    arguments = parser.parse_args()
    if arguments.case_spec:
        result = run_case(json.loads(arguments.case_spec.read_text()))
        return int(not result["passed"])
    if min(arguments.sessions, arguments.history_turns, arguments.active) < 1 or arguments.active > arguments.sessions:
        parser.error("Session, history, and active counts must be positive, with active <= sessions")
    if arguments.cases == "github" and not arguments.remote:
        parser.error("--cases github requires --remote")
    sys.path.insert(0, str(ROOT))
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid4().hex[:8]
    directory = (arguments.run_dir or ROOT / ".runtime" / "benchmarks" / run_id).resolve()
    directory.mkdir(parents=True, exist_ok=True)
    if not directory.is_relative_to(Path.home()):
        parser.error("The run directory must be under the server account home for real workspace validation")
    baseline = directory / "baseline-source"
    baseline_commit = archive_source(arguments.baseline_ref, baseline)
    # Generate the old schema once. The candidate must perform its own migration;
    # giving the baseline candidate-only indexes would distort the comparison.
    sys.path.insert(0, str(baseline))
    selected = ["baseline", "current"] if arguments.variants == "both" else [arguments.variants]
    sizes = [arguments.sessions]
    if not arguments.skip_small and arguments.sessions != 10 and arguments.cases != "github":
        sizes.insert(0, 10)
    cases = [json.loads(path.read_text()) for path in sorted(directory.glob("*/report.json"))]
    for size in sizes:
        fixture = directory / f"fixture-{size}"
        print(f"Preparing {size} sessions with {arguments.history_turns} historical turns each", flush=True)
        fixture_manifest = make_fixture(fixture, size, arguments.history_turns, min(size, arguments.active))
        environments = []
        if arguments.cases != "github":
            environments.append("local")
        if arguments.remote and size == arguments.sessions and arguments.cases != "local":
            environments.append("github")
        for environment in environments:
            for variant in selected:
                name = f"{environment}-{size}-{variant}"
                case_dir = directory / name
                report_path = case_dir / "report.json"
                remote = arguments.remote if environment == "github" else str(case_dir / "remote.git")
                counts = {"idle": 100, "unchanged": 10 if environment == "github" else 30,
                          "serial": 30, "batches": 5, "output": 10, "restarts": 3}
                if arguments.quick:
                    counts = {key: 2 for key in counts}
                spec = {"name": name, "source": str(baseline if variant == "baseline" else ROOT),
                        "source_revision": baseline_commit if variant == "baseline" else git("rev-parse", "HEAD"),
                        "source_fingerprint": source_fingerprint(baseline if variant == "baseline" else ROOT),
                        "working_tree_changes": git("diff", "--stat") if variant == "current" else "",
                        "directory": str(case_dir), "fixture": str(fixture), "remote": remote,
                        "branch": f"test/remote-claude/perf-{directory.name}-{name}", "samples": counts,
                        "policy": bool(arguments.policy and variant == "current" and size == arguments.sessions)}
                if report_path.exists():
                    previous = json.loads(report_path.read_text())
                    validate_reuse(previous, spec, fixture_manifest)
                    cases = [item for item in cases if item["spec"]["name"] != name] + [previous]
                    print(f"Reusing completed case {name}", flush=True)
                    continue
                case_dir.mkdir()
                if environment == "local":
                    git("init", "--bare", "--quiet", remote)
                save(case_dir / "spec.json", spec)
                print(f"Starting {name}; reports: {case_dir}", flush=True)
                process = subprocess.run([sys.executable, str(Path(__file__).resolve()), "--case-spec", str(case_dir / "spec.json")], cwd=ROOT)
                if report_path.exists():
                    cases = [item for item in cases if item["spec"]["name"] != name] + [json.loads(report_path.read_text())]
                    report_markdown(directory, cases)
                    save(directory / "report.json", {"cases": cases, "baseline_commit": baseline_commit})
                if process.returncode:
                    print(f"Case failed; diagnostics and synthetic branch retained: {case_dir}", file=sys.stderr)
                    return process.returncode
    print(f"Benchmark complete: {directory / 'REPORT.md'}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
