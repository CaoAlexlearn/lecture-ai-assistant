from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from context_loader import CourseNotFoundError, load_context


class ContextLoaderTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name).resolve()
        (self.root / "agents").mkdir()
        (self.root / "instructions").mkdir()
        (self.root / "courses" / "course-a" / "lectures").mkdir(parents=True)
        (self.root / "courses" / "course-a" / "memory").mkdir()
        (self.root / "courses" / "course-b" / "lectures").mkdir(parents=True)
        (self.root / "courses" / "course-b" / "memory").mkdir()

        for agent_name in ("agent1", "agent2", "agent3", "ppt_preparation"):
            (self.root / "agents" / f"{agent_name}.md").write_text(
                f"contract:{agent_name}", encoding="utf-8"
            )

        (self.root / "instructions" / "faculty.md").write_text("", encoding="utf-8")
        (self.root / "instructions" / "user.md").write_text("", encoding="utf-8")
        (self.root / "courses" / "course-a" / "course.md").write_text(
            "course-a-context", encoding="utf-8"
        )
        (self.root / "courses" / "course-b" / "course.md").write_text(
            "course-b-context", encoding="utf-8"
        )
        (self.root / "courses" / "course-a" / "memory" / "index.md").write_text(
            "course-a-memory", encoding="utf-8"
        )
        (self.root / "courses" / "course-b" / "memory" / "index.md").write_text(
            "course-b-memory", encoding="utf-8"
        )

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def test_agent2_loads_only_current_course_instruction(self) -> None:
        context = load_context("agent2", "course-a", project_root=self.root)

        self.assertEqual(context.course, "course-a-context")
        self.assertNotIn("course-b-context", context.course)
        self.assertNotIn(
            self.root / "courses" / "course-b" / "course.md",
            context.loaded_paths,
        )

    def test_agent3_does_not_read_another_course_memory(self) -> None:
        context = load_context("agent3", "course-a", project_root=self.root)

        self.assertEqual(context.course, "course-a-context")
        self.assertTrue(all("memory" not in path.parts for path in context.loaded_paths))
        self.assertNotIn(
            self.root / "courses" / "course-b" / "memory" / "index.md",
            context.loaded_paths,
        )

    def test_agent1_does_not_load_full_course_instruction(self) -> None:
        context = load_context("agent1", "course-a", project_root=self.root)

        self.assertIsNone(context.faculty)
        self.assertIsNone(context.user)
        self.assertIsNone(context.course)
        self.assertEqual(
            context.loaded_paths,
            (self.root / "agents" / "agent1.md",),
        )

    def test_empty_global_and_course_instructions_are_safe(self) -> None:
        (self.root / "courses" / "course-a" / "course.md").write_text(
            "", encoding="utf-8"
        )

        context = load_context("agent2", "course-a", project_root=self.root)

        self.assertEqual(context.faculty, "")
        self.assertEqual(context.user, "")
        self.assertEqual(context.course, "")

    def test_missing_course_fails_without_fallback(self) -> None:
        for agent_name in ("agent1", "agent2", "agent3", "ppt_preparation"):
            with (
                self.subTest(agent_name=agent_name),
                self.assertRaises(CourseNotFoundError),
            ):
                load_context(
                    agent_name,  # type: ignore[arg-type]
                    "missing-course",
                    project_root=self.root,
                )

    def test_each_agent_loads_only_its_own_contract(self) -> None:
        cases = {
            "agent1": None,
            "agent2": "course-a",
            "agent3": "course-a",
            "ppt_preparation": "course-a",
        }
        for agent_name, course_id in cases.items():
            with self.subTest(agent_name=agent_name):
                context = load_context(
                    agent_name, course_id, project_root=self.root  # type: ignore[arg-type]
                )
                self.assertEqual(context.agent_contract, f"contract:{agent_name}")
                loaded_contracts = [
                    path for path in context.loaded_paths if path.parent.name == "agents"
                ]
                self.assertEqual(
                    loaded_contracts,
                    [self.root / "agents" / f"{agent_name}.md"],
                )

    def test_agent_loading_matrix(self) -> None:
        ppt = load_context("ppt_preparation", "course-a", project_root=self.root)
        agent2 = load_context("agent2", "course-a", project_root=self.root)
        agent3 = load_context("agent3", "course-a", project_root=self.root)

        self.assertEqual((ppt.faculty, ppt.user, ppt.course), ("", "", "course-a-context"))
        self.assertEqual(
            (agent2.faculty, agent2.user, agent2.course),
            ("", "", "course-a-context"),
        )
        self.assertEqual((agent3.faculty, agent3.user), (None, None))
        self.assertEqual(agent3.course, "course-a-context")

    def test_active_agent_lab_prompt_is_the_canonical_contract(self) -> None:
        prompt_root = self.root / "agent_lab" / "agents" / "agent2"
        (prompt_root / "prompts").mkdir(parents=True)
        (prompt_root / "agent.json").write_text(
            json.dumps({"activePromptVersionId": "v7"}), encoding="utf-8"
        )
        active_path = prompt_root / "prompts" / "v7.md"
        active_path.write_text("active Agent2 prompt", encoding="utf-8")

        context = load_context("agent2", "course-a", project_root=self.root)

        self.assertEqual(context.agent_contract, "active Agent2 prompt")
        self.assertEqual(context.loaded_paths[0], active_path)


if __name__ == "__main__":
    unittest.main()
