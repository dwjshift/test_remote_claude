"""Preflight and run the outbound-only Claude mailbox server."""

from __future__ import annotations

import argparse
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import threading

from common.config import ConfigError, load_config
from common.git_transport import GitError, GitMailbox
from .engine import Engine, EngineError, validate_config
from .runner import check_claude

DEFAULT_CONFIG = Path.home() / ".config" / "remote-claude" / "server.json"


def pinned_executable(value: str) -> str:
    """Resolve symlinks once so updates cannot replace a running server's CLI."""
    located = shutil.which(os.path.expanduser(value))
    if not located:
        raise EngineError(f"Claude executable was not found: {value}")
    return str(Path(located).resolve(strict=True))


def authentication(executable: str) -> dict:
    """Report authentication status without exposing account or token details."""
    try:
        result = subprocess.run([executable, "auth", "status", "--json"],
                                stdin=subprocess.DEVNULL, capture_output=True,
                                text=True, timeout=20,
                                env={**os.environ, "DISABLE_AUTOUPDATER": "1"})
        if result.returncode != 0:
            return {"logged_in": False, "error": "Could not verify Claude authentication"}
        status = json.loads(result.stdout)
        if not isinstance(status, dict):
            return {"logged_in": False, "error": "Could not verify Claude authentication"}
        return {"logged_in": status.get("loggedIn") is True,
                "auth_method": status.get("authMethod"), "api_provider": status.get("apiProvider")}
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return {"logged_in": False, "error": "Could not verify Claude authentication"}


def doctor(config: dict) -> dict:
    report = {"ok": False, "platform": sys.platform, "python": sys.version.split()[0],
              "branch": config["branch"], "state_dir": config["state_dir"], "errors": []}
    if not sys.platform.startswith("linux") or not Path("/proc").is_dir():
        report["errors"].append("The server currently requires Linux with /proc")
    try:
        executable = pinned_executable(config["claude_executable"])
        report["claude"] = check_claude(executable, config["claude_version"])
        if not report["claude"]["ok"]:
            report["errors"].append("Claude version or required flags failed preflight")
        report["authentication"] = authentication(executable)
        if not report["authentication"]["logged_in"]:
            report["errors"].append("Run claude auth login interactively before starting the server")
    except (OSError, EngineError) as exc:
        report["errors"].append(str(exc))
    try:
        mailbox = GitMailbox(config["remote"], config["branch"],
                             Path(config["state_dir"]) / "mailbox", git_timeout=config.get("git_timeout", 30))
        report["mailbox_head"] = mailbox.head
    except GitError as exc:
        report["errors"].append(str(exc))
    report["ok"] = not report["errors"]
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run unattended Claude Code through a Git mailbox.")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG), help="Server JSON configuration path.")
    subparsers = parser.add_subparsers(dest="command", required=True)
    for name, help_text in (("doctor", "Check CLI version, authentication, and mailbox access."),
                            ("serve", "Run the server until SIGINT or SIGTERM.")):
        command = subparsers.add_parser(name, help=help_text)
        command.add_argument("--config", default=argparse.SUPPRESS, help="Server JSON configuration path.")
    args = parser.parse_args(argv)
    try:
        config = validate_config(load_config(args.config))
        if args.command == "doctor":
            report = doctor(config)
            print(json.dumps(report, ensure_ascii=False, indent=2))
            return 0 if report["ok"] else 1
        if not sys.platform.startswith("linux"):
            raise EngineError("The server currently requires Linux with /proc")
        config["claude_executable"] = pinned_executable(config["claude_executable"])
        preflight = check_claude(config["claude_executable"], config["claude_version"])
        if not preflight["ok"]:
            raise EngineError("; ".join(preflight["errors"]))
        if not authentication(config["claude_executable"])["logged_in"]:
            raise EngineError("Claude is not authenticated; run claude auth login before starting the server")
        state_dir = Path(config["state_dir"])
        state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        handler = RotatingFileHandler(state_dir / "server.log", maxBytes=2 * 1024 * 1024,
                                     backupCount=3, encoding="utf-8")
        logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                            handlers=[logging.StreamHandler(), handler])
        stopped = threading.Event()
        previous = {}
        for signum in (signal.SIGINT, signal.SIGTERM):
            previous[signum] = signal.signal(signum, lambda *_: stopped.set())
        try:
            with Engine(config) as engine:
                logging.info("Server started with pinned Claude %s at %s", config["claude_version"], config["claude_executable"])
                while not stopped.is_set():
                    engine.tick()
                    stopped.wait(0.1)
                logging.info("Stopping server and cancelling active workers")
        finally:
            for signum, old_handler in previous.items():
                signal.signal(signum, old_handler)
            handler.close()
        return 0
    except (ConfigError, EngineError, GitError, OSError, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
