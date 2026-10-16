"""Versioned, immutable messages exchanged through the Git mailbox."""

from __future__ import annotations

import hashlib
import json
import math
import re
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID, uuid4


SCHEMA_VERSION = 1
ID_TIMEZONE = timezone(timedelta(hours=-8), name="PST")
ID_TIMESTAMP_FORMAT = "%Y%m%dT%H%M%S%z"
_ID_TIMESTAMP = re.compile(r"[0-9]{8}T[0-9]{6}-0800")
TERMINAL_STATES = frozenset(
    {"completed", "failed", "cancelled", "timed_out", "interrupted", "rejected"}
)


class ProtocolError(ValueError):
    """A mailbox message does not satisfy the protocol."""


def canonical_json(obj: Any) -> bytes:
    """Serialize JSON deterministically, including a final newline."""
    try:
        return (
            json.dumps(
                obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                allow_nan=False,
            ) + "\n"
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError) as exc:
        raise ProtocolError(f"Invalid JSON value: {exc}") from exc


def content_hash(obj: Any) -> str:
    return hashlib.sha256(canonical_json(obj)).hexdigest()


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def new_id(now: datetime | None = None) -> str:
    """Generate a second-precision PST timestamp with a unique random suffix.

    A fixed UTC-08:00 offset avoids daylight-saving clock rollback. The UUID
    preserves uniqueness across clients that generate IDs in the same second.
    """
    moment = datetime.now(timezone.utc) if now is None else now
    if moment.utcoffset() is None:
        raise ValueError("ID timestamps must be timezone-aware")
    timestamp = moment.astimezone(ID_TIMEZONE).strftime(ID_TIMESTAMP_FORMAT)
    return f"{timestamp}_{uuid4()}"


def validate_id(value: str) -> str:
    """Accept timestamp-prefixed IDs and existing canonical UUID identifiers."""
    error = "IDs must be a fixed-PST timestamp followed by a canonical UUID, or a legacy canonical UUID"
    if not isinstance(value, str):
        raise ProtocolError(error)
    suffix = value
    if "_" in value:
        timestamp, suffix = value.split("_", 1)
        if not _ID_TIMESTAMP.fullmatch(timestamp):
            raise ProtocolError(error)
        try:
            datetime.strptime(timestamp, ID_TIMESTAMP_FORMAT)
        except ValueError as exc:
            raise ProtocolError("ID timestamp must contain a valid calendar date and time") from exc
    try:
        parsed = UUID(suffix)
    except (ValueError, AttributeError) as exc:
        raise ProtocolError(error) from exc
    if str(parsed) != suffix:
        raise ProtocolError("ID UUID suffixes must be lowercase and hyphenated")
    return value


def _text(message: dict, name: str) -> str:
    value = message.get(name)
    if not isinstance(value, str) or not value.strip() or "\x00" in value:
        raise ProtocolError(f"{name} must be a nonempty string without NUL bytes")
    return value


def _base(message: dict) -> None:
    if not isinstance(message, dict):
        raise ProtocolError("Messages must be JSON objects")
    version = message.get("schema_version")
    if type(version) is not int or version != SCHEMA_VERSION:
        raise ProtocolError(f"Unsupported schema_version: {version!r}")
    validate_id(message.get("session_id"))
    created_at = _text(message, "created_at")
    try:
        parsed = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ProtocolError("created_at must be an ISO 8601 timestamp") from exc
    if parsed.utcoffset() is None:
        raise ProtocolError("created_at must include a timezone")


def validate_session(message: dict) -> dict:
    _base(message)
    workspace = _text(message, "workspace")
    if not workspace.startswith("~/"):
        raise ProtocolError("workspace must be a server-home path starting with ~/")
    for name in ("model", "effort"):
        value = _text(message, name)
        if value.startswith("-") or "\n" in value or "\r" in value:
            raise ProtocolError(f"{name} must be a value, not a command-line option")
    timeout = message.get("timeout_seconds")
    if (
        isinstance(timeout, bool) or not isinstance(timeout, (int, float))
        or not math.isfinite(timeout) or timeout <= 0
    ):
        raise ProtocolError("timeout_seconds must be a positive finite number")
    return message


def validate_request(message: dict) -> dict:
    _base(message)
    validate_id(message.get("request_id"))
    revision = message.get("expected_revision")
    if type(revision) is not int or revision < 0:
        raise ProtocolError("expected_revision must be a nonnegative integer")
    _text(message, "prompt")
    return message


def validate_control(message: dict) -> dict:
    _base(message)
    validate_id(message.get("control_id"))
    kind = message.get("kind")
    if not isinstance(kind, str) or kind not in {"cancel", "close", "recover"}:
        raise ProtocolError("Control kind must be cancel, close, or recover")
    if kind == "cancel":
        validate_id(message.get("request_id"))
    elif "request_id" in message:
        validate_id(message["request_id"])
    if kind == "recover" and message.get("acknowledge") is not True:
        raise ProtocolError("Recovery requires acknowledge=true")
    return message
