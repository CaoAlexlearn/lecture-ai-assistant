"""Minimal workspace-scoped file tools for future agent runtimes.

This module is intentionally not a process sandbox. It provides a hard boundary
for callers that can access files only through ``WorkspaceTools``.
"""

from __future__ import annotations

import os
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import TypeAlias

PathInput: TypeAlias = str | os.PathLike[str]

IMAGE_FORMATS = {
    ".png": ("png", "image/png"),
    ".jpg": ("jpeg", "image/jpeg"),
    ".jpeg": ("jpeg", "image/jpeg"),
    ".webp": ("webp", "image/webp"),
}


class WorkspaceToolError(Exception):
    """Base error for workspace tool failures."""


class PathResolutionError(WorkspaceToolError):
    """The requested path cannot be normalized."""


class PathOutsideWorkspaceError(PathResolutionError):
    """The resolved path escapes the workspace root."""


class PermissionDeniedError(WorkspaceToolError):
    """The permission profile does not authorize the operation."""


class ToolFileNotFoundError(WorkspaceToolError):
    """An authorized path does not exist."""


class NotARegularFileError(WorkspaceToolError):
    """A tool operation received a directory or special file."""


class TextDecodeError(WorkspaceToolError):
    """An authorized text file is not valid UTF-8."""


class FileReadError(WorkspaceToolError):
    """The operating system failed to read an authorized file."""


class FileWriteError(WorkspaceToolError):
    """The operating system failed to atomically write an authorized file."""


class UnsupportedImageError(WorkspaceToolError):
    """An image extension or byte signature is unsupported or inconsistent."""


@dataclass(frozen=True, slots=True)
class ToolPermissions:
    """Simple allowlist of exact files and recursive directory roots.

    Permission entries must be workspace-relative. Read and write authority are
    deliberately independent.
    """

    readable_files: tuple[PathInput, ...] = ()
    readable_dirs: tuple[PathInput, ...] = ()
    writable_files: tuple[PathInput, ...] = ()
    writable_dirs: tuple[PathInput, ...] = ()


@dataclass(frozen=True, slots=True)
class ImageReadResult:
    """Bytes and transport-neutral metadata returned by ``read_image``."""

    path: str
    data: bytes
    format: str
    mime_type: str
    size_bytes: int


@dataclass(frozen=True, slots=True)
class DirectoryListing:
    """Bounded workspace-relative metadata from one directory walk."""

    files: tuple[str, ...]
    directories: tuple[str, ...]
    truncated: bool


@dataclass(frozen=True, slots=True)
class _CompiledPermissions:
    readable_files: frozenset[Path]
    readable_dirs: tuple[Path, ...]
    writable_files: frozenset[Path]
    writable_dirs: tuple[Path, ...]


