import subprocess

import pytest

from common.git_transport import GitError, GitMailbox, ImmutableConflict


@pytest.fixture
def transport_remote(tmp_path):
    path = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", "--quiet", str(path)], check=True)
    return str(path)


def test_new_mailbox_is_orphan_and_never_uses_main(tmp_path, transport_remote):
    mailbox = GitMailbox(transport_remote, "mailbox", tmp_path / "checkout", create=True)
    assert (mailbox.path / ".mailbox.json").is_file()
    assert mailbox._run("rev-list", "--count", "HEAD").stdout.strip() == "1"
    refs = subprocess.run(
        ["git", "--git-dir", transport_remote, "for-each-ref", "--format=%(refname)"],
        check=True, text=True, capture_output=True,
    ).stdout.splitlines()
    assert refs == ["refs/heads/mailbox"]
    with pytest.raises(GitError, match="main"):
        GitMailbox(transport_remote, "main", tmp_path / "forbidden", create=True)


def test_immutable_retry_is_idempotent_and_status_can_be_mutable(tmp_path, transport_remote):
    mailbox = GitMailbox(transport_remote, "mailbox", tmp_path / "checkout", create=True)
    first = mailbox.publish({"sessions/a/request.json": b"one\n"}, "First request")
    assert mailbox.publish({"sessions/a/request.json": b"one\n"}, "Retry") == first
    with pytest.raises(ImmutableConflict):
        mailbox.publish({"sessions/a/request.json": b"different\n"}, "Bad retry")
    mailbox.publish({"sessions/a/status.json": b"queued\n"}, "Queued")
    mailbox.publish(
        {"sessions/a/status.json": b"running\n"}, "Running",
        mutable_paths={"sessions/a/status.json"},
    )
    assert (mailbox.path / "sessions/a/status.json").read_bytes() == b"running\n"


def test_push_race_preserves_both_publishers_files(tmp_path, transport_remote, monkeypatch):
    first = GitMailbox(transport_remote, "mailbox", tmp_path / "first", create=True)
    second = GitMailbox(transport_remote, "mailbox", tmp_path / "second")
    original = first._run
    raced = False

    def run(*args, **kwargs):
        nonlocal raced
        if args[0] == "push" and not raced:
            raced = True
            second.publish({"sessions/b/request.json": b"second\n"}, "Other publisher")
        return original(*args, **kwargs)

    monkeypatch.setattr(first, "_run", run)
    first.publish({"sessions/a/request.json": b"first\n"}, "First publisher")
    assert raced
    second.sync()
    assert (second.path / "sessions/a/request.json").read_bytes() == b"first\n"
    assert (second.path / "sessions/b/request.json").read_bytes() == b"second\n"
    assert second._run("rev-list", "--count", "HEAD").stdout.strip() == "3"


def test_push_that_succeeded_before_connection_loss_is_not_duplicated(tmp_path, transport_remote, monkeypatch):
    mailbox = GitMailbox(transport_remote, "mailbox", tmp_path / "checkout", create=True)
    original = mailbox._run
    failed = False

    def run(*args, **kwargs):
        nonlocal failed
        result = original(*args, **kwargs)
        if args[0] == "push" and not failed:
            failed = True
            return subprocess.CompletedProcess(result.args, 1, "", "connection lost after push")
        return result

    monkeypatch.setattr(mailbox, "_run", run)
    mailbox.publish({"result.json": b"done\n"}, "Completed")
    assert failed
    assert mailbox._run("rev-list", "--count", "HEAD").stdout.strip() == "2"


def test_failed_initial_push_can_be_retried_with_same_cache(tmp_path, transport_remote, monkeypatch):
    original = GitMailbox._run

    def fail_push(self, *args, **kwargs):
        if args[0] == "push":
            return subprocess.CompletedProcess(["git", *args], 1, "", "offline")
        return original(self, *args, **kwargs)

    with monkeypatch.context() as context:
        context.setattr(GitMailbox, "_run", fail_push)
        with pytest.raises(GitError, match="initialize"):
            GitMailbox(transport_remote, "mailbox", tmp_path / "checkout", create=True)
    mailbox = GitMailbox(transport_remote, "mailbox", tmp_path / "checkout", create=True)
    assert mailbox._run("rev-list", "--count", "HEAD").stdout.strip() == "1"


