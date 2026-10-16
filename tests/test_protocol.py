import json
from uuid import uuid4

import pytest

from common.config import ConfigError, load_config, save_config
from common.protocol import (
    ProtocolError, canonical_json, content_hash, utc_now, validate_control,
    validate_id, validate_request, validate_session,
)


def base_message():
    return {"schema_version": 1, "session_id": str(uuid4()), "created_at": utc_now()}


def test_canonical_serialization_is_stable_and_rejects_nan():
    assert canonical_json({"z": "é", "a": 1}) == b'{"a":1,"z":"\xc3\xa9"}\n'
    assert content_hash({"b": 2, "a": 1}) == content_hash({"a": 1, "b": 2})
    with pytest.raises(ProtocolError):
        canonical_json({"value": float("nan")})


@pytest.mark.parametrize("value", [None, "../escape", "x", str(uuid4()).upper(), uuid4().hex])
def test_ids_must_be_canonical_uuid_strings(value):
    with pytest.raises(ProtocolError):
        validate_id(value)


def test_session_settings_validate_protocol_and_server_home_workspace():
    settings = {
        **base_message(), "workspace": "~/projects/demo", "model": "sonnet",
        "effort": "high", "timeout_seconds": 3600,
    }
    assert validate_session(settings) is settings
    for update in (
        {"workspace": "/tmp/demo"}, {"timeout_seconds": 0},
        {"timeout_seconds": float("inf")}, {"timeout_seconds": True},
        {"schema_version": True}, {"created_at": "2026-01-01T10:00:00"},
        {"model": "--dangerously-skip-permissions"}, {"effort": "--help"},
    ):
        with pytest.raises(ProtocolError):
            validate_session({**settings, **update})


def test_request_requires_revision_prompt_and_valid_identifiers():
    request = {
        **base_message(), "request_id": str(uuid4()),
        "expected_revision": 0, "prompt": "Please inspect this project.",
    }
    assert validate_request(request) is request
    for update in ({"expected_revision": -1}, {"expected_revision": True}, {"prompt": " "}):
        with pytest.raises(ProtocolError):
            validate_request({**request, **update})


def test_recovery_requires_explicit_acknowledgement_and_cancel_requires_target():
    control = {**base_message(), "control_id": str(uuid4()), "kind": "recover"}
    with pytest.raises(ProtocolError, match="acknowledge"):
        validate_control(control)
    assert validate_control({**control, "acknowledge": True})["kind"] == "recover"
    with pytest.raises(ProtocolError):
        validate_control({**control, "kind": "cancel"})
    with pytest.raises(ProtocolError):
        validate_control({**control, "kind": []})


def test_config_paths_are_relative_to_config_and_extra_server_fields_survive(tmp_path):
    path = tmp_path / "config" / "settings.json"
    save_config(path, {"remote": "git@example.test:mailbox.git", "state_dir": "../runtime", "max_workers": 7})
    loaded = load_config(path)
    assert loaded["state_dir"] == str(tmp_path / "runtime")
    assert loaded["branch"] == "mailbox"
    assert loaded["watch_interval"] == 10
    assert loaded["max_workers"] == 7
    save_config(path, loaded)
    assert json.loads(path.read_text())["max_workers"] == 7
    assert path.stat().st_mode & 0o077 == 0


@pytest.mark.parametrize("update", [{"remote": None}, {"watch_interval": 0}, {"state_dir": 3}])
def test_invalid_config_is_rejected(tmp_path, update):
    path = tmp_path / "config.json"
    save_config(path, {"remote": "example", **update})
    with pytest.raises(ConfigError):
        load_config(path)


def test_local_remote_in_a_config_is_relative_to_the_config_file(tmp_path):
    path = tmp_path / "config.json"
    save_config(path, {"remote": "remote.git"})
    assert load_config(path)["remote"] == str(tmp_path / "remote.git")