def _is_same_or_descendant(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _detect_image_format(data: bytes) -> str | None:
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if data.startswith(b"\xff\xd8\xff"):
        return "jpeg"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    return None


class WorkspaceTools:
    """Permission-checked UTF-8 and image file operations under one workspace."""

    def __init__(
        self,
        *,
        workspace_root: PathInput,
        permissions: ToolPermissions,
    ) -> None:
        root = Path(workspace_root).expanduser()
        try:
            self._workspace_root = root.resolve(strict=True)
        except FileNotFoundError as exc:
            raise PathResolutionError(f"Workspace root does not exist: {root}") from exc
        except OSError as exc:
            raise PathResolutionError(
                f"Unable to resolve workspace root {root}: {exc}"
            ) from exc
        if not self._workspace_root.is_dir():
            raise PathResolutionError(
                f"Workspace root is not a directory: {self._workspace_root}"
            )
        self._permissions = self._compile_permissions(permissions)

    @property
    def workspace_root(self) -> Path:
        return self._workspace_root

    def _resolve(self, requested_path: PathInput) -> Path:
        raw_path = Path(requested_path).expanduser()
        candidate = (
            raw_path if raw_path.is_absolute() else self._workspace_root / raw_path
        )
        try:
            resolved = candidate.resolve(strict=False)
        except (OSError, RuntimeError, ValueError) as exc:
            raise PathResolutionError(
                f"Unable to resolve path {str(requested_path)!r}: {exc}"
            ) from exc
        if not _is_same_or_descendant(resolved, self._workspace_root):
            raise PathOutsideWorkspaceError(
                f"Path resolves outside workspace: {str(requested_path)!r} -> {resolved}"
            )
        return resolved

    def _resolve_permission_entry(self, entry: PathInput) -> Path:
        entry_path = Path(entry).expanduser()
        if entry_path.is_absolute():
            raise PathResolutionError(
                f"Permission entries must be workspace-relative: {entry_path}"
            )
        return self._resolve(entry_path)

    def _compile_permissions(
        self, permissions: ToolPermissions
    ) -> _CompiledPermissions:
        return _CompiledPermissions(
            readable_files=frozenset(
                self._resolve_permission_entry(path)
                for path in permissions.readable_files
            ),
            readable_dirs=tuple(
                self._resolve_permission_entry(path)
                for path in permissions.readable_dirs
            ),
            writable_files=frozenset(
                self._resolve_permission_entry(path)
                for path in permissions.writable_files
            ),
            writable_dirs=tuple(
                self._resolve_permission_entry(path)
                for path in permissions.writable_dirs
            ),
        )

    @staticmethod
    def _matches(path: Path, files: frozenset[Path], dirs: tuple[Path, ...]) -> bool:
        return path in files or any(
            _is_same_or_descendant(path, directory) for directory in dirs
        )

    def _authorize(self, requested_path: PathInput, *, operation: str) -> Path:
        resolved = self._resolve(requested_path)
        if operation == "read":
            allowed = self._matches(
                resolved,
                self._permissions.readable_files,
                self._permissions.readable_dirs,
            )
        elif operation == "write":
            allowed = self._matches(
                resolved,
                self._permissions.writable_files,
                self._permissions.writable_dirs,
            )
        else:
            raise ValueError(f"Unsupported operation: {operation}")
        if not allowed:
            relative = resolved.relative_to(self._workspace_root)
            raise PermissionDeniedError(
                f"{operation} permission denied for workspace path: {relative.as_posix()}"
            )
        return resolved

    @staticmethod
    def _ensure_regular_file(path: Path) -> None:
        try:
            metadata = path.stat()
        except FileNotFoundError as exc:
            raise ToolFileNotFoundError(f"File not found: {path}") from exc
        except OSError as exc:
            raise FileReadError(f"Unable to inspect file {path}: {exc}") from exc
        if not stat.S_ISREG(metadata.st_mode):
            raise NotARegularFileError(f"Path is not a regular file: {path}")

    @classmethod
    def _read_bytes(cls, path: Path) -> bytes:
        cls._ensure_regular_file(path)
        try:
            return path.read_bytes()
        except OSError as exc:
            raise FileReadError(f"Unable to read file {path}: {exc}") from exc

    def read_file(self, path: PathInput) -> str:
        """Read an authorized regular file as UTF-8 text."""

        resolved = self._authorize(path, operation="read")
        data = self._read_bytes(resolved)
        try:
            return data.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise TextDecodeError(f"File is not valid UTF-8: {resolved}") from exc

    def list_files(
        self,
        path: PathInput,
        *,
        recursive: bool = False,
        max_results: int,
        max_depth: int,
    ) -> DirectoryListing:
        """List authorized directory metadata without reading file contents.

        Symlinks are never followed or returned. Every returned item is a
        workspace-relative regular file or directory path in deterministic order.
        """

        if not isinstance(recursive, bool):
            raise TypeError("recursive must be a boolean")
        if not isinstance(max_results, int) or isinstance(max_results, bool) or max_results <= 0:
            raise ValueError("max_results must be a positive integer")
        if not isinstance(max_depth, int) or isinstance(max_depth, bool) or max_depth < 0:
            raise ValueError("max_depth must be a non-negative integer")
        resolved = self._authorize(path, operation="read")
        try:
            metadata = resolved.stat()
        except FileNotFoundError as exc:
            raise ToolFileNotFoundError(f"Directory not found: {resolved}") from exc
        except OSError as exc:
            raise FileReadError(f"Unable to inspect directory {resolved}: {exc}") from exc
        if not stat.S_ISDIR(metadata.st_mode):
            raise NotARegularFileError(f"Path is not a directory: {resolved}")

        files: list[str] = []
        directories: list[str] = []
        discovered = 0
        truncated = False

        def walk(directory: Path, depth: int) -> None:
            nonlocal discovered, truncated
            try:
                with os.scandir(directory) as iterator:
                    entries = sorted(iterator, key=lambda item: item.name)
            except OSError as exc:
                raise FileReadError(
                    f"Unable to list directory {directory}: {exc}"
                ) from exc
            for entry in entries:
                if truncated:
                    return
                try:
                    if entry.is_symlink():
                        continue
                    is_directory = entry.is_dir(follow_symlinks=False)
                    is_file = entry.is_file(follow_symlinks=False)
                except OSError as exc:
                    raise FileReadError(
                        f"Unable to inspect directory entry {entry.path}: {exc}"
                    ) from exc
                if not is_directory and not is_file:
                    continue
                entry_path = Path(entry.path).resolve(strict=True)
                if not _is_same_or_descendant(entry_path, self._workspace_root):
                    raise PathOutsideWorkspaceError(
                        f"Directory entry resolves outside workspace: {entry.name!r}"
                    )
                discovered += 1
                if discovered > max_results:
                    truncated = True
                    return
                relative = entry_path.relative_to(self._workspace_root).as_posix()
                (directories if is_directory else files).append(relative)
                if recursive and is_directory and depth < max_depth:
                    walk(entry_path, depth + 1)

        walk(resolved, 0)
        return DirectoryListing(
            files=tuple(sorted(files)),
            directories=tuple(sorted(directories)),
            truncated=truncated,
        )

    def write_file(self, path: PathInput, content: str) -> None:
        """Atomically create or replace an authorized UTF-8 text file."""

        if not isinstance(content, str):
            raise TypeError("write_file content must be a string")
        resolved = self._authorize(path, operation="write")
        try:
            if resolved.exists() and not resolved.is_file():
                raise NotARegularFileError(f"Path is not a regular file: {resolved}")
            resolved.parent.mkdir(parents=True, exist_ok=True)
            canonical_parent = resolved.parent.resolve(strict=True)
        except NotARegularFileError:
            raise
        except OSError as exc:
            raise FileWriteError(
                f"Unable to prepare parent directory for {resolved}: {exc}"
            ) from exc

        final_path = canonical_parent / resolved.name
        if not _is_same_or_descendant(final_path, self._workspace_root):
            raise PathOutsideWorkspaceError(
                f"Write parent resolves outside workspace: {canonical_parent}"
            )
        if not self._matches(
            final_path,
            self._permissions.writable_files,
            self._permissions.writable_dirs,
        ):
            raise PermissionDeniedError(
                f"write permission denied after parent resolution: {final_path}"
            )
        self._atomic_write_utf8(final_path, content)

    @staticmethod
    def _atomic_write_utf8(path: Path, content: str) -> None:
        temporary_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=path.parent,
                prefix=f".{path.name}.",
                suffix=".tmp",
                delete=False,
            ) as temporary_handle:
                temporary_path = Path(temporary_handle.name)
                temporary_handle.write(content)
                temporary_handle.flush()
                os.fsync(temporary_handle.fileno())
            os.replace(temporary_path, path)
            temporary_path = None
        except OSError as exc:
            raise FileWriteError(
                f"Unable to write file atomically {path}: {exc}"
            ) from exc
        finally:
            if temporary_path is not None:
                try:
                    temporary_path.unlink(missing_ok=True)
                except OSError:
                    pass

    def read_image(self, path: PathInput) -> ImageReadResult:
        """Load an authorized PNG, JPEG, or WebP without analyzing its content."""

        resolved = self._authorize(path, operation="read")
        data = self._read_bytes(resolved)
        suffix = resolved.suffix.lower()
        expected = IMAGE_FORMATS.get(suffix)
        if expected is None:
            allowed = ", ".join(sorted(IMAGE_FORMATS))
            raise UnsupportedImageError(
                f"Unsupported image extension {suffix!r}; allowed: {allowed}"
            )
        expected_format, mime_type = expected
        detected_format = _detect_image_format(data)
        if detected_format != expected_format:
            raise UnsupportedImageError(
                f"Image bytes do not match extension {suffix!r}: {resolved}"
            )
        relative_path = resolved.relative_to(self._workspace_root).as_posix()
        return ImageReadResult(
            path=relative_path,
            data=data,
            format=detected_format,
            mime_type=mime_type,
            size_bytes=len(data),
        )
