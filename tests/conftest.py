"""Isolated filesystem and Git fixtures for the local integration suite."""

from __future__ import annotations

from pathlib import Path
import shutil
import subprocess
import uuid

import pytest


@pytest.fixture
def bare_remote(tmp_path: Path) -> Path:
    """Create a local remote without contacting a network service."""
    remote = tmp_path / "remote.git"
    subprocess.run(
        ["git", "init", "--bare", "--initial-branch=main", str(remote)],
        check=True,
        capture_output=True,
        text=True,
    )
    return remote


@pytest.fixture
def workspace() -> Path:
    """Use a real home descendant without replacing the HOME environment."""
    project_root = Path(__file__).resolve().parents[1]
    path = project_root / ".runtime" / "tests" / str(uuid.uuid4())
    path.mkdir(parents=True)
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


@pytest.fixture
def fake_claude() -> Path:
    return Path(__file__).with_name("fake_claude.py").resolve()


@pytest.fixture
def fake_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "fake-state"
    path.mkdir()
    monkeypatch.setenv("FAKE_CLAUDE_STATE", str(path))
    return path
