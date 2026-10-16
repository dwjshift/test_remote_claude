"""Validate the pressure-test fixture and measurement helpers."""

import json
import importlib.util
from pathlib import Path
import sqlite3

import pytest

_spec = importlib.util.spec_from_file_location("benchmark_mailbox", Path(__file__).resolve().parents[1] / "scripts" / "benchmark_mailbox.py")
benchmark = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(benchmark)
distribution, git, make_fixture = benchmark.distribution, benchmark.git, benchmark.make_fixture
Probe, summarize = benchmark.Probe, benchmark.summarize


def test_distribution_and_failed_samples():
    assert distribution([]) == {"samples": 0, "p50": None, "p95": None, "max": None}
    assert distribution(list(range(1, 21))) == {"samples": 20, "p50": 10, "p95": 19, "max": 20}
    summary = summarize([{"scenario": "send", "success": True, "submit_seconds": 2},
                         {"scenario": "send", "success": False, "submit_seconds": 4}])
    assert summary["send"]["success_rate"] == .5
    assert summary["send"]["metrics"]["submit_seconds"]["max"] == 4
    assert benchmark.peak_parallel([(0, 1), (1, 2)]) == 1
    assert benchmark.peak_parallel([(0, 3), (1, 2), (1.5, 4)]) == 3


def test_fixture_history_matches_ledger_and_native_context(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    fixture = tmp_path / "fixture"
    result = make_fixture(fixture, count=3, turns=2, active=2)
    assert result["commit_count"] == 13
    assert int(git("rev-list", "--count", "fixture", cwd=fixture / "history.git")) == 13
    entries = git("ls-tree", "-r", "--name-only", "fixture", cwd=fixture / "history.git").splitlines()
    assert sum("/requests/" in entry for entry in entries) == 6
    assert sum("/events/" in entry for entry in entries) == 18
    assert sum(entry.endswith("/answer.md") for entry in entries) == 6
    with sqlite3.connect(fixture / "ledger.sqlite3") as database:
        assert database.execute("SELECT COUNT(*) FROM outbox WHERE published=1").fetchone()[0] == 24
        assert database.execute("SELECT COUNT(*) FROM requests WHERE state='completed'").fetchone()[0] == 6
        assert database.execute("SELECT COUNT(*) FROM sessions WHERE state='dormant'").fetchone()[0] == 1
        assert database.execute("SELECT COUNT(*) FROM sessions WHERE state='idle'").fetchone()[0] == 2
        for native, revision in database.execute("SELECT claude_session_id,revision FROM sessions"):
            assert revision == 2
            assert (fixture / "fake-state" / f"session-{native}.json").is_file()
        checkpoint = json.loads(database.execute("SELECT value FROM metadata WHERE key='ingested_head'").fetchone()[0])
        assert checkpoint == result["head"]
    assert make_fixture(fixture, count=3, turns=2, active=2) == result
    with pytest.raises(ValueError, match="parameters differ"):
        make_fixture(fixture, count=3, turns=3, active=2)


def test_probe_history_reads_do_not_allocate_per_read_events():
    probe = Probe()
    marker = probe.mark()
    probe.input_reads += 10000
    assert probe.events == []
    assert probe.metrics(marker)["input_json_count"] == 10000


def test_archive_ref_identity_and_legacy_provenance(tmp_path, monkeypatch):
    repository = tmp_path / "repository"
    repository.mkdir()
    git("init", "--quiet", cwd=repository)
    git("config", "user.name", "Benchmark Test", cwd=repository)
    git("config", "user.email", "benchmark@localhost", cwd=repository)
    (repository / "server").mkdir()
    source = repository / "server" / "example.py"
    source.write_text("value = 1\n")
    git("add", ".", cwd=repository)
    git("commit", "--quiet", "-m", "First", cwd=repository)
    first = git("rev-parse", "HEAD", cwd=repository)
    monkeypatch.setattr(benchmark, "ROOT", repository)
    destination = tmp_path / "run" / "baseline-source"
    assert benchmark.archive_source(first, destination) == first
    assert benchmark.archive_source(first, destination) == first
    source.write_text("value = 2\n")
    git("commit", "--quiet", "-am", "Second", cwd=repository)
    with pytest.raises(ValueError, match="different commit"):
        benchmark.archive_source("HEAD", destination)
    manifest = destination.with_name("baseline-source.json")
    manifest.unlink()
    with pytest.raises(ValueError, match="Cannot verify"):
        benchmark.archive_source(first, destination)
    case = destination.parent / "local-2-baseline"
    case.mkdir()
    (case / "spec.json").write_text(json.dumps({"source": str(destination), "source_revision": first}))
    assert benchmark.archive_source(first, destination) == first
    assert manifest.is_file()
    (destination / "server" / "example.py").write_text("value = 3\n")
    with pytest.raises(ValueError, match="was modified"):
        benchmark.archive_source(first, destination)


@pytest.mark.parametrize("field,value", [("samples", {"serial": 30}), ("policy", True),
                                        ("remote", "different.git"), ("source_revision", "different"),
                                        ("source_fingerprint", "different"), ("name", "local-2-current")])
def test_completed_case_reuse_requires_matching_measurement(field, value):
    spec = {"name": "local-2-baseline", "source": "/example/baseline", "source_revision": "commit",
            "fixture": "/example/fixture", "remote": "example.git", "samples": {"serial": 2},
            "policy": False, "source_fingerprint": "fingerprint"}
    fixture = {"sessions": 2, "history_turns": 3, "active": 2, "head": "fixture-commit"}
    previous = {"spec": spec, "fixture": fixture, "passed": True, "branch_deleted": True}
    benchmark.validate_reuse(previous, dict(spec), fixture)
    with pytest.raises(ValueError, match="different"):
        benchmark.validate_reuse(previous, {**spec, field: value}, fixture)


def test_reuse_rejects_changed_fixture_and_failed_case():
    spec = {"name": "local-2-baseline", "source_fingerprint": "fingerprint"}
    fixture = {"sessions": 2, "history_turns": 3, "active": 2, "head": "commit"}
    previous = {"spec": spec, "fixture": fixture, "passed": True, "branch_deleted": True}
    with pytest.raises(ValueError, match="history_turns"):
        benchmark.validate_reuse(previous, spec, {**fixture, "history_turns": 4})
    with pytest.raises(ValueError, match="incomplete"):
        benchmark.validate_reuse({**previous, "passed": False}, spec, fixture)
