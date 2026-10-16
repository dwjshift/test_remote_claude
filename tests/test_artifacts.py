from __future__ import annotations

from hashlib import sha256
import os
from pathlib import Path

from PIL import Image
import pytest

from server.artifacts import collect_artifacts
from common.protocol import canonical_json


def make_image(path: Path, format: str = "PNG") -> bytes:
    Image.new("RGB", (16, 16), color=(42, 128, 192)).save(path, format=format)
    return path.read_bytes()


def test_collects_real_static_images_and_preserves_https(tmp_path):
    exports = tmp_path / "exports"
    exports.mkdir()
    payload = make_image(exports / "plot space.png")
    make_image(exports / "photo.jpg", "JPEG")
    make_image(exports / "preview.webp", "WEBP")
    answer = "See [reference](https://example.com/a?q=x)."
    bundle = collect_artifacts(exports, answer, remaining_bytes=100000)
    assert bundle.total_bytes == sum(len(data) for data in bundle.files.values())
    assert len(bundle.artifacts) == 3
    assert "https://example.com/a?q=x" in bundle.answer
    assert "artifacts/plot%20space.png" in bundle.answer
    item = next(item for item in bundle.artifacts if item["path"].endswith("plot space.png"))
    assert item == {"path": "artifacts/plot space.png", "mime": "image/png",
                    "size": len(payload), "sha256": sha256(payload).hexdigest()}
    (exports / "plot space.png").write_bytes(b"changed after collection")
    assert bundle.files["artifacts/plot space.png"] == payload


def test_rejects_data_spoofed_formats_links_and_special_files(tmp_path):
    exports = tmp_path / "exports"
    exports.mkdir()
    make_image(exports / "valid.png")
    (exports / "weights.bin").write_bytes(b"x" * 100)
    (exports / "fake.png").write_text("not an image")
    make_image(exports / "wrong.png", "JPEG")
    (exports / "link.png").symlink_to(exports / "valid.png")
    source = tmp_path / "outside.png"
    make_image(source)
    os.link(source, exports / "hard.png")
    os.mkfifo(exports / "pipe.png")
    nested = exports / "nested"
    nested.mkdir()
    make_image(nested / "hidden.png")
    bundle = collect_artifacts(exports, "Done", remaining_bytes=100000)
    assert list(bundle.files) == ["artifacts/valid.png"]
    assert len(bundle.skipped) == 7
    assert "not uploaded" in bundle.answer


def test_unsupported_filenames_do_not_block_valid_response_publication(tmp_path):
    make_image(tmp_path / "valid.png")
    for name in ("back\\slash.png", "line\nbreak.png", "carriage\rreturn.png", "invalid-\udcff.png"):
        make_image(tmp_path / name)
    bundle = collect_artifacts(tmp_path, "Done", remaining_bytes=100000)
    assert list(bundle.files) == ["artifacts/valid.png"]
    assert len(bundle.skipped) == 4
    canonical_json(bundle.skipped)
    assert "not uploaded" in bundle.answer


def test_size_limits_accept_exact_boundary_and_skip_one_byte_over(tmp_path):
    payload = make_image(tmp_path / "a.png")
    size = len(payload)
    bundle = collect_artifacts(tmp_path, "Done", {"image_limit": size}, remaining_bytes=size)
    assert bundle.total_bytes == size
    for limits, remaining in (({"image_limit": size - 1}, size),
                              ({"turn_image_limit": size - 1}, size), ({}, size - 1)):
        bundle = collect_artifacts(tmp_path, "Done", limits, remaining_bytes=remaining)
        assert bundle.total_bytes == 0
        assert bundle.skipped


def test_count_and_total_budgets_are_enforced(tmp_path):
    payload = make_image(tmp_path / "a.png")
    make_image(tmp_path / "b.png")
    make_image(tmp_path / "c.png")
    limited = collect_artifacts(tmp_path, "Done", {"image_count": 1}, remaining_bytes=100000)
    assert len(limited.artifacts) == 1
    assert len(limited.skipped) == 2
    limited = collect_artifacts(tmp_path, "Done", {"turn_image_limit": len(payload) * 2},
                                remaining_bytes=100000)
    assert len(limited.artifacts) == 2
    assert limited.total_bytes == len(payload) * 2


def test_zero_remaining_budget_still_publishes_text(tmp_path):
    make_image(tmp_path / "plot.png")
    bundle = collect_artifacts(tmp_path, "Answer [link](https://example.com)", remaining_bytes=0)
    assert bundle.files == {}
    assert bundle.answer.startswith("Answer")
    assert "https://example.com" in bundle.answer
    assert "not uploaded" in bundle.answer


def test_answer_bound_applies_after_appending_notes_and_keeps_valid_utf8(tmp_path):
    make_image(tmp_path / "plot.png")
    bundle = collect_artifacts(tmp_path, "🙂" * 1000, {"text_limit": 200}, remaining_bytes=10000)
    assert len(bundle.answer.encode("utf-8")) <= 200
    assert "truncated" in bundle.answer
    assert "\ufffd" not in bundle.answer


def test_inline_data_is_removed_without_downloading_links(tmp_path):
    answer = "![huge](data:image/png;base64," + "a" * 1_000_000 + ") [ok](https://example.com/image.png)"
    bundle = collect_artifacts(tmp_path, answer, remaining_bytes=0)
    assert "data:image" not in bundle.answer
    assert "inline data omitted" in bundle.answer
    assert "https://example.com/image.png" in bundle.answer
    assert bundle.files == {}


def test_export_directory_link_is_not_followed(tmp_path):
    actual = tmp_path / "actual"
    actual.mkdir()
    make_image(actual / "hidden.png")
    symlink = tmp_path / "exports"
    symlink.symlink_to(actual, target_is_directory=True)
    bundle = collect_artifacts(symlink, "Done", remaining_bytes=10000)
    assert bundle.files == {}
    assert bundle.skipped


def test_sparse_large_image_is_rejected_before_read(tmp_path):
    with (tmp_path / "large.png").open("wb") as file:
        file.truncate(500 * 1024 * 1024)
    bundle = collect_artifacts(tmp_path, "Done", remaining_bytes=10_000_000)
    assert bundle.files == {}
    assert "size limit" in bundle.skipped[0]["reason"]


def test_decoded_pixel_limit_and_animation_are_rejected(tmp_path, monkeypatch):
    monkeypatch.setattr("server.artifacts._MAX_IMAGE_PIXELS", 100)
    make_image(tmp_path / "large_dimensions.png")
    picture = Image.new("RGB", (2, 2), "red")
    second = Image.new("RGB", (2, 2), "blue")
    picture.save(tmp_path / "animated.png", save_all=True, append_images=[second])
    bundle = collect_artifacts(tmp_path, "Done", remaining_bytes=10000)
    assert bundle.files == {}
    assert len(bundle.skipped) == 2


@pytest.mark.parametrize("limits", [{"image_limit": -1}, {"image_count": True}, {"unknown": 2}])
def test_invalid_limits_fail_explicitly(tmp_path, limits):
    with pytest.raises(ValueError):
        collect_artifacts(tmp_path, "Done", limits, remaining_bytes=100)