def test_init_does_not_recreate_a_deleted_mailbox_branch(tmp_path, transport_remote):
    mailbox = GitMailbox(transport_remote, "mailbox", tmp_path / "checkout", create=True)
    mailbox.publish({"session.json": b"history\n"}, "Persist history")
    subprocess.run(["git", "--git-dir", transport_remote, "update-ref", "-d", "refs/heads/mailbox"], check=True)
    with pytest.raises(GitError, match="disappeared"):
        GitMailbox(transport_remote, "mailbox", mailbox.path, create=True)
    assert (mailbox.path / "session.json").read_bytes() == b"history\n"
    assert subprocess.run(
        ["git", "--git-dir", transport_remote, "show-ref", "--verify", "refs/heads/mailbox"],
        capture_output=True,
    ).returncode != 0


def test_an_unmarked_code_branch_is_not_accepted_as_a_mailbox(tmp_path, transport_remote):
    mailbox = GitMailbox(transport_remote, "mailbox", tmp_path / "checkout", create=True)
    mailbox._run("rm", "--", ".mailbox.json")
    (mailbox.path / "README.md").write_text("Ordinary source branch.\n")
    mailbox._run("add", "--", "README.md")
    mailbox._run("commit", "--quiet", "-m", "Code branch fixture")
    mailbox._run("push", "--quiet", "origin", "HEAD:refs/heads/source")
    with pytest.raises(GitError, match="marker"):
        GitMailbox(transport_remote, "source", tmp_path / "source-cache")


def test_existing_directory_is_never_treated_as_disposable(tmp_path, transport_remote):
    directory = tmp_path / "project"
    directory.mkdir()
    sentinel = directory / "valuable.txt"
    sentinel.write_text("keep me")
    with pytest.raises(GitError, match="non-mailbox"):
        GitMailbox(transport_remote, "mailbox", directory, create=True)
    assert sentinel.read_text() == "keep me"


@pytest.mark.parametrize("path", ["../escape", "/absolute", ".git/config", "a/../b", "a//b", "a\\b"])
def test_publish_rejects_unsafe_paths(tmp_path, transport_remote, path):
    mailbox = GitMailbox(transport_remote, "mailbox", tmp_path / "checkout", create=True)
    with pytest.raises(GitError, match="path"):
        mailbox.publish({path: b"no"}, "Invalid")


