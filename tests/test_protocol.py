import json
from datetime import datetime, timedelta, timezone
import time
from uuid import uuid4

import pytest

from common.config import ConfigError, load_config, save_config
from common.protocol import (
    ProtocolError, canonical_json, content_hash, new_id, utc_now, validate_control,
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
def test_ids_reject_malformed_legacy_identifiers(value):
    with pytest.raises(ProtocolError):
        validate_id(value)


@pytest.mark.parametrize(
    ("instant", "expected"),
    [
        ("2026-01-16T02:03:04.123456+00:00", "20260115T180304-0800"),
        ("2026-07-16T02:03:04.123456+00:00", "20260715T180304-0800"),
        ("2026-11-01T08:59:59.999999+00:00", "20261101T005959-0800"),
        ("2026-11-01T09:00:00.000000+00:00", "20261101T010000-0800"),
    ],
)
def test_ids_use_fixed_pst_including_summer_and_clock_rollback(instant, expected):
    moment = datetime.fromisoformat(instant)
    identifier = new_id(moment)
    assert identifier.split("_", 1)[0] == expected
    assert validate_id(identifier) == identifier
    same_instant = moment.astimezone(timezone(timedelta(hours=9)))
    assert new_id(same_instant).split("_", 1)[0] == expected


def test_default_id_generation_is_independent_of_machine_timezone(monkeypatch):
    instant = datetime(2026, 7, 16, 2, 3, 4, 123456, tzinfo=timezone.utc)

    class FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            if tz is None:
                return instant.astimezone().replace(tzinfo=None)
            return instant.astimezone(tz)

    monkeypatch.setattr("common.protocol.datetime", FrozenDatetime)
    timestamps = []
    try:
        with monkeypatch.context() as environment:
            for zone in ("UTC0", "PST8PDT", "JST-9"):
                environment.setenv("TZ", zone)
                time.tzset()
                timestamps.append(new_id().split("_", 1)[0])
    finally:
        time.tzset()
    assert timestamps == ["20260715T180304-0800"] * 3


def test_ids_sort_chronologically_and_remain_unique_within_the_same_second():
    moments = [datetime.fromisoformat(value) for value in (
        "2026-11-01T07:59:59.999999+00:00",
        "2026-11-01T08:00:00.000000+00:00",
        "2026-11-01T08:59:59.999999+00:00",
        "2026-11-01T09:00:00.000000+00:00",
    )]
    identifiers = [new_id(moment) for moment in moments]
    assert sorted(reversed(identifiers)) == identifiers
    concurrent = {new_id(moments[0].replace(microsecond=index)) for index in range(100)}
    assert len(concurrent) == 100
    assert {identifier.split("_", 1)[0] for identifier in concurrent} == {
        "20261031T235959-0800"
    }
    with pytest.raises(ValueError, match="timezone-aware"):
        new_id(datetime(2026, 10, 15, 18, 45, 30))


@pytest.mark.parametrize("timestamp", [
    "20260230T184530-0800", "20261315T184530-0800",
    "20261015T244530-0800", "20261015T184530.123456-0800",
    "20261015T184530-0700", "20261015T184530Z",
    "../20261015T184530-0800", "20261015T184530-0800/extra",
])
def test_timestamp_ids_reject_invalid_dates_formats_and_paths(timestamp):
    with pytest.raises(ProtocolError):
        validate_id(f"{timestamp}_{uuid4()}")


def test_timestamp_ids_require_canonical_uuid_suffix_and_accept_legacy_ids():
    legacy = str(uuid4())
    assert validate_id(legacy) == legacy
    prefix = "20261015T184530-0800_"
    for suffix in (legacy.upper(), legacy.replace("-", ""), "../escape", "", legacy + "_extra"):
        with pytest.raises(ProtocolError):
            validate_id(prefix + suffix)


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
