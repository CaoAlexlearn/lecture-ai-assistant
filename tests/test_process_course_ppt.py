from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from process_course_ppt import (
    CoursePptError,
    add_local_proxy_bypass,
    build_mineru_command,
    find_mineru,
    resolve_course_directory,
    resolve_input_path,
)


class CoursePptToolTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name).resolve()
        self.course_directory = self.root / "courses" / "engineering-economics"
        self.course_directory.mkdir(parents=True)
        (self.course_directory / "course.md").write_text(
            "# Engineering Economics", encoding="utf-8"
        )

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def test_resolves_existing_course(self) -> None:
        resolved = resolve_course_directory(self.root, "engineering-economics")
        self.assertEqual(resolved, self.course_directory)

    def test_rejects_course_path_traversal(self) -> None:
        with self.assertRaises(CoursePptError):
            resolve_course_directory(self.root, "../another-course")

    def test_validates_supported_input(self) -> None:
        source = self.root / "lecture.pdf"
        source.write_bytes(b"pdf")
        self.assertEqual(resolve_input_path(str(source)), source)

        unsupported = self.root / "lecture.txt"
        unsupported.write_text("text", encoding="utf-8")
        with self.assertRaises(CoursePptError):
            resolve_input_path(str(unsupported))

    def test_adds_localhost_without_discarding_external_proxy_bypass(self) -> None:
        environment = {
            "HTTP_PROXY": "http://127.0.0.1:7897",
            "NO_PROXY": "example.com,localhost",
        }
        result = add_local_proxy_bypass(environment)

        self.assertEqual(result["HTTP_PROXY"], "http://127.0.0.1:7897")
        self.assertEqual(result["NO_PROXY"], "example.com,localhost,127.0.0.1,::1")
        self.assertEqual(result["no_proxy"], result["NO_PROXY"])

    def test_finds_explicit_executable(self) -> None:
        executable = self.root / "mineru"
        executable.write_text("#!/bin/sh\n", encoding="utf-8")
        executable.chmod(0o755)
        self.assertEqual(find_mineru(str(executable), self.root), executable)

    def test_builds_requested_hybrid_command(self) -> None:
        command = build_mineru_command(
            Path("/venv/bin/mineru"),
            Path("/input/Lecture1.pdf"),
            Path("/courses/course-a/lectures"),
            backend="hybrid-engine",
            effort="medium",
            image_analysis=False,
            method="auto",
        )
        self.assertEqual(
            command,
            [
                "/venv/bin/mineru",
                "-p",
                "/input/Lecture1.pdf",
                "-o",
                "/courses/course-a/lectures",
                "-b",
                "hybrid-engine",
                "--effort",
                "medium",
                "--image-analysis",
                "false",
                "-m",
                "auto",
            ],
        )


if __name__ == "__main__":
    unittest.main()
