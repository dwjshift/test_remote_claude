"""Build bounded, immutable Git attachments from an explicit export directory."""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
from io import BytesIO
import os
from pathlib import Path
import re
import stat
from urllib.parse import quote
import warnings

from PIL import Image


DEFAULT_LIMITS = {
    "text_limit": 262144,
    "image_limit": 2097152,
    "image_count": 10,
    "turn_image_limit": 10485760,
}
_FORMATS = {
    ".png": ("PNG", "image/png"),
    ".jpg": ("JPEG", "image/jpeg"),
    ".jpeg": ("JPEG", "image/jpeg"),
    ".webp": ("WEBP", "image/webp"),
}
_DATA_URI = re.compile(r"data:[^\s<>\"')]+", re.IGNORECASE)
_MAX_IMAGE_PIXELS = 20_000_000
_MAX_ENTRIES = 1000


@dataclass(frozen=True)
class ArtifactBundle:
    answer: str
    files: dict[str, bytes]
    artifacts: list[dict]
    skipped: list[dict]
    total_bytes: int


def _bounded_text(value: str, limit: int) -> str:
    marker = "\n\n[Answer truncated; complete output remains on the server.]\n"
    raw = value.encode("utf-8", errors="replace")
    if len(raw) <= limit:
        return value
    suffix = marker.encode("utf-8")
    if limit < len(suffix):
        return suffix[:limit].decode("utf-8", errors="ignore")
    return raw[: limit - len(suffix)].decode("utf-8", errors="ignore") + marker


def _display_name(value: str) -> str:
    return "".join(c if c.isprintable() else "?" for c in value).replace("`", "'")[:240]


def _read_regular_file(directory_fd: int, name: str, maximum: int) -> bytes:
    """Open relative to a trusted directory fd, refusing links and special files."""
    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory_fd)
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode):
            raise ValueError("not a regular file")
        if before.st_nlink != 1:
            raise ValueError("hard links are not accepted")
        if before.st_size > maximum:
            raise ValueError("exceeds the per-image size limit")
        chunks: list[bytes] = []
        size = 0
        while size <= maximum:
            chunk = os.read(fd, min(65536, maximum + 1 - size))
            if not chunk:
                break
            size += len(chunk)
            chunks.append(chunk)
        after = os.fstat(fd)
        if size > maximum:
            raise ValueError("exceeds the per-image size limit")
        if (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
            after.st_size, after.st_mtime_ns, after.st_ctime_ns
        ) or size != after.st_size:
            raise ValueError("file changed while being collected")
        return b"".join(chunks)
    finally:
        os.close(fd)


def _verify_image(data: bytes, suffix: str) -> str:
    expected_format, mime = _FORMATS[suffix]
    with warnings.catch_warnings():
        warnings.simplefilter("error", Image.DecompressionBombWarning)
        with Image.open(BytesIO(data)) as picture:
            if picture.format != expected_format:
                raise ValueError("image content does not match its extension")
            if picture.width * picture.height > _MAX_IMAGE_PIXELS:
                raise ValueError("image exceeds the decoded pixel limit")
            if getattr(picture, "n_frames", 1) != 1:
                raise ValueError("animated images are not supported")
            picture.verify()
        with Image.open(BytesIO(data)) as picture:
            picture.load()
    return mime