def test_symlink_target_is_never_followed_and_hooks_do_not_run(tmp_path, transport_remote):
    mailbox = GitMailbox(transport_remote, "mailbox", tmp_path / "checkout", create=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    (mailbox.path / "escape").symlink_to(outside, target_is_directory=True)
    marker = tmp_path / "hook-ran"
    hook = mailbox.path / ".git/hooks/pre-commit"
    hook.write_text(f"#!/bin/sh\ntouch '{marker}'\n")
    hook.chmod(0o755)
    mailbox._run("add", "--", "escape")
    mailbox._run("commit", "--quiet", "-m", "Add symlink fixture")
    mailbox._run("push", "--quiet", "origin", "HEAD:refs/heads/mailbox")
    assert not marker.exists()
    with pytest.raises(GitError, match="symlink"):
        mailbox.publish({"escape/leak.txt": b"no"}, "Rejected")
    assert not (outside / "leak.txt").exists()


def test_cache_lock_times_out_instead_of_overlapping_git_operations(tmp_path, transport_remote):
    mailbox = GitMailbox(transport_remote, "mailbox", tmp_path / "checkout", create=True)
    mailbox.git_timeout = 0.1
    with mailbox._locked():
        with pytest.raises(GitError, match="checkout lock"):
            mailbox.sync()


def record_git_commands(monkeypatch, mailbox):
    commands = []
    original = mailbox._run

    def run(*args, **kwargs):
        commands.append(args)
        return original(*args, **kwargs)

    monkeypatch.setattr(mailbox, "_run", run)
    return commands


def test_unchanged_head_skips_tree_validation_but_cleans_cache(tmp_path, transport_remote, monkeypatch):
    mailbox = GitMailbox(transport_remote, "mailbox", tmp_path / "checkout", create=True)
    commands = record_git_commands(monkeypatch, mailbox)
    (mailbox.path / ".mailbox.json").write_text("Local edits must be discarded.")
    (mailbox.path / "untracked.txt").write_text("Temporary content.")
    assert mailbox.sync() is False
    assert not any(command[0] in {"ls-tree", "diff-tree", "cat-file", "show"} for command in commands)
    assert not (mailbox.path / "untracked.txt").exists()
    assert '"schema_version":1' in (mailbox.path / ".mailbox.json").read_text()


def test_reopening_cache_always_validates_complete_tree(tmp_path, transport_remote, monkeypatch):
    mailbox = GitMailbox(transport_remote, "mailbox", tmp_path / "checkout", create=True)
    original = GitMailbox._run
    commands = []

    def run(self, *args, **kwargs):
        commands.append(args)
        return original(self, *args, **kwargs)

    monkeypatch.setattr(GitMailbox, "_run", run)
    reopened = GitMailbox(transport_remote, "mailbox", mailbox.path)
    assert sum(command[0] == "ls-tree" for command in commands) == 1
    commands.clear()
    reopened.sync()
    assert not any(command[0] == "ls-tree" for command in commands)


def test_changed_paths_reports_add_modify_delete_without_rename_folding(tmp_path, transport_remote, monkeypatch):
    mailbox = GitMailbox(transport_remote, "mailbox", tmp_path / "first", create=True)
    base = mailbox.publish({"old.txt": b"move\n", "delete.txt": b"delete\n", "modify.txt": b"before\n"}, "Seed")
    mailbox.sync()
    publisher = GitMailbox(transport_remote, "mailbox", tmp_path / "second")
    publisher._run("mv", "--", "old.txt", "new.txt")
    publisher._run("rm", "--", "delete.txt")
    (publisher.path / "modify.txt").write_bytes(b"after\n")
    (publisher.path / "tab\tname.txt").write_bytes(b"new\n")
    publisher._run("add", "--all")
    publisher._run("commit", "--quiet", "-m", "Change mailbox files")
    publisher._run("push", "--quiet", "origin", "HEAD:refs/heads/mailbox")
    commands = record_git_commands(monkeypatch, mailbox)
    assert mailbox.sync() is True
    assert set(mailbox.changed_paths(base)) == {"delete.txt", "old.txt", "new.txt", "modify.txt", "tab\tname.txt"}
    assert not any(command[0] == "ls-tree" for command in commands)
    # Validation and discovery can reuse the same raw diff safely.
    assert sum(command[0] == "diff-tree" for command in commands) == 1
    assert mailbox.changed_paths(mailbox.head) == []


def test_output_only_commit_lists_only_output_paths(tmp_path, transport_remote):
    mailbox = GitMailbox(transport_remote, "mailbox", tmp_path / "first", create=True)
    base = mailbox.head
    output = "sessions/example/responses/request/answer.md"
    head = mailbox.publish({output: b"A response.\n", "_server/status.json": b"{}\n"}, "Output")
    assert mailbox.changed_paths(base, head) == ["_server/status.json", output]


@pytest.mark.parametrize("base", [None, "", "0" * 40, "--all", "HEAD", "HEAD~1"])
def test_missing_or_invalid_checkpoint_requires_full_discovery(tmp_path, transport_remote, base):
    mailbox = GitMailbox(transport_remote, "mailbox", tmp_path / "checkout", create=True)
    assert mailbox.changed_paths(base) is None
    with pytest.raises(GitError, match="resolve mailbox HEAD"):
        mailbox.changed_paths(mailbox.head, "--all")


def test_rewritten_history_falls_back_to_complete_validation_and_discovery(tmp_path, transport_remote, monkeypatch):
    mailbox = GitMailbox(transport_remote, "mailbox", tmp_path / "checkout", create=True)
    original_head = mailbox.head
    newer = mailbox.publish({"later.txt": b"later\n"}, "Later")
    mailbox.sync()
    subprocess.run(
        ["git", "--git-dir", transport_remote, "update-ref", "refs/heads/mailbox", original_head], check=True,
    )
    commands = record_git_commands(monkeypatch, mailbox)
    assert mailbox.sync() is True
    assert mailbox.changed_paths(newer) is None
    assert sum(command[0] == "ls-tree" for command in commands) == 1
    assert not (mailbox.path / "later.txt").exists()


def test_missing_validated_commit_falls_back_to_complete_validation(tmp_path, transport_remote, monkeypatch):
    mailbox = GitMailbox(transport_remote, "mailbox", tmp_path / "checkout", create=True)
    mailbox._validated_head = "0" * 40
    commands = record_git_commands(monkeypatch, mailbox)
    mailbox.sync()
    assert sum(command[0] == "ls-tree" for command in commands) == 1


@pytest.mark.parametrize("unsafe", ["symlink", "gitlink", "attributes", "modules", "newline", "invalid_utf8", "marker_deleted", "marker_invalid", "marker_large"])
def test_incremental_validation_rejects_unsafe_change_before_checkout(tmp_path, transport_remote, unsafe):
    mailbox = GitMailbox(transport_remote, "mailbox", tmp_path / "reader", create=True)
    validated_head = mailbox.head
    writer = GitMailbox(transport_remote, "mailbox", tmp_path / "writer")
    if unsafe == "symlink":
        (writer.path / "link").symlink_to(tmp_path / "outside")
    elif unsafe == "gitlink":
        writer._run("update-index", "--add", "--cacheinfo", f"160000,{writer.head},submodule")
    elif unsafe == "attributes":
        (writer.path / ".gitattributes").write_text("* filter=untrusted\n")
    elif unsafe == "modules":
        (writer.path / ".gitmodules").write_text("Untrusted submodule configuration.\n")
    elif unsafe == "newline":
        (writer.path / "line\nbreak.txt").write_text("Invalid filename.\n")
    elif unsafe == "invalid_utf8":
        (writer.path / "invalid\udcff.txt").write_text("Invalid filename.\n")
    elif unsafe == "marker_deleted":
        (writer.path / ".mailbox.json").unlink()
    elif unsafe == "marker_invalid":
        (writer.path / ".mailbox.json").write_text('{"kind":"wrong","schema_version":1}\n')
    else:
        (writer.path / ".mailbox.json").write_text(" " * 4097)
    if unsafe != "gitlink":
        writer._run("add", "--all")
    writer._run("commit", "--quiet", "-m", "Unsafe mailbox fixture")
    writer._run("push", "--quiet", "origin", "HEAD:refs/heads/mailbox")
    with pytest.raises(GitError):
        mailbox.sync()
    assert mailbox.head == validated_head
    assert mailbox._validated_head == validated_head
    assert not (mailbox.path / "link").is_symlink()
    assert not (mailbox.path / "submodule").exists()


def test_valid_marker_update_is_checked_incrementally(tmp_path, transport_remote, monkeypatch):
    mailbox = GitMailbox(transport_remote, "mailbox", tmp_path / "first", create=True)
    publisher = GitMailbox(transport_remote, "mailbox", tmp_path / "second")
    publisher.publish(
        {".mailbox.json": b'{"kind":"remote-claude-mailbox","schema_version":1,"note":"valid"}\n'},
        "Update marker", mutable_paths={".mailbox.json"},
    )
    commands = record_git_commands(monkeypatch, mailbox)
    mailbox.sync()
    assert not any(command[0] == "ls-tree" for command in commands)
    assert sum(command[0] == "cat-file" for command in commands) == 1
