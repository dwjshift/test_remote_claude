"""Publication validation catches corruption after durable result preparation."""

import hashlib

import pytest

from common.protocol import canonical_json
from server.publication import validate_publication


def test_manifest_requires_exact_immutable_bytes():
    prefix = "sessions/session/responses/request/"
    payload = b"previously validated image bytes"
    manifest = {"artifacts": [{"path": "artifacts/image.png", "size": len(payload),
                               "sha256": hashlib.sha256(payload).hexdigest()}]}
    files = {prefix + "result.json": canonical_json(manifest), prefix + "answer.md": b"Answer",
             prefix + "artifacts/image.png": payload}
    validate_publication(files)
    files[prefix + "artifacts/image.png"] = b"x" * len(payload)
    with pytest.raises(ValueError, match="immutable publication manifest"):
        validate_publication(files)


def test_publication_rejects_unlisted_attachments_and_large_text():
    prefix = "sessions/session/responses/request/"
    files = {prefix + "result.json": canonical_json({"artifacts": []}), prefix + "answer.md": b"Answer"}
    with pytest.raises(ValueError, match="Markdown"):
        validate_publication(files, {"text_limit": 2})
    files[prefix + "artifacts/data.zip"] = b"payload"
    with pytest.raises(ValueError, match="missing"):
        validate_publication(files)
