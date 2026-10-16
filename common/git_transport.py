"""A disposable, locked Git cache for the append-only mailbox protocol."""

from __future__ import annotations

import contextlib
import fcntl
import json
import math
import os
import re
import signal
import subprocess
import time
from pathlib import Path, PurePosixPath
from typing import Iterator

from .protocol import canonical_json


class GitError(RuntimeError):
    """A mailbox Git operation failed."""

    def __init__(self, message: str) -> None:
        super().__init__(re.sub(r"(https?://)[^/@\s]+@", r"\1[redacted]@", message))


class ImmutableConflict(GitError):
    """An immutable mailbox path already contains different bytes."""


class GitMailbox:
    """Synchronize and publish through an exclusively owned checkout.

    The checkout is a cache: sync discards its local edits. Unpublished work must
    be held in the caller's durable outbox, outside this directory.
    """

    MAX_PUSH_ATTEMPTS = 5

    def __init__(
        self, remote: str, branch: str, path: Path, create: bool = False,
        git_timeout: float = 30,
    ) -> None:
        if not isinstance(remote, str) or not remote or remote.startswith("-") or "\n" in remote or "\x00" in remote:
            raise GitError("Invalid Git remote")
        if not isinstance(branch, str) or branch in {"main", "master"} or branch.startswith(("-", "refs/")):
            raise GitError("Use a dedicated mailbox branch; main and master are forbidden")
        if (
            isinstance(git_timeout, bool) or not isinstance(git_timeout, (int, float))
            or not math.isfinite(git_timeout) or git_timeout <= 0
        ):
            raise GitError("git_timeout must be positive")
        self.remote = str(Path(remote).expanduser().resolve()) if ":" not in remote else remote
        self.branch = branch
        self.path = Path(path).expanduser().resolve()
        self.git_timeout = git_timeout
        self._remote_ref = f"refs/remotes/origin/{branch}"
        self._branch_ref = f"refs/heads/{branch}"
        self._lock_path = self.path.parent / f".{self.path.name}.lock"
        self._remote_initialized = False
        self._run("check-ref-format", self._branch_ref, cwd=self.path.parent)
        with self._locked():
            self._initialize()
            self._sync_unlocked(create=create)

    @contextlib.contextmanager
    def _locked(self) -> Iterator[None]:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock_path.open("a+b") as lock:
            os.chmod(self._lock_path, 0o600)
            deadline = time.monotonic() + self.git_timeout
            while True:
                try:
                    fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise GitError("Timed out waiting for the mailbox checkout lock")
                    time.sleep(0.05)
            try:
                yield
            finally:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)

    def _run(
        self, *args: str, check: bool = True, cwd: Path | None = None,
    ) -> subprocess.CompletedProcess:
        env = os.environ.copy()
        for key in (
            "GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_OBJECT_DIRECTORY",
            "GIT_ALTERNATE_OBJECT_DIRECTORIES", "GIT_COMMON_DIR",
        ):
            env.pop(key, None)
        env.update({
            "GIT_TERMINAL_PROMPT": "0", "GCM_INTERACTIVE": "Never",
            "GIT_ASKPASS": "/bin/false", "SSH_ASKPASS": "/bin/false",
            "GIT_SSH_COMMAND": "ssh -oBatchMode=yes -oConnectTimeout=10",
            "GIT_EDITOR": ":", "GIT_SEQUENCE_EDITOR": ":",
        })
        command = [
            "git", "-c", "core.hooksPath=/dev/null", "-c", "commit.gpgsign=false",
            "-c", "core.autocrlf=false", "-c", "core.fsmonitor=false",
            "-c", "core.attributesFile=/dev/null", "-c", "protocol.ext.allow=never", *args,
        ]
        operation_dir = cwd if cwd is not None else self.path
        try:
            process = subprocess.Popen(
                command, cwd=operation_dir if operation_dir.exists() else None,
                env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace",
                start_new_session=True,
            )
            try:
                stdout, stderr = process.communicate(timeout=self.git_timeout)
            except BaseException:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.communicate()
                raise
            result = subprocess.CompletedProcess(command, process.returncode, stdout, stderr)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise GitError(f"Git operation {args[0]} failed: {exc}") from exc
        if check and result.returncode:
            detail = (result.stderr or result.stdout).strip()
            detail = re.sub(r"(https?://)[^/@\s]+@", r"\1[redacted]@", detail)
            raise GitError(f"Git {args[0]} failed: {detail}")
        return result

    def _initialize(self) -> None:
        marker = self.path / ".git" / "remote-claude-mailbox.json"
        expected = {"remote": self.remote, "branch": self.branch}
        if self.path.exists() and any(self.path.iterdir()):
            try:
                actual = json.loads(marker.read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                raise GitError("Refusing to use a non-mailbox directory as a disposable cache") from exc
            if not isinstance(actual, dict) or any(actual.get(key) != value for key, value in expected.items()):
                raise GitError("Mailbox cache belongs to a different remote or branch")
            # Old cache metadata without the flag is treated conservatively.
            self._remote_initialized = actual.get("remote_initialized", bool(self._head_unlocked())) is True
        else:
            self.path.mkdir(parents=True, exist_ok=True, mode=0o700)
            self._run("init", "--quiet")
            self._run("remote", "add", "origin", self.remote)
            marker.write_bytes(canonical_json({**expected, "remote_initialized": False}))
        self._run("config", "user.name", "Remote Claude Mailbox")
        self._run("config", "user.email", "remote-claude@localhost")
        self._run("config", "core.hooksPath", "/dev/null")

    def _head_unlocked(self) -> str:
        result = self._run("rev-parse", "--verify", "HEAD", check=False)
        return result.stdout.strip() if result.returncode == 0 else ""

    @property
    def head(self) -> str:
        with self._locked():
            return self._head_unlocked()

    def _fetch(self) -> subprocess.CompletedProcess:
        return self._run(
            "fetch", "--quiet", "--no-tags", "--no-recurse-submodules", "origin",
            f"+{self._branch_ref}:{self._remote_ref}", check=False,
        )

    def _validate_remote_tree(self) -> None:
        listing = self._run("ls-tree", "-rz", "--full-tree", self._remote_ref).stdout
        marker_found = False
        for entry in listing.split("\x00"):
            if not entry:
                continue
            metadata, name = entry.split("\t", 1)
            mode = metadata.split(" ", 1)[0]
            self._validate_path(name)
            if mode == "120000":
                raise GitError(f"Mailbox symlinks are not supported: {name}")
            if mode not in {"100644", "100755"}:
                raise GitError(f"Mailbox entries must be regular files: {name}")
            marker_found = marker_found or name == ".mailbox.json"
        if not marker_found:
            raise GitError("The remote branch is missing its .mailbox.json marker")
        marker_ref = f"{self._remote_ref}:.mailbox.json"
        marker_size = int(self._run("cat-file", "-s", marker_ref).stdout.strip())
        if marker_size > 4096:
            raise GitError("The remote mailbox marker is too large")
        try:
            marker = json.loads(self._run("show", marker_ref).stdout)
        except ValueError as exc:
            raise GitError("The remote mailbox marker is not valid JSON") from exc
        if (
            not isinstance(marker, dict) or type(marker.get("schema_version")) is not int
            or marker["schema_version"] != 1 or marker.get("kind") != "remote-claude-mailbox"
        ):
            raise GitError("The remote mailbox marker has an unsupported kind or schema")

    def _mark_remote_initialized(self) -> None:
        if not self._remote_initialized:
            marker = self.path / ".git" / "remote-claude-mailbox.json"
            temporary = marker.with_suffix(".tmp")
            temporary.write_bytes(canonical_json({
                "remote": self.remote, "branch": self.branch, "remote_initialized": True,
            }))
            os.replace(temporary, marker)
            self._remote_initialized = True

    def _sync_unlocked(self, create: bool = False) -> bool:
        before = self._head_unlocked()
        fetched = self._fetch()
        if fetched.returncode:
            listed = self._run(
                "ls-remote", "--exit-code", "--heads", "origin", self._branch_ref,
                check=False,
            )
            if listed.returncode != 2:
                detail = (listed.stderr or fetched.stderr).strip()
                raise GitError(f"Cannot fetch mailbox branch: {detail}")
            if not create:
                raise GitError(f"Mailbox branch {self.branch!r} does not exist; run client init")
            if self._remote_initialized:
                raise GitError("The mailbox branch disappeared; refusing to recreate existing conversation history")
            self._create_branch()
        else:
            self._validate_remote_tree()
            self._run("checkout", "--quiet", "--force", "-B", self.branch, self._remote_ref)
            self._run("clean", "-fdx", "--quiet")
        self._mark_remote_initialized()
        return before != self._head_unlocked()

    def _create_branch(self) -> None:
        # A previous initialization may have committed locally before its push
        # failed. Reset only this dedicated cache to an unborn branch.
        self._run("symbolic-ref", "HEAD", self._branch_ref)
        self._run("update-ref", "-d", self._branch_ref)
        self._run("read-tree", "--empty")
        self._run("clean", "-fdx", "--quiet")
        (self.path / ".mailbox.json").write_bytes(canonical_json({
            "schema_version": 1, "kind": "remote-claude-mailbox",
        }))
        self._run("add", "--", ".mailbox.json")
        self._run("commit", "--quiet", "-m", "Initialize remote Claude mailbox")
        pushed = self._run("push", "--porcelain", "origin", f"HEAD:{self._branch_ref}", check=False)
        if pushed.returncode:
            fetched = self._fetch()
            if fetched.returncode:
                raise GitError(f"Cannot initialize mailbox branch: {pushed.stderr.strip()}")
            self._validate_remote_tree()
            self._run("checkout", "--quiet", "--force", "-B", self.branch, self._remote_ref)
            self._run("clean", "-fdx", "--quiet")

    def sync(self) -> bool:
        with self._locked():
            return self._sync_unlocked()

    @staticmethod
    def _validate_path(value: str) -> str:
        if not isinstance(value, str) or not value or any(char in value for char in ("\\", "\x00", "\n", "\r")):
            raise GitError("Mailbox paths must be relative POSIX paths")
        parsed = PurePosixPath(value)
        if parsed.is_absolute() or str(parsed) != value or any(part in {"..", ".git", ".gitattributes", ".gitmodules"} for part in parsed.parts):
            raise GitError(f"Unsafe mailbox path: {value!r}")
        if not parsed.parts or value == ".":
            raise GitError("Mailbox paths must identify files")
        return value

    def _target(self, name: str) -> Path:
        target = self.path / name
        current = self.path
        for part in PurePosixPath(name).parts:
            current = current / part
            if current.is_symlink():
                raise GitError(f"Refusing to follow a mailbox symlink: {name}")
        return target

    def publish(
        self, files: dict[str, bytes], message: str,
        mutable_paths: set[str] | None = None,
    ) -> str:
        immutable_payload = {}
        for name, payload in files.items():
            self._validate_path(name)
            if not isinstance(payload, bytes):
                raise GitError("Mailbox payloads must be bytes")
            immutable_payload[name] = payload
        mutable = set(mutable_paths or ())
        for name in mutable:
            self._validate_path(name)
        with self._locked():
            last_error = ""
            for attempt in range(self.MAX_PUSH_ATTEMPTS):
                self._sync_unlocked()
                changed = []
                for name, payload in immutable_payload.items():
                    target = self._target(name)
                    if target.exists():
                        if not target.is_file():
                            raise GitError(f"Mailbox path is not a regular file: {name}")
                        if target.read_bytes() == payload:
                            continue
                        if name not in mutable:
                            raise ImmutableConflict(f"Immutable mailbox file already differs: {name}")
                    changed.append((target, payload))
                for target, payload in changed:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(payload)
                if not immutable_payload:
                    return self._head_unlocked()
                self._run("add", "--force", "--", *immutable_payload)
                difference = self._run("diff", "--cached", "--quiet", check=False)
                if difference.returncode == 0:
                    return self._head_unlocked()
                if difference.returncode != 1:
                    raise GitError("Cannot inspect staged mailbox changes")
                self._run("commit", "--quiet", "-m", message)
                try:
                    pushed = self._run(
                        "push", "--porcelain", "origin", f"HEAD:{self._branch_ref}",
                        check=False,
                    )
                    if pushed.returncode == 0:
                        return self._head_unlocked()
                    last_error = (pushed.stderr or pushed.stdout).strip()
                except GitError as exc:
                    last_error = str(exc)
                if attempt + 1 < self.MAX_PUSH_ATTEMPTS:
                    time.sleep(min(0.1 * (2 ** attempt), 1.0))
            raise GitError(f"Mailbox push failed after {self.MAX_PUSH_ATTEMPTS} attempts: {last_error}")
