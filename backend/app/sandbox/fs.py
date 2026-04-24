"""Filesystem primitives with path-boundary enforcement.

These are the ONLY filesystem helpers the Coder tools should use for reading and writing
files in a project. They guarantee:

  1. Path resolves inside the project root (no `../../etc/passwd` escapes).
  2. Symlinks are followed during resolution but the final target must also be inside
     the project root.
  3. Writes always use UTF-8 encoding.
  4. Reads cap file size — no loading a 2GB file into model context.
  5. Listing a directory returns structured entries, not a string we'd have to parse.

Direct use of `open()`, `Path.write_text()`, etc. by agent tools is a bug. Always go
through these.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .executor import PathOutsideProject

# Reading a file returns at most this many bytes. Coder tool callers who genuinely need
# more should call with explicit max_bytes. This prevents a runaway agent from pulling
# gigabytes of node_modules into its context window.
_DEFAULT_READ_LIMIT = 500_000

# Directory listing truncation — most directories are small but a rogue node_modules
# has tens of thousands of entries.
_DEFAULT_LIST_LIMIT = 500


@dataclass
class FileEntry:
    """One entry in a directory listing."""

    name: str
    relative_path: str  # path relative to project root, using forward slashes
    is_dir: bool
    size_bytes: int  # 0 for directories


@dataclass
class ReadResult:
    content: str
    truncated: bool
    total_bytes: int  # actual file size, even if we only returned part


def _resolve_inside(project_root: Path, path: str) -> Path:
    """Resolve `path` (absolute or relative to project_root) and confirm it stays inside.

    Both the project root and the input path are resolved (symlinks followed) before
    comparison, so a symlink inside the project pointing outside will be caught.

    Raises PathOutsideProject if the resolved path is outside the project root.
    """
    # Resolve project_root defensively — callers might pass a raw string or a relative
    # Path. If root is relative and the input path is absolute, the .relative_to() check
    # below would be unreliable.
    root = project_root.resolve()
    p = Path(path)
    if not p.is_absolute():
        p = root / p
    resolved = p.resolve()
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise PathOutsideProject(
            f"Path {path!r} resolves to {resolved}, which is outside the project root "
            f"{root}"
        ) from exc
    return resolved


def safe_read(
    project_root: Path, path: str, *, max_bytes: int = _DEFAULT_READ_LIMIT
) -> ReadResult:
    """Read a text file from inside the project. Returns up to `max_bytes`."""
    resolved = _resolve_inside(project_root, path)
    if not resolved.exists():
        raise FileNotFoundError(f"No such file: {path}")
    if not resolved.is_file():
        raise IsADirectoryError(f"Not a file: {path}")

    total_bytes = resolved.stat().st_size
    raw = resolved.read_bytes()
    truncated = False
    if len(raw) > max_bytes:
        raw = raw[:max_bytes]
        truncated = True
    text = raw.decode("utf-8", errors="replace")
    if truncated:
        text = text + f"\n\n[...file truncated at {max_bytes} bytes of {total_bytes}]"
    return ReadResult(content=text, truncated=truncated, total_bytes=total_bytes)


def safe_write(project_root: Path, path: str, content: str) -> Path:
    """Write text to a file inside the project. Creates parent directories as needed.

    Returns the resolved path of the written file.
    """
    resolved = _resolve_inside(project_root, path)
    resolved.parent.mkdir(parents=True, exist_ok=True)
    resolved.write_text(content, encoding="utf-8")
    return resolved


def safe_list(
    project_root: Path,
    path: str = ".",
    *,
    max_entries: int = _DEFAULT_LIST_LIMIT,
    include_hidden: bool = False,
) -> tuple[list[FileEntry], bool]:
    """List a directory inside the project. Returns (entries, truncated)."""
    resolved = _resolve_inside(project_root, path)
    if not resolved.exists():
        raise FileNotFoundError(f"No such directory: {path}")
    if not resolved.is_dir():
        raise NotADirectoryError(f"Not a directory: {path}")

    # We resolve project_root the same way _resolve_inside does so the relative_to()
    # check uses a consistent base. If the caller passes a raw/relative root, we still
    # compute relative paths correctly.
    root = project_root.resolve()

    entries: list[FileEntry] = []
    truncated = False
    for child in sorted(resolved.iterdir(), key=lambda p: (not p.is_dir(), p.name)):
        if not include_hidden and child.name.startswith("."):
            continue
        # Check truncation AFTER filtering hidden — otherwise a directory of 10k hidden
        # files followed by 3 visible ones would return zero visible entries.
        if len(entries) >= max_entries:
            truncated = True
            break
        try:
            rel = child.resolve().relative_to(root)
        except ValueError:
            # A symlink pointing outside the project. Skip it rather than following.
            continue
        is_dir = child.is_dir()
        size = 0 if is_dir else child.stat().st_size
        entries.append(
            FileEntry(
                name=child.name,
                relative_path=str(rel).replace("\\", "/"),
                is_dir=is_dir,
                size_bytes=size,
            )
        )
    return entries, truncated
