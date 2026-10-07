from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from workspace_tools import (
    FileWriteError,
    NotARegularFileError,
    PathOutsideWorkspaceError,
    PathResolutionError,
    PermissionDeniedError,
    TextDecodeError,
    ToolFileNotFoundError,
    ToolPermissions,
    UnsupportedImageError,
    WorkspaceTools,
)

PNG_BYTES = b"\x89PNG\r\n\x1a\nminimal-test-payload"


class WorkspaceToolsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name).resolve()
        (self.root / "normalized").mkdir()
        (self.root / "normalized" / "normalized_slides.json").write_text(
            '{"slides": []}', encoding="utf-8"
        )
        (self.root / "private.txt").write_text("private", encoding="utf-8")
        (self.root / "source" / "mineru" / "images").mkdir(parents=True)
        (self.root / "source" / "mineru" / "images" / "slide.png").write_bytes(
            PNG_BYTES
        )
        (self.root / "allowed").mkdir()
        (self.root / "allowed" / "note.md").write_text("allowed", encoding="utf-8")
        (self.root / "denied").mkdir()
        (self.root / "denied" / "note.md").write_text("denied", encoding="utf-8")

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def tools(self, **overrides: object) -> WorkspaceTools:
        defaults: dict[str, object] = {
            "readable_files": ("normalized/normalized_slides.json",),
            "readable_dirs": ("source/mineru/images", "allowed"),
            "writable_files": ("intermediate/lecture_structure.json",),
            "writable_dirs": ("intermediate/sections",),
        }
        defaults.update(overrides)
        return WorkspaceTools(
            workspace_root=self.root,
            permissions=ToolPermissions(**defaults),
        )

    def test_reads_allowed_exact_file(self) -> None:
        self.assertEqual(
            self.tools().read_file("normalized/normalized_slides.json"),
            '{"slides": []}',
        )

    def test_writes_allowed_exact_file_and_creates_parent(self) -> None:
        tools = self.tools()
        tools.write_file("intermediate/lecture_structure.json", "structure")
        self.assertEqual(
            (self.root / "intermediate" / "lecture_structure.json").read_text(
                encoding="utf-8"
            ),
            "structure",
        )

    def test_read_of_unauthorized_file_is_denied(self) -> None:
        with self.assertRaises(PermissionDeniedError):
            self.tools().read_file("private.txt")

    def test_write_of_unauthorized_file_is_denied(self) -> None:
        with self.assertRaises(PermissionDeniedError):
            self.tools().write_file("intermediate/overview.json", "no")
        self.assertFalse((self.root / "intermediate" / "overview.json").exists())

    def test_parent_traversal_outside_workspace_is_rejected(self) -> None:
        with self.assertRaises(PathOutsideWorkspaceError):
            self.tools().read_file("../outside.txt")

    def test_absolute_path_outside_workspace_is_rejected(self) -> None:
        outside = self.root.parent / "outside.txt"
        with self.assertRaises(PathOutsideWorkspaceError):
            self.tools().read_file(outside)

    def test_symlink_to_outside_workspace_is_rejected(self) -> None:
        outside_directory = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: outside_directory.rmdir())
        outside_file = outside_directory / "secret.txt"
        outside_file.write_text("secret", encoding="utf-8")
        self.addCleanup(outside_file.unlink)
        (self.root / "allowed" / "escape.txt").symlink_to(outside_file)

        with self.assertRaises(PathOutsideWorkspaceError):
            self.tools().read_file("allowed/escape.txt")

    def test_file_inside_allowed_directory_can_be_read(self) -> None:
        self.assertEqual(self.tools().read_file("allowed/note.md"), "allowed")

    def test_file_inside_disallowed_directory_is_denied(self) -> None:
        with self.assertRaises(PermissionDeniedError):
            self.tools().read_file("denied/note.md")

    def test_atomic_write_preserves_original_when_replace_fails(self) -> None:
        target = self.root / "intermediate" / "lecture_structure.json"
        target.parent.mkdir()
        target.write_text("original", encoding="utf-8")

        with (
            mock.patch("workspace_tools.os.replace", side_effect=OSError("failure")),
            self.assertRaises(FileWriteError),
        ):
            self.tools().write_file("intermediate/lecture_structure.json", "new")

        self.assertEqual(target.read_text(encoding="utf-8"), "original")
        self.assertEqual(list(target.parent.glob(".lecture_structure.json.*.tmp")), [])

    def test_read_image_returns_bytes_and_metadata(self) -> None:
        result = self.tools().read_image("source/mineru/images/slide.png")
        self.assertEqual(result.path, "source/mineru/images/slide.png")
        self.assertEqual(result.data, PNG_BYTES)
        self.assertEqual(result.format, "png")
        self.assertEqual(result.mime_type, "image/png")
        self.assertEqual(result.size_bytes, len(PNG_BYTES))

    def test_read_image_cannot_bypass_read_permission(self) -> None:
        (self.root / "denied" / "secret.png").write_bytes(PNG_BYTES)
        with self.assertRaises(PermissionDeniedError):
            self.tools().read_image("denied/secret.png")

    def test_read_image_rejects_unsupported_extension(self) -> None:
        (self.root / "allowed" / "image.gif").write_bytes(b"GIF89a")
        with self.assertRaises(UnsupportedImageError):
            self.tools().read_image("allowed/image.gif")

    def test_read_image_rejects_extension_signature_mismatch(self) -> None:
        (self.root / "allowed" / "fake.png").write_bytes(b"not a png")
        with self.assertRaises(UnsupportedImageError):
            self.tools().read_image("allowed/fake.png")

    def test_authorized_missing_file_has_specific_error(self) -> None:
        tools = self.tools(readable_files=("normalized/missing.json",))
        with self.assertRaises(ToolFileNotFoundError):
            tools.read_file("normalized/missing.json")

    def test_directory_passed_to_read_file_has_specific_error(self) -> None:
        tools = self.tools(readable_dirs=("allowed",), readable_files=("allowed",))
        with self.assertRaises(NotARegularFileError):
            tools.read_file("allowed")

    def test_invalid_utf8_has_specific_error(self) -> None:
        (self.root / "allowed" / "invalid.txt").write_bytes(b"\xff\xfe")
        with self.assertRaises(TextDecodeError):
            self.tools().read_file("allowed/invalid.txt")

    def test_symlinked_write_parent_cannot_escape_workspace(self) -> None:
        outside_directory = Path(tempfile.mkdtemp())
        self.addCleanup(outside_directory.rmdir)
        write_link = self.root / "write-link"
        write_link.mkdir()
        tools = self.tools(writable_dirs=("intermediate/sections", "write-link"))
        write_link.rmdir()
        write_link.symlink_to(outside_directory, target_is_directory=True)

        with self.assertRaises(PathOutsideWorkspaceError):
            tools.write_file("write-link/escaped.txt", "no")
        self.assertFalse((outside_directory / "escaped.txt").exists())

    def test_permission_model_expresses_future_agent_profiles(self) -> None:
        structure = ToolPermissions(
            readable_files=("normalized/normalized_slides.json",),
            writable_files=(
                "intermediate/lecture_structure.json",
                "prepared/lecture_overview.md",
            ),
        )
        section = ToolPermissions(
            readable_files=(
                "normalized/normalized_slides.json",
                "intermediate/lecture_structure.json",
            ),
            readable_dirs=("source/mineru/images",),
            writable_files=(
                "intermediate/sections/sec_03.json",
                "intermediate/sections/sec_03.md",
            ),
        )
        compiler = ToolPermissions(
            readable_files=("intermediate/lecture_structure.json",),
            readable_dirs=("intermediate/sections",),
            writable_files=(
                "prepared/lecture_preparation.json",
                "prepared/hotwords.json",
            ),
        )

        structure_tools = WorkspaceTools(
            workspace_root=self.root, permissions=structure
        )
        self.assertEqual(
            structure_tools.read_file("normalized/normalized_slides.json"),
            '{"slides": []}',
        )
        structure_tools.write_file("intermediate/lecture_structure.json", "structure")
        structure_tools.write_file("prepared/lecture_overview.md", "overview")

        section_tools = WorkspaceTools(workspace_root=self.root, permissions=section)
        self.assertEqual(
            section_tools.read_file("intermediate/lecture_structure.json"),
            "structure",
        )
        self.assertEqual(
            section_tools.read_image("source/mineru/images/slide.png").format,
            "png",
        )
        section_tools.write_file("intermediate/sections/sec_03.json", "section")
        section_tools.write_file("intermediate/sections/sec_03.md", "section-md")
        with self.assertRaises(PermissionDeniedError):
            section_tools.write_file("intermediate/lecture_structure.json", "forbidden")

        compiler_tools = WorkspaceTools(workspace_root=self.root, permissions=compiler)
        self.assertEqual(
            compiler_tools.read_file("intermediate/sections/sec_03.json"),
            "section",
        )
        compiler_tools.write_file("prepared/lecture_preparation.json", "prepared")
        compiler_tools.write_file("prepared/hotwords.json", "hotwords")

    def test_permission_entries_must_be_workspace_relative(self) -> None:
        with self.assertRaisesRegex(
            PathResolutionError, "Permission entries must be workspace-relative"
        ):
            self.tools(readable_files=(self.root / "private.txt",))


if __name__ == "__main__":
    unittest.main()