def collect_artifacts(
    export_dir: Path,
    answer: str,
    limits: dict | None = None,
    *,
    remaining_bytes: int,
    output_warnings: list[dict] | None = None,
) -> ArtifactBundle:
    """Collect small static images only; no network calls or project-wide scans.

    Only files immediately inside the export directory are accepted. Nested
    directories are deliberately excluded, so no directory symlink can escape
    the explicit export boundary. Returned bytes are the validated snapshot.
    """
    policy = dict(DEFAULT_LIMITS)
    if limits:
        unknown = set(limits) - set(policy)
        if unknown:
            raise ValueError(f"Unknown artifact limits: {sorted(unknown)}")
        policy.update(limits)
    if any(not isinstance(v, int) or isinstance(v, bool) or v < 0 for v in policy.values()):
        raise ValueError("Artifact limits must be non-negative integers")
    if remaining_bytes < 0:
        raise ValueError("remaining_bytes must be non-negative")
    files: dict[str, bytes] = {}
    artifacts: list[dict] = []
    skipped: list[dict] = []
    total = 0
    notes: list[str] = []

    def skip(name: str, reason: str, size: int | None = None) -> None:
        # Filesystem surrogate escapes are not valid protocol UTF-8 strings.
        record: dict = {"path": name.encode("utf-8", errors="backslashreplace").decode("utf-8"), "reason": reason}
        if size is not None:
            record["size"] = size
        skipped.append(record)

    export_dir = Path(export_dir)
    directory_fd: int | None = None
    try:
        directory_fd = os.open(export_dir, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except FileNotFoundError:
        pass
    except OSError:
        skip("exports", "export directory is unavailable or is a symbolic link")
    if directory_fd is not None:
        try:
            # Bound directory enumeration too: an accidental dataset export must
            # not create millions of notes or consume unbounded server memory.
            names: list[str] = []
            with os.scandir(directory_fd) as entries:
                for entry in entries:
                    if len(names) >= _MAX_ENTRIES:
                        skip("exports", f"only the first {_MAX_ENTRIES} directory entries were inspected")
                        break
                    names.append(entry.name)
            for name in sorted(names):
                try:
                    name.encode("utf-8")
                    if any(character in name for character in ("\\", "\n", "\r", "\x00")):
                        raise ValueError("unsupported filename")
                except (UnicodeError, ValueError):
                    skip(name, "filename cannot be represented safely in the Git mailbox")
                    continue
                suffix = Path(name).suffix.lower()
                try:
                    info = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
                    if not stat.S_ISREG(info.st_mode):
                        skip(name, "only regular files directly in the export directory are accepted")
                        continue
                    if info.st_nlink != 1:
                        skip(name, "hard links are not accepted")
                        continue
                    if suffix not in _FORMATS:
                        skip(name, "unsupported attachment type; download data separately", info.st_size)
                        continue
                    if info.st_size > policy["image_limit"]:
                        skip(name, "exceeds the per-image size limit", info.st_size)
                        continue
                    if len(artifacts) >= policy["image_count"]:
                        skip(name, "exceeds the per-turn image count", info.st_size)
                        continue
                    budget = min(policy["turn_image_limit"], remaining_bytes)
                    if total + info.st_size > budget:
                        skip(name, "exceeds the remaining image byte budget", info.st_size)
                        continue
                    data = _read_regular_file(directory_fd, name, policy["image_limit"])
                    if total + len(data) > budget:
                        skip(name, "exceeds the remaining image byte budget", len(data))
                        continue
                    mime = _verify_image(data, suffix)
                except (OSError, ValueError, SyntaxError, EOFError, Image.DecompressionBombError,
                        Image.DecompressionBombWarning) as exc:
                    skip(name, f"invalid or unreadable image: {type(exc).__name__}")
                    continue
                relative = f"artifacts/{name}"
                files[relative] = data
                artifacts.append({"path": relative, "mime": mime, "size": len(data),
                                  "sha256": sha256(data).hexdigest()})
                total += len(data)
                label = _display_name(name).replace("[", "\\[").replace("]", "\\]")
                notes.append(f"![{label}]({quote(relative, safe='/')})")
        finally:
            os.close(directory_fd)
    for item in skipped:
        size = f" ({item['size']} bytes)" if "size" in item else ""
        notes.append(f"- Attachment `{_display_name(item['path'])}`{size} not uploaded: {item['reason']}.")
    safe_answer = _DATA_URI.sub("[inline data omitted]", str(answer))
    if notes:
        safe_answer += "\n\n" + "\n\n".join(notes)
    if output_warnings:
        safe_answer = (
            "> **Output notice:** Some Claude content could not be included in this Markdown reply. "
            "Ask for a text explanation or a static PNG, JPEG, or WebP image if needed. "
            "Details are recorded in `result.json` under `metadata.output_warnings`.\n\n"
            + safe_answer
        )
    safe_answer = _bounded_text(safe_answer, policy["text_limit"])
    return ArtifactBundle(safe_answer, files, artifacts, skipped, total)
