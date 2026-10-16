"""Validate immutable response manifests immediately before Git staging."""

from __future__ import annotations

import hashlib
import json
from pathlib import PurePosixPath


def validate_publication(files: dict[str, bytes], limits: dict | None = None) -> None:
    selected = {"text_limit": 256 * 1024, "image_limit": 2 * 1024 * 1024,
                "image_count": 10, "turn_image_limit": 10 * 1024 * 1024}
    selected.update(limits or {})
    expected_images = set()
    for name, data in files.items():
        parts = PurePosixPath(name).parts
        if len(parts) != 5 or parts[0] != "sessions" or parts[2] != "responses" or parts[4] != "result.json":
            continue
        prefix = name.removesuffix("result.json")
        result = json.loads(data)
        answer = files.get(prefix + "answer.md")
        if answer is None or len(answer) > selected["text_limit"]:
            raise ValueError("A published response must include Markdown within the configured limit")
        answer.decode("utf-8")
        artifacts = result.get("artifacts", [])
        if not isinstance(artifacts, list) or len(artifacts) > selected["image_count"]:
            raise ValueError("The publication manifest exceeds the image count limit")
        total = 0
        for artifact in artifacts:
            relative = artifact["path"]
            path = PurePosixPath(relative)
            if len(path.parts) != 2 or path.parts[0] != "artifacts" or path.parts[1] in {".", ".."} or str(path) != relative:
                raise ValueError("The publication manifest contains an unsafe artifact path")
            full_path = prefix + relative
            if full_path in expected_images:
                raise ValueError("The publication manifest contains a duplicate artifact")
            payload = files.get(full_path)
            if payload is None or len(payload) != artifact["size"] or len(payload) > selected["image_limit"]:
                raise ValueError("Artifact bytes do not match the publication size limits")
            if hashlib.sha256(payload).hexdigest() != artifact["sha256"]:
                raise ValueError("Artifact bytes do not match the immutable publication manifest")
            expected_images.add(full_path)
            total += len(payload)
        if total > selected["turn_image_limit"]:
            raise ValueError("The publication manifest exceeds the per-turn image limit")
    for name in files:
        if "/responses/" in name and "/artifacts/" in name and name not in expected_images:
            raise ValueError("An artifact is missing from its publication manifest")
